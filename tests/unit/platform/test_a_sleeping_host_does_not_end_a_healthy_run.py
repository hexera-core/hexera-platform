"""A suspended host must not cost a finished mesh, and a budget must fit the work it buys.

WHAT HAPPENED, job 4860d75f, 2026-11-15/16. The laptop running the stack slept twice. The proof is
celery beat, an independent container on a fixed five-minute cron with `restarts=0`: it ticked
every five minutes to 19:46:34, then nothing until 20:26:42 (40 min), then every five minutes to
21:00:33, then nothing until 07:28:17 (10 h 28 min). A container that was never restarted cannot
miss a five-minute cron for ten hours unless its clock stopped.

The mesh itself was fine - it ran on a GCE VM, which does not care that a laptop is asleep, and it
FINISHED. What died was everything local. On wake:

  07:27:08  generation 3 is claimed
  07:27:10  generation 3 fails: "pipeline total budget exhausted before start"
  07:27:43  generation 2 returns WITH THE FINISHED MESH and is fenced at
            "builder driver accept native mesh output"

THREE THINGS THIS FILE PINS, each measured on that run.

1. THE BUDGET CANNOT FIT THE ENGINE, suspend or no suspend. `PIPELINE_TOTAL_TIMEOUT_SECONDS` is
   21,600 s. The permitted wait for ONE mesh is `timeout + 600 + VM_START_S` = 24,000 s on a VM.
   The whole job's budget is smaller than a single mesh's allowance, before CAD prep, extraction,
   the review surface or the boundary index. The database row shows `pipeline_deadline_at` set to
   `started_at + 06:00:00` exactly, to the microsecond, and never moved across 13 hours.

2. A SUSPEND MANUFACTURES FRESHNESS. `progress_clock.seconds_since` measures with
   `time.monotonic()` alone, and on Linux CLOCK_MONOTONIC does not advance while the system is
   suspended. So after a ten-hour sleep a run that last progressed ten hours ago looks like it
   progressed seconds ago, and `progress_extension` would hand it a deadline seventeen minutes in
   the future, earned by nothing.

3. RENEWAL LATCHES. `heartbeat` returns a bare False for four different reasons - the row is gone
   or terminal, the generation does not match, the token does not match, the deadline has passed -
   and both renewal paths treat all four as permanent supersession and stop for good. Only two of
   the four are permanent. The log said "a newer generation/token owns this job" at 20:26:45.474,
   0.6 s BEFORE generation 2 was claimed; it did not, the reaper had just cleared the token.
"""
from __future__ import annotations

import time

import pytest


# # 1. the budget has to fit the work it buys


def test_one_mesh_wait_fits_inside_the_whole_job_budget():
    """The incident in one assertion.

    If the longest a single mesh may be waited for exceeds the budget for the entire job, then a
    run that uses its mesher's own allowance is dead before it starts, and no amount of lease
    correctness saves it.
    """
    from meshpipeline.adapters.mesh_execution.cloud_run_client import VM_START_S
    from meshpipeline.settings import runtime as rtcfg

    # the conjugate spec's run_timeout is exactly this setting (spec.py: `run_timeout=lambda:
    # int(rtcfg.CONJUGATE_RUN_TIMEOUT_S)`), read from the setting so the test does not depend on
    # how the spec happens to expose it
    mesh_timeout = int(rtcfg.CONJUGATE_RUN_TIMEOUT_S)
    wait_s = mesh_timeout + 600 + VM_START_S          # cloud_run_client, the VM road
    budget = int(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS)
    assert wait_s <= budget, (
        f"one conjugate mesh may be waited for {wait_s:,} s but the whole job is budgeted "
        f"{budget:,} s, so the deadline expires while the mesher is still legitimately working")


def test_the_budget_stays_under_the_worker_task_limit():
    """And it must not grow past the task that is waiting to collect the result.

    A budget above the celery hard limit would kill the worker before the VM, which loses a
    finished mesh rather than saving one - the failure this engine's own driver documents.
    """
    from meshpipeline.settings import runtime as rtcfg

    budget = int(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS)
    assert budget < 86400, (
        f"the job budget {budget:,} s is at or past the worker task's soft limit, so the collector "
        "dies before the mesh does")


# # 2. a suspend must not look like progress


