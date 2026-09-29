# Responsibility: Verify the geometry check is served through the time box - a scout whose worker
# died reads as failed with a reason and a way on, a naming that never answered too, a scouted
# check with no naming asked is served as it is - that the retry route runs the right step
# again, the confirm waits for the file's unit, a confirmation that fails leaves the stored copy
# as it stood, and the hold read from the store lets the chat go once the check is not working.
# Boundaries: the routes with the session, the store and the queue seam stood in for; no
# database, no worker.
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts import geometry_check as seam
from meshpipeline.contracts import object_storage

pytestmark = pytest.mark.asyncio

SID = uuid.UUID("aaaa1111-2222-4222-b222-aaaaaaaaaaaa")
SOURCE = {"source_id": "s1", "owner_id": "alice", "object_key": "uploads/s1/part.stl", "sha256": "0" * 64,
          "size_bytes": 10, "original_filename": "part.stl", "suffix_hint": ".stl"}


class _Store:
    def __init__(self, objects: dict):
        self.objects = dict(objects)
        self.deleted: list[str] = []

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = local_path.read_bytes()

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)
        self.deleted.append(object_key)

    def create_download_url(self, *, object_key, expires_in):
        return f"https://pictures/{object_key}"


def _key(name: str) -> str:
    return f"sessions/{SID}/geometry_check/{name}"


def _scouted() -> dict:
    return {"status": "scouted", "named": False, "written_at": 1.0, "facts": {"size_mm": [1, 2, 3], "scale_to_m": 0.001},
            "proposal": {"openings": []}, "snapshots": [], "skin_key": _key("skin.json")}


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    session = SimpleNamespace(id=SID, job_id=None, geometry_source_id=uuid.uuid4(),
                              geometry_interpretation_id=None, messages=[{"role": "user", "content": "a pipe"}])

    async def _owned(session_id, owner_id, organization_id):
        return session
    monkeypatch.setattr(route, "_owned_session", _owned)

    @asynccontextmanager
    async def _db():
        yield None
    from meshpipeline.persistence import session as dbmod
    monkeypatch.setattr(dbmod, "get_db", _db)

    async def _interp(db, sess, owner_id, organization_id):
        return None
    monkeypatch.setattr(gh, "interpretation_payload", _interp)

    async def _source(db, sess, owner_id, organization_id):
        return SOURCE
    monkeypatch.setattr(route, "_source_payload", _source)

    def _with(objects: dict, **session_over):
        for k, v in session_over.items():
            setattr(session, k, v)
        store = _Store({_key(n): json.dumps(v).encode() for n, v in objects.items()})
        monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
        return store
    return _with


