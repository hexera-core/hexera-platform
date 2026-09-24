# Responsibility: Verify one typed authority owns every product mode, and nothing can reach around it.
# Boundaries: the mode seam itself - what each mode then does to a payload is the trace and capture suites.
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from meshpipeline.settings.env import ConfigurationError
from meshpipeline.settings.inventory import all_vars, render_env
from meshpipeline.settings.modes import ProductModes, TraceDisclosure, load_product_modes

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "meshpipeline"
COMPOSE = (REPO / "docker-compose.yml").read_text()

#: The typed loaders allowed to read the environment. Everything else must take a settings value.
#: Each is a named authority for one domain, not a general exception.
APPROVED_LOADERS = {
    "settings/env.py",              # the loader itself
    "settings/modes.py",            # ProductModes
    "settings/observability.py",    # ObservabilitySettings
    "runtime/mesh_invocation.py",   # MeshJobInvocation - per-run container arguments
}

#: Reads that are library or platform contracts, not developer settings.
LIBRARY_CONTRACTS = {
    "runtime/metrics_server.py",    # PROMETHEUS_MULTIPROC_DIR - prometheus_client's own contract
    "runtime/migrate.py",           # ALEMBIC_CONFIG - alembic's own contract
    "sandbox/backend.py",           # sets VTK/Mesa vars; reads nothing
    "settings/runtime.py",          # the removed-name rejection
    "runtime/startup.py",           # the removed-name rejection
    "sandbox/safe_exec.py",         # names the pattern it forbids, in a guard
    "persistence/repositories/capture_repository.py",   # DATABASE_URL, the hosted database path
    # Enumerates the WHOLE environment to build a redaction table - the values are what it
    # scrubs from captured text, not configuration it obeys.
    "capture/trace.py",
}


def _env_reads(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("getenv",):
            hits.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            hits.append(node.lineno)
    return hits


def test_no_production_module_reads_a_developer_setting_directly():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in APPROVED_LOADERS or rel in LIBRARY_CONTRACTS:
            continue
        if lines := _env_reads(path):
            offenders.append(f"{rel}:{','.join(str(n) for n in sorted(set(lines)))}")
    assert offenders == [], (
        "these modules read the environment instead of a typed setting:\n  " + "\n  ".join(offenders))


def test_product_modes_is_the_only_thing_that_answers_a_mode_question():
    # No module may re-derive a mode from its own environment read: the names exist in exactly
    # one place, the catalogue, and one loader, modes.py.
    names = ("DATA_COLLECTION_ENABLED", "PUBLIC_TRACE_MODE", "ALLOW_PUBLIC_RAW_TRACE")
    allowed = {"settings/modes.py", "settings/inventory.py"}
    for name in names:
        hits = subprocess.run(["git", "grep", "-l", "-F", name, "--", "src/"],
                              cwd=REPO, capture_output=True, text=True).stdout.split()
        rogue = [h for h in hits if h.removeprefix("src/meshpipeline/") not in allowed]
        # a remaining mention must be prose, never a read
        for h in rogue:
            for n, line in enumerate((REPO / h).read_text().splitlines(), 1):
                if name in line:
                    stripped = line.strip()
                    assert stripped.startswith("#") or '"' in line or "'" in line, \
                        f"{h}:{n} reads {name} outside the catalogue and its loader"


# the two modes, behaviourally


def test_an_unset_environment_resolves_to_whatever_the_catalogue_declares(monkeypatch):
    # WHICH way these ship is a product decision and may change; that the two authorities agree is
    # the invariant. Pinning the values here made a deliberate change look like three broken tests.
    for n in ("DATA_COLLECTION_ENABLED", "PUBLIC_TRACE_MODE", "ALLOW_PUBLIC_RAW_TRACE"):
        monkeypatch.delenv(n, raising=False)
    catalogue = {v.name: v.default for v in all_vars()}
    modes = load_product_modes()
    assert modes.data_collection_enabled is (catalogue["DATA_COLLECTION_ENABLED"] == "true")
    assert modes.trace_disclosure.value == catalogue["PUBLIC_TRACE_MODE"]


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("on", True),
                                          ("false", False), ("0", False), ("off", False)])
