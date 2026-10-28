# Responsibility: Verify the geometry check's live line - what the chat says at each step, the notes
# a slow step gets for the size of its file, the plain message and the way on when a step fails,
# that nothing is shown as still working once the check waits on the user, is done or failed -
# and that the worker records each step as it starts without ever failing the check for it.
# Boundaries: the pure view and the worker's record against a stood-in store; no CAD, no worker.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_progress as gp
from meshpipeline.contracts import object_storage

T0 = 1_000_000.0
LIMITS = {"scout_limit_s": gc.SCOUT_SOFT_LIMIT_S, "naming_limit_s": gc.NAMING_SOFT_LIMIT_S}


def _view(stored, requested=None, progress=None, now=T0):
    return gp.progress_view(stored, requested, progress, now, **LIMITS)


def _pending(at=T0):
    return {"status": "pending", "written_at": at}


def _scout_record(current, steps, *, marker=T0, picked=T0 + 2, size_bytes=1_000_000, suffix=".step", **extra):
    return {"scout": {"marker": marker, "picked_at": picked, "mode": "generic", "current": current,
                      "work": {"size_bytes": size_bytes, "suffix": suffix}, "steps": steps, **extra}}


def _states(view):
    return {s["key"]: s["state"] for s in view["steps"]}


# ------------------------------------------------------------------------- before a worker ----
def test_a_check_no_worker_has_picked_up_says_it_is_starting_then_waiting_then_why():
    first = _view(_pending(), now=T0 + 3)
    assert first["state"] == "queued" and first["live"] is True and first["current"] == "Starting…"
    later = _view(_pending(), now=T0 + 15)
    assert later["current"] == "Waiting to start…" and later["note"] == ""
    long = _view(_pending(), now=T0 + 90)
    assert "No worker is free yet" in long["note"] and long["elapsed_s"] == 90.0


def test_a_record_left_by_an_earlier_attempt_is_not_read_as_this_one():
    old = _scout_record("measuring", {"reading": {"at": T0 - 500, "end": T0 - 490},
                                      "measuring": {"at": T0 - 490}}, marker=T0 - 600, picked=T0 - 500)
    view = _view(_pending(), progress=old, now=T0 + 3)
    assert view["state"] == "queued" and view["current"] == "Starting…"


# -------------------------------------------------------------------------------- running ----
def test_the_step_running_now_is_named_and_the_steps_before_it_are_ticked_with_their_times():
    rec = _scout_record("measuring", {"reading": {"at": T0 + 2, "end": T0 + 6}, "measuring": {"at": T0 + 6}})
    view = _view(_pending(), progress=rec, now=T0 + 10)
    assert view["state"] == "running" and view["live"] is True
    assert view["current"] == "Measuring the shape…" and view["elapsed_s"] == 4.0
    assert _states(view) == {"queued": "done", "reading": "done", "measuring": "active", "openings": "todo",
                             "pictures": "todo", "naming": "todo"}
    by = {s["key"]: s for s in view["steps"]}
    assert by["reading"]["seconds"] == 4.0 and by["reading"]["label"] == "Read your CAD"
    assert by["queued"]["seconds"] == 2.0                      # how long it waited for a worker
    assert view["note"] == ""


def test_a_big_file_that_reads_slowly_says_so_with_its_size():
    rec = _scout_record("reading", {"reading": {"at": T0 + 2}}, size_bytes=180_000_000)
    limit = gp.soft_limit_s("reading", {"size_bytes": 180_000_000})
    assert limit > 300                                          # scaled by the work, not one number
    quick = _view(_pending(), progress=rec, now=T0 + 2 + limit - 1)
    assert quick["note"] == ""
    slow = _view(_pending(), progress=rec, now=T0 + 2 + limit + 1)
    assert slow["note"] == "Still reading - big file (180 MB), this can take a few minutes"
    assert slow["live"] is True