async def test_a_scout_whose_worker_died_reads_as_failed_with_a_reason_and_a_way_on(served):
    served({"scout.json": {"status": "pending", "session_id": str(SID),
                           "written_at": time.time() - gc.SCOUT_HARD_LIMIT_S - gc.STALE_MARGIN_S - 5}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "failed" and d["reason"] == gc.SCOUT_TOO_LONG and d["retry"] == "scout"
    assert d["naming_requested"] is False and d["unit_needed"] is True and d["named"] is False


async def test_a_fresh_pending_check_is_still_pending(served):
    served({"scout.json": {"status": "pending", "session_id": str(SID), "written_at": time.time()}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "pending" and "retry" not in d


async def test_a_scouted_check_with_no_naming_asked_is_served_for_the_stage_to_open(served):
    served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4())
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "scouted" and d["named"] is False and d["naming_requested"] is False
    assert d["skin"] is True and d["unit_needed"] is False and "facts" not in d
    # the scale the numbers were read under, and the unit they are read in, on whose word
    assert d["proposal"] == {"openings": [], "scale_to_m": 0.001, "unit": "mm", "unit_basis": "assumed"}


async def test_the_unit_the_session_holds_is_served_beside_the_sizes(served, monkeypatch):
    served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4())

    async def _declared(db, sess, owner_id, organization_id):
        return {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "m", "scale_to_metres": 1.0,
                "basis": "file_declared", "evidence": "SI_UNIT"}
    monkeypatch.setattr(gh, "interpretation_payload", _declared)
    d = await route.get_check(SID, "alice", "org-1")
    assert d["proposal"]["unit"] == "m" and d["proposal"]["unit_basis"] == "file_declared"
    assert route.unit_in_effect(None) == ("mm", "assumed")


def test_a_confirmation_drawn_under_an_assumed_unit_is_re_read_in_the_confirmed_one():
    """A "466 mm" opening on a form drawn while a metres file was read as millimetres is
    466,000 mm; the far-field margins are body lengths and do not move."""
    body = route.ConfirmIn(
        input_kind="solid-body", flow="external", part="car",
        openings=[route.ConfirmedOpening(id=1, name="inlet", role="inlet", diameter_mm=466.2, centroid_mm=[1197.1, 867.2, 0.0]),
                  route.ConfirmedOpening(id=2, name="slot", role="outlet", width_mm=120.0, height_mm=80.0)],
        seed_point_mm=[119.7, 86.7, 0.0], size_mm=[1044.0, 389.0, 288.0], flow_axis="+x",
        reference_length_mm=1044.0, extents={"upstream": 5.0, "downstream": 10.0}, grounded=True, scale_to_m=0.001)
    metres = route.in_confirmed_unit(body, 1.0)
    assert metres.openings[0].diameter_mm == 466200.0 and metres.openings[0].centroid_mm == [1197100.0, 867200.0, 0.0]
    assert metres.openings[1].width_mm == 120000.0 and metres.openings[1].height_mm == 80000.0
    assert metres.seed_point_mm == [119700.0, 86700.0, 0.0] and metres.size_mm == [1044000.0, 389000.0, 288000.0]
    assert metres.reference_length_mm == 1044000.0 and metres.extents == {"upstream": 5.0, "downstream": 10.0}
    assert metres.grounded is True and metres.flow_axis == "+x" and metres.scale_to_m == 1.0
    assert "1044000 x 389000 x 288000 mm" in route.confirmation_message(metres)
    assert route.patches_from(route.ConfirmIn(**dict(metres.model_dump(), flow="internal")))[0]["near_mm"] == [1197100.0, 867200.0, 0.0]
    assert route.in_confirmed_unit(body, 0.001) is body                       # the scales agree
    assert route.in_confirmed_unit(body, None) is body                        # no unit confirmed yet
    assert route.in_confirmed_unit(route.ConfirmIn(input_kind="body-surface", flow="internal"), 1.0).scale_to_m is None  # a form with no scale


async def test_a_naming_that_never_answered_reads_as_failed_and_keeps_the_part_for_the_stage(served):
    served({"scout.json": _scouted(),
            "naming.json": {"requested_at": time.time() - gc.NAMING_HARD_LIMIT_S - gc.STALE_MARGIN_S - 5,
                            "purpose_text": "a pipe"}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "failed" and d["reason"] == gc.NAMING_TOO_LONG and d["retry"] == "naming"
    assert d["skin"] is True and d["naming_requested"] is True
    assert d["proposal"] == {"openings": [], "scale_to_m": 0.001, "unit": "mm", "unit_basis": "assumed"}


async def test_a_naming_that_gave_up_waiting_for_the_scout_is_not_reported_as_running(served):
    served({"scout.json": {"status": "pending", "written_at": time.time()},
            "naming.json": {"requested_at": 1.0, "withdrawn": True}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "pending" and d["naming_requested"] is False


async def test_the_retry_of_a_dead_scout_queues_the_scout_again_and_forgets_the_old_naming(served):
    store = served({"scout.json": {"status": "pending", "written_at": time.time() - 5000},
                    "naming.json": {"requested_at": 1.0, "purpose_text": "old words"}})
    queued: list = []
    seam.set_scout_enqueuer(lambda **kw: queued.append(kw))
    try:
        d = await route.retry_check(SID, None, "alice", "org-1")
    finally:
        seam.set_scout_enqueuer(None)
    assert queued and queued[0]["source"] == SOURCE and queued[0]["session_id"] == str(SID)
    assert d["status"] == "pending" and d["naming_requested"] is False
    assert _key("naming.json") in store.deleted
    assert json.loads(store.objects[_key("scout.json")])["written_at"] > time.time() - 60   # the box starts again


async def test_the_retry_of_a_naming_that_never_answered_runs_it_again_over_the_scouts_facts(served):
    store = served({"scout.json": _scouted(),
                    "naming.json": {"requested_at": time.time() - 5000, "purpose_text": "a pipe"}},
                   geometry_interpretation_id=uuid.uuid4())
    queued: list = []
    seam.set_naming_enqueuer(lambda **kw: queued.append(kw))
    try:
        d = await route.retry_check(SID, None, "alice", "org-1")
    finally:
        seam.set_naming_enqueuer(None)
    assert queued and queued[0]["purpose_text"] == "a pipe"
    assert d["status"] == "scouted" and d["naming_requested"] is True and d["skin"] is True
    assert json.loads(store.objects[_key("naming.json")])["requested_at"] > time.time() - 60


async def test_a_ready_check_has_nothing_to_run_again_even_when_a_step_is_named(served):
    served({"scout.json": {"status": "ready", "named": True, "facts": {}, "proposal": {}, "snapshots": []}})
    with pytest.raises(HTTPException) as refused:
        await route.retry_check(SID, None, "alice", "org-1")
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as refused:
        await route.retry_check(SID, route.RetryIn(step="scout"), "alice", "org-1")
    assert refused.value.status_code == 409 and "nothing to run again" in refused.value.detail


async def test_a_check_still_within_its_time_is_not_run_again(served):
    """A scout or a naming still within its time is a worker still at work: a second one would
    race it for the record. The time box turns a lost worker into a failed check, so a retry
    is never refused for good."""
    store = served({"scout.json": {"status": "pending", "written_at": time.time()}})
    with pytest.raises(HTTPException) as refused:
        await route.retry_check(SID, route.RetryIn(step="scout"), "alice", "org-1")
    assert refused.value.status_code == 409 and "still running" in refused.value.detail
    assert json.loads(store.objects[_key("scout.json")])["status"] == "pending"      # untouched
    served({"scout.json": _scouted(), "naming.json": {"requested_at": time.time(), "purpose_text": "a pipe"}})
    with pytest.raises(HTTPException) as refused:
        await route.retry_check(SID, None, "alice", "org-1")
    assert refused.value.status_code == 409 and "still running" in refused.value.detail


async def test_a_step_that_is_not_the_one_the_check_reports_is_refused(served):
    served({"scout.json": {"status": "pending", "written_at": time.time() - 5000}})
    with pytest.raises(HTTPException) as refused:
        await route.retry_check(SID, route.RetryIn(step="naming"), "alice", "org-1")
    assert refused.value.status_code == 409 and "the scout, not the naming" in refused.value.detail


async def test_the_confirm_waits_for_the_files_unit(served):
    """The numbers on the stage are the file's own read as millimetres until somebody says
    otherwise: a confirmation in a unit nobody has named would bind the ports at the wrong scale."""
    served({"scout.json": _scouted()})
    body = route.ConfirmIn(input_kind="body-surface", flow="internal", openings=[])
    with pytest.raises(HTTPException) as refused:
        await route.confirm_check(SID, body, "alice", "org-1")
    assert refused.value.status_code == 409 and "unit" in refused.value.detail
    assert route.unit_needed(SimpleNamespace(geometry_source_id=uuid.uuid4(), geometry_interpretation_id=uuid.uuid4())) is False
    assert route.unit_needed(SimpleNamespace(geometry_source_id=None, geometry_interpretation_id=None)) is False


def _confirming(monkeypatch, session, interpretation: dict | None, recorded: list, commit=None):
    """The confirm's collaborators stood in for: the session's interpretation, the unit record,
    the session row, and the chat turn that follows."""
    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.persistence import session as dbmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    async def _interp(db, sess, owner_id, organization_id):
        return interpretation
    monkeypatch.setattr(gh, "interpretation_payload", _interp)

    async def _record(db, sess, owner_id, organization_id, unit):
        recorded.append(unit)
        return {"interpretation_id": "i2", "geometry_source_id": "s1", "unit": unit,
                "scale_to_metres": {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254}[unit],
                "basis": "user_confirmed", "evidence": "confirmed on the geometry stage"}
    monkeypatch.setattr(gh, "record_unit", _record)

    @asynccontextmanager
    async def _db():
        yield SimpleNamespace(commit=commit or AsyncMock())
    monkeypatch.setattr(dbmod, "get_db", _db)

    class _Sessions:
        async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
            return session
    monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

    async def _turn(body, owner_id, organization_id):
        return SimpleNamespace(model_dump=lambda mode="json": {"reply": "next"})
    monkeypatch.setattr(chatmod, "chat_message", _turn)


async def test_a_unit_corrected_on_the_stage_is_recorded_and_the_sizes_re_read(served, monkeypatch):
    """A STEP file that says millimetres for a blade drawn in metres: the user picks metres in the
    box beside the sizes, the unit is recorded as theirs, and the declaration carries the sizes
    in true millimetres - 117,000 of them - before the intake reads it."""
    store = served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4(), messages=[])
    session = (await route._owned_session(SID, "alice", "org-1"))
    recorded: list = []
    declared_mm = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
                   "basis": "file_declared", "evidence": "SI_UNIT"}
    _confirming(monkeypatch, session, declared_mm, recorded)
    body = route.ConfirmIn(input_kind="body-surface", flow="internal", size_mm=[7.0, 7.0, 117.0],
                           openings=[route.ConfirmedOpening(id=1, name="root", role="inlet", diameter_mm=4.2, centroid_mm=[0.0, 0.0, 0.0])],
                           scale_to_m=0.001, unit="m")
    out = await route.confirm_check(SID, body, "alice", "org-1")
    assert recorded == ["m"]
    assert "Part size: 7000 x 7000 x 117000 mm" in out["message"] and "4200 mm across" in out["message"]
    assert out["patches"][0]["diameter_mm"] == 4200.0
    assert json.loads(store.objects[_key("confirmed.json")])["unit"] == "m"
    assert session.input_kind == "body-surface"
    # the unit the session already holds, left as it is: nothing recorded, nothing re-read
    recorded.clear()
    same = route.ConfirmIn(input_kind="body-surface", flow="internal", size_mm=[7.0, 7.0, 117.0], openings=[],
                           scale_to_m=0.001, unit="mm")
    out = await route.confirm_check(SID, same, "alice", "org-1")
    assert recorded == [] and "Part size: 7 x 7 x 117 mm" in out["message"]


async def test_openings_that_share_a_name_are_named_apart_not_refused(served, monkeypatch):
    """The intake soak: the check named five discarded stickers "not_an_opening", and Proceed was
    refused ("Every opening needs its own name") until the user renamed stickers they were throwing
    away. Discarded stickers are not compared; real openings that collide are numbered."""
    served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4(), messages=[])
    session = (await route._owned_session(SID, "alice", "org-1"))
    _confirming(monkeypatch, session, {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm",
                                       "scale_to_metres": 0.001, "basis": "file_declared", "evidence": "SI_UNIT"}, [])
    ops = [route.ConfirmedOpening(id=i, name="not_an_opening", role="not_an_opening") for i in range(1, 6)]
    ops += [route.ConfirmedOpening(id=6, name="outlet", role="inlet", diameter_mm=84.0, centroid_mm=[0.0, 0.0, 0.0]),
            route.ConfirmedOpening(id=7, name="Outlet", role="outlet", diameter_mm=61.0, centroid_mm=[400.0, 0.0, 0.0])]
    out = await route.confirm_check(SID, route.ConfirmIn(input_kind="fluid-domain", flow="internal", openings=ops,
                                                         scale_to_m=0.001, unit="mm"), "alice", "org-1")
    assert [p["name"] for p in out["patches"]] == ["outlet", "Outlet_2", "wall"]
    assert [p["type"] for p in out["patches"]] == ["inlet", "outlet", "wall"]


async def test_a_confirmation_whose_transaction_fails_takes_its_stored_copy_back(served, monkeypatch):
    """The stored copy goes first, the session's transaction second. A transaction that fails
    leaves no copy behind: the next read would otherwise report a confirmation that never
    landed, and the console would stop waiting for one."""
    store = served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4(), messages=[])
    session = await route._owned_session(SID, "alice", "org-1")
    declared_mm = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
                   "basis": "file_declared", "evidence": "SI_UNIT"}
    _confirming(monkeypatch, session, declared_mm, [], commit=AsyncMock(side_effect=RuntimeError("db down")))
    body = route.ConfirmIn(input_kind="body-surface", flow="internal", openings=[], scale_to_m=0.001, unit="m")
    with pytest.raises(RuntimeError):
        await route.confirm_check(SID, body, "alice", "org-1")
    assert _key("confirmed.json") in store.deleted and _key("confirmed.json") not in store.objects


async def test_a_failed_confirmation_puts_the_earlier_ones_copy_back(served, monkeypatch):
    """A session already confirmed, then confirmed again with a transaction that fails: the
    session still holds the first declaration, so its copy goes back - deleting it would make
    the read stop reporting the confirmation and let the hold catch the chat again."""
    first = {"confirmed_at": 1.0, "owner_id": "alice", "message": "the first confirmation", "patches": []}
    store = served({"scout.json": _scouted(), "confirmed.json": first},
                   geometry_interpretation_id=uuid.uuid4(), messages=[])
    earlier = store.objects[_key("confirmed.json")]
    session = await route._owned_session(SID, "alice", "org-1")
    declared_mm = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
                   "basis": "file_declared", "evidence": "SI_UNIT"}
    _confirming(monkeypatch, session, declared_mm, [], commit=AsyncMock(side_effect=RuntimeError("db down")))
    body = route.ConfirmIn(input_kind="body-surface", flow="internal", openings=[], scale_to_m=0.001, unit="mm")
    with pytest.raises(RuntimeError):
        await route.confirm_check(SID, body, "alice", "org-1")
    assert store.objects[_key("confirmed.json")] == earlier and _key("confirmed.json") not in store.deleted
    assert (await route.get_check(SID, "alice", "org-1"))["confirmed"] == first
    assert gh.hold_applies(str(SID)) is None


