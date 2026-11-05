# Responsibility: Verify retention is owned by the process that captures, and cannot be granted from outside it.
# Boundaries: real worker processes against real PostgreSQL; the product default is never changed.

# Capture is gated by a product mode that settings/modes.py reads ONCE, at process start, and
# freezes onto polcfg.MODES. That is deliberate - a running process does not silently change what
# it retains - and it is what the capture suites got wrong: they assigned
# `polcfg.DATA_COLLECTION_ENABLED = True`, a name the policy module does not define, so the
# assignment bound an attribute nobody reads and every process stayed disabled. Nine assertions
# about durable operations then measured a system that had been asked to record nothing.
#
# These controls pin the boundary itself, so the next process that needs collection has to own its
# own configuration rather than borrow one.
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

from meshpipeline.persistence.repositories import capture_repository as cap

OWNER = "owner-process-config"


def _template_default() -> bool | None:
    # What a clone starts with, read from the tracked template rather than restated here. Keeping a
    # second copy of the default in the test is what let the code and the template drift apart
    # without anything failing.
    #
    # None when the template is not readable: this tier runs INSIDE the worker image, which ships
    # the source and the tests but not the repository's .env.example.
    template = Path(__file__).parents[2] / ".env.example"
    if not template.is_file():
        return None
    for line in template.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "DATA_COLLECTION_ENABLED":
            return value.strip().lower() in ("1", "true", "yes", "on")
    raise AssertionError("DATA_COLLECTION_ENABLED is not in .env.example at all")


_TEMPLATE_DEFAULT = _template_default()


def _code_default(monkeypatch) -> bool:
    # The default the AUTHORITY produces with the variable absent, which is what a bare deployment
    # gets. Read from it rather than restated, so this file pins no mode of its own.
    from meshpipeline.settings.modes import load_product_modes
    monkeypatch.delenv("DATA_COLLECTION_ENABLED", raising=False)
    return load_product_modes().data_collection_enabled

#: A worker that reports what it was configured to be, then writes one operation through the real
#: durable authority. It never edits its own retention setting: whatever it inherited at startup
#: is what it is.
_PROBE = """
import json, os, sys
sys.path.insert(0, os.environ["SRC"])
import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
rtcfg.JOBS_DIR = os.environ["DATA_ROOT"] + "/jobs"
rtcfg.CORPUS_DIR = os.environ["DATA_ROOT"] + "/corpus"
os.makedirs(rtcfg.JOBS_DIR, exist_ok=True); os.makedirs(rtcfg.CORPUS_DIR, exist_ok=True)
from meshpipeline.capture import scope, trace
job, payload, op = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
scope.bind(os.environ["OWNER"], job)
if os.environ.get("PROBE_FAIL") == "1":
    raise SystemExit("this worker was asked to fail")
trace.add_event(job, "node_effect", payload, attributes={"op_id": op})
print(json.dumps({"pid": os.getpid(),
                  "collecting": polcfg.MODES.data_collection_enabled}))
"""


class WorkerRefused(AssertionError):
    pass


def _spawn(job_id: str, op_id: str, root: Path, *, collecting: bool, payload=None,
           fail: bool = False) -> dict:
    # Retention is supplied HERE, before the interpreter starts. There is no other way for this
    # worker to end up collecting, which is the whole point.
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "SRC": str(Path.cwd() / "src"),
           "OWNER": OWNER,
           "DATA_ROOT": str(root),
           "DATA_COLLECTION_ENABLED": "true" if collecting else "false",
           "PROBE_FAIL": "1" if fail else "0"}
    proc = subprocess.run([sys.executable, "-c", _PROBE, job_id,
                           json.dumps(payload if payload is not None else {"verdict": "PASS"}),
                           op_id],
                          cwd=root, capture_output=True, text=True, timeout=180, env=env)
    if proc.returncode != 0:
        # A worker that died is a failure of this test, never a quiet zero-operation result that
        # would read as "collection is off".
        raise WorkerRefused(f"worker exited {proc.returncode}: {proc.stderr[-1500:]}")
    # The PID comes from the worker itself, so it names the process that actually did the work.
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    report["returncode"] = proc.returncode
    return report


