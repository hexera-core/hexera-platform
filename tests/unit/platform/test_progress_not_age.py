# Responsibility: Verify that a job still making progress is never ended by a clock - the pipeline's
# deadline moves on while progress is seen, the heartbeat renews past it only for a progressing job,
# a deterministic driver's attempt is judged by the pipeline's time, the worker keeps waiting for a
# remote mesh whose beacon still moves, the remote's beacon pulses while its output moves, the local
# watchdog notes progress, and the Cloud Run wall routes a pass to a VM (RUN-5) - while a job that has
# STOPPED making progress is still ended as before.
# Boundaries: pure functions and stood-in rows, buckets and clocks; no database, no GCS, no mesher.
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meshpipeline.application import pipeline_budget as pb
from meshpipeline.contracts import mesh_machine as MM
from meshpipeline.contracts import progress_clock


@pytest.fixture(autouse=True)
def _fresh_clock():
    progress_clock.reset()
    pb._EXTENDED.clear()
    yield
    progress_clock.reset()
    pb._EXTENDED.clear()


# THE PIPELINE DEADLINE

def test_no_progress_means_no_extension_and_the_deadline_stands():
    assert pb.progress_extension() is None
    deadline = time.time() - 5
    assert pb.is_exhausted(deadline)


def test_recent_progress_moves_the_deadline_a_stall_window_past_it():
    progress_clock.touch()
    now = time.time()
    to = pb.progress_extension(now=now)
    assert to == pytest.approx(now + pb.PROGRESS_GRACE_S, abs=2)
    deadline = now - 60                                  # the anchored deadline has passed
    pb.extend(deadline, to)
    assert not pb.is_exhausted(deadline)
    assert pb.remaining_seconds(deadline, now=now) == pytest.approx(pb.PROGRESS_GRACE_S, abs=2)
    # every child budget capped by the deadline sees the moved one
    assert pb.cap_child_budget(600, deadline, now=now) == 600
    assert pb.cap_child_deadline_epoch(now + 10_000, deadline) == pytest.approx(to)


def test_progress_too_long_ago_extends_nothing(monkeypatch):
    progress_clock.touch()
    monkeypatch.setattr(progress_clock, "seconds_since", lambda: pb.PROGRESS_GRACE_S + 1)
    assert pb.progress_extension() is None


def test_an_extension_never_moves_the_deadline_back():
    d = time.time() + 100
    pb.extend(d, d + 500)
    pb.extend(d, d + 10)
    assert pb.effective_deadline(d) == pytest.approx(d + 500)
    assert pb.effective_deadline(d + 1) == d + 1          # another run's deadline is untouched


def test_the_engine_ladder_reads_the_moved_pipeline_deadline_but_not_the_builders():
    from meshpipeline.pipeline.engine_fallback import remaining_seconds

    now = time.time()
    pipe = now - 10
    pb.extend(pipe, now + 900)
    assert remaining_seconds({"pipeline_deadline_epoch": pipe}, now=now) == pytest.approx(900, abs=1)
    assert remaining_seconds({"pipeline_deadline_epoch": pipe, "builder_deadline_epoch": now + 60},
                             now=now) == pytest.approx(60, abs=1)


# THE HEARTBEAT

class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _lease(deadline_delta_s: int):
    from meshpipeline.persistence.lease import ExecutionOwnership

    now = datetime.now(UTC)
    token, job = uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(id=job, status="running", execution_generation=1, active_worker_token=token,
                          lease_heartbeat_at=now, lease_expires_at=now + timedelta(seconds=900),
                          pipeline_deadline_at=now + timedelta(seconds=deadline_delta_s))
    own = ExecutionOwnership(job_id=job, execution_generation=1, worker_token=token, backend="celery",
                             pipeline_deadline_at=row.pipeline_deadline_at)
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(row)), flush=AsyncMock())
    return db, own, row, now


@pytest.mark.asyncio
async def test_a_progressing_job_past_its_deadline_keeps_its_lease_and_its_deadline_moves():
    from meshpipeline.persistence.lease import LeaseRepository

    db, own, row, now = _lease(deadline_delta_s=-60)
    to = now + timedelta(seconds=pb.PROGRESS_GRACE_S)
    assert await LeaseRepository().heartbeat(db, own, extend_deadline_to=to) is True
    assert row.pipeline_deadline_at == to and row.lease_expires_at > now


@pytest.mark.asyncio
async def test_a_stalled_job_past_its_deadline_is_still_refused():
    from meshpipeline.persistence.lease import LeaseRepository

    db, own, row, now = _lease(deadline_delta_s=-60)
    before = row.pipeline_deadline_at
    assert await LeaseRepository().heartbeat(db, own) is False
    # an "extension" that is itself already past changes nothing either
    assert await LeaseRepository().heartbeat(db, own, extend_deadline_to=now - timedelta(seconds=1)) is False
    db.flush.assert_not_awaited()
    assert row.pipeline_deadline_at <= now and row.pipeline_deadline_at >= before