def test_a_suspended_host_cannot_manufacture_progress(monkeypatch):
    """Wall time jumps ten hours; the monotonic clock does not. The run is NOT fresh."""
    from meshpipeline.contracts import progress_clock

    fake_mono = [1000.0]
    fake_wall = [1_700_000_000.0]
    monkeypatch.setattr(progress_clock.time, "monotonic", lambda: fake_mono[0])
    monkeypatch.setattr(progress_clock.time, "time", lambda: fake_wall[0], raising=False)

    progress_clock.touch()
    assert (progress_clock.seconds_since() or 0.0) < 1.0, "a fresh touch reads as fresh"

    # the host sleeps for ten hours: CLOCK_MONOTONIC stands still, the wall clock does not
    fake_wall[0] += 10 * 3600
    age = progress_clock.seconds_since()
    assert age is not None and age >= 10 * 3600 - 5, (
        f"after a ten-hour suspend the last progress read as {age!r} s old, so the run looks "
        "like it was working seconds ago when it was frozen all night")


def test_no_deadline_extension_is_earned_by_sleeping(monkeypatch):
    from meshpipeline.application import pipeline_budget as pb
    from meshpipeline.contracts import progress_clock

    fake_mono = [1000.0]
    fake_wall = [1_700_000_000.0]
    monkeypatch.setattr(progress_clock.time, "monotonic", lambda: fake_mono[0])
    monkeypatch.setattr(progress_clock.time, "time", lambda: fake_wall[0], raising=False)

    progress_clock.touch()
    fake_wall[0] += 10 * 3600
    assert pb.progress_extension(now=fake_wall[0]) is None, (
        "a run that slept through the night was handed a deadline extension it had not earned")


def test_real_progress_still_extends(monkeypatch):
    """The negative control: this must not break the mechanism it is guarding."""
    from meshpipeline.application import pipeline_budget as pb
    from meshpipeline.contracts import progress_clock

    fake_mono = [1000.0]
    fake_wall = [1_700_000_000.0]
    monkeypatch.setattr(progress_clock.time, "monotonic", lambda: fake_mono[0])
    monkeypatch.setattr(progress_clock.time, "time", lambda: fake_wall[0], raising=False)

    progress_clock.touch()
    fake_mono[0] += 60.0       # a minute of real work
    fake_wall[0] += 60.0
    out = pb.progress_extension(now=fake_wall[0])
    assert out is not None, "a run that progressed a minute ago is not out of time"
    assert out == pytest.approx(fake_wall[0] - 60.0 + pb.PROGRESS_GRACE_S, abs=2.0)


# # 3. renewal must not latch on a refusal that can reverse


def test_the_refusal_reasons_are_told_apart():
    from meshpipeline.persistence import lease as L

    for name in ("RENEWED", "SUPERSEDED", "TERMINAL", "PAST_DEADLINE"):
        assert hasattr(L, name), (
            f"lease.{name} does not exist, so every refusal still looks the same to its callers")
    assert len({L.RENEWED, L.SUPERSEDED, L.TERMINAL, L.PAST_DEADLINE}) == 4


def test_only_an_irreversible_refusal_ends_renewal():
    """A deadline can move forward again on fresh progress; a rotated token never comes back."""
    from meshpipeline.persistence import lease as L

    assert L.ends_renewal(L.SUPERSEDED) is True
    assert L.ends_renewal(L.TERMINAL) is True
    assert L.ends_renewal(L.PAST_DEADLINE) is False, (
        "a deadline refusal ended renewal for the rest of the run, even though a later beat that "
        "sees fresh progress may legitimately move the deadline on and recover the lease")
    assert L.ends_renewal(L.RENEWED) is False


def test_the_beat_keeps_beating_past_the_deadline():
    """Pinned at the call site, because the latch was in the caller, not in the lease."""
    import inspect

    from meshpipeline.application import pipeline_run as PR

    src = inspect.getsource(PR)
    assert "ends_renewal" in src, (
        "the beat still returns on any falsy heartbeat, so one refusal at a moment of silence "
        "ends renewal for the rest of the run")


def test_the_keeper_keeps_watching_past_the_deadline():
    import inspect

    from meshpipeline.application import lease_keeper as LK

    src = inspect.getsource(LK)
    assert "ends_renewal" in src, (
        "the keeper subprocess still exits on the first refusal of any kind")


def test_heartbeat_still_answers_true_or_false():
    """The bool form is load-bearing for a dozen existing tests and both other callers."""
    import inspect

    from meshpipeline.persistence.lease import LeaseRepository

    sig = inspect.signature(LeaseRepository.heartbeat)
    assert sig.return_annotation in (bool, "bool"), (
        "heartbeat's bool contract changed; every existing caller asserts on it")