def test_a_step_far_past_its_limit_says_when_it_stops_by_itself():
    rec = _scout_record("pictures", {"reading": {"at": T0 + 2, "end": T0 + 3}, "measuring": {"at": T0 + 3, "end": T0 + 4},
                                     "openings": {"at": T0 + 4, "end": T0 + 5}, "pictures": {"at": T0 + 5}},
                        triangles=0)
    limit = gp.soft_limit_s("pictures", {})
    view = _view(_pending(), progress=rec, now=T0 + 5 + 3 * limit + 1)
    assert view["slow"] == "very" and "taking much longer than usual" in view["note"]
    assert "will stop by itself within" in view["note"] and "carry on in the chat" in view["note"]


# ----------------------------------------------------------------------- waiting and done ----
def _scouted(**extra):
    return {"status": "scouted", "written_at": T0 + 30, "seconds": 20.0, "source": {"suffix_hint": ".step"}, **extra}


DONE_STEPS = {"reading": {"at": T0 + 2, "end": T0 + 6}, "measuring": {"at": T0 + 6, "end": T0 + 12},
              "openings": {"at": T0 + 12, "end": T0 + 14}, "pictures": {"at": T0 + 14, "end": T0 + 20}}


def test_a_drawn_part_waiting_on_the_users_answer_does_not_spin():
    view = _view(_scouted(), progress=_scout_record("pictures", DONE_STEPS, ok=True), now=T0 + 100)
    assert view["state"] == "waiting" and view["live"] is False
    assert view["current"].startswith("Picture ready")
    assert _states(view)["naming"] == "waiting" and _states(view)["pictures"] == "done"


def test_a_naming_no_worker_has_picked_up_says_it_is_waiting_to_start():
    asked = {"requested_at": T0 + 200, "purpose_text": "a pipe"}
    view = _view(_scouted(), asked, _scout_record("pictures", DONE_STEPS), now=T0 + 215)
    assert view["state"] == "running" and view["current"] == "Waiting to start naming…"


def test_the_naming_running_is_the_current_step_and_a_slow_model_is_said():
    asked = {"requested_at": T0 + 200}
    rec = _scout_record("pictures", DONE_STEPS)
    rec["naming"] = {"picked_at": T0 + 201, "current": "naming", "steps": {"naming": {"at": T0 + 202}}}
    view = _view(_scouted(), asked, rec, now=T0 + 210)
    assert view["current"] == "Naming what it sees…" and view["elapsed_s"] == 8.0 and view["live"] is True
    slow = _view(_scouted(), asked, rec, now=T0 + 202 + gp.soft_limit_s("naming", {}) + 1)
    assert slow["note"] == "Still naming - the model is slow right now"


def test_a_ready_check_collapses_to_one_line_with_the_time_the_work_took():
    asked = {"requested_at": T0 + 200}
    rec = _scout_record("pictures", DONE_STEPS)
    rec["naming"] = {"picked_at": T0 + 201, "current": "naming", "steps": {"naming": {"at": T0 + 202, "end": T0 + 208}}}
    view = _view({"status": "ready", "written_at": T0 + 208, "seconds": 18.0, "naming_seconds": 7.0}, asked, rec,
                 now=T0 + 400)
    # 4 + 6 + 2 + 6 for the scout and 6 for the naming: the user's own time over the answer is not counted
    assert view["state"] == "done" and view["live"] is False and view["current"] == "Checked in 24 s"
    assert set(_states(view).values()) == {"done"}


def test_a_check_stored_before_the_steps_were_recorded_still_reads_its_time():
    view = _view({"status": "ready", "written_at": T0, "seconds": 9.0, "naming_seconds": 5.0}, {"requested_at": T0})
    assert view["state"] == "done" and view["current"] == "Checked in 14 s"


# ----------------------------------------------------------------------------- failures ----
def test_a_scout_that_failed_says_where_in_plain_words_and_what_to_do_for_an_iges_file():
    rec = _scout_record("measuring", {"reading": {"at": T0 + 2, "end": T0 + 6}, "measuring": {"at": T0 + 6}},
                        suffix=".igs")
    stored = {"status": "failed", "written_at": T0 + 30, "step": "scout", "retry": "scout",
              "reason": "RuntimeError: the file has no solids to read"}
    view = _view(stored, progress=rec, now=T0 + 40)
    assert view["state"] == "failed" and view["live"] is False
    assert view["current"] == "Couldn't finish measuring the shape."
    assert view["reason"] == "The file has no solids to read."           # no exception class in front
    assert "export the part as STEP" in view["advice"] and view["can_retry"] is True
    assert _states(view)["reading"] == "done" and _states(view)["measuring"] == "failed"


