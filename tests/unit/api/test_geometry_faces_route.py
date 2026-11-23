# Responsibility: Hold the face-pick route (NOTE-17, POST /geometry/{sid}/check/faces/at) to its contract: owner-scoped
# like its neighbours (another tenant's session is 404 before anything is read), 409 once the run started, 404 when the
# check kept no face map, the face under the click handed through, and a click that is no point refused.
# Boundaries: the session lookup and application/face_pick are stood in for.
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import face_pick

SID = uuid.uuid4()


@pytest.fixture
def world(monkeypatch):
    calls: list[tuple] = []
    state = {"job_id": None, "owned": True, "answer": {"id": "Face_3", "label": "Face 3 · round Ø20 mm"}}

    async def _owned(session_id, owner_id, organization_id):
        if not state["owned"]:
            raise HTTPException(404, "Session not found")
        return SimpleNamespace(id=session_id, job_id=state["job_id"])

    def _face_at(session_id, point_m, normal=None):
        calls.append((session_id, point_m, normal))
        return state["answer"]

    monkeypatch.setattr(route, "_owned_session", _owned)
    monkeypatch.setattr(face_pick, "face_at", _face_at)
    return state, calls


def _ask(point=(0.01, 0.02, 0.03), normal=(0, 0, 1)):
    body = route.FaceAtIn(point_m=list(point), normal=list(normal) if normal else None)
    return asyncio.run(route.check_face_at(SID, body, owner_id="owner-1", organization_id="org-1"))


def test_the_face_under_the_click_is_answered(world):
    state, calls = world
    assert _ask() == state["answer"]
    assert calls == [(str(SID), [0.01, 0.02, 0.03], [0, 0, 1])]


def test_another_tenants_session_is_not_found_and_nothing_is_read(world):
    state, calls = world
    state["owned"] = False
    with pytest.raises(HTTPException) as e:
        _ask()
    assert e.value.status_code == 404 and calls == []


def test_no_pick_once_the_run_started(world):
    state, calls = world
    state["job_id"] = uuid.uuid4()
    with pytest.raises(HTTPException) as e:
        _ask()
    assert e.value.status_code == 409 and calls == []


def test_a_part_with_no_face_map_is_404(world):
    state, _calls = world
    state["answer"] = None
    with pytest.raises(HTTPException) as e:
        _ask()
    assert e.value.status_code == 404 and e.value.detail == "Faces cannot be picked on this part."


def test_a_click_on_nothing_is_answered_with_no_face(world):
    state, _calls = world
    state["answer"] = {"id": None}
    assert _ask(normal=None) == {"id": None}


@pytest.mark.parametrize("point", [(0.0, 1.0), (0.0, 1.0, 2.0, 3.0), (float("nan"), 0.0, 0.0), (float("inf"), 0, 0)])
def test_a_click_that_is_no_point_is_refused(point):
    with pytest.raises(ValidationError):
        route.FaceAtIn(point_m=list(point))