@pytest.mark.asyncio
async def test_the_heartbeat_never_moves_a_deadline_back():
    from meshpipeline.persistence.lease import LeaseRepository

    db, own, row, now = _lease(deadline_delta_s=7200)
    before = row.pipeline_deadline_at
    assert await LeaseRepository().heartbeat(db, own, extend_deadline_to=now + timedelta(seconds=60)) is True
    assert row.pipeline_deadline_at == before


# THE BUILDER'S BUDGET

def test_a_drivers_attempt_is_judged_by_the_pipelines_time_not_the_model_loops():
    """An FEA retry after a long, progressing first pass: the model loop's aggregate budget is spent,
    the pipeline still has time - the driver's attempt runs."""
    from meshpipeline.agents.builder.budget import settle

    now = time.time()
    b = settle(carried_epoch=now - 10, pipeline_deadline_epoch=now + 3600, now=now)
    assert b.exhausted and not b.driver_exhausted and b.driver_remaining_s == pytest.approx(3600, abs=2)
    # and the pipeline's time is the time progress moved it to
    gone = now - 30
    pb.extend(gone, now + 600)
    b = settle(carried_epoch=now - 10, pipeline_deadline_epoch=gone, now=now)
    assert not b.driver_exhausted and b.driver_remaining_s == pytest.approx(600, abs=2)
    b = settle(carried_epoch=now - 10, pipeline_deadline_epoch=now - 5, now=now)
    assert b.driver_exhausted


def test_only_an_engine_with_a_driver_for_this_run_is_held_to_the_pipelines_time(monkeypatch):
    # the engine's own choice of driver is stood in (the real gmsh one needs the CAD toolchain,
    # tests/unit/engines/test_fea_driver.py covers it): this pins how takes_driver reads it
    from meshpipeline.agents.builder import invoke

    class _Spec:
        build_driver = None

        def build_driver_for(self, state):
            return (lambda *a, **k: None) if state.get("purpose") == "structural" else None

    def _get_spec(name):
        if name != "gmsh":
            raise KeyError(name)
        return _Spec()

    # raising=False: the stand-in is set whatever an earlier test in the shard left of the module
    monkeypatch.setattr(invoke, "get_spec", _get_spec, raising=False)
    assert invoke.takes_driver(SimpleNamespace(engine="gmsh"), {"purpose": "structural"}) is True
    assert invoke.takes_driver(SimpleNamespace(engine="gmsh"), {"purpose": "external_cfd"}) is False
    assert invoke.takes_driver(SimpleNamespace(engine="no-such-engine"), {}) is False


# RUN-5: THE CLOUD RUN WALL

def test_a_plan_planned_past_the_cloud_run_wall_goes_to_the_smallest_vm_that_holds_it():
    cloud = MM.choose(process_gib=4.0, workspace_gib=2.0)
    assert cloud["where"] == "cloud_run"
    assert MM.for_time(cloud, planned_s=1000.0, wall_s=13_800) is cloud
    assert MM.for_time(cloud, planned_s=None, wall_s=13_800) is cloud
    moved = MM.for_time(cloud, planned_s=13_800 / MM.TIME_MARGIN + 60, wall_s=13_800)
    assert moved["where"] == "gce" and moved["machine"] == MM.MACHINES[0].name and moved["why"] == "time"
    vm = MM.choose(process_gib=100.0, workspace_gib=10.0)
    assert MM.for_time(vm, planned_s=1e9, wall_s=13_800) is vm         # already on a VM


def test_a_pass_that_met_the_wall_still_working_moves_to_a_vm_once():
    cloud = MM.choose(process_gib=4.0, workspace_gib=2.0)
    moved = MM.off_the_wall(cloud)
    assert moved is not None and moved["where"] == "gce" and moved["stepped_from"] == "cloud_run"
    assert "nothing coarsened" in moved["words"]
    assert MM.off_the_wall(moved) is None                               # a VM has no wall to move off


# THE WORKER'S WAIT FOR A REMOTE MESH

class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Beacon:
    def __init__(self, gen, doc):
        self.generation, self._doc = gen, doc

    def download_as_bytes(self):
        return json.dumps(self._doc).encode()


class _Bucket:
    """No result ever lands. While `alive(t)` the remote rewrites its beacon (a pulsing runner); while
    `moving(t)` too, its output moved since the last one (progress_at advances)."""

    def __init__(self, clock, alive, moving, pulses=True):
        self.clock, self.alive, self.moving, self.pulses = clock, alive, moving, pulses
        self.gen, self.progress_at = 0, clock.t

    def blob(self, _key):
        return SimpleNamespace(exists=lambda: False)

    def get_blob(self, _key):
        t = self.clock.t
        if self.alive(t):
            self.gen += 1
            if self.moving(t):
                self.progress_at = t
        if not self.gen:
            return None
        doc = {"key": "volume", "label": "filling the volume"}
        if self.pulses:
            doc["progress_at"] = self.progress_at
        return _Beacon(self.gen, doc)