def test_a_scout_no_worker_ever_picked_up_reads_as_such_once_the_time_box_fails_it():
    stored = gc.time_boxed(_pending(T0), None, now=T0 + gc.SCOUT_HARD_LIMIT_S + gc.STALE_MARGIN_S + 5)
    view = _view(stored, now=T0 + gc.SCOUT_HARD_LIMIT_S + gc.STALE_MARGIN_S + 5)
    assert view["state"] == "failed" and view["current"] == "No worker picked it up in time."
    assert view["can_retry"] is True and view["advice"].startswith("Try again")


def test_a_naming_that_failed_keeps_the_picture_and_says_to_name_it_by_hand():
    stored = {"status": "failed", "written_at": T0 + 300, "step": "naming", "retry": "naming",
              "reason": "the naming step did not answer", "timed_out": True, "seconds": 20.0}
    view = _view(stored, {"requested_at": T0 + 200}, _scout_record("pictures", DONE_STEPS), now=T0 + 400)
    assert view["current"] == "Naming what it sees took too long." and view["reason"] == ""
    assert "name the openings on it yourself" in view["advice"]
    assert _states(view)["pictures"] == "done" and _states(view)["naming"] == "failed"


def test_an_unsupported_file_is_told_to_carry_on_in_the_chat():
    view = _view({"status": "unsupported", "reason": "this upload is a .dwg file"})
    assert view["state"] == "unsupported" and view["live"] is False and "carry on in the chat" in view["advice"].lower()


def test_a_thermal_model_is_read_in_its_own_words_and_has_no_openings_step():
    rec = _scout_record("measuring", {"reading": {"at": T0 + 2, "end": T0 + 3}, "measuring": {"at": T0 + 3}},
                        suffix=".ecxml")
    rec["scout"]["mode"] = "thermal"
    view = _view(_pending(), progress=rec, now=T0 + 5)
    assert view["current"] == "Sizing the grid…" and "openings" not in _states(view)


def test_nothing_is_live_once_the_check_waits_on_the_user_is_done_or_failed():
    for stored in (_scouted(), {"status": "ready", "written_at": T0}, {"status": "failed", "written_at": T0, "retry": "scout"}):
        assert _view(stored, {"requested_at": T0} if stored["status"] == "ready" else None)["live"] is False
    assert _view(None) is None


# ---------------------------------------------------------------------- the worker's record ----
class _Store:
    def __init__(self, fail=False):
        self.objects: dict[str, bytes] = {}
        self.fail, self.calls = fail, 0

    def upload_file(self, *, local_path, object_key, **_):
        self.calls += 1
        if self.fail:
            raise OSError("the store is down")
        self.objects[object_key] = Path(local_path).read_bytes()

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]


