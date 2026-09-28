# Responsibility: Verify a unit changed after it was settled re-reads every length derived under the
# old one - the declaration the intake reads (re-worded where it stands), the declared patches -
# withdraws a run proposed with the old sizes, never compounds two changes, and tells the user in
# plain words what changed.
# Boundaries: application/unit_change with the store, the interpretation table and the session
# repository stood in for; no database, no model.
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import unit_change as uch
from meshpipeline.application.geometry_confirmation import (
    CONFIRMED_MARK,
    confirmation_message,
    patches_from,
    reread_record,
)
from meshpipeline.contracts import object_storage
from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.persistence.repositories import geometry_interpretation_repository as girmod

SID = uuid.UUID("bbbb1111-2222-4222-b222-bbbbbbbbbbbb")
SCALE = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254}


def _key(name: str) -> str:
    return f"sessions/{SID}/geometry_check/{name}"


class _Store:
    def __init__(self, objects: dict):
        self.objects = {_key(k): json.dumps(v).encode() for k, v in objects.items()}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]


#: What the stage stored for the IEA 15 MW blade, confirmed in the millimetres its header declares:
#: a body in a flow, 117 "mm" long, and (for the internal-flow patches) one 4.2 mm opening.
BLADE = {"confirmed_at": 1.0, "owner_id": "alice", "message": "old words", "patches": [],
         "input_kind": "solid-body", "flow": "external", "part": "wind turbine blade", "openings": [],
         "seed_point_mm": None, "size_mm": [6.836, 6.595, 117.0], "flow_axis": "+x",
         "reference_length_mm": 117.0, "extents": {"upstream": 5.0, "downstream": 10.0,
                                                   "lateral": 5.0, "vertical": 5.0},
         "grounded": False, "scale_to_m": 0.001, "unit": "mm"}
DUCT = {"confirmed_at": 1.0, "owner_id": "alice", "message": "old", "patches": [],
        "input_kind": "body-surface", "flow": "internal", "part": "duct",
        "openings": [{"id": 1, "name": "inlet", "role": "inlet", "diameter_mm": 4.2, "centroid_mm": [0.0, 1.0, 2.0],
                      "width_mm": None, "height_mm": None}],
        "seed_point_mm": [1.0, 1.0, 1.0], "size_mm": [7.0, 7.0, 117.0], "scale_to_m": 0.001, "unit": "mm"}


def test_a_confirmation_re_read_in_metres_says_every_size_a_thousand_times_larger():
    again = reread_record(BLADE, scale_to_metres=1.0, unit="m")
    assert again["reference_length_mm"] == 117000.0 and again["size_mm"] == [6836.0, 6595.0, 117000.0]
    assert again["extents"] == BLADE["extents"]                   # body lengths: unit-free
    assert again["scale_to_m"] == 1.0 and again["unit"] == "m"
    assert "Reference length: 117000 mm along the flow." in again["message"]
    assert "The file is in metres: the part is 117 m long." in again["message"]
    assert again["message"].startswith(CONFIRMED_MARK)
    duct = reread_record(DUCT, scale_to_metres=0.0254, unit="in")
    assert duct["patches"][0] == {"name": "inlet", "type": "inlet", "diameter_mm": 106.68, "near_mm": [0.0, 25.4, 50.8]}
    assert "107 mm across" in duct["message"] and "The file is in inches" in duct["message"]
    # a confirmation stored without its scale was made in the unit in force then
    unscaled = reread_record({**DUCT, "scale_to_m": None}, scale_to_metres=1.0, unit="m", confirmed_scale=0.001)
    assert unscaled["patches"][0]["diameter_mm"] == 4200.0
    assert reread_record({**DUCT, "scale_to_m": None}, scale_to_metres=1.0, unit="m") is None


def test_the_same_record_read_back_in_its_own_unit_changes_no_number():
    same = reread_record(DUCT, scale_to_metres=0.001, unit="mm")
    assert same["patches"] == patches_from(SimpleNamespace(**{**DUCT, "openings": [SimpleNamespace(**o) for o in DUCT["openings"]]}))


def _live() -> dict:
    return {"id": "snap", "status": ap.AWAITING, "expires_at": time.time() + 600}


