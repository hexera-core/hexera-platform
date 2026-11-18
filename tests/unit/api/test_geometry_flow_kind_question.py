# Responsibility: Verify the stage's read of the check asks the one flow-kind question once the naming has landed and the part is still undecided, never twice, and serves the flow the user stated in the chat on the form - so a reload keeps their answer.
# Boundaries: the check route with the session, the store and the session row stood in for; the flow-kind authority and the intake's seam are the real ones. No database, no worker.
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.agents.intake import flow
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import flow_kind as fk
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts import object_storage

pytestmark = pytest.mark.asyncio

SID = uuid.UUID("bbbb1111-2222-4222-b222-bbbbbbbbbbbb")
#: a straight convex bar with two flat ends: the authority cannot tell through from around (FLOW-2)
BAR = {"body_kind": "bar", "flow": "internal", "input_kind": "fluid-domain", "size_mm": [40.0, 40.0, 200.0],
       "scale_to_m": 0.001,
       "openings": [{"id": 1, "role": "inlet", "normal": [0, 0, -1], "centroid_mm": [20, 20, 0], "diameter_mm": 40.0},
                    {"id": 2, "role": "outlet", "normal": [0, 0, 1], "centroid_mm": [20, 20, 200], "diameter_mm": 40.0}]}


def _key(name: str) -> str:
    return f"sessions/{SID}/geometry_check/{name}"


class _Store:
    def __init__(self, objects: dict):
        self.objects = {_key(n): json.dumps(v).encode() for n, v in objects.items()}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def create_download_url(self, *, object_key, expires_in):
        return f"https://pictures/{object_key}"


def _named(facts: dict) -> dict:
    facts = {**facts, "flow_kind": fk.decide(facts, words="CFD please")}
    record = facts["flow_kind"]
    return {"status": "ready", "named": True, "written_at": 1.0, "facts": facts, "snapshots": [],
            "skin_key": _key("skin.json"),
            "proposal": {"openings": facts["openings"], "flow": record["flow"], "input_kind": record["input_kind"],
                         "flow_kind": record, "size_mm": facts["size_mm"]}}


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT
    session = SimpleNamespace(id=SID, owner_id="alice", job_id=None, geometry_source_id=uuid.uuid4(),
                              geometry_interpretation_id=uuid.uuid4(),
                              messages=[{"role": "assistant", "content": UPLOAD_ACKNOWLEDGEMENT},
                                        {"role": "user", "content": "CFD please"},
                                        {"role": "assistant", "content": gh.HOLD_REPLY}])

    async def _owned(session_id, owner_id, organization_id):
        return session
    monkeypatch.setattr(route, "_owned_session", _owned)

    @asynccontextmanager
    async def _db():
        async def _commit():
            return None
        yield SimpleNamespace(commit=_commit)
    from meshpipeline.persistence import session as dbmod
    monkeypatch.setattr(dbmod, "get_db", _db)

    class _Repo:
        async def get_for_update(self, db, session_id):
            return session

        async def append_message(self, db, session_id, role, content):
            session.messages = [*session.messages, {"role": role, "content": content}]
    from meshpipeline.persistence.repositories import session_repository as srmod
    monkeypatch.setattr(srmod, "SessionRepository", _Repo)

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(gh, "interpretation_payload", _none)
    monkeypatch.setattr(gh, "unit_fixing_format", _none)

    def _with(objects: dict):
        monkeypatch.setattr(object_storage, "get_object_store", lambda: _Store(objects))
        return session
    return _with


async def test_an_undecided_part_gets_the_one_question_once_the_naming_has_landed(served):
    session = served({"scout.json": _named(BAR)})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["route"] == "flow" and d["proposal"]["flow_kind"]["kind"] == "undecided"
    assert d["ask"]["reply"] == flow.FLOW_KIND_QUESTION
    assert [c["send"] for c in d["ask"]["choices"]] == [send for _l, send in flow.FLOW_KIND_CHOICES]
    assert session.messages[-1] == {"role": "assistant", "content": flow.FLOW_KIND_QUESTION}
    # the stage reads the check again: nothing is asked twice
    again = await route.get_check(SID, "alice", "org-1")
    assert "ask" not in again and [m["content"] for m in session.messages].count(flow.FLOW_KIND_QUESTION) == 1


async def test_a_scouted_check_is_never_asked_about(served):
    scouted = {**_named(BAR), "status": "scouted", "named": False}
    session = served({"scout.json": scouted})
    d = await route.get_check(SID, "alice", "org-1")
    assert "ask" not in d and flow.FLOW_KIND_QUESTION not in [m["content"] for m in session.messages]


async def test_the_answer_stays_on_the_form_through_a_reload(served):
    session = served({"scout.json": _named(BAR)})
    session.messages = [*session.messages, {"role": "assistant", "content": flow.FLOW_KIND_QUESTION},
                        {"role": "user", "content": flow.FLOW_KIND_CHOICES[1][1]},
                        {"role": "assistant", "content": gh.HOLD_REPLY}]
    d = await route.get_check(SID, "alice", "org-1")
    assert "ask" not in d
    assert d["proposal"]["flow"] == "external" and d["proposal"]["input_kind"] == "solid-body"
    assert d["proposal"]["flow_kind"]["by"] == "words" and d["proposal"]["flow_kind"]["rule"] == "FLOW-1"


async def test_a_confirmed_part_is_neither_asked_nor_changed(served):
    session = served({"scout.json": _named(BAR), "confirmed.json": {"flow": "internal"}})
    session.messages = [*session.messages, {"role": "user", "content": flow.FLOW_KIND_CHOICES[1][1]}]
    d = await route.get_check(SID, "alice", "org-1")
    assert "ask" not in d and d["proposal"]["flow"] == "internal"
