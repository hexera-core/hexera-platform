"""RUN-4, 28 Oct: a job whose mesh runs elsewhere keeps its lease, and its remote work is never thrown away.

The cyclone (job adf9412b, a 34 M-element FEA mesh on a VM) meshed for 7 hours while its worker
waited, rightly idle. Its gmsh fill wrote no log for long stretches, so the worker saw no progress,
the job's deadline passed, the lease was lost and the VM was deleted while it was sending the
result back - minutes before delivery, with no output saved.

Now: the remote's beacon says when its processes last WORKED (`alive_at`), which is progress; the
lease keeper renews for a worker whose remote is heard from; only an owner's cancel stops a remote
run; a run already sending its result back is never stopped; and a new owner adopts the run an
earlier generation started instead of meshing again."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.application import lease_keeper as lk
from meshpipeline.contracts import progress_clock
from meshpipeline.contracts.mesh_execution import CANCELLED_BY_OWNER, HANDED_ON

# ------------------------------------------------------------------ the keeper's decision


def test_an_idle_worker_whose_remote_is_heard_keeps_its_lease():
    # the beat silent (its loop busy elsewhere), the worker idle for 6 hours: the remote speaks for it
    assert lk.decide(beat_age_s=21600.0, life_age_s=21600.0, fresh_s=135.0, stall_s=900.0,
                     remote_age_s=50.0) == "renew"


def test_a_remote_gone_silent_for_the_window_is_let_go():
    assert lk.decide(beat_age_s=21600.0, life_age_s=21600.0, fresh_s=135.0, stall_s=900.0,
                     remote_age_s=901.0) == "hold"
    assert lk.decide(beat_age_s=21600.0, life_age_s=21600.0, fresh_s=135.0, stall_s=900.0,
                     remote_age_s=None) == "hold"


# ------------------------------------------------------------------ the keeper over six hours


RENEWALS: list = []


def record_renewal(cfg) -> bool:
    RENEWALS.append(dict(cfg))
    return True


class _Ticks:
    """The keeper's stop: it ends the loop after `n` looks, and each look is a minute later."""

    def __init__(self, n: int) -> None:
        self.n, self.i = n, 0

    def wait(self, _timeout) -> bool:
        self.i += 1
        return self.i > self.n


class _IdleWorker:
    """The worker's process tree, as the keeper reads it: idle - its mesh runs on another machine.
    Each look, the remote is heard (its beacon written again) until `silent_after` looks."""

    def __init__(self, remote: Path, progress: Path, silent_after: int) -> None:
        self.remote, self.progress, self.silent_after, self.look = remote, progress, silent_after, 0

    def poll(self):
        self.look += 1
        if self.look <= self.silent_after:
            os.utime(self.remote, ns=(self.look * 10**9, self.look * 10**9))
            os.utime(self.progress, ns=(self.look * 10**9, self.look * 10**9))
        return None                          # no CPU, no memory change: the worker is idle


def _keeper_run(tmp_path, monkeypatch, *, looks: int, silent_after: int):
    import meshpipeline.sandbox.liveness as liveness
    RENEWALS.clear()
    beat, remote, progress = (tmp_path / n for n in ("beat", "remote", "progress"))
    for p in (beat, remote, progress):
        p.touch()
    worker = _IdleWorker(remote, progress, silent_after)
    monkeypatch.setattr(liveness, "LivenessWatch", lambda *a, **k: worker)
    minutes = iter(range(10**6))
    cfg = {"ownership": {"job_id": "cyclone"}, "watch_pid": os.getppid(), "interval_s": 60.0,
           "stall_s": 900.0, "fresh_s": 135.0, "beat_path": str(beat), "remote_path": str(remote),
           "progress_path": str(progress),
           "renew": f"{__name__}:record_renewal"}
    why = lk.run(cfg, _Ticks(looks), clock=lambda: 60.0 * next(minutes))
    return why, list(RENEWALS)


