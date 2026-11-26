# Responsibility: Verify the naming step waits on the SCOUT - its heartbeat and its own deadline -
# and never on a clock of its own, and that the naming's limits are derived so the wait fits inside
# them.
# Boundaries: application/geometry_check.step_limits, scout_deadline and _wait_for_scout, with the
# store and the progress record stood in for; no worker, no queue.
"""THE NAMING LOST A RACE IT COULD NOT WIN, BY ARITHMETIC.

`_wait_for_scout` waited `NAMING_SOFT_LIMIT_S - 60` seconds - 540 + 30/MB - for a scout whose own
allowance was `SCOUT_SOFT_LIMIT_S` - 900 + 60/MB. Two numbers picked beside each other, so the
naming gave up on every file whose scout needed more than 58% of its own allowance, by a margin
that GROWS with the file (360 + 30/MB seconds). It then named the PREVIEW, whose facts carry
`openings: []` and `confidence.input_kind: 0.0` by construction, and published that as ready.

MEASURED on the first customer assembly, ColdPlateAssembly_CLEAN.step, 15,883,539 bytes, 6 solids:

    the naming waited           1,016 s   (its own soft limit 1,076 less a minute)
    the scout was allowed       1,853 s   (soft; 2,153 hard)
    the full reading took       1,042.5 s on the customer's worker, session 42043234 (2026-11-26)
                                  733.0 s in process on a developer laptop (2026-11-26)

So the budget sat BETWEEN the two measurements of the same file, and which side of it a run landed
on was decided by the machine that ran it - with nothing in the stored record to say which. And
winning the wait by a second would not have saved it: the wait took all but 60 s of the naming's
own limit, so the vision call it then had to make had a minute before the worker killed it.

WHAT ENDS THE WAIT NOW is what ends the scout: its heartbeat stopping, or its own deadline passing
(`scout_deadline`) - the same two tests `time_boxed` applies, so the naming and the console cannot
disagree about whether the part is still being measured. The naming's own limit is derived from the
scout's, so it can see that out.
"""
from __future__ import annotations

import pytest

from meshpipeline.application import geometry_check as GC
from meshpipeline.application import geometry_progress as gp
from meshpipeline.engines import determinism as D

T0 = 2_000_000.0


def _preview(**over) -> dict:
    return {"status": GC.STATUS_SCOUTED, "preview": GC.PREVIEW_RUNNING, "named": False,
            "soft_limit_s": 1853, "hard_limit_s": 2153, "size_bytes": 15_883_539,
            "facts": {"openings": [], "confidence": {"input_kind": 0.0}, "preview": True,
                      "scale_to_m": 0.001},
            **over}


def _full() -> dict:
    return {"status": GC.STATUS_SCOUTED, "named": False,
            "facts": {"openings": [{"id": 1}, {"id": 2}, {"id": 3}], "confidence": {"input_kind": 0.85}}}


@pytest.fixture
def notes(monkeypatch):
    """Every note_time_limit the call under test writes."""
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(D, "note_time_limit", lambda where, decision, **f: seen.append((where, decision, f)))
    monkeypatch.setattr("meshpipeline.engines.determinism.note_time_limit",
                        lambda where, decision, **f: seen.append((where, decision, f)))
    return seen


@pytest.fixture
def waiting(monkeypatch):
    """`_wait_for_scout` driven over a stored record and a progress record we choose, with no
    sleeping and no store. Returns a setter and the count of looks the wait took."""
    looks = {"n": 0}
    state: dict = {"stored": None, "progress": None}

    def read_check(_sid):
        looks["n"] += 1
        value = state["stored"]
        return value(looks["n"]) if callable(value) else value
    monkeypatch.setattr(GC, "read_check", read_check)
    monkeypatch.setattr(gp, "read_progress", lambda _sid: state["progress"])
    monkeypatch.setattr(GC.time, "sleep", lambda _s: None)

    def drive(stored, progress=None, naming_limits=(3349, 3649)):
        state.update(stored=stored, progress=progress)
        monkeypatch.setattr(GC, "naming_limits_for", lambda _sid: naming_limits)
        return GC._wait_for_scout("s-1")
    return drive, looks