def _wait(monkeypatch, *, alive, moving=lambda t: True, deadline_s=600.0, pulses=True):
    from meshpipeline.adapters.mesh_execution import cloud_run_client as C

    clock = _Clock()
    touches: list[float] = []
    monkeypatch.setattr(C.time, "time", clock.time)
    monkeypatch.setattr(C.time, "sleep", clock.sleep)
    monkeypatch.setattr(C, "abandonment_reason", lambda: None)
    monkeypatch.setattr(progress_clock, "touch", lambda: touches.append(clock.t))
    start = clock.t
    out = C._poll_gcs_json(_Bucket(clock, alive, moving, pulses), "res", deadline_s=deadline_s, progress_key="p")
    return out, clock.t - start, touches, C


T0 = 1_000_000.0


def test_a_remote_still_heard_from_is_waited_for_past_the_deadline(monkeypatch):
    out, waited, touches, C = _wait(monkeypatch, alive=lambda t: t < T0 + 5000)
    assert out is None
    # kept waiting while the remote was there, then stopped one stall window after it went silent
    assert waited == pytest.approx(5000 + C.WAIT_STALL_S, abs=30)
    assert touches and touches[-1] >= T0 + 4900           # its moving output was the job's progress


def test_a_remote_alive_but_not_moving_is_not_progress(monkeypatch):
    out, waited, touches, C = _wait(monkeypatch, alive=lambda t: t < T0 + 3000, moving=lambda t: t < T0 + 100)
    assert waited == pytest.approx(3000 + C.WAIT_STALL_S, abs=30)
    assert touches and max(touches) < T0 + 200            # the pulse alone moved no deadline


def test_a_pulsing_remote_that_goes_silent_is_given_up_long_before_the_deadline(monkeypatch):
    """A VM deleted under its run: the wait ends a stall window after the last pulse, not 20 hours later."""
    out, waited, _t, C = _wait(monkeypatch, alive=lambda t: t < T0 + 100, deadline_s=20 * 3600)
    assert out is None and waited == pytest.approx(100 + C.WAIT_STALL_S, abs=30)


def test_a_remote_that_does_not_pulse_is_waited_for_to_the_deadline_as_before(monkeypatch):
    out, waited, _t, _C = _wait(monkeypatch, alive=lambda t: t < T0 + 100, deadline_s=7200, pulses=False)
    assert waited == pytest.approx(7200, abs=15)
    out, waited, _t, _C = _wait(monkeypatch, alive=lambda t: False, deadline_s=600.0)
    assert out is None and waited == pytest.approx(600, abs=15)


# THE REMOTE'S BEACON AND THE LOCAL WATCHDOG

def test_the_beacon_pulses_while_the_runner_lives_and_says_when_the_output_last_moved(tmp_path, monkeypatch):
    from meshpipeline.adapters.mesh_execution import gcs_exchange
    from meshpipeline.runtime import mesh_runner

    written: list[dict] = []
    monkeypatch.setattr(gcs_exchange, "upload_progress", lambda uri, doc: written.append(doc))
    log = tmp_path / "gmshfea.log"
    log.write_text("start\n")
    stop, returning = threading.Event(), threading.Event()
    t = threading.Thread(target=mesh_runner._beacon, args=(str(tmp_path), "gs://b/k/out.tar.gz", stop),
                         kwargs={"every_s": 0.02, "pulse_s": 0.1, "returning": returning})
    t.start()
    for k in range(5):                                   # the mesher keeps printing, no new stage
        time.sleep(0.08)
        log.write_text(log.read_text() + f"line {k}\n")
    time.sleep(0.05)
    moving = list(written)
    time.sleep(0.4)                                      # the mesher goes quiet: pulses, no movement
    quiet = written[len(moving):]
    returning.set()                                      # the result is being sent back
    time.sleep(0.3)
    stop.set()
    t.join(2)
    back = [w for w in written if w.get("key") == "returning"]
    assert len({w["progress_at"] for w in moving}) >= 3
    assert quiet and len({w["progress_at"] for w in quiet}) == 1 and quiet[0]["progress_at"] <= moving[-1]["progress_at"] + 0.1
    assert back and back[-1]["progress_at"] > quiet[-1]["progress_at"]   # sending it back is progress


def test_the_local_watchdog_notes_the_meshers_progress(tmp_path):
    import sys

    from meshpipeline.sandbox.safe_exec import run_watched

    w = run_watched([sys.executable, "-c", "import time\nfor i in range(3):\n print(i, flush=True); time.sleep(0.3)"],
                    log_path=tmp_path / "m.log", hard_timeout=30, stall_s=20, poll_s=0.1)
    assert w.stopped == "ok"
    assert progress_clock.seconds_since() is not None and progress_clock.seconds_since() < 5