def _wire(monkeypatch, *, stored: dict, current: dict | None):
    monkeypatch.setattr(object_storage, "get_object_store", lambda: _Store(stored))

    async def _interp(db, sess, owner_id, organization_id):
        return current
    monkeypatch.setattr(gh, "interpretation_payload", _interp)
    recorded: list = []

    class _Repo:
        async def record(self, db, *, owner_id, geometry_source_id, unit, basis, evidence, organization_id=""):
            recorded.append((unit, basis.value, evidence))
            return SimpleNamespace(interpretation_id=str(uuid.uuid4()))
    monkeypatch.setattr(girmod, "GeometryInterpretationRepository", _Repo)
    repo = MagicMock()
    for name in ("bind_geometry_interpretation", "set_intake_patches", "set_messages", "set_intake_gate"):
        setattr(repo, name, AsyncMock())
    return repo, recorded


def _session(**over):
    base = {"id": SID, "geometry_source_id": uuid.uuid4(), "geometry_interpretation_id": uuid.uuid4(),
            "intake_gate": {}, "messages": []}
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_a_store_that_cannot_be_read_stops_the_change_before_anything_is_written(monkeypatch):
    """The unit is never bound over confirmed sizes left in the old one: the confirmation is read
    first, and a store that fails stops the change with nothing recorded."""
    repo, recorded = _wire(monkeypatch, stored={}, current={"unit": "mm", "scale_to_metres": 0.001,
                                                            "basis": "file_declared"})

    class _Broken:
        def get_bytes(self, *, object_key):
            raise ConnectionError("the store is down")
    monkeypatch.setattr(object_storage, "get_object_store", lambda: _Broken())
    with pytest.raises(uch.UnitChangeError):
        await uch.change_unit(None, _session(intake_gate={"approval": _live()}), owner_id="a", organization_id="",
                              unit=LengthUnit.metre, session_repo=repo)
    assert recorded == []
    repo.bind_geometry_interpretation.assert_not_awaited(); repo.set_intake_patches.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_file_is_in_metres_re_reads_the_confirmation_and_withdraws_the_proposed_run(monkeypatch):
    declared_mm = {"unit": "mm", "scale_to_metres": 0.001, "basis": "file_declared"}
    repo, recorded = _wire(monkeypatch, stored={"confirmed.json": DUCT,
                                                "scout.json": {"facts": {"size_mm": [7.0, 7.0, 117.0], "scale_to_m": 0.001}}},
                           current=declared_mm)
    history = [{"role": "user", "content": "a duct"},
               {"role": "assistant", "content": confirmation_message(SimpleNamespace(**{**DUCT, "unit": "mm", "openings": [SimpleNamespace(**o) for o in DUCT["openings"]]}))},
               {"role": "assistant", "content": "What fluid?"},
               {"role": "user", "content": "the file is in metres"}]
    session = _session(intake_gate={"approval": _live(), "selection": {"engine": "snappy"}}, messages=history)
    change = await uch.change_unit(None, session, owner_id="alice", organization_id="org-1",
                                   unit=LengthUnit.metre, session_repo=repo)
    assert recorded == [(LengthUnit.metre, "user_confirmed", "changed by the user in the chat")]
    repo.bind_geometry_interpretation.assert_awaited_once()
    # every derived length, re-read: the patches the port binding reads ...
    patches = repo.set_intake_patches.await_args.args[2]
    assert patches[0]["diameter_mm"] == 4200.0 and patches[0]["near_mm"] == [0.0, 1000.0, 2000.0]
    # ... and the declaration the intake reads, re-worded where it stood
    messages = repo.set_messages.await_args.args[2]
    assert [m["content"][:20] for m in messages] == [m["content"][:20] for m in history]
    assert "4200 mm across" in messages[1]["content"] and "The file is in metres" in messages[1]["content"]
    # the run proposed with the old sizes can never dispatch: the gate to write says so, and it
    # is handed back for the intake authority to write - the only writer of the gate
    repo.set_intake_gate.assert_not_awaited()
    gate = change.gate
    assert gate["approval"]["status"] == ap.INVALIDATED and gate["selection"] == {"engine": "snappy"}
    assert change.approval_withdrawn and change.reread
    assert change.reply() == ("Noted: the file is in metres, so the part is 117 m long, not 117 mm. I've re-read "
                              "every size you confirmed on the picture in metres. Everything else you confirmed "
                              "stands. The run I summarised used the old sizes, so I've withdrawn it; say go on "
                              "and I'll set it up again with these.")