def test_collection_accepts_only_the_repository_boolean_syntax(monkeypatch, raw, expected):
    monkeypatch.setenv("DATA_COLLECTION_ENABLED", raw)
    assert load_product_modes().data_collection_enabled is expected


def test_a_non_boolean_collection_value_is_refused(monkeypatch):
    monkeypatch.setenv("DATA_COLLECTION_ENABLED", "sometimes")
    with pytest.raises(ConfigurationError, match="DATA_COLLECTION_ENABLED"):
        load_product_modes()


@pytest.mark.parametrize("bad", ["debug", "full", "off", "uncensored", ""])
def test_only_safe_and_raw_are_disclosure_modes(monkeypatch, bad):
    monkeypatch.setenv("PUBLIC_TRACE_MODE", bad)
    with pytest.raises(ConfigurationError, match="PUBLIC_TRACE_MODE"):
        load_product_modes()


def test_raw_disclosure_still_requires_the_acknowledgement(monkeypatch):
    # Both switches default on, so the acknowledgement no longer guards the DEFAULT. It still
    # guards the disagreement: a deployment that withdrew it and left the mode at raw is refused
    # rather than quietly publishing content.
    monkeypatch.setenv("PUBLIC_TRACE_MODE", "raw")
    monkeypatch.setenv("ALLOW_PUBLIC_RAW_TRACE", "false")
    with pytest.raises(ConfigurationError, match="ALLOW_PUBLIC_RAW_TRACE"):
        load_product_modes()
    monkeypatch.setenv("ALLOW_PUBLIC_RAW_TRACE", "true")
    assert load_product_modes().trace_disclosure is TraceDisclosure.RAW


