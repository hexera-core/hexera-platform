# Responsibility: Verify the operator's standalone-repair re-dispatch is guarded, fails closed with
#                 the posture off, refuses a threshold it may not grant, and changes nothing in the
#                 payload but the goal.
from __future__ import annotations

import contextlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import meshpipeline.settings.cad_repair as rcfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import admin_repair
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import JobStatus

ADMIN_KEY = "admin-secret-value"
HEADERS = {"X-Admin-Key": ADMIN_KEY}
JOB = uuid.uuid4()

#: The payload the job already carries. Everything in it must survive untouched.
_PAYLOAD = {"schema_version": 2, "job_id": str(JOB), "owner_id": "owner-a",
            "mesh_engine": "snappy", "purpose": "external_cfd"}


class _Jobs:
    def __init__(self, payload=None, transition=TransitionResult.applied):
        self.payload = dict(payload) if payload else None
        self._transition = transition
        self.written: list[dict] = []
        self.transitions: list = []

    async def get_dispatch_payload(self, db, job_id):
        return dict(self.payload) if self.payload else None

    async def set_dispatch_payload(self, db, job_id, payload):
        self.written.append(dict(payload))

    async def transition(self, db, job_id, target):
        self.transitions.append(target)
        return self._transition


class _Session:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def world(monkeypatch):
    jobs = _Jobs(_PAYLOAD)
    dispatched: list[tuple] = []

    @contextlib.asynccontextmanager
    async def _no_db():
        yield _Session()

    async def _dispatch(db, job_id, payload):
        dispatched.append((job_id, dict(payload)))

    monkeypatch.setattr(admin_repair, "get_db", _no_db)
    monkeypatch.setattr(admin_repair, "jobs_repo", lambda: jobs)
    monkeypatch.setattr(polcfg, "ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", "live", raising=False)
    from meshpipeline.application import pipeline_run
    monkeypatch.setattr(pipeline_run, "dispatch", _dispatch)

    app = FastAPI()
    app.include_router(admin_repair.router, prefix="/api/v1/admin/repair")
    from types import SimpleNamespace
    return SimpleNamespace(jobs=jobs, dispatched=dispatched,
                           client=TestClient(app, raise_server_exceptions=False))


def _post(world, **body):
    return world.client.post(f"/api/v1/admin/repair/jobs/{JOB}/standalone",
                             headers=HEADERS, json={"actor": "ops-ann", **body})


def test_the_credential_guards_it(world):
    assert world.client.post(f"/api/v1/admin/repair/jobs/{JOB}/standalone",
                             json={"goal": "standalone"}).status_code == 403


def test_the_goal_is_the_only_thing_the_payload_gains(world):
    out = _post(world, goal="standalone", auto_accept_deviation_mm=0.05)
    assert out.status_code == 200, out.text
    assert out.json()["goal"] == "standalone"
    assert out.json()["auto_accept_deviation_mm"] == 0.05
    written = world.jobs.written[-1]
    assert written["repair_goal"] == {"goal": "standalone", "auto_accept_deviation_mm": 0.05}
    assert {k: v for k, v in written.items() if k != "repair_goal"} == _PAYLOAD
    # the CAS runs before the dispatch, and the dispatch gets the payload that was stored
    assert world.jobs.transitions == [JobStatus.pending]
    assert world.dispatched[-1][1] == written


def test_the_posture_off_refuses_before_anything_is_written(world, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", "off", raising=False)
    out = _post(world, goal="standalone")
    assert out.status_code == 409
    assert "CAD_REPAIR_STANDALONE_JOBS" in out.text
    assert world.jobs.written == [] and world.dispatched == []


@pytest.mark.parametrize("threshold", [5.0, -1.0, float("inf"), float("-inf"), float("nan")])
def test_a_threshold_it_may_not_grant_is_refused_and_nothing_is_dispatched(world, threshold):
    out = _post(world, goal="standalone", auto_accept_deviation_mm=threshold)
    assert out.status_code == 422, out.text
    assert world.jobs.written == [] and world.dispatched == []


@pytest.mark.parametrize("goal", ["benchmark", "", "Standalone", "mesh"])
def test_only_a_customer_goal_may_be_asked_for(world, goal):
    assert _post(world, goal=goal).status_code == 422
    assert world.dispatched == []


def test_a_job_with_no_payload_is_not_invented(monkeypatch, world):
    world.jobs.payload = None
    out = _post(world, goal="standalone")
    assert out.status_code == 409 and "no dispatch payload" in out.text
    assert world.dispatched == []


def test_a_job_that_cannot_be_re_run_is_refused_before_the_payload_is_written(world):
    world.jobs._transition = TransitionResult.rejected_current_state
    out = _post(world, goal="standalone")
    assert out.status_code == 409
    assert world.jobs.written == [] and world.dispatched == []
