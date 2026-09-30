# Responsibility: Verify the reference length follows the flow axis the user confirms - the NASA CRM
# read along +y and turned to +x is sized on its 64.6 m length, not its 30.4 m span - that a length
# the user typed stays theirs, and that the confirmation says when it corrected one.
# Boundaries: the pure rules, and the confirm route with the session, the store and the chat turn
# stood in for; no database, no worker.
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application.geometry_confirmation import reference_length_along_the_flow, stored_lengths
from meshpipeline.contracts import object_storage
from meshpipeline.contracts.geometry_fields import length_of_another_axis

SID = uuid.UUID("cccc1111-2222-4222-b222-cccccccccccc")
#: The real parts from the demo transcript gate (2026-09-30), in the reading's millimetres.
CRM = [64593.0, 30386.0, 18621.0]          # the NASA CRM half model, read from inches
SAE = [840.0, 320.0, 291.0]                # the SAE notchback


# ------------------------------------------------------------------------- the pure rule ----
def test_a_length_along_another_axis_is_named_with_the_length_along_the_flow():
    assert length_of_another_axis(30386.0, "+x", CRM) == (64593.0, "y")    # the span, left by +y
    assert length_of_another_axis(320.0, "-x", SAE) == (840.0, "y")        # the car's width
    assert length_of_another_axis(291.0, "+x", SAE) == (840.0, "z")        # its height
    # the proposal rounds to 0.01 mm and a unit re-read to six decimals: still the same length
    assert length_of_another_axis(30386.004, "+x", CRM) == (64593.0, "y")


def test_a_length_along_the_flow_or_of_no_axis_is_left_alone():
    assert length_of_another_axis(64593.0, "+x", CRM) is None                 # already right
    assert length_of_another_axis(64593.0, "-x", CRM) is None                 # the sign is the same line
    assert length_of_another_axis(7005.0, "+x", CRM) is None                  # a chord the user chose
    assert length_of_another_axis(320.0, "unknown", SAE) is None              # no axis settled
    assert length_of_another_axis(320.0, None, SAE) is None
    assert length_of_another_axis(None, "+x", SAE) is None
    assert length_of_another_axis(320.0, "+x", None) is None
    assert length_of_another_axis(320.0, "+x", [840.0, 320.0]) is None
    # a part as long as it is wide: the length is the flow's as much as the other axis's
    assert length_of_another_axis(500.0, "+x", [500.0, 500.0, 90.0]) is None


def _external(**over):
    body = {"input_kind": "solid-body", "flow": "external", "flow_axis": "+x", "size_mm": CRM,
            "reference_length_mm": 30386.0,
            "extents": {"upstream": 5, "downstream": 10, "lateral": 5, "vertical": 5}}
    body.update(over)
    return route.ConfirmIn(**body)


def test_the_confirmation_is_corrected_and_says_so():
    body, note = reference_length_along_the_flow(_external())
    assert body.reference_length_mm == 64593.0
    assert "30386 mm, was the part's length along y" in note and "64593 mm" in note
    assert "Tell the user" in note


def test_a_length_the_user_typed_is_theirs_and_an_internal_flow_is_not_read():
    body, note = reference_length_along_the_flow(_external(reference_length_typed=True))
    assert body.reference_length_mm == 30386.0 and note == ""
    internal = route.ConfirmIn(input_kind="fluid-domain", flow="internal", size_mm=CRM,
                               flow_axis="+x", reference_length_mm=30386.0)
    assert reference_length_along_the_flow(internal) == (internal, "")


def test_only_a_length_the_check_proposed_is_ever_changed():
    # the check proposed the span (along its +y guess) and the caller kept it: corrected
    assert reference_length_along_the_flow(_external(), proposed_mm=(30386.0,))[0].reference_length_mm == 64593.0
    # a form drawn before the naming landed holds the measuring step's length, which the naming's
    # proposal replaced in the store: still the check's own, still corrected
    assert reference_length_along_the_flow(_external(), proposed_mm=(18621.0, 30386.0))[0].reference_length_mm == 64593.0
    # a caller that sent the car's width on purpose, where the check had proposed its length,
    # chose it: an old console or an API caller cannot say "typed", but the number says it
    car = _external(size_mm=SAE, flow_axis="-x", reference_length_mm=320.0)
    assert reference_length_along_the_flow(car, proposed_mm=(840.0,)) == (car, "")


def test_a_body_without_its_size_is_read_against_the_stored_check():
    body = _external(size_mm=None)
    fixed, note = reference_length_along_the_flow(body, CRM)
    assert fixed.reference_length_mm == 64593.0 and note
    assert reference_length_along_the_flow(body, None) == (body, "")