def test_a_six_hour_remote_mesh_keeps_its_lease_and_its_deadline(tmp_path, monkeypatch):
    # six hours of a remote mesh, one look a minute; the worker idle and its beat silent all along
    why, renewals = _keeper_run(tmp_path, monkeypatch, looks=360, silent_after=10**6)
    assert why == "stopped"
    # renewed at every look once the beat's own window passed - never left to lapse
    assert len(renewals) >= 357
    # and the deadline moved on with the remote's progress, as the in-process beat would have
    assert all(r.get("extend_to") for r in renewals)


def test_a_remote_that_dies_still_lets_the_lease_lapse(tmp_path, monkeypatch):
    # the remote is heard for 100 minutes, then its machine is gone: renewals stop one stall window
    # (15 minutes) later, and the lease lapses as a stuck worker's does
    why, renewals = _keeper_run(tmp_path, monkeypatch, looks=360, silent_after=100)
    assert why == "stopped"
    assert 100 <= len(renewals) <= 117


# ------------------------------------------------------------------ the worker hears its remote


class _Blob:
    def __init__(self, generation: int, doc: dict) -> None:
        import json
        self.generation, self._doc = generation, json.dumps(doc).encode()

    def download_as_bytes(self) -> bytes:
        return self._doc


class _Beacons:
    def __init__(self) -> None:
        self.blob = None

    def get_blob(self, _key):
        return self.blob


def test_a_remote_that_works_without_writing_is_progress(tmp_path, monkeypatch):
    from meshpipeline.adapters.mesh_execution.cloud_run_client import _relay_progress
    monkeypatch.setattr("meshpipeline.contracts.mesh_progress.report", lambda doc: None)
    remote, progress = tmp_path / "remote", tmp_path / "progress"
    progress_clock.set_marks(remote=str(remote), progress=str(progress))
    try:
        b, seen = _Beacons(), {}
        b.blob = _Blob(1, {"key": "fill", "progress_at": 100.0, "alive_at": 100.0})
        assert _relay_progress(b, "p", seen) and remote.exists()
        progress.unlink()
        # an hour later: its log has not moved (progress_at the same), but its processes worked
        b.blob = _Blob(2, {"key": "fill", "progress_at": 100.0, "alive_at": 3700.0})
        assert _relay_progress(b, "p", seen) and progress.exists()
        progress.unlink()
        # neither moved: heard (the run is in flight) but no progress
        b.blob = _Blob(3, {"key": "fill", "progress_at": 100.0, "alive_at": 3700.0})
        assert _relay_progress(b, "p", seen) and not progress.exists()
        assert seen["stage"] == "fill"
    finally:
        progress_clock.set_marks(remote=None, progress=None)


def test_the_beacon_says_when_the_mesher_last_worked(tmp_path, monkeypatch):
    from meshpipeline.runtime import mesh_runner
    sent: list = []
    monkeypatch.setattr("meshpipeline.adapters.mesh_execution.gcs_exchange.upload_progress",
                        lambda uri, doc: sent.append(doc))
    monkeypatch.setattr(mesh_runner, "_life_watch", lambda: SimpleNamespace(poll=lambda: "cpu"))
    monkeypatch.setattr("meshpipeline.engines.mesher_stage.read_stage", lambda ws: {"key": "fill"})
    stop = threading.Event()
    t = threading.Thread(target=mesh_runner._beacon, args=(str(tmp_path), "gs://b/o", stop),
                         kwargs={"every_s": 0.01, "pulse_s": 0.0})
    t.start()
    import time
    time.sleep(0.2)
    stop.set()
    t.join()
    assert sent and all("alive_at" in d for d in sent)
    assert sent[-1]["alive_at"] > sent[0]["alive_at"] or len(sent) == 1


# ------------------------------------------------------------------ never thrown away


class _Exchange:
    def __init__(self, written: bool = False) -> None:
        self.written = written

    def blob(self, _key):
        return SimpleNamespace(exists=lambda: self.written)


def test_a_cancel_stops_the_remote_but_a_hand_on_never_does():
    from meshpipeline.adapters.mesh_execution.cloud_run_client import remote_left_running
    assert remote_left_running(CANCELLED_BY_OWNER, {"stage": "fill"}, _Exchange(), "o") == (False, "")
    keep, why = remote_left_running(HANDED_ON, {"stage": "fill"}, _Exchange(), "o")
    assert keep and "handed on" in why
    # the worker going away hands the job back: the remote finishes for the next owner
    assert remote_left_running("the worker is shutting down; the job is being handed back", {}, _Exchange(),
                               "o")[0]


