# Responsibility: Verify the real-service integration runner exposes the whole tests package and a real database.
# Boundaries: the runner contract only; what the integration tier then proves is that tier's own business.
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text())
MAKEFILE = (REPO / "Makefile").read_text()
#: The recipe delegates the run to a script, so the contract spans both - reading only the recipe
#: would let the DSN move into the script and go unchecked.
RUNNER = (MAKEFILE[MAKEFILE.index("test-integration:"):MAKEFILE.index("wait-postgres:")]
          + (REPO / "tests" / "integration" / "run_disposable.sh").read_text())

#: Root helpers the integration tier imports. Mounting only tests/integration made every module
#: that touches one of these uncollectable - 30 collection errors, and the tier silently shrank.
ROOT_HELPERS = ("_geometry_support.py", "cad_fixtures.py", "capture_authority.py",
                "engine_workspaces.py", "harness_provisioning.py", "product_modes.py")


def _worker_mounts() -> list[str]:
    return [str(v) for v in COMPOSE["services"]["worker"].get("volumes", [])]


def test_the_runner_mounts_the_whole_tests_package():
    assert "./tests:/srv/tests:ro" in _worker_mounts(), \
        "the runner exposes something other than the complete tests package"
    assert not any(m.startswith("./tests/") for m in _worker_mounts()), \
        "a narrower tests mount is back; the tier would lose its root helpers again"


def test_every_root_helper_the_tier_imports_really_exists():
    for helper in ROOT_HELPERS:
        assert (REPO / "tests" / helper).is_file(), f"tests/{helper} is imported but absent"


def test_no_helper_is_duplicated_under_integration():
    for helper in ROOT_HELPERS:
        assert not (REPO / "tests" / "integration" / helper).exists(), \
            f"tests/integration/{helper} is a second copy of a root helper"


def test_the_runner_provisions_a_database_for_this_run_rather_than_reusing_the_shared_one():
    # The tier drops and rebuilds the public schema, so it must never be handed the development
    # database. Seven pre-existing rows were destroyed exactly that way.
    assert "disposable_database" in RUNNER or "dd.provision" in RUNNER, \
        "the runner no longer provisions a database of its own"
    assert "MESH_TEST_RUN_ID" in RUNNER, "the runner supplies no run identity to prove ownership"
    assert "meshtest_" in RUNNER, "the runner does not name a task database"
    preflight = (REPO / "tests" / "integration" / "preflight.py")
    # An EXECUTED line, not a mention. The comment above the call names preflight.py too, and a
    # guard satisfied by prose would survive the delegation being deleted.
    invocations = [ln.strip() for ln in RUNNER.splitlines()
                   if "preflight.py" in ln and not ln.lstrip().startswith("#")]
    assert invocations, \
        "the runner no longer checks the image revision; a stale image could be tested again"
    assert "MESH_SOURCE_TREE" in preflight.read_text(), \
        "the preflight it delegates to does not check the image revision either"


def test_the_runner_supplies_a_real_local_database_to_the_test_process_only():
    assert "DATABASE_URL=" in RUNNER, "the tier is run without a real database endpoint"
    assert "@postgres:" in RUNNER, "the DSN names a host-only address instead of the service"
    assert "sslmode=disable" in RUNNER, \
        "the local DSN omits sslmode; the hosted normaliser then requires TLS the service lacks"
    for external in ("neon.tech", "amazonaws", "localhost:5432", "127.0.0.1"):
        assert external not in RUNNER, f"the runner points at {external}"
    # derived from the stack's own settings, not a second default
    assert "$POSTGRES_USER" in RUNNER and "$POSTGRES_DB" in RUNNER
    for service, spec in COMPOSE["services"].items():
        assert "DATABASE_URL" not in (spec.get("environment") or {}), \
            f"{service} was given the test database setting; it belongs to the test process alone"


def test_the_runner_waits_on_readiness_rather_than_sleeping():
    assert "wait-postgres" in RUNNER, "the runner starts without waiting for the database"
    wait = MAKEFILE[MAKEFILE.index("wait-postgres: ##!"):]
    wait = wait[:wait.index("test-container-smoke:")]
    body = "\n".join(ln for ln in wait.splitlines()[1:] if not ln.strip().startswith("@#"))
    assert "Health.Status" in body and "healthy" in body, "readiness is not health-based"
    assert "sleep" not in body, "the runner sleeps instead of reading readiness state"


def test_production_images_still_carry_no_tests():
    ignore = [ln.strip() for ln in (REPO / ".dockerignore").read_text().splitlines()]
    assert "tests/" in ignore, "tests would be baked into the image"
    dockerfile = (REPO / "Dockerfile").read_text()
    assert not re.search(r"^COPY\s+tests", dockerfile, re.M), "a stage copies tests into an image"


def test_only_the_runner_service_receives_the_tests_mount():
    for name, spec in COMPOSE["services"].items():
        mounts = [str(v) for v in spec.get("volumes", [])]
        if name != "worker":
            assert not any("/srv/tests" in m for m in mounts), \
                f"{name} is given the test package but is not the integration runner"
