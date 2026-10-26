"""application/lease_keeper: a job is never lost to its own long step.

dev-areen, 25 Oct: helmholtz_coil, odri_biped, ox_mc_laser and ender3_full stopped logging inside the
builder's surface preparation - a long CPU-bound step on the run's event loop - and the lease beat,
a task on that same loop, stopped with it. The lease lapsed, the job was re-run into the same step,
and it ended as "worker lost" (ender3_full: a poison job). The keeper is a process of its own that
renews the lease while the worker is still working, and never for a worker that is stuck."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from meshpipeline.application import lease_keeper as lk

_linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                 reason="the worker's CPU is read from /proc")


def test_the_keeper_only_watches_while_the_beat_runs():
    assert lk.decide(beat_age_s=1.0, life_age_s=5000.0, fresh_s=135.0, stall_s=900.0) == "watch"


def test_a_silent_beat_on_a_working_worker_is_renewed_for():
    assert lk.decide(beat_age_s=400.0, life_age_s=10.0, fresh_s=135.0, stall_s=900.0) == "renew"
    # a beat that never marked at all is silent too
    assert lk.decide(beat_age_s=None, life_age_s=10.0, fresh_s=135.0, stall_s=900.0) == "renew"


def test_a_stuck_worker_is_left_to_lose_its_lease():
    assert lk.decide(beat_age_s=2000.0, life_age_s=901.0, fresh_s=135.0, stall_s=900.0) == "hold"


def test_one_slow_beat_never_sends_the_keeper_in():
    assert lk.fresh_window(60) == 135.0


def test_a_run_without_a_lease_has_no_keeper(monkeypatch):
    assert lk.start(None, interval_s=60) is None
    monkeypatch.setattr(lk, "ENABLED", False)
    assert lk.start(object(), interval_s=60) is None


def test_a_keeper_that_cannot_start_never_stops_the_run(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no fork")
    monkeypatch.setattr(lk.subprocess, "Popen", _boom)
    own = type("O", (), {"job_id": "j", "execution_generation": 1, "worker_token": "t"})()
    assert lk.start(own, interval_s=60) is None


# the real thing: a worker process, its keeper, and the three phases a run goes through

_PROBE = textwrap.dedent("""
    import json, os, time
    def renew(cfg):
        with open(os.environ["KEEPER_PROBE_OUT"], "a") as fh:
            fh.write(json.dumps({"t": time.monotonic(), "job": cfg["ownership"]["job_id"]}) + "\\n")
        return os.environ.get("KEEPER_PROBE_OWNED", "1") == "1"
""")

_WORKER = textwrap.dedent("""
    import json, sys, time, uuid
    from meshpipeline.application import lease_keeper as lk
    from meshpipeline.persistence.lease import ExecutionOwnership
    own = ExecutionOwnership(job_id=uuid.uuid4(), execution_generation=1, worker_token=uuid.uuid4(),
                             backend="test", pipeline_deadline_at=None)
    k = lk.start(own, interval_s=0.2, stall_s=2.0, fresh_s=1.5, renew="keeper_probe:renew")
    marks = {"keeper_pid": k.proc.pid}
    # 1. THE BEAT RUNS: the in-process beat marks the keeper every tick
    t = marks["beating"] = time.monotonic()
    while time.monotonic() - t < 2.5:
        k.mark_alive(); time.sleep(0.1)
    # 2. ONE LONG CPU-BOUND STEP holds the loop: no beat, but the CPU is busy
    t = marks["busy"] = time.monotonic()
    while time.monotonic() - t < 4.0:
        sum(range(10000))
    marks["keeper_alive_after_busy"] = k.proc.poll() is None
    # 3. STUCK: no beat and no CPU
    marks["stuck"] = time.monotonic()
    time.sleep({stuck_s})
    marks["end"] = time.monotonic()
    if {exit_hard}:
        print(json.dumps(marks), flush=True)
        import os; os._exit(0)
    k.stop()
    marks["keeper_rc"] = k.proc.returncode
    print(json.dumps(marks), flush=True)
""")


def _run_worker(tmp_path: Path, *, stuck_s: float = 5.0, owned: bool = True,
                exit_hard: bool = False) -> tuple[dict, list[float]]:
    (tmp_path / "keeper_probe.py").write_text(_PROBE)
    out = tmp_path / "renewals.jsonl"
    env = {**os.environ, "KEEPER_PROBE_OUT": str(out), "KEEPER_PROBE_OWNED": "1" if owned else "0",
           "PYTHONPATH": os.pathsep.join([str(tmp_path), *[p for p in sys.path if p]])}
    script = _WORKER.replace("{stuck_s}", str(stuck_s)).replace("{exit_hard}", str(exit_hard))
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    marks = json.loads(proc.stdout.strip().splitlines()[-1])
    times = ([json.loads(line)["t"] for line in out.read_text().splitlines()]
             if out.exists() else [])
    return marks, times


@_linux_only
def test_a_worker_busy_in_one_long_step_keeps_its_lease_and_a_stuck_one_does_not(tmp_path):
    marks, renewals = _run_worker(tmp_path)
    beating = [t for t in renewals if marks["beating"] <= t < marks["busy"]]
    busy = [t for t in renewals if marks["busy"] + 1.6 <= t < marks["stuck"]]
    # the stall window is 2 s from the last sign of life; one poll and a margin after it
    stuck_late = [t for t in renewals if marks["stuck"] + 3.5 <= t <= marks["end"]]
    assert beating == [], f"the keeper renewed while the in-process beat was running: {marks} {renewals}"
    assert len(busy) >= 2, f"the keeper did not hold the lease through the busy step: {marks} {renewals}"
    assert stuck_late == [], f"the keeper kept renewing for a worker with no sign of life: {marks} {renewals}"
    assert marks["keeper_alive_after_busy"] and marks["keeper_rc"] == 0


@_linux_only
def test_a_lease_that_is_no_longer_ours_ends_the_keeper(tmp_path):
    marks, renewals = _run_worker(tmp_path, stuck_s=0.2, owned=False)
    assert len(renewals) == 1                    # one renewal answered "not yours", then it stopped
    assert marks["keeper_alive_after_busy"] is False


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state in ("Z", "X")


@_linux_only
def test_a_keeper_never_outlives_its_worker(tmp_path):
    marks, _ = _run_worker(tmp_path, stuck_s=0.2, exit_hard=True)
    deadline = time.monotonic() + 10.0
    while not _gone(int(marks["keeper_pid"])) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _gone(int(marks["keeper_pid"])), "the keeper kept running after its worker died"


def test_the_worker_token_never_reaches_the_command_line(monkeypatch):
    seen: dict = {}

    class _P:
        pid = 1

        def __init__(self, args, **kw):
            seen["args"] = args
            self.stdin = type("I", (), {"write": lambda s, b: seen.setdefault("stdin", b),
                                        "flush": lambda s: None})()
    monkeypatch.setattr(lk.subprocess, "Popen", _P)
    import uuid
    token = uuid.uuid4()
    own = type("O", (), {"job_id": uuid.uuid4(), "execution_generation": 2, "worker_token": token,
                         "backend": "celery", "pipeline_deadline_at": None})()
    k = lk.start(own, interval_s=60)
    assert k is not None and str(token) not in " ".join(seen["args"])
    assert str(token) in seen["stdin"].decode()
    k.workdir.joinpath("beat").unlink()
    k.workdir.rmdir()