def _ops(job_id: str) -> list[dict]:
    return cap.trusted_operations(owner_id=OWNER, job_id=job_id)


@pytest.fixture()
async def SessionLocal():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import meshpipeline.settings.providers as provcfg

    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=8)
    await hp.reset_schema(provcfg.POSTGRES_DSN)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
async def job(SessionLocal):
    from meshpipeline.persistence.models import SimulationJob
    row = SimulationJob(owner_id=OWNER)
    async with SessionLocal() as db:
        db.add(row)
        await db.commit()
        return str(row.id)


# 1 / 14. the shipped default retains nothing


async def test_a_worker_started_disabled_captures_nothing(job, tmp_path):
    report = _spawn(job, "work:disabled", tmp_path / "d", collecting=False)
    assert report["collecting"] is False, "the worker reported itself enabled"
    assert _ops(job) == [], "a disabled process wrote a durable operation"


def test_the_shipped_default_is_what_the_template_declares(monkeypatch):
    # Read from the authority that decides it, with the variable absent - not from a copy of the
    # default kept here. What the default IS is a product decision; what this pins is that the code
    # and the template a clone starts from cannot disagree about it, because a clone that retains
    # when its own .env says it should not is a surprise either way round.
    if _TEMPLATE_DEFAULT is None:
        pytest.skip("the tracked .env.example is not present in this image")
    assert _code_default(monkeypatch) is _TEMPLATE_DEFAULT, (
        f"the code defaults to {not _TEMPLATE_DEFAULT} but .env.example ships "
        f"DATA_COLLECTION_ENABLED={_TEMPLATE_DEFAULT}")


