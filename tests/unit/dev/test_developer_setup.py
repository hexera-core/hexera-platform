# Responsibility: Verify dependencies live in one place and each make target claims only what it actually runs.
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]

PUBLIC_COMMANDS = [
    "setup", "up", "down", "check", "test", "logs", "clean",
    "mesh-deploy", "mesh-doctor",
]
# Targets that must exist but must NOT clutter `make help`.
INTERNAL_SAMPLES = ["rebuild", "migrate", "wheel", "lint", "typecheck", "dependencies",
                    "test-integration", "mesh-preflight", "help-all"]


def test_dependencies_live_only_under_requirements_dir():
    assert (REPO / "requirements" / "runtime.txt").is_file()
    assert (REPO / "requirements" / "dev.txt").is_file()


def test_pyproject_declares_no_runtime_pins():
    import re
    txt = (REPO / "pyproject.toml").read_text()
    assert not re.search(r"^dependencies\s*=\s*\[[^\]]*[=<>~]=", txt, re.M | re.S)


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_make_help_shows_only_the_public_commands():
    out = subprocess.run(["make", "help"], cwd=REPO, capture_output=True, text=True).stdout
    for cmd in PUBLIC_COMMANDS:
        assert cmd in out, f"`make help` is missing the public command {cmd!r}"
    # internal targets must not clutter the default help
    for internal in ("rebuild", "migrate", "wheel", "shell-api", "test-integration"):
        assert f" {internal} " not in out and f"{internal}\n" not in out, \
            f"`make help` leaks the internal target {internal!r} - mark it ##! and it drops out"


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_help_all_exposes_the_internal_targets():
    out = subprocess.run(["make", "help-all"], cwd=REPO, capture_output=True, text=True).stdout
    for t in INTERNAL_SAMPLES + PUBLIC_COMMANDS:
        assert t in out, f"`make help-all` is missing {t!r}"
    assert "[internal]" in out, "help-all should label internal targets"


# container test-target honesty (evidence tiers must not be ambiguous)

def _makefile_recipe(target: str) -> str:
    lines = (REPO / "Makefile").read_text().splitlines()
    out, capturing, in_continuation = [], False, False
    for ln in lines:
        if not capturing:
            if ln.startswith(f"{target}:"):
                capturing = True
            continue
        if in_continuation:
            out.append(ln)
            in_continuation = ln.rstrip().endswith("\\")
            continue
        if ln.startswith("\t") or ln.strip() == "":
            out.append(ln)
            in_continuation = ln.rstrip().endswith("\\")
        else:
            break
    return "\n".join(out)


def test_container_smoke_is_hermetic_and_makes_no_integration_claim():
    recipe = _makefile_recipe("test-container-smoke")
    assert "--network none" in recipe, "smoke must run with no network (no service can be dialled)"
    assert "-m hermetic" in recipe, "smoke must select ONLY the hermetic-marked integration tests"
    # every reference to the integration path must be filtered by `-m hermetic` - the smoke target
    # must never run the tier unfiltered (that would need Postgres/Redis/MinIO)
    for ln in recipe.splitlines():
        if "/srv/tests/integration" in ln:
            assert "-m hermetic" in ln, f"smoke runs the integration path unfiltered: {ln.strip()}"


def test_hermetic_marker_selects_only_service_free_integration_files():
    import re
    intdir = REPO / "tests" / "integration"
    marked = {p.name for p in intdir.glob("*.py")
              if re.search(r"^pytestmark\s*=\s*pytest\.mark\.hermetic", p.read_text(), re.M)}
    assert marked == {
        "test_graph_runtime.py", "test_surface_self_intersection.py",
        "test_surface_deviation_metric.py", "test_celery_ack_policy.py",
    }, f"unexpected hermetic-marked set: {marked}"
    # the service-backed files must NOT be hermetic
    for f in ("test_artifact_delivery_minio.py", "test_final_result_restart_postgres.py",
              "test_job_transitions_postgres.py"):
        assert "pytest.mark.hermetic" not in (intdir / f).read_text(), \
            f"{f} needs a service - it must not be marked hermetic"