def test_safe_is_reachable_by_naming_the_mode_alone(monkeypatch):
    # Narrowing must not need both switches: the operator who wants content withheld says so once.
    for n in ("DATA_COLLECTION_ENABLED", "ALLOW_PUBLIC_RAW_TRACE"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("PUBLIC_TRACE_MODE", "safe")
    assert load_product_modes().trace_disclosure is TraceDisclosure.SAFE


@pytest.mark.parametrize("collect", ["true", "false"])
@pytest.mark.parametrize("disclose", ["safe", "raw"])
def test_the_two_dimensions_are_independent(monkeypatch, collect, disclose):
    monkeypatch.setenv("DATA_COLLECTION_ENABLED", collect)
    monkeypatch.setenv("PUBLIC_TRACE_MODE", disclose)
    monkeypatch.setenv("ALLOW_PUBLIC_RAW_TRACE", "true")
    modes = load_product_modes()
    assert modes.data_collection_enabled is (collect == "true")
    assert modes.trace_disclosure == disclose


def test_the_acknowledgement_is_not_carried_on_the_object():
    # Once construction succeeds there is nothing left for a caller to re-decide.
    assert set(ProductModes.__dataclass_fields__) == {
        "data_collection_enabled", "trace_disclosure"}
    assert ProductModes.__dataclass_params__.frozen


# adding a future mode must be explicit


def test_a_new_product_mode_needs_a_catalogue_entry_and_a_typed_field():
    declared = {v.name for v in all_vars()}
    fields = set(ProductModes.__dataclass_fields__)
    assert fields == {"data_collection_enabled", "trace_disclosure"}, (
        "ProductModes gained a field; it needs its own catalogue entry, generated comment, "
        "validation and behavioural tests for every accepted value")
    for name in ("DATA_COLLECTION_ENABLED", "PUBLIC_TRACE_MODE", "ALLOW_PUBLIC_RAW_TRACE"):
        assert name in declared, f"{name} is not declared in the catalogue"
    assert not any(m.value in ("developer", "public", "production") for m in TraceDisclosure), \
        "TraceDisclosure gained a profile-shaped value; modes are dimensions, not profiles"


# the catalogue is the sole default authority


def test_env_example_is_exactly_what_the_catalogue_renders():
    assert (REPO / ".env.example").read_text() == render_env()


def test_the_generated_product_mode_section_is_short_and_exact():
    # Its SHAPE is the contract - three settings, each one help line and one assignment, nothing
    # else in the group. The values come from the catalogue rather than being repeated here, so
    # changing a default does not have to be applied in two places to keep the suite green.
    section = render_env().split("# Product modes\n", 1)[1].split("\n\n", 1)[0]
    catalogue = {v.name: v for v in all_vars()}
    expected = "\n".join(
        f"# {catalogue[name].help}\n{name}={catalogue[name].default}"
        for name in ("DATA_COLLECTION_ENABLED", "PUBLIC_TRACE_MODE", "ALLOW_PUBLIC_RAW_TRACE"))
    assert section == expected, section


def test_no_catalogue_default_is_duplicated_in_compose():
    declared = {v.name for v in all_vars()}
    duplicated = sorted({n for n, _ in re.findall(r"\$\{([A-Z_]+):-([^}]*)\}", COMPOSE)}
                        & declared - {"GOOGLE_ADC_FILE"})
    assert duplicated == [], f"a second default lives in docker-compose.yml for: {duplicated}"


# per-run mesh inputs are not settings


def test_per_run_mesh_inputs_go_through_the_invocation_boundary():
    from meshpipeline.runtime.mesh_invocation import MeshJobInvocation, from_environment

    inv = from_environment({"INPUT_URI": "gs://in/a.tar.gz", "OUTPUT_URI": "gs://out/a",
                            "ENGINE": "gmsh", "TIMEOUT": "900"})
    assert inv == MeshJobInvocation("gs://in/a.tar.gz", "gs://out/a", "gmsh", 900)

    default = from_environment({"INPUT_URI": "gs://in/b", "OUTPUT_URI": "gs://out/b"})
    assert (default.engine, default.timeout_seconds) == ("cfmesh", 1800)

    for broken in ({"OUTPUT_URI": "gs://out/c"}, {"INPUT_URI": "gs://in/c"},
                   {"INPUT_URI": "a", "OUTPUT_URI": "b", "TIMEOUT": "soon"},
                   {"INPUT_URI": "a", "OUTPUT_URI": "b", "TIMEOUT": "0"}):
        with pytest.raises(ConfigurationError):
            from_environment(broken)

    runner = (SRC / "runtime" / "mesh_runner.py").read_text()
    for name in ("INPUT_URI", "OUTPUT_URI", "ENGINE", "TIMEOUT"):
        assert f'"{name}"' not in runner, f"mesh_runner still parses {name} itself"
    assert not any(v.name in ("INPUT_URI", "OUTPUT_URI") for v in all_vars()), \
        "a per-run container argument was declared as a developer setting"


# mutation controls


def test_bypassing_product_modes_would_be_caught(tmp_path):
    rogue = tmp_path / "rogue.py"
    rogue.write_text('import os\nX = os.environ.get("DATA_COLLECTION_ENABLED", "")\n')
    assert _env_reads(rogue), "the direct-read detector sees nothing - the guard proves nothing"


def test_a_duplicated_compose_default_would_be_caught():
    declared = {v.name for v in all_vars()}
    mutated = COMPOSE.replace("${MINIO_BUCKET}", "${MINIO_BUCKET:-mesh-artifacts}", 1)
    duplicated = sorted({n for n, _ in re.findall(r"\$\{([A-Z_]+):-([^}]*)\}", mutated)}
                        & declared - {"GOOGLE_ADC_FILE"})
    assert duplicated == ["MINIO_BUCKET"], \
        "reintroducing a Compose default went undetected - the parity guard proves nothing"


def test_an_unacknowledged_raw_mode_would_be_caught(monkeypatch):
    monkeypatch.setenv("PUBLIC_TRACE_MODE", "raw")
    monkeypatch.setenv("ALLOW_PUBLIC_RAW_TRACE", "false")
    with pytest.raises(ConfigurationError):
        load_product_modes()


def test_dropping_a_collection_seam_would_be_caught():
    # Every owning boundary must consult the authority. Losing one is losing a gate.
    seams = {
        "capture/trace.py", "application/post_terminal.py", "agents/intake/message.py",
        "application/maintenance/export.py", "runtime/startup.py",
    }
    consulting = {p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                  if "MODES.data_collection_enabled" in p.read_text()}
    assert seams <= consulting, f"these seams no longer consult ProductModes: {seams - consulting}"