def test_the_tracker_records_each_step_as_it_starts_and_ends_the_one_before(monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    t = gp.Tracker("s1", "scout")
    t.begin(marker=5.0, mode="generic", work={"size_bytes": 10, "suffix": ".stl"})
    token = gp.track(t)
    try:
        gp.mark(gp.READING)
        gp.mark(gp.MEASURING, triangles=1200)
    finally:
        gp.untrack(token)
    t.finish(ok=True)
    rec = json.loads(store.objects[gp.progress_key("s1")])["scout"]
    assert rec["marker"] == 5.0 and rec["current"] == "measuring" and rec["ok"] is True
    assert rec["work"] == {"size_bytes": 10, "suffix": ".stl", "triangles": 1200}
    assert rec["steps"]["reading"]["end"] == rec["steps"]["measuring"]["at"]
    gp.mark(gp.OPENINGS)                     # nothing tracked here any more: a no-op
    assert "openings" not in json.loads(store.objects[gp.progress_key("s1")])["scout"]["steps"]


def test_a_naming_record_keeps_the_scouts_steps_beside_it(monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    gp.Tracker("s1", "scout").begin(marker=1.0)
    gp.Tracker("s1", "naming").begin()
    rec = json.loads(store.objects[gp.progress_key("s1")])
    assert set(rec) == {"scout", "naming"}


def test_a_store_that_refuses_the_record_never_fails_the_task_and_is_soon_left_alone(monkeypatch):
    store = _Store(fail=True)
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    t = gp.Tracker("s1", "scout")
    t.begin()
    for key in (gp.READING, gp.MEASURING, gp.OPENINGS, gp.PICTURES):
        t.step(key)
    t.finish(ok=True)
    assert store.calls == 3


def test_the_scout_records_its_steps_in_order(tmp_path, monkeypatch):
    import tempfile

    import meshpipeline.cad.scout as scout_mod
    import meshpipeline.render.scout_snapshots as snaps_mod
    from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary

    store = _Store()
    store.objects[gc.check_object_key("abcdef12-2222", "scout.json")] = json.dumps(
        {"status": "pending", "written_at": 42.0}).encode()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(gc, "_fetch", lambda ref, work: ((work / "geometry.step").write_text("step"), work / "geometry.step")[1])
    monkeypatch.setattr(gc, "_prepared_coordinates", lambda path, interp_ref, ref: (None, ""))
    monkeypatch.setattr(scout_mod, "read_cad", lambda path: None)

    class _Scout:
        def as_dict(self):
            return {"input_kind": "body-surface", "flow": "internal", "size_mm": [1, 1, 1], "openings": [],
                    "notes": [], "confidence": {}}
    monkeypatch.setattr(scout_mod, "scout_cad", lambda path, *, prepared, shape=None: _Scout())

    def _skin(path, dest, *, prepared, shape=None):
        write_stl_binary(Path(dest), _box_triangles((0, 0, 0), (1, 1, 1)))
        return Path(dest)
    monkeypatch.setattr(scout_mod, "write_view_stl", _skin)
    monkeypatch.setattr(snaps_mod, "render_snapshots", lambda skin, openings, out: [])
    monkeypatch.setattr(snaps_mod, "render_upright_sheet",
                        lambda skin, out, **k: (_ for _ in ()).throw(RuntimeError("nothing is drawn in this test")))
    source = {"source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/elbow.step", "sha256": "0" * 64,
              "size_bytes": 4, "original_filename": "elbow.step", "suffix_hint": ".step"}
    result = gc.run_geometry_check(session_id="abcdef12-2222", owner_id="o1", source=source)
    assert result["status"] == "scouted", result
    rec = json.loads(store.objects[gp.progress_key("abcdef12-2222")])["scout"]
    assert rec["marker"] == 42.0 and rec["ok"] is True and rec["work"]["size_bytes"] == 4
    assert list(rec["steps"]) == ["reading", "measuring", "openings", "pictures"]
    assert rec["work"]["triangles"] == 12
    assert all("end" in s for s in rec["steps"].values())


def test_a_scout_stopped_by_the_workers_soft_limit_is_stored_as_too_long(monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)

    class SoftTimeLimitExceeded(Exception):
        pass

    def _slow(**_):
        gp.mark(gp.READING)
        raise SoftTimeLimitExceeded()
    monkeypatch.setattr(gc, "_scout", _slow)
    result = gc.run_geometry_check(session_id="s9", owner_id="o1", source={"suffix_hint": ".step"})
    assert result["status"] == "failed" and result["reason"] == gc.SCOUT_TOO_LONG and result["timed_out"] is True
    rec = json.loads(store.objects[gp.progress_key("s9")])["scout"]
    assert rec["current"] == "reading" and rec["ok"] is False


@pytest.mark.parametrize("seconds, said", [(0.4, "0 s"), (14, "14 s"), (125, "2 min 5 s"), (720, "12 min")])
def test_durations_are_said_in_plain_units(seconds, said):
    assert gp.duration_words(seconds) == said


def test_a_check_the_user_confirmed_before_the_naming_landed_is_done_on_the_measuring_steps_names():
    asked = {"requested_at": T0 + 200}
    rec = _scout_record("pictures", DONE_STEPS)
    rec["naming"] = {"picked_at": T0 + 201, "current": "naming", "steps": {"naming": {"at": T0 + 202}}}
    view = gp.progress_view(_scouted(), asked, rec, T0 + 230, confirmed=True, **LIMITS)
    assert view["state"] == "done" and view["live"] is False and view["current"] == "Checked in 18 s"
    assert _states(view)["naming"] == "skipped"


# ------------------------------------------------------------------------------ FEA parts ----
# An FEA part has no openings and nothing to name: its steps are said in its own words (Areen's F1
# car showed "Found the openings" on its FEA check, 21 Oct) - no CFD words on the FEA path.
CFD_WORDS = ("opening", "name what", "naming", "named", "flow", "sticker")


def _no_cfd_words(view):
    said = " ".join([view.get("current") or "", view.get("note") or "", view.get("advice") or ""]
                    + [s["label"] + " " + s["doing"] for s in view["steps"]]).lower()
    return [w for w in CFD_WORDS if w in said]


def test_an_fea_part_done_reads_its_own_step_names():
    asked = {"requested_at": T0 + 200, "purpose_text": "static stress analysis"}
    rec = _scout_record("pictures", DONE_STEPS)
    rec["naming"] = {"picked_at": T0 + 201, "current": "naming", "steps": {"naming": {"at": T0 + 202, "end": T0 + 208}}}
    view = _view({"status": "ready", "written_at": T0 + 208, "seconds": 18.0}, asked, rec, now=T0 + 400)
    labels = {s["key"]: s["label"] for s in view["steps"]}
    assert labels["openings"] == "Read the surface" and labels["naming"] == "Found the walls and hot spots"
    assert view["fea"] is True and _no_cfd_words(view) == []


def test_the_route_the_api_settled_speaks_first_even_while_the_scout_runs():
    rec = _scout_record("openings", {"reading": {"at": T0 + 2, "end": T0 + 6},
                                     "measuring": {"at": T0 + 6, "end": T0 + 12}, "openings": {"at": T0 + 12}})
    view = gp.progress_view(_pending(), None, rec, T0 + 14, fea=True, **LIMITS)
    assert view["current"] == "Reading the surface…" and _no_cfd_words(view) == []
    slow = gp.progress_view(_pending(), None, rec, T0 + 12 + gp.soft_limit_s("openings", rec["scout"]["work"]) + 1,
                            fea=True, **LIMITS)
    assert slow["note"] == "Still reading - a part with many faces takes longer"
    # the same check on the flow path keeps the flow words
    flow = gp.progress_view(_pending(), None, rec, T0 + 14, fea=False, **LIMITS)
    assert flow["current"] == "Finding the openings…"


def test_an_fea_measurement_running_long_and_failing_is_said_in_fea_words():
    asked = {"requested_at": T0 + 200, "purpose_text": "I need FEA of this bracket"}
    rec = _scout_record("pictures", DONE_STEPS)
    rec["naming"] = {"picked_at": T0 + 201, "current": "naming", "steps": {"naming": {"at": T0 + 202}}}
    running = _view(_scouted(), asked, rec, now=T0 + 210)
    assert running["current"] == "Measuring the walls and hot spots…"
    slow = _view(_scouted(), asked, rec, now=T0 + 202 + gp.soft_limit_s("naming", {}) + 1)
    assert slow["note"] == "Still measuring - a part with many faces takes longer" and _no_cfd_words(slow) == []
    stored = {"status": "failed", "written_at": T0 + 300, "step": "naming", "retry": "naming",
              "reason": "the naming step did not answer", "timed_out": True, "seconds": 20.0}
    failed = _view(stored, asked, rec, now=T0 + 400)
    assert failed["current"] == "Measuring the walls and hot spots took too long."
    assert failed["advice"].startswith("The part is drawn: check its size and unit") and _no_cfd_words(failed) == []


def test_a_stored_fea_measurement_marks_the_check_as_fea_without_the_words():
    from meshpipeline.cad.fea_measure import FACT_KEY

    assert gp.is_fea({"facts": {FACT_KEY: {"measured": True}}}, None) is True
    assert gp.is_fea({"facts": {}}, {"purpose_text": "external aerodynamics of a car"}) is False
    assert gp.is_fea({}, None) is False
