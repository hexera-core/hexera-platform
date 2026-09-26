# Responsibility: Verify every agent round budget is inventoried with its owning module's default, and never required.
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.settings.inventory import INVENTORY

REPO = pathlib.Path(__file__).resolve().parents[3]

#: name -> the module that OWNS the setting. The module is the subject of the comparison below; the
#: resolved value is not, and `_OWNED` is kept separately for the tests that really do want it.
_OWNERS = {
    "INTAKE_MAX_ROUNDS": icfg,
    "BUILDER_MAX_ROUNDS": bcfg,
    "BUILDER_RETRY_MAX_ROUNDS": bcfg,
    "REVIEWER_MAX_ROUNDS": rcfg,
}

_OWNED = {
    "INTAKE_MAX_ROUNDS": icfg.INTAKE_MAX_ROUNDS,
    "BUILDER_MAX_ROUNDS": bcfg.BUILDER_MAX_ROUNDS,
    "BUILDER_RETRY_MAX_ROUNDS": bcfg.BUILDER_RETRY_MAX_ROUNDS,
    "REVIEWER_MAX_ROUNDS": rcfg.REVIEWER_MAX_ROUNDS,
}


def _entry(name):
    return next((v for g in INVENTORY for v in g.vars if v.name == name), None)


def _declared_default(name: str) -> str:
    """The default the owning module SHIPS, read off its own `optional_env(name, "<default>")` call.

    NOT the resolved value, and the difference is the whole point. `optional_env` returns whatever
    this machine's environment says, and `meshpipeline.settings.env` loads the repository `.env` at
    import - so comparing the catalogue against the resolved value makes this test a report on the
    developer's `.env` rather than on the product. A local `REVIEWER_MAX_ROUNDS=30` against a
    shipped 60 held it red for a whole session, and the first attempt at it moved the SHIPPED
    default to match the override: the environment editing the product, through the check that was
    supposed to protect it. The catalogue documents a default, so a default is what it is checked
    against, and the comparison now answers the same on every machine.

    Reading the literal also fails when a budget stops being declared this way at all, which is the
    one change that would leave nothing to compare.
    """
    source = pathlib.Path(inspect.getsourcefile(_OWNERS[name]))
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and len(node.args) >= 2):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "optional_env"):
            continue
        key, default = node.args[0], node.args[1]
        if not (isinstance(key, ast.Constant) and key.value == name):
            continue
        if isinstance(default, ast.Constant):
            return str(default.value)
    raise AssertionError(
        f"{name} is not read as optional_env({name!r}, <literal default>) in "
        f"{source.name}, so the owning module declares no default for the catalogue to match")


def _example_env_defaults() -> dict[str, str]:
    """`{name: value}` from the shipped `.env.example`, ignoring comments and blank lines."""
    out: dict[str, str] = {}
    text = (REPO / ".env.example").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


@pytest.mark.parametrize("name", sorted(_OWNED))
def test_every_agent_round_budget_is_inventoried(name):
    assert _entry(name) is not None, f"{name} is configurable but undiscoverable"


@pytest.mark.parametrize("name", sorted(_OWNED))
def test_the_inventoried_default_matches_the_owning_module(name):
    declared = _declared_default(name)
    assert _entry(name).default == declared, (
        f"the catalogue advertises {name}={_entry(name).default!r} and "
        f"{_OWNERS[name].__name__} ships {declared!r}. One of the two is lying to whoever reads it; "
        f"this machine happens to resolve {name} to {_OWNED[name]!r}, which settles nothing.")


@pytest.mark.parametrize("name", sorted(_OWNED))
def test_the_shipped_example_env_documents_the_same_default(name):
    # The third independently written copy of the same number. A developer's .env starts as a copy of
    # this file, so an example that disagrees with the module hands every new checkout an override it
    # never asked for - which is exactly how the comparison above came to be red.
    example = _example_env_defaults()
    assert name in example, f"{name} is a shipped round budget that .env.example does not mention"
    assert example[name] == _declared_default(name), (
        f".env.example sets {name}={example[name]!r} but {_OWNERS[name].__name__} ships "
        f"{_declared_default(name)!r}")


@pytest.mark.parametrize("name", sorted(_OWNED))
def test_a_round_budget_is_never_required(name):
    assert _entry(name).required is False
    assert _entry(name).secret is False


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_non_positive_configured_round_budget_is_rejected(bad, monkeypatch):
    import importlib

    from meshpipeline.settings.env import ConfigurationError
    monkeypatch.setenv("INTAKE_MAX_ROUNDS", bad)
    with pytest.raises(ConfigurationError):
        importlib.reload(icfg)
    monkeypatch.delenv("INTAKE_MAX_ROUNDS", raising=False)
    importlib.reload(icfg)
    assert icfg.INTAKE_MAX_ROUNDS == _OWNED["INTAKE_MAX_ROUNDS"]


def test_they_share_one_group_so_the_set_reads_as_a_set():
    groups = {g.title for g in INVENTORY for v in g.vars if v.name in _OWNED}
    assert len(groups) == 1, f"the round budgets are scattered across {groups}"


def test_no_unenforced_limit_was_added_alongside_them():
    names = {v.name for g in INVENTORY for v in g.vars}
    for absent in ("INTAKE_MAX_TOOL_CALLS", "BUILDER_MAX_TOOL_CALLS", "REVIEWER_MAX_TOOL_CALLS",
                   "INTAKE_NO_PROGRESS_THRESHOLD", "INTAKE_TOTAL_TIMEOUT_SECONDS"):
        assert absent not in names, f"the inventory advertises an unenforced limit: {absent}"


def test_the_retired_tool_call_budget_is_still_rejected_at_startup():
    from meshpipeline.settings.inventory import REMOVED
    assert "REVIEWER_MAX_TOOL_CALLS" in REMOVED, (
        "a stale .env must fail loudly, not have its value silently reinterpreted")


# retired settings must not be injected at startup
def _startup_configs() -> dict[str, str]:
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[3]
    out = {}
    for rel in ["docker-compose.yml",
                "deploy/gcp/cloud-run/mesh-job.yaml",
                ".env.example",
                "deploy/gcp/scripts/bootstrap-env.sh"]:
        p = repo / rel
        if p.is_file():
            out[rel] = p.read_text(encoding="utf-8", errors="replace")
    return out


@pytest.mark.parametrize("retired", sorted(__import__(
    "meshpipeline.settings.inventory", fromlist=["REMOVED"]).REMOVED))
def test_no_startup_config_injects_a_retired_setting(retired):
    offenders = []
    for rel, text in _startup_configs().items():
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if retired in stripped and not stripped.startswith("#"):
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        f"{retired} is retired but still injected by startup config:\n  "
        + "\n  ".join(offenders))


def test_the_retired_reviewer_setting_is_still_rejected_by_startup(monkeypatch):
    import importlib

    import meshpipeline.runtime.startup as startup
    from meshpipeline.settings.env import ConfigurationError

    monkeypatch.setenv("REVIEWER_MAX_TOOL_CALLS", "30")
    importlib.reload(startup)
    with pytest.raises(ConfigurationError, match="REVIEWER_MAX_TOOL_CALLS"):
        startup.validate()
    monkeypatch.delenv("REVIEWER_MAX_TOOL_CALLS", raising=False)
    importlib.reload(startup)
