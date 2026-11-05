# Responsibility: Hold the geometry check's speed-ups to giving the SAME answer: the same bytes checked twice are
# answered from the first check (per owner), the work begun beside the scout or before the naming is used only
# when it is of the very same input, the Boolean check runs once per solid, and the waiting line says where
# the check stands instead of "No worker is free yet".
# Boundaries: an in-memory object store and stubbed geometry; nothing is read from CAD here (the corpus proof is
# the lab's: tests/unit/cad/test_contacts_not_corners.py holds the measurement rules).
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.application import geometry_progress as gp
from meshpipeline.application import geometry_reuse as reuse
from meshpipeline.contracts.object_storage import ObjectNotFound

# THE DEFECTS THIS PINS. Shared dev, 1 Nov: a scout took 186-201 s and the FEA naming 422-596 s, so a user waited
# ten minutes for the walls and hot spots; the same landing gear was checked twice back to back (identical
# bytes, identical 279.95 x 689.75 x 380 mm box); and the line said "No worker is free yet" while its check
# was running, or queued behind two others.


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()

    def download_file(self, *, object_key, destination):
        if object_key not in self.objects:
            raise ObjectNotFound(object_key)
        Path(destination).write_bytes(self.objects[object_key])

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise ObjectNotFound(object_key)
        return self.objects[object_key]

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)


@pytest.fixture
def store(monkeypatch):
    from meshpipeline.contracts import object_storage

    s = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: s)
    return s


SHA = "ab" * 32


def _source(sid="src-1"):
    return {"source_id": sid, "owner_id": "o1", "object_key": f"sources/{sid}", "sha256": SHA, "size_bytes": 10,
            "original_filename": "gear.igs", "suffix_hint": ".igs"}


def _scouted(session: str) -> dict:
    base = f"sessions/{session}/geometry_check/"
    return {"status": "scouted", "named": False, "facts": {"size_mm": [279.95, 689.75, 380.0], "validity": {}},
            "proposal": {"openings": []}, "skin_key": base + "skin.json",
            "snapshots": [{"name": "iso", "object_key": base + "iso.png"}], "source": _source()}


# ------------------------------------------------------------------------ the same bytes, again ----
def test_the_same_bytes_from_the_same_owner_are_answered_from_the_first_check(store):
    first = _scouted("s-first")
    store.objects["sessions/s-first/geometry_check/skin.json"] = b"skin"
    store.objects["sessions/s-first/geometry_check/iso.png"] = b"png"
    reuse.remember_scout("s-first", "o1", first, None)
    again = reuse.reuse_scout("s-again", "o1", _source("src-2"), None)
    assert again is not None
    # the same reading, its objects re-stored under the new session (the first may be deleted later)
    assert again["facts"] == first["facts"] and again["proposal"] == first["proposal"]
    assert again["skin_key"] == "sessions/s-again/geometry_check/skin.json"
    assert store.objects["sessions/s-again/geometry_check/iso.png"] == b"png"
    assert again["source"]["source_id"] == "src-2" and again["reused_from"] == {"session_id": "s-first"}


def test_another_owner_or_another_unit_is_never_answered_from_it(store):
    store.objects["sessions/s-first/geometry_check/skin.json"] = b"skin"
    store.objects["sessions/s-first/geometry_check/iso.png"] = b"png"
    reuse.remember_scout("s-first", "o1", _scouted("s-first"), None)
    assert reuse.reuse_scout("s-x", "someone-else", _source(), None) is None
    inches = {"unit": "inch", "scale_to_metres": 0.0254}
    assert reuse.reuse_scout("s-x", "o1", _source(), inches) is None


def test_a_first_check_whose_objects_are_gone_is_a_miss(store):
    reuse.remember_scout("s-first", "o1", _scouted("s-first"), None)      # its skin was never stored
    assert reuse.reuse_scout("s-again", "o1", _source(), None) is None


def test_a_preview_or_a_failed_check_is_never_remembered(store):
    reuse.remember_scout("s-first", "o1", {**_scouted("s-first"), "preview": "kept"}, None)
    reuse.remember_scout("s-other", "o1", {**_scouted("s-other"), "status": "failed"}, None)
    assert not [k for k in store.objects if k.startswith("geometry_index/")]


def test_an_fea_measurement_is_reused_only_under_the_same_unit_and_clean_verdict(store):
    record = {"measured": True, "hot_spots": [], "bodies": 1}
    key = reuse.fea_key(SHA, None, 0.001, {"reasons": []})
    reuse.remember_fea("o1", SHA, key, record, session_id="s-first")
    assert reuse.known_fea("o1", SHA, key) == record
    assert reuse.known_fea("o1", SHA, reuse.fea_key(SHA, None, 0.0254, {"reasons": []})) is None
    assert reuse.known_fea("o1", SHA, reuse.fea_key(SHA, None, 0.001, {"reasons": [{"code": "x"}]})) is None
    assert reuse.known_fea("o2", SHA, key) is None