# ------------------------------------------------------------------------- the arithmetic ----
def test_the_naming_is_allowed_longer_than_the_scout_it_waits_for():
    """The whole fix, as one inequality, for every size the upload accepts (500 MiB)."""
    for mb in (0, 0.2, 1, 15.883539, 45.857044, 100, 215):
        size = mb * 1e6
        over = GC.step_limits(GC.RETRY_SCOUT, size)[1] + GC.STALE_MARGIN_S
        allows = GC.step_limits(GC.RETRY_NAMING, size)[0] - GC.NAMING_LANDING_S
        assert allows >= over, f"{mb} MB: the naming may wait {allows} s for a scout over at {over} s"


def test_the_clock_it_used_before_could_not_win():
    """Named so it is never put back. The old wait was NAMING_SOFT_LIMIT_S - 60 against the scout's
    own soft limit, and lost at every size - by more, the bigger the file."""
    for mb in (0, 15.883539, 45.857044, 215):
        old_wait = (600 + 30.0 * mb) - 60
        scout_may_take = 900 + 60.0 * mb
        assert old_wait < scout_may_take
        assert scout_may_take - old_wait == pytest.approx(360 + 30.0 * mb)


def test_the_naming_keeps_its_own_work_back_whatever_the_scout_takes():
    """Winning the wait is no use without time to name. The limit it is given past the scout's is
    exactly the naming's own work for that file."""
    for mb in (0, 15.883539, 45.857044):
        size = mb * 1e6
        over = GC.step_limits(GC.RETRY_SCOUT, size)[1] + GC.STALE_MARGIN_S
        left = GC.step_limits(GC.RETRY_NAMING, size)[0] - over
        assert left == pytest.approx(GC.NAMING_WORK_S + GC.NAMING_WORK_PER_MB * mb, abs=1.0)


# ------------------------------------------------------------------- when the scout is over ----
def test_the_scouts_deadline_is_measured_from_when_it_was_queued():
    """A PREVIEW RESTAMPS `written_at`, and the deadline used to be read off that - so a scout that
    spent four minutes drawing the part was handed four extra minutes to finish, which it does not
    have. `queued_at` is the one point both the naming and the read path measure from."""
    queued = _preview(queued_at=T0, written_at=T0 + 240.0)
    assert GC.scout_deadline(queued) == T0 + 2153 + GC.STALE_MARGIN_S
    # still being measured right up to it, the quick drawing is the answer after it
    assert GC.time_boxed(queued, None, now=T0 + 2153) is queued
    over = GC.time_boxed(queued, None, now=T0 + 2153 + GC.STALE_MARGIN_S + 1)
    assert over["preview"] == GC.PREVIEW_KEPT and over["preview_reason"] == GC.SCOUT_TOO_LONG
    # the reading off `written_at` alone still called it running, a full four minutes later
    assert T0 + 240.0 + 2153 + GC.STALE_MARGIN_S > T0 + 2153 + GC.STALE_MARGIN_S + 1
    # a record from before `queued_at` falls back to `written_at`, as the read path always did
    assert GC.scout_deadline({"written_at": T0}) == T0 + GC.SCOUT_HARD_LIMIT_S + GC.STALE_MARGIN_S
    assert GC.scout_deadline(None) == GC.STALE_MARGIN_S + GC.SCOUT_HARD_LIMIT_S
    assert GC.scout_deadline({"written_at": "not a time"}) == 0.0


