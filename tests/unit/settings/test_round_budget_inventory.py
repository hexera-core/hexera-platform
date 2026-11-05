# Responsibility: Verify every agent round budget is inventoried with its owning module's default, and never required.
from __future__ import annotations

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.settings.inventory import INVENTORY

_OWNED = {
    "INTAKE_MAX_ROUNDS": icfg.INTAKE_MAX_ROUNDS,
    "BUILDER_MAX_ROUNDS": bcfg.BUILDER_MAX_ROUNDS,
    "BUILDER_RETRY_MAX_ROUNDS": bcfg.BUILDER_RETRY_MAX_ROUNDS,
    "REVIEWER_MAX_ROUNDS": rcfg.REVIEWER_MAX_ROUNDS,
}


def _entry(name):
    return next((v for g in INVENTORY for v in g.vars if v.name == name), None)


@pytest.mark.parametrize("name", sorted(_OWNED))
def test_every_agent_round_budget_is_inventoried(name):
    assert _entry(name) is not None, f"{name} is configurable but undiscoverable"


@pytest.mark.parametrize("name,value", sorted(_OWNED.items()))
def test_the_inventoried_default_matches_the_owning_module(name, value):
    assert _entry(name).default == str(value)


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