async def test_a_worker_with_the_variable_absent_entirely_follows_the_shipped_default(
        job, tmp_path, monkeypatch):
    # The default as a DEPLOYMENT experiences it: a process where the variable was never set at
    # all, rather than one where a test deleted it after import. This is the case a bare clone
    # runs, and what it does must be the shipped default rather than anything this file decides.
    root = tmp_path / "bare"
    root.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "DATA_COLLECTION_ENABLED"}
    env.update({"SRC": str(Path.cwd() / "src"), "OWNER": OWNER, "DATA_ROOT": str(root),
                "PROBE_FAIL": "0"})
    proc = subprocess.run([sys.executable, "-c", _PROBE, job, json.dumps({"verdict": "BARE"}),
                           "work:bare"],
                          cwd=root, capture_output=True, text=True, timeout=180, env=env)
    assert proc.returncode == 0, proc.stderr[-1500:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    expected = _code_default(monkeypatch)
    assert report["collecting"] is expected, (
        "a bare process did not reach the same default the settings authority reports")
    if not expected:
        assert _ops(job) == []


# 2 / 9. an enabled worker records exactly what it was asked to


async def test_an_enabled_worker_captures_exactly_the_expected_operations(job, tmp_path):
    _spawn(job, "work:one", tmp_path / "a", collecting=True, payload={"verdict": "PASS"})
    _spawn(job, "work:two", tmp_path / "b", collecting=True, payload={"verdict": "OTHER"})

    rows = _ops(job)
    assert len(rows) == 2, f"expected exactly two durable operations, found {len(rows)}: {rows}"
    assert sorted(r["payload"]["verdict"] for r in rows) == ["OTHER", "PASS"]
    assert cap.conflicted_operations(owner_id=OWNER, job_id=job) == []


# 3. the surrounding process cannot grant collection to a worker


async def test_the_pytest_environment_cannot_reconfigure_a_worker(job, tmp_path, monkeypatch):
    import meshpipeline.settings.policy as polcfg

    # The invariant is that retention is decided ONCE, at startup - not which way it was decided.
    # Asserting a literal here made this a test of the current default, so flipping that default
    # broke a test that has nothing to do with it.
    decided_at_startup = polcfg.MODES.data_collection_enabled
    monkeypatch.setenv("DATA_COLLECTION_ENABLED", str(not decided_at_startup).lower())
    assert polcfg.MODES.data_collection_enabled is decided_at_startup, (
        "this process re-read the environment after import; retention must be decided once, at "
        "startup, or a deployment could be made to retain data by a late assignment")

    report = _spawn(job, "work:not-granted", tmp_path / "n", collecting=False)
    assert report["collecting"] is False
    assert _ops(job) == [], (
        "an environment variable set in the parent after import turned on collection in a worker "
        "that was started disabled")


async def test_an_orphaned_legacy_flag_cannot_enable_collection(job, tmp_path, monkeypatch):
    # The exact defect: assigning the name the suites used to assign must not appear to work.
    import meshpipeline.settings.policy as polcfg

    decided_at_startup = polcfg.MODES.data_collection_enabled
    monkeypatch.setattr(polcfg, "DATA_COLLECTION_ENABLED", not decided_at_startup, raising=False)
    assert polcfg.MODES.data_collection_enabled is decided_at_startup
    report = _spawn(job, "work:legacy", tmp_path / "l", collecting=False)
    assert report["collecting"] is False
    assert _ops(job) == [], "a legacy attribute assignment enabled collection"


# 4 / 5. two workers, two configurations, no cross-configuration


async def test_an_enabled_and_a_disabled_worker_do_not_cross_configure(job, tmp_path):
    off = _spawn(job, "work:off", tmp_path / "off", collecting=False, payload={"verdict": "OFF"})
    on = _spawn(job, "work:on", tmp_path / "on", collecting=True, payload={"verdict": "ON"})

    assert off["pid"] != on["pid"], "both units of work ran in the same process"
    assert off["collecting"] is False and on["collecting"] is True, (
        "a worker did not get the configuration it was started with")

    rows = _ops(job)
    assert len(rows) == 1, f"exactly one of the two workers may have recorded: {rows}"
    assert rows[0]["payload"] == {"verdict": "ON"}, (
        "the operation that survived came from the disabled worker")


async def test_each_unit_of_work_runs_in_the_process_it_was_given_to(job, tmp_path):
    first = _spawn(job, "work:p1", tmp_path / "p1", collecting=True, payload={"verdict": "P1"})
    second = _spawn(job, "work:p2", tmp_path / "p2", collecting=True, payload={"verdict": "P2"})
    assert first["pid"] != second["pid"], "the two operations shared one process"
    assert len(_ops(job)) == 2


# 10 / 11 / 12. a failing worker fails the test, and nothing is left running


async def test_a_worker_that_dies_fails_the_test_rather_than_reading_as_disabled(job, tmp_path):
    with pytest.raises(WorkerRefused) as refused:
        _spawn(job, "work:dead", tmp_path / "x", collecting=True, fail=True)
    assert "asked to fail" in str(refused.value)
    assert _ops(job) == [], "a worker that died still left a durable operation"


async def test_no_worker_process_outlives_its_run(job, tmp_path):
    import errno

    report = _spawn(job, "work:reaped", tmp_path / "r", collecting=True)
    assert report["returncode"] == 0
    with pytest.raises(OSError) as gone:
        os.kill(report["pid"], 0)
    assert gone.value.errno in (errno.ESRCH, errno.EPERM), (
        f"the worker process {report['pid']} is still alive after its run")


async def test_no_worker_process_outlives_a_failing_run(job, tmp_path):
    import errno

    with pytest.raises(WorkerRefused):
        _spawn(job, "work:dead2", tmp_path / "x2", collecting=True, fail=True)
    # Nothing to reap: subprocess.run waits, so a failed worker is already gone. The only
    # acceptable answer is ECHILD - "this process has no children at all".
    #
    # Not `waitpid(...) == 0`: that is what waitpid returns while a child is STILL RUNNING, so
    # asserting it would pass on precisely the state this test exists to reject.
    try:
        pid, _status = os.waitpid(-1, os.WNOHANG)
    except OSError as exc:
        assert exc.errno == errno.ECHILD, f"unexpected waitpid error: {exc}"
    else:
        pytest.fail(f"a child process survived the failing run (waitpid returned {pid})")


# 13. the run leaves nothing of its own behind


async def test_a_disabled_run_leaves_no_row_for_any_owner(job, tmp_path):
    _spawn(job, "work:clean", tmp_path / "c", collecting=False)
    assert _ops(job) == []
    assert cap.trusted_operations(owner_id="stranger", job_id=job) == [], (
        "an operation was filed under an owner that never ran anything")