@pytest.mark.asyncio
async def test_two_changes_in_a_row_re_read_from_the_confirmation_and_never_compound(monkeypatch):
    """mm -> m -> in: the second change starts from what the user confirmed, not from the first
    change's numbers, so the opening is 4.2 in = 106.68 mm, not 4200 in."""
    now_m = {"unit": "m", "scale_to_metres": 1.0, "basis": "user_confirmed"}
    repo, _ = _wire(monkeypatch, stored={"confirmed.json": DUCT}, current=now_m)
    session = _session(messages=[{"role": "assistant", "content": CONFIRMED_MARK + " old"}])
    change = await uch.change_unit(None, session, owner_id="alice", organization_id="", unit=LengthUnit.inch,
                                   session_repo=repo)
    assert repo.set_intake_patches.await_args.args[2][0]["diameter_mm"] == 106.68
    assert change.old_unit == "m" and change.new_unit == "in" and not change.approval_withdrawn
    assert change.gate is None                                    # nothing on the gate to change
    assert change.reply().endswith("Say go on when you're ready.")


@pytest.mark.asyncio
async def test_the_unit_already_held_changes_nothing_but_becomes_the_users_word(monkeypatch):
    """"It is in millimetres", said of a file that declares millimetres: nothing to re-read, no
    reply of its own - but the user has now said so, and the stage stops offering the other
    reading of a size it found doubtful."""
    repo, recorded = _wire(monkeypatch, stored={}, current={"unit": "m", "scale_to_metres": 1.0, "basis": "file_declared"})
    assert await uch.change_unit(None, _session(), owner_id="a", organization_id="", unit=LengthUnit.metre,
                                 session_repo=repo) is None
    assert recorded == [(LengthUnit.metre, "user_confirmed", "confirmed by the user in the chat")]
    repo.set_intake_patches.assert_not_awaited(); repo.set_intake_gate.assert_not_awaited()
    recorded.clear()
    repo, recorded = _wire(monkeypatch, stored={}, current={"unit": "m", "scale_to_metres": 1.0, "basis": "user_confirmed"})
    assert await uch.change_unit(None, _session(), owner_id="a", organization_id="", unit=LengthUnit.metre,
                                 session_repo=repo) is None
    assert recorded == []
    assert await uch.change_unit(None, _session(geometry_source_id=None), owner_id="a", organization_id="",
                                 unit=LengthUnit.inch, session_repo=repo) is None


@pytest.mark.asyncio
async def test_before_anything_was_confirmed_the_unit_changes_and_the_reply_says_the_new_length(monkeypatch):
    repo, _ = _wire(monkeypatch, stored={"scout.json": {"facts": {"size_mm": [6.836, 6.595, 117.0], "scale_to_m": 0.001}}},
                    current={"unit": "mm", "scale_to_metres": 0.001, "basis": "file_declared"})
    change = await uch.change_unit(None, _session(), owner_id="a", organization_id="", unit=LengthUnit.metre,
                                   session_repo=repo)
    assert not change.reread and change.longest_new_m == pytest.approx(117.0)
    repo.set_intake_patches.assert_not_awaited(); repo.set_messages.assert_not_awaited()
    assert change.reply() == "Noted: the file is in metres, so the part is 117 m long, not 117 mm. Say go on when you're ready."


def test_withdrawing_touches_only_a_live_proposal():
    gate, withdrawn = uch.withdraw_live_approval({"approval": {"status": ap.INVALIDATED}})
    assert not withdrawn and gate["approval"]["status"] == ap.INVALIDATED
    gate, withdrawn = uch.withdraw_live_approval(None)
    assert not withdrawn and gate == {}
    gate, withdrawn = uch.withdraw_live_approval({"approval": _live()})
    assert withdrawn and gate["approval"]["invalidated_reason"] == uch.WITHDRAWN_REASON