async def test_a_copy_another_confirmation_wrote_since_is_left_alone(served, monkeypatch):
    """Two confirmations close together: the one that fails takes back only its own copy."""
    store = served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4(), messages=[])
    theirs = json.dumps({"confirmed_at": 2.0, "message": "the other confirmation"}).encode()

    async def _overtaken():
        store.objects[_key("confirmed.json")] = theirs     # the other press landed in between
        raise RuntimeError("db down")
    session = await route._owned_session(SID, "alice", "org-1")
    declared_mm = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
                   "basis": "file_declared", "evidence": "SI_UNIT"}
    _confirming(monkeypatch, session, declared_mm, [], commit=_overtaken)
    body = route.ConfirmIn(input_kind="body-surface", flow="internal", openings=[], scale_to_m=0.001, unit="mm")
    with pytest.raises(RuntimeError):
        await route.confirm_check(SID, body, "alice", "org-1")
    assert store.objects[_key("confirmed.json")] == theirs and _key("confirmed.json") not in store.deleted


async def test_the_hold_read_from_the_store_lets_go_of_a_failed_check_and_a_scouted_one_confirmed(served, monkeypatch):
    """The chat is held only while the check is working. A scout whose worker died reads as
    failed and never holds; a naming still running holds the chat until the user confirms the
    scout's part by hand, and from then the intake answers."""
    served({"scout.json": {"status": "pending", "written_at": time.time()}})
    assert gh.hold_applies(str(SID)) == "queue"
    served({"scout.json": {"status": "pending", "written_at": time.time() - gc.SCOUT_HARD_LIMIT_S - gc.STALE_MARGIN_S - 5}})
    assert gh.hold_applies(str(SID)) is None
    served({"scout.json": _scouted(), "naming.json": {"requested_at": time.time(), "purpose_text": "a pipe"}},
           geometry_interpretation_id=uuid.uuid4(), messages=[])
    assert gh.hold_applies(str(SID)) == "wait"
    session = await route._owned_session(SID, "alice", "org-1")
    declared_mm = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
                   "basis": "file_declared", "evidence": "SI_UNIT"}
    _confirming(monkeypatch, session, declared_mm, [])
    await route.confirm_check(SID, route.ConfirmIn(input_kind="body-surface", flow="internal", openings=[],
                                                   scale_to_m=0.001), "alice", "org-1")
    assert gh.hold_applies(str(SID)) is None