def test_the_queue_time_is_written_on_the_pending_marker(monkeypatch):
    """Where `scout_deadline` reads it from. (That the PREVIEW carries it on is
    test_geometry_check_scales.test_the_preview_carries_the_queue_time_it_was_marked_with.)"""
    import json

    from meshpipeline.contracts import geometry_check as seam
    from meshpipeline.contracts import object_storage

    written: dict[str, dict] = {}

    class _Store:
        def upload_file(self, *, local_path, object_key, **_):
            written[object_key] = json.loads(local_path.read_bytes())

        def get_bytes(self, *, object_key):
            if object_key not in written:
                raise object_storage.ObjectNotFound(object_key)
            return json.dumps(written[object_key]).encode()
    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    seam.set_scout_enqueuer(lambda **kw: None)
    try:
        assert GC.start_scout("q-1", "o1", source={"size_bytes": 15_883_539, "suffix_hint": ".step"},
                              interpretation=None)
    finally:
        seam.set_scout_enqueuer(None)
    marker = GC.read_check("q-1")
    assert marker["queued_at"] == pytest.approx(marker["written_at"], abs=1.0)
    assert (marker["soft_limit_s"], marker["hard_limit_s"]) == GC.step_limits(GC.RETRY_SCOUT, 15_883_539)
    assert GC.scout_deadline(marker) == pytest.approx(
        marker["queued_at"] + GC.step_limits(GC.RETRY_SCOUT, 15_883_539)[1] + GC.STALE_MARGIN_S)


# ------------------------------------------------------------------------------- the wait ----
def test_the_wait_lasts_as_long_as_the_scout_may_still_take(waiting, notes):
    """The reading lands at the 1,042 s this file needs, well past the 1,016 s clock that used to
    end the wait, and the naming has it."""
    drive, looks = waiting
    got = drive(lambda n: _preview(queued_at=GC.time.time()) if n < 4 else _full())
    assert got["facts"]["openings"] == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert looks["n"] == 4 and not notes, "a reading that arrived was recorded as a time limit"


def test_the_wait_ends_the_moment_the_scouts_heartbeat_stops(waiting, notes):
    """A scout that was killed, or whose VM went away, will never land anything - and the old clock
    sat there waiting for it for the rest of its budget."""
    drive, looks = waiting
    now = GC.time.time()
    dead = {"scout": {"picked_at": now - 600, "beat_at": now - 10 * gp.BEAT_STALE_S, "steps": {}}}
    got = drive(_preview(queued_at=now), progress=dead)
    assert GC.is_preview(got), "the preview is still what there is to name"
    assert looks["n"] == 1, "it waited for a worker that had stopped"
    where, decision, facts = notes[-1]
    assert where == "geometry_check.naming" and "preview" in decision.lower()
    assert facts["ended_by"] == "the scout stopped beating"


def test_the_wait_ends_when_the_scout_runs_out_of_its_own_time(waiting, notes):
    drive, looks = waiting
    got = drive(_preview(queued_at=GC.time.time() - 5000))
    assert GC.is_preview(got) and looks["n"] == 1
    where, decision, facts = notes[-1]
    assert "named the preview" in decision and facts["ended_by"] == "the scout's own limit"
    # the scout was already over when the naming first looked: there was no wait to be had
    assert facts["budget_s"] == 0.0 and facts["waited_s"] == 0.0


def test_a_scout_that_never_started_is_not_waited_out(waiting, notes):
    """Nothing stored at all: there is no scout deadline to read, so the naming's own limit is what
    bounds it - and the request is withdrawn so the next chat turn can ask again (`_name`)."""
    drive, looks = waiting
    assert drive(None, naming_limits=(0.0, 0.0)) is None
    assert looks["n"] == 1
    where, decision, facts = notes[-1]
    assert decision == "the naming stopped waiting for the scout"
    assert facts["ended_by"] == "the naming's own limit"


def test_an_earlier_attempts_heartbeat_does_not_end_this_wait(waiting, notes):
    """A retried scout that no worker has picked up yet: the progress record on the store is the
    previous attempt's, and it says nothing about this one (`geometry_progress._this_attempt`)."""
    drive, looks = waiting
    now = GC.time.time()
    old = {"scout": {"marker": now - 9000, "picked_at": now - 9000, "ended_at": now - 8000, "steps": {}}}
    pending = {"status": GC.STATUS_PENDING, "written_at": now, "queued_at": now,
               "soft_limit_s": 1853, "hard_limit_s": 2153}
    got = drive(lambda n: pending if n < 3 else _full(), progress=old)
    assert got["facts"]["confidence"]["input_kind"] == 0.85
    assert looks["n"] == 3 and not notes