def test_the_stored_lengths_are_re_read_in_the_scale_the_confirmation_is_in():
    # the scout read the file as millimetres; the user confirmed inches
    stored = {"facts": {"size_mm": [v / 25.4 for v in CRM], "scale_to_m": 0.001, "flow": "external"},
              "proposal": {"reference_length_mm": 30386.0 / 25.4}}
    size, offered = stored_lengths(stored, 0.0254)
    assert size == pytest.approx(CRM, rel=1e-9)
    # the naming's proposal (the span), and the measuring step's own guess (the longest side
    # across +z: the length) that the scouted form showed
    # (the guess is rounded to 0.01 mm in the scout's reading, so it comes back 0.04 mm short)
    assert offered == pytest.approx((30386.0, 64593.0), rel=1e-5)
    assert stored_lengths({"proposal": {"size_mm": [1, 2, 3]}}, 0.01) == ([10.0, 20.0, 30.0], ())
    assert stored_lengths(None, 0.001) == (None, ()) and stored_lengths({"facts": {}}, 0.001) == (None, ())


# ---------------------------------------------------------------------- the confirm route ----
class _Store:
    def __init__(self, objects: dict):
        self.objects = {f"sessions/{SID}/geometry_check/{k}": json.dumps(v).encode() for k, v in objects.items()}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = local_path.read_bytes()

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)


@pytest.fixture
def confirming(monkeypatch):
    """The confirm route with the session (its unit declared in inches), the store and the chat
    turn after it stood in for; returns the store to read the recorded copy from."""
    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.persistence import session as dbmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    session = SimpleNamespace(id=SID, job_id=None, geometry_source_id=uuid.uuid4(),
                              geometry_interpretation_id=uuid.uuid4(), messages=[], intake_gate={})

    async def _owned(session_id, owner_id, organization_id):
        return session
    monkeypatch.setattr(route, "_owned_session", _owned)

    async def _interp(db, sess, owner_id, organization_id):
        return {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "in", "scale_to_metres": 0.0254,
                "basis": "file_declared", "evidence": "declared in the file as inch"}
    monkeypatch.setattr(gh, "interpretation_payload", _interp)

    @asynccontextmanager
    async def _db():
        yield SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(dbmod, "get_db", _db)

    class _Sessions:
        async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
            return session
    monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

    async def _turn(body, owner_id, organization_id):
        return SimpleNamespace(model_dump=lambda mode="json": {"reply": "next"})
    monkeypatch.setattr(chatmod, "chat_message", _turn)

    def _with(scout: dict):
        store = _Store({"scout.json": scout})
        monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
        return store
    return _with


def _scout() -> dict:
    return {"status": "ready", "named": True, "facts": {"size_mm": CRM, "scale_to_m": 0.0254},
            "proposal": {"openings": [], "size_mm": CRM, "flow_axis": "+y", "reference_length_mm": 30386.0,
                         "up_axis": "+z"}}


def _recorded(store) -> dict:
    return json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])


@pytest.mark.asyncio
async def test_the_crm_turned_to_x_is_confirmed_on_its_length_not_its_span(confirming):
    """The demo gate: the check read the CRM along +y, the user turned it to +x and pressed
    Proceed with the span still in the box. The far field is sized on the length along the flow."""
    store = confirming(_scout())
    out = await route.confirm_check(SID, _external(scale_to_m=0.0254, unit="in"), "alice", "org-1")
    assert "Reference length: 64593 mm along the flow." in out["message"]
    assert "was the part's length along y" in out["message"]            # said, never silent
    assert _recorded(store)["reference_length_mm"] == 64593.0


@pytest.mark.asyncio
async def test_a_length_the_user_typed_or_along_the_flow_is_confirmed_as_given(confirming):
    store = confirming(_scout())
    out = await route.confirm_check(SID, _external(scale_to_m=0.0254, unit="in", reference_length_typed=True),
                                    "alice", "org-1")
    assert "Reference length: 30386 mm along the flow." in out["message"] and "was the part's length" not in out["message"]
    assert _recorded(store)["reference_length_mm"] == 30386.0 and _recorded(store)["reference_length_typed"] is True
    out = await route.confirm_check(SID, _external(scale_to_m=0.0254, unit="in", reference_length_mm=64593.0),
                                    "alice", "org-1")
    assert "Reference length: 64593 mm" in out["message"] and "was the part's length" not in out["message"]
    # another axis's extent the check never proposed (the height): a caller's choice, kept
    out = await route.confirm_check(SID, _external(scale_to_m=0.0254, unit="in", reference_length_mm=18621.0),
                                    "alice", "org-1")
    assert "Reference length: 18621 mm" in out["message"] and "was the part's length" not in out["message"]


@pytest.mark.asyncio
async def test_a_caller_that_sends_no_size_is_read_against_the_stored_check(confirming):
    """The user-path harness and other API callers may leave the size out: the stored check's
    size, re-read in the confirmed unit, stands in for it."""
    store = confirming(_scout())
    await route.confirm_check(SID, _external(size_mm=None, scale_to_m=0.0254, unit="in"), "alice", "org-1")
    assert _recorded(store)["reference_length_mm"] == 64593.0