def test_start_scout_answers_a_reupload_at_once_and_queues_nothing(store, monkeypatch):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.contracts import geometry_check as contract

    store.objects["sessions/s-first/geometry_check/skin.json"] = b"skin"
    store.objects["sessions/s-first/geometry_check/iso.png"] = b"png"
    reuse.remember_scout("s-first", "o1", _scouted("s-first"), None)
    queued: list = []
    monkeypatch.setattr(contract, "_enqueuer", lambda **k: queued.append(k))
    assert gc.start_scout("s-again", "o1", source=_source("src-2"), interpretation=None) is True
    assert queued == []
    stored = json.loads(store.objects["sessions/s-again/geometry_check/scout.json"])
    assert stored["status"] == "scouted" and stored["facts"]["size_mm"] == [279.95, 689.75, 380.0]


# ------------------------------------------------------------------ work begun beside the scout ----
class _Done:
    def __init__(self, answer):
        self.answer, self.proc, self.closed = answer, object(), False

    def result(self):
        return self.answer

    def close(self):
        self.closed = True


def test_the_clean_check_begun_beside_the_scout_is_used_when_it_answered():
    from meshpipeline.application import geometry_check as gc

    verdict = {"clean": False, "kind": "brep", "reasons": [{"code": "open_shell"}], "summary": "not clean"}
    assert gc.clean_verdict(None, None, started=_Done(verdict)) == verdict


def test_a_child_that_gave_no_answer_is_judged_in_process(monkeypatch):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.cad import validity

    class _Report:
        def as_dict(self):
            return {"clean": True, "kind": "brep", "reasons": []}
    monkeypatch.setattr(validity, "check_canonical", lambda uploaded, canonical: _Report())
    assert gc.clean_verdict(None, None, started=_Done(None))["clean"] is True


def test_the_early_fluid_split_is_used_only_for_its_own_task(tmp_path, monkeypatch):
    from meshpipeline.cad import fluid_volume_check as fv

    answer = {"found": False, "reason": "no space inside the parts"}
    early = _Done(answer)
    early.task = fv._task(tmp_path / "a.step", 1.0, "cad")
    monkeypatch.setattr(fv, "wants_fluid_volume", lambda facts: True)
    seen = {}
    monkeypatch.setattr(fv, "fold", lambda facts, got: seen.update(got=got))
    fv.read_fluid_volume({}, tmp_path / "a.step", work=tmp_path, scale=1.0, started=early)
    assert seen["got"] == answer
    # a split begun for another scale is stopped and run again for this one
    import meshpipeline.cad.fluid_volume as fvol
    monkeypatch.setattr(fvol, "run_bounded", lambda task, work, budget: {"found": False, "reason": "rerun"})
    other = _Done(answer)
    other.task = fv._task(tmp_path / "a.step", 2.0, "cad")
    fv.read_fluid_volume({}, tmp_path / "a.step", work=tmp_path, scale=1.0, started=other)
    assert other.closed and seen["got"]["reason"] == "rerun"


# ----------------------------------------------------------------- the Boolean check, once a solid ----
def test_the_boolean_check_runs_once_per_solid_however_many_suspects_lie_on_it(monkeypatch):
    from meshpipeline.cad import suspect_defects as SD

    calls: list[int] = []
    monkeypatch.setattr(SD, "_face_names", lambda shape, record: [f"Face_{k}" for k in range(1, 9)])
    monkeypatch.setattr(SD, "_boolean_faces", lambda shape, body: calls.append(body) or {1, 2})
    validity = {"reasons": [{"code": "self_intersection", "details": {"faces": [[1, 2]], "bodies": [1]}},
                            {"code": "self_intersection", "details": {"faces": [[3, 4]], "bodies": [1]}},
                            {"code": "self_intersection", "details": {"faces": [[5, 6]], "bodies": [2]}}]}
    found = SD.find(object(), {}, validity=validity)
    assert len(found) == 3 and sorted(calls) == [1, 2]
    assert [s["confirmed"] for s in found] == [True, False, False]
    assert SD.BOOLEAN_PARALLEL is True


# --------------------------------------------------------------------------- the waiting line ----
T0 = 1_000_000.0


def _view(queue, now=T0 + 90.0, stored=None, requested=None, progress=None):
    return gp.progress_view(stored or {"status": "pending", "written_at": T0}, requested, progress, now=now,
                            scout_limit_s=900, naming_limit_s=600, queue=queue)


def test_a_queued_check_says_how_many_checks_are_ahead_of_it():
    view = _view({"scout": {"ahead": 2}})
    assert view["current"] == "Queued behind 2 other checks" and view["state"] == "queued"
    assert "No worker" not in view["note"]
    assert _view({"scout": {"ahead": 0}})["current"] == "Next in line…"
    assert _view({"scout": {"ahead": 1}})["current"] == "Queued behind 1 other check"


def test_a_check_a_worker_has_taken_says_it_is_checking_with_its_timer():
    view = _view({"scout": {"running": True}}, now=T0 + 4.0)
    assert view["current"] == gp.CHECKING and view["live"] is True and view["elapsed_s"] == 4.0
    assert view["note"] == ""


def test_with_no_word_from_the_queue_the_line_never_blames_a_worker():
    view = _view(None)
    assert view["current"] == "Waiting to start…" and "No worker" not in view["note"]


def test_a_naming_queued_behind_other_checks_says_so():
    stored = {"status": "scouted", "written_at": T0, "facts": {}}
    requested = {"requested_at": T0, "purpose_text": "an elbow, water"}
    view = _view({"naming": {"ahead": 3}}, stored=stored, requested=requested, now=T0 + 30)
    assert view["current"] == "Queued behind 3 other checks" and view["state"] == "running"