def test_a_remote_sending_its_result_back_is_never_stopped_even_on_a_cancel():
    from meshpipeline.adapters.mesh_execution.cloud_run_client import remote_left_running
    assert remote_left_running(CANCELLED_BY_OWNER, {"stage": "returning"}, _Exchange(), "o")[0]
    assert remote_left_running(CANCELLED_BY_OWNER, {}, _Exchange(written=True), "o")[0]


def test_the_exchange_says_whether_a_run_is_still_there():
    import datetime as dt

    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc
    key = "a" * 64

    class _B:
        def __init__(self, result: bool, beacon_age_s: float | None) -> None:
            self.result, self.age = result, beacon_age_s

        def blob(self, _k):
            return SimpleNamespace(exists=lambda: self.result)

        def get_blob(self, _k):
            if self.age is None:
                return None
            return SimpleNamespace(updated=dt.datetime.fromtimestamp(10_000.0 - self.age, tz=dt.UTC))

    assert crc.remote_state(key, bucket=_B(True, None), now=10_000.0) == "result"
    assert crc.remote_state(key, bucket=_B(False, 60.0), now=10_000.0) == "running"
    assert crc.remote_state(key, bucket=_B(False, crc.WAIT_STALL_S + 1), now=10_000.0) is None
    assert crc.remote_state(key, bucket=_B(False, None), now=10_000.0) is None


# ------------------------------------------------------------------ the new owner adopts the run


class _Provider:
    def __init__(self, states: dict) -> None:
        self.states, self.collected, self.ran = states, [], []

    def remote_state(self, key):
        return self.states.get(key)

    def collect(self, workspace, *, engine, timeout, operation_key):
        self.collected.append(operation_key)
        return {"rc": 0}

    def run(self, workspace, *, engine, timeout, operation_key):
        self.ran.append(operation_key)
        return {"rc": 0, "provider_reference": "gce:projects/p/zones/z/instances/new"}


@pytest.fixture()
def new_owner(monkeypatch, tmp_path):
    from meshpipeline.application import execution_fence
    from meshpipeline.persistence.repositories import native_submission_repository as claims
    own = SimpleNamespace(job_id="job", execution_generation=2, worker_token="tok2")
    monkeypatch.setattr(execution_fence, "current_ownership", lambda: own)
    seen: dict = {"accepted": []}
    monkeypatch.setattr(claims, "claim", lambda **kw: claims.ClaimOutcome(
        claims.ClaimResult.acquired, claims.operation_key("job", 2, kw["semantic_operation"]),
        claims.Disposition.claimed))
    monkeypatch.setattr(claims, "earlier_runs", lambda **kw: seen.setdefault("asked", kw) and ["gen1key"])
    monkeypatch.setattr(claims, "mark_accepted", lambda **kw: seen["accepted"].append(kw))
    monkeypatch.setattr(claims, "still_owned", lambda **kw: True)
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    return ws, seen


def test_a_new_owner_collects_the_mesh_still_running_instead_of_meshing_again(new_owner):
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    ws, seen = new_owner
    provider = _Provider({"gen1key": "running"})
    out = ClaimingMeshExecutor(provider).run(ws, engine="gmsh", timeout=60)
    assert provider.collected == ["gen1key"] and provider.ran == []
    assert out["adopted_from"] == "gen1key"
    assert seen["accepted"] and seen["accepted"][0]["provider_reference"] == "adopted:gen1key"
    # the same run: same job, operation, payload and engine, an earlier generation
    assert seen["asked"]["execution_generation"] == 2 and seen["asked"]["engine"] == "gmsh"


def test_a_new_owner_meshes_again_only_when_nothing_is_left_to_adopt(new_owner):
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    ws, _seen = new_owner
    provider = _Provider({})                      # the earlier run is gone, with no result
    ClaimingMeshExecutor(provider).run(ws, engine="gmsh", timeout=60)
    assert provider.collected == [] and len(provider.ran) == 1
