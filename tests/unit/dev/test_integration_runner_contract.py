# Responsibility: Verify the integration runner provisions its own services, propagates failure, and prunes nothing.
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "tests" / "integration" / "run_in_container.sh"
GUARD = REPO / "tests" / "integration" / "assert_integration_coverage.py"


@pytest.fixture(scope="module")
def script() -> str:
    return RUNNER.read_text()


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("_cov_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# the script's environment

def test_the_runner_exports_the_canonical_database_url(script):
    assert "export DATABASE_URL=" in script
    assert "postgresql+asyncpg://" in script


def test_the_database_url_targets_the_disposable_service_not_an_ambient_one(script):
    line = next(ln for ln in script.splitlines() if ln.startswith("export DATABASE_URL="))
    assert "${PG}" in line or "$PG" in line, (
        "the URL must be built from the container this script starts, never from the caller")


def test_ssl_is_disabled_only_for_the_task_local_plaintext_postgres(script):
    assert "sslmode=disable" in script
    executable = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    exempt = [ln for ln in executable if "sslmode=disable" in ln]
    assert exempt, "the task-local exemption is gone"
    # One exempt SERVER, not one line: the bootstrap URL and the suite's task-database URL both
    # address the same throwaway plaintext Postgres. Any other host would be a real exemption.
    for ln in exempt:
        assert "${PG}" in ln or "$PG:" in ln, \
            f"sslmode=disable on something other than the task-local Postgres: {ln.strip()}"


def test_a_writable_task_owned_api_root_is_created_and_passed_in(script):
    assert "API_ROOT_HOST=" in script and 'mkdir -p "$API_ROOT_HOST"' in script
    assert "-e API_ROOT=/srv/api-root" in script
    assert '-v "$API_ROOT_HOST:/srv/api-root"' in script


def test_hostile_ambient_values_are_unset_rather_than_inherited(script):
    assert "unset DATABASE_URL API_ROOT" in script, (
        "a developer's exported DATABASE_URL must never reach this suite")


@pytest.mark.parametrize("var", ["DATABASE_URL", "API_ROOT", "MINIO_ENDPOINT", "REDIS_URL"])
def test_the_required_values_reach_the_pytest_process(script, var):
    assert f"-e {var}=" in script


# failure propagation

def test_strict_shell_failure_propagation_is_on(script):
    assert "set -euo pipefail" in script


def test_the_pytest_status_is_captured_from_the_pipeline_not_from_tee(script):
    assert "PIPESTATUS[0]" in script, (
        "piping pytest into tee makes tee's status the pipeline's - the real one must be taken "
        "from PIPESTATUS")


def test_a_nonzero_pytest_status_is_re_propagated_after_the_guard_runs(script):
    assert 'exit "$overall"' in script
    assert "overall=\"$rc\"" in script, "a failing pytest must survive the trailing guard/logging"


def test_teardown_cannot_turn_a_red_run_green(script):
    body = script.split("cleanup() {", 1)[1].split("}", 1)[0]
    assert "exit" not in body, "an exiting trap would discard the pytest status"


def test_the_randomised_pass_does_not_name_a_nonexistent_plugin(script):
    executable = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("-p randomly" in ln for ln in executable)
    assert any("-p no:randomly" in ln for ln in executable), (
        "the deterministic passes must still pin the order")


def test_the_runner_invokes_the_structured_coverage_guard(script):
    assert "assert_integration_coverage.py" in script
    assert "--junitxml=" in script, "the guard reads a structured report, not console text"


# containment

def test_teardown_removes_only_its_own_named_resources_and_never_prunes(script):
    assert "docker rm -fv" in script, "-v so each container's anonymous volume goes with it"
    for forbidden in ("docker system prune", "docker volume prune", "docker image prune",
                      "dangling=true", "docker container prune"):
        assert forbidden not in script, f"{forbidden} would take resources this round did not create"


def test_an_unset_scratch_dir_cannot_become_a_root_delete(script):
    assert '${RUNDIR:?}' in script, "rm -rf must refuse an empty RUNDIR"


def test_the_pre_run_sweep_does_not_delete_this_run_s_scratch_directory(script):
    assert "docker_cleanup() {" in script, "the container sweep must be separable from rm -rf"
    body = script.split("docker_cleanup() {", 1)[1].split("}", 1)[0]
    assert "rm -rf" not in body and "RUNDIR" not in body
    # the sweep before provisioning must be the container-only one
    # anchored on the freshness check: the runner no longer builds, so that is what now precedes
    # provisioning. The contract is unchanged - the pre-run sweep must be container-only.
    pre = script.split("assert_mesh_image_current.sh", 1)[1].split("docker network create", 1)[0]
    assert "docker_cleanup" in pre and "\ncleanup\n" not in pre


def test_created_resources_carry_a_unique_label(script):
    assert "LABEL=hexera-ci-local" in script
    assert script.count('--label "$LABEL=1"') >= 4, "network + three services"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_the_script_is_syntactically_valid(script):
    assert subprocess.run(["bash", "-n", str(RUNNER)], capture_output=True).returncode == 0


# the guard actually refuses

def _report(tmp_path: Path, cases: str, name: str = "r.xml") -> str:
    p = tmp_path / name
    p.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    return str(p)


def _case(module: str, name: str, inner: str = "") -> str:
    return f'<testcase classname="tests.integration.{module}" name="{name}">{inner}</testcase>'


def _full_pass(extra: str = "") -> str:
    filler = "".join(_case("test_misc", f"test_filler_{i}") for i in range(320))
    return (filler
            + _case("test_geometry_source_providers", "test_another_tenant_cannot_resolve_the_source")
            + _case("test_geometry_source_providers",
                    "test_a_snapshot_naming_another_tenant_is_not_authorized")
            + _case("test_checkpoint_restart_postgres",
                    "test_the_abandoned_worker_is_fenced_at_each_point[after_admission]")
            + extra)


def test_a_correctly_provisioned_report_passes(guard, tmp_path):
    assert guard.main([_report(tmp_path, _full_pass())]) == 0


def test_the_optional_skips_are_still_allowed(guard, tmp_path):
    optional = (_case("test_graph_runtime", "test_x",
                      '<skipped message="real STEP not staged (make test-integration stages it)"/>')
                + _case("test_upload_compensation_http", "test_y",
                        '<skipped message="restricted MinIO identity not provisioned (MINIO_READONLY_KEY)"/>'))
    assert guard.main([_report(tmp_path, _full_pass(optional))]) == 0


def test_an_empty_run_is_not_a_pass(guard, tmp_path):
    assert guard.main([_report(tmp_path, "")]) == 1


def test_a_missing_report_is_not_a_pass(guard, tmp_path):
    assert guard.main([str(tmp_path / "nope.xml")]) == 1


def test_a_run_below_the_execution_floor_is_not_a_pass(guard, tmp_path):
    few = "".join(_case("test_misc", f"test_f{i}") for i in range(10))
    assert guard.main([_report(tmp_path, few)]) == 1


def test_a_database_backed_module_skipping_for_a_missing_service_is_rejected(guard, tmp_path):
    bad = _case("test_capture_authority_postgres", "test_z",
                '<skipped message="a real PostgreSQL endpoint is required"/>')
    assert guard.main([_report(tmp_path, _full_pass(bad))]) == 1


@pytest.mark.parametrize("module,name", [
    ("test_geometry_source_providers", "test_another_tenant_cannot_resolve_the_source"),
    ("test_checkpoint_restart_postgres", "test_the_abandoned_worker_is_fenced_at_each_point"),
])
def test_a_named_guarantee_that_never_ran_is_rejected(guard, tmp_path, module, name):
    report = "".join(c for c in _full_pass().split("</testcase>")
                     if not (module in c and name in c))
    report = "</testcase>".join(report.split("</testcase>")) + "</testcase>"
    filler = "".join(_case("test_misc", f"test_g{i}") for i in range(320))
    keep = [c for c in (
        _case("test_geometry_source_providers", "test_another_tenant_cannot_resolve_the_source"),
        _case("test_geometry_source_providers",
              "test_a_snapshot_naming_another_tenant_is_not_authorized"),
        _case("test_checkpoint_restart_postgres",
              "test_the_abandoned_worker_is_fenced_at_each_point[after_admission]"),
    ) if not (module in c and name in c)]
    assert guard.main([_report(tmp_path, filler + "".join(keep))]) == 1


def test_a_named_guarantee_that_was_skipped_is_rejected(guard, tmp_path):
    filler = "".join(_case("test_misc", f"test_h{i}") for i in range(320))
    body = (filler
            + _case("test_geometry_source_providers",
                    "test_another_tenant_cannot_resolve_the_source",
                    '<skipped message="real STEP not staged"/>')
            + _case("test_geometry_source_providers",
                    "test_a_snapshot_naming_another_tenant_is_not_authorized")
            + _case("test_checkpoint_restart_postgres",
                    "test_the_abandoned_worker_is_fenced_at_each_point[after_admission]"))
    assert guard.main([_report(tmp_path, body)]) == 1


def test_a_named_guarantee_that_failed_is_rejected(guard, tmp_path):
    body = _full_pass().replace(
        _case("test_checkpoint_restart_postgres",
              "test_the_abandoned_worker_is_fenced_at_each_point[after_admission]"),
        _case("test_checkpoint_restart_postgres",
              "test_the_abandoned_worker_is_fenced_at_each_point[after_admission]",
              '<failure message="fence bypassed"/>'))
    assert guard.main([_report(tmp_path, body)]) == 1


#: The canonical disposable runner must hand the tier every coordinate it needs. `run_in_container.sh`
#: is a separate runner with its own contract and is not asserted here.
CANONICAL_RUNNER = "run_disposable.sh"


@pytest.mark.parametrize("var", ["DATABASE_URL", "MESH_TEST_RUN_ID", "API_ROOT"])
def test_the_canonical_disposable_runner_supplies_every_coordinate(var):
    script = (REPO / "tests" / "integration" / CANONICAL_RUNNER).read_text()
    assert f"{var}=" in script, (
        f"{CANONICAL_RUNNER} does not supply {var}; the tier would fail downstream instead of "
        "saying what is missing")


def test_the_canonical_runner_owns_its_scratch_root_lifecycle():
    script = (REPO / "tests" / "integration" / CANONICAL_RUNNER).read_text()
    assert "API_ROOT_DIR=" in script and "${RUN_ID}" in script, \
        "the scratch root is not derived from this run's identity"
    assert "mkdir -p" in script and "test -w" in script, \
        "the runner does not create and verify a writable scratch root"
    # The EXIT trap now runs `finish`, which drops the scratch root and task database AND removes
    # this run's task bucket - storage became disposable alongside the database.
    assert "drop_scratch_root" in script and "trap finish EXIT" in script, \
        "the scratch root is not removed through the existing cleanup trap"
    assert "drop_task_db" in script and "drop_task_storage" in script, \
        "the cleanup trap does not remove both the task database and the task bucket"


#: The dependency-backed container tier. It provisions its own throwaway services, so its
#: disposable authority must be minted per invocation exactly like the canonical runner's.
CONTAINER_RUNNER = REPO / "tests" / "integration" / "run_in_container.sh"


def _container_runner() -> str:
    return CONTAINER_RUNNER.read_text()


def test_the_container_runner_mints_a_fresh_run_identity():
    src = _container_runner()
    assert "/dev/urandom" in src, "the run identity is not generated from the kernel CSPRNG"
    assert not re.search(r'^RUN_ID="(?!\$\()', src, re.MULTILINE), \
        "the run identity is a literal; two invocations would reuse it"


def test_the_container_runner_database_derives_from_that_identity():
    src = _container_runner()
    assert 'TASK_DB="meshtest_${RUN_ID}"' in src, \
        "the task database name is not derived from this run's identity"
    assert "${TASK_DB}" in src, "the suite is not pointed at the derived task database"


def _pytest_invocation() -> str:
    # The runner starts two containers: one to provision, one to run the suite. Only the second
    # carries the contract, so the assertions below must look at THAT block, not the whole file.
    src = _container_runner()
    end = src.index("-m pytest")
    return src[src.rindex("docker run", 0, end):end]


def test_the_container_runner_passes_a_matching_database_and_identity():
    src = _container_runner()
    invocation = _pytest_invocation()
    assert "-e MESH_TEST_RUN_ID=" in invocation, \
        "the SUITE container is given no run identity (the provisioning container is not enough)"
    assert "-e DATABASE_URL=" in invocation, "the suite container is given no database"
    # the exported URL the suite receives must name the derived task database
    url = re.search(r'^export DATABASE_URL="([^"]+)"', src, re.MULTILINE)
    assert url and "${TASK_DB}" in url.group(1), \
        "DATABASE_URL and MESH_TEST_RUN_ID do not describe the same database/run pair"


def test_the_container_runner_provisions_the_marker_before_pytest():
    src = _container_runner()
    assert "dd.provision(" in src and "dd.authorize(" in src, \
        "the container runner does not provision and verify a disposable marker"
    assert "DisposableAuthority(" not in src, \
        "the runner constructs an authority object instead of proving one"
    assert src.index("dd.provision(") < src.index("-m pytest"), \
        "the marker is provisioned after the suite starts"


def test_the_container_runner_checks_image_freshness_before_task_resources():
    src = _container_runner()
    assert "assert_mesh_image_current.sh" in src, "the container runner skips the freshness check"
    check = src.index("assert_mesh_image_current.sh")
    assert check < src.index("docker network create"), \
        "task resources are created before the image is proven current"


def test_the_container_runner_never_builds_and_has_no_skip_hatch():
    src = _container_runner()
    assert "docker build" not in src, "the container runner builds its own image"
    assert "SKIP_BUILD" not in src, "a caller-supplied build bypass is back"


def test_the_container_runner_cleans_up_through_one_trap():
    src = _container_runner()
    assert "trap cleanup EXIT" in src, "cleanup is not bound to every exit path"
    assert 'rm -rf "${RUNDIR:?}"' in src, "the scratch root is not removed"
    assert "docker rm -fv" in src and "network rm" in src, \
        "task containers or the task network are not removed"


def test_the_container_runner_leaves_retention_on_the_shipped_default():
    # This used to require the opposite - `-e DATA_COLLECTION_ENABLED=true` on the tier process -
    # from a time when the capture suites tried to enable collection by assigning
    # `polcfg.DATA_COLLECTION_ENABLED`, a name the policy module does not define, and so recorded
    # nothing. That was repaired at the source: every capture worker now supplies retention in its
    # OWN environment before its interpreter starts (test_capture_cross_worker,
    # test_capture_process_configuration, test_restart_side_effects, test_crash_takeover_replay,
    # test_checkpoint_dispositions all do), which is the only way a frozen-at-import mode can be
    # set at all.
    #
    # The global switch then stopped being redundant and became WRONG: it starts the tier process
    # itself in a retention mode the product does not ship, and two controls that prove a late
    # assignment cannot reconfigure a running process fail - they need the parent frozen at the
    # default to have anything to say.
    assert "-e DATA_COLLECTION_ENABLED" not in _pytest_invocation(), (
        "the tier process must run on the shipped retention default; each capture worker sets its "
        "own before it starts")


# the guard under sharding
#
# Splitting the tier four ways moved two of the guard's assertions - the execution floor and the
# three named real-PostgreSQL guarantees - off the individual shard and onto the union of every
# shard's report. That is only safe if the union really is judged as one run and if --partial
# really does keep everything a shard CAN prove. Both directions are asserted below, because
# --partial is the shape of an escape hatch and the way it would go wrong is by becoming one.

def _shard_of(tmp_path: Path, n: int, cases: str) -> str:
    return _report(tmp_path, cases, name=f"shard{n}.xml")


def _quartered(tmp_path: Path) -> list[str]:
    """The full passing tier, cut into four reports the way four shards would produce it."""
    filler = [_case("test_misc", f"test_q{i}") for i in range(320)]
    named = [
        _case("test_geometry_source_providers", "test_another_tenant_cannot_resolve_the_source"),
        _case("test_geometry_source_providers",
              "test_a_snapshot_naming_another_tenant_is_not_authorized"),
        _case("test_checkpoint_restart_postgres",
              "test_the_abandoned_worker_is_fenced_at_each_point[after_admission]"),
    ]
    per = len(filler) // 4
    shards = [filler[i * per:(i + 1) * per] for i in range(4)]
    # One named guarantee each into three different shards - which is the real arrangement: the
    # three live in different modules, so no shard can hold them all.
    for i, c in enumerate(named):
        shards[i].append(c)
    return [_shard_of(tmp_path, i + 1, "".join(s)) for i, s in enumerate(shards)]


def test_the_union_of_four_shards_is_a_pass(guard, tmp_path):
    assert guard.main(_quartered(tmp_path)) == 0


def test_no_single_shard_could_have_passed_the_whole_suite_assertions(guard, tmp_path):
    # The premise of the whole arrangement. If one shard DID satisfy the floor and the named
    # guarantees on its own, --partial would be unnecessary and this design would be pointless.
    for report in _quartered(tmp_path):
        assert guard.main([report]) == 1


def test_a_shard_passes_under_partial(guard, tmp_path):
    for report in _quartered(tmp_path):
        assert guard.main([report], partial=True) == 0


def test_partial_still_refuses_an_empty_report(guard, tmp_path):
    assert guard.main([_report(tmp_path, "")], partial=True) == 1


def test_partial_still_refuses_a_missing_report(guard, tmp_path):
    assert guard.main([str(tmp_path / "gone.xml")], partial=True) == 1


def test_partial_still_refuses_a_failure(guard, tmp_path):
    body = ("".join(_case("test_misc", f"test_p{i}") for i in range(10))
            + _case("test_worker_lease_postgres", "test_boom", "<failure>nope</failure>"))
    assert guard.main([_report(tmp_path, body)], partial=True) == 1


def test_partial_still_refuses_a_skip_for_a_missing_service(guard, tmp_path):
    # The regression the guard was written for. A shard that skipped its database-backed tests
    # because no database was provisioned must be red on the shard, not deferred to the union.
    body = ("".join(_case("test_misc", f"test_s{i}") for i in range(10))
            + _case("test_capture_authority_postgres", "test_z",
                    '<skipped message="a real PostgreSQL endpoint is required"/>'))
    assert guard.main([_report(tmp_path, body)], partial=True) == 1


def test_a_shard_missing_from_the_union_is_caught_by_the_union(guard, tmp_path):
    # Three quarters of the tier can still clear the 300-test floor, which is exactly why ci.yml
    # counts the reports before calling the guard. The guard's own backstop is the named
    # guarantees: drop the shard carrying one and the union says so.
    assert guard.main(_quartered(tmp_path)[:3]) == 1
