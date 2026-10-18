# Responsibility: Verify the repair review routes: the grouped payload, recorded decisions, and the run that resumes a parked job.
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.repair_review as rr
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import JobStatus

_app = FastAPI()
_app.include_router(rr.router, prefix="/api/v1/simulation")
_JOB = uuid.UUID("aaaabbbb-1111-4111-b111-aaaaaaaaaaaa")
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _db():
    yield AsyncMock()


def _item(n, group="small_edge:fix_small_edges", severity="warning", pairing="", state="open",
          proposal="auto", decision=None, decided=0):
    fp = f"edge:{n:04d}"
    if proposal == "auto":
        proposal = {"op": "fix_small_edges", "targets": [fp], "rationale": "r",
                    "provenance": "rules"}
    return SimpleNamespace(
        id=uuid.UUID(int=n), group_key=group, defect_code=group.split(":")[0],
        severity=severity, entity_type="edge", entity_index=n, fingerprint=fp,
        measurements={}, message="m", origin="triage", pairing_key=pairing,
        proposal=proposal, camera={}, state=state, decision=decision,
        decided_at=_T0 + timedelta(seconds=decided) if decided else None)


def _park(kept=()):
    return SimpleNamespace(id=uuid.uuid4(), status="open", geometry_session_id="gs-1",
                           kept_operations=list(kept))


def _job(status="pending_review"):
    return SimpleNamespace(id=_JOB, status=JobStatus(status),
                           dispatch_payload={"job_id": str(_JOB), "owner_id": "u"})


class World:
    """Everything the routes touch, mocked at the module seams."""

    def __init__(self, items, park=None, job="pending_review"):
        self.park = park if park is not None else _park()
        self.items = items
        self.job = _job(job) if job else None
        self.repo = MagicMock()
        self.repo.open_park = AsyncMock(return_value=self.park)
        self.repo.latest_park = AsyncMock(return_value=self.park)
        self.repo.items_for = AsyncMock(side_effect=lambda *_a: list(self.items))
        self.repo.record_decision = AsyncMock()
        self.repo.record_group_decision = AsyncMock(return_value=2)
        self.repo.close_park = AsyncMock()
        self.job_repo = MagicMock()
        self.job_repo.transition = AsyncMock(return_value=TransitionResult.applied)
        self.dispatch = AsyncMock()
        self.admit = AsyncMock()
        self.interpret = AsyncMock()
        self.logged = []

    async def call(self, method, path, **kw):
        stack = [
            patch.object(rr, "get_db", _db), patch.object(rr, "repo", self.repo),
            patch.object(rr, "job_repo", self.job_repo),
            patch.object(rr.svc, "get_job", new=AsyncMock(return_value=self.job)),
            patch("meshpipeline.application.pipeline_run.dispatch", self.dispatch),
            patch("meshpipeline.application.spend_gate.admit", self.admit),
            patch("meshpipeline.agents.repair_review.interpreter.interpret", self.interpret),
            patch("meshpipeline.capture.logger.TrainingLogger.log",
                  lambda _s, ev, payload, **kw: self.logged.append((ev, payload, kw))),
        ]
        for p in stack:
            p.start()
        try:
            async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://t") as c:
                return await c.request(method, f"/api/v1/simulation/{_JOB}{path}", **kw)
        finally:
            for p in stack:
                p.stop()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_SAMPLE_SIZE", 2)


async def test_the_review_is_served_grouped_sampled_and_blocking_first():
    items = [_item(1), _item(2), _item(3),
             _item(4, group="open_shell:sew_faces", severity="error", pairing="p1"),
             _item(5, group="open_shell:sew_faces", severity="error", pairing="p1")]
    items[1].state, items[1].decision = "accepted", {"action": "accept"}
    body = (await World(items).call("GET", "/review")).json()
    assert body["job_id"] == str(_JOB) and body["status"] == "open"
    assert body["geometry_session_id"] == "gs-1"
    assert (body["decided"], body["open"]) == (1, 4)
    first, second = body["groups"]
    assert first["group_key"] == "open_shell:sew_faces"          # blocking before larger
    assert first["apply_all_allowed"] is False and first["count"] == 2
    assert second["proposed_op"] == "fix_small_edges" and second["count"] == 3
    assert second["apply_all_allowed"] is True
    assert second["sampled_item_ids"] == [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    assert second["items"][0]["fingerprint"] == "edge:0001"


async def test_apply_all_is_refused_for_a_paired_group_end_to_end():
    items = [_item(1, pairing="p"), _item(2, pairing="p")]
    world = World(items)
    resp = await world.call("POST", "/review/decision",
                            json={"action": "apply_all", "group_key": items[0].group_key})
    assert resp.status_code == 409
    world.repo.record_group_decision.assert_not_called()
    assert world.logged == []


async def test_decisions_on_a_foreign_or_unparked_job_record_nothing():
    foreign = World([_item(1)], job=None)
    r = await foreign.call("POST", "/review/decision",
                           json={"action": "accept", "item_id": str(uuid.UUID(int=1))})
    assert r.status_code == 404
    unparked = World([_item(1)], job="running")
    r = await unparked.call("POST", "/review/decision",
                            json={"action": "accept", "item_id": str(uuid.UUID(int=1))})
    assert r.status_code == 409
    for w in (foreign, unparked):
        w.repo.record_decision.assert_not_called()
        w.repo.record_group_decision.assert_not_called()
        assert w.logged == []
    # an item id that is not this job's own is not found, whatever it exists as elsewhere
    other = World([_item(1)])
    r = await other.call("POST", "/review/decision",
                         json={"action": "leave", "item_id": str(uuid.UUID(int=99))})
    assert r.status_code == 404
    other.repo.record_decision.assert_not_called()


async def test_a_decision_is_recorded_and_logged():
    world = World([_item(1)])
    r = await world.call("POST", "/review/decision",
                         json={"action": "accept", "item_id": str(uuid.UUID(int=1))})
    assert r.status_code == 200
    world.repo.record_decision.assert_awaited_once()
    (ev, payload, kw), = world.logged
    assert ev == "repair_review" and kw["op_id"] == "review:decide"
    assert payload["action"] == "accept" and payload["item"] == str(uuid.UUID(int=1))
    assert "latency_ms" in payload and payload["proposal"]["op"] == "fix_small_edges"


async def test_a_redirect_stores_a_directive_and_a_refusal_stores_nothing():
    world = World([_item(1)])
    directive = {"op": "sew_faces", "targets": ["edge:0001"], "defect_code": "small_edge",
                 "defect_fingerprint": "edge:0001", "source": "engineer", "item_id": ""}
    world.interpret.return_value = SimpleNamespace(
        kind="directive", playback="i will sew the faces", reason="", scope="item",
        directive=directive)
    body = {"action": "redirect", "text": "sew it", "item_id": str(uuid.UUID(int=1))}
    r = await world.call("POST", "/review/decision", json=body)
    assert r.json() == {"kind": "directive", "playback": "i will sew the faces"}
    _a, args, _k = world.repo.record_decision.mock_calls[0]
    assert args[2] == "redirected" and args[3]["directive"]["op"] == "sew_faces"

    refusing = World([_item(1)])
    refusing.interpret.return_value = SimpleNamespace(
        kind="refusal", playback="cannot move geometry", reason="cannot move geometry",
        scope="", directive={})
    r = await refusing.call("POST", "/review/decision", json=body)
    assert r.json()["kind"] == "refusal" and r.json()["reason"] == "cannot move geometry"
    refusing.repo.record_decision.assert_not_called()


async def test_run_builds_replay_then_engineer_directives_in_order_and_skips_the_spend_gate():
    kept = [{"name": "sew_faces", "status": "kept", "targets": ["edge:kept"],
             "defect_code": "open_shell"}]
    redirect = {"op": "fill_planar_hole", "targets": ["edge:0002"], "defect_code": "small_edge",
                "defect_fingerprint": "edge:0002", "source": "engineer", "item_id": "x"}
    items = [_item(1, state="accepted", decided=20),
             _item(2, state="redirected", decided=10, decision={"directive": redirect}),
             _item(3, state="left", decided=5)]
    world = World(items, park=_park(kept))
    r = await world.call("POST", "/review/run")
    assert r.status_code == 202
    (_db_arg, job_id, payload), _ = world.dispatch.call_args
    assert job_id == str(_JOB) and payload["review_continuation"] is True
    directives = payload["repair_directives"]
    assert [(d["op"], d["source"]) for d in directives] == [
        ("sew_faces", "replay"), ("fill_planar_hole", "engineer"),
        ("fix_small_edges", "engineer")]
    assert directives[0]["targets"] == ["edge:kept"]
    assert payload["owner_id"] == "u"                       # the original dispatch is kept
    world.admit.assert_not_called()
    world.job_repo.transition.assert_awaited_once()
    assert world.job_repo.transition.call_args.args[2] == JobStatus.pending
    world.repo.close_park.assert_awaited_once()
    assert world.repo.close_park.call_args.args[2] == "run"


async def test_run_marks_unanswered_items_unreviewed():
    items = [_item(1, state="accepted", decided=1), _item(2), _item(3)]
    world = World(items)
    assert (await world.call("POST", "/review/run")).status_code == 202
    marked = [c.args[1] for c in world.repo.record_decision.mock_calls
              if c.args[2] == "unreviewed"]
    assert marked == [uuid.UUID(int=2), uuid.UUID(int=3)]


async def test_a_park_with_no_proposals_still_serves_and_runs():
    items = [_item(1, proposal=None), _item(2, proposal=None)]
    world = World(items)
    body = (await world.call("GET", "/review")).json()
    assert body["groups"][0]["proposed_op"] == "" and body["groups"][0]["count"] == 2
    leave = await world.call("POST", "/review/decision",
                             json={"action": "leave", "item_id": str(uuid.UUID(int=1))})
    assert leave.status_code == 200
    accept = await world.call("POST", "/review/decision",
                              json={"action": "accept", "item_id": str(uuid.UUID(int=1))})
    assert accept.status_code == 409                      # nothing proposed, nothing to accept
    run = await world.call("POST", "/review/run")
    assert run.status_code == 202
    assert world.dispatch.call_args.args[2]["repair_directives"] == []


async def test_a_delivered_run_is_told_to_run_it_again():
    world = World([_item(1)], job="succeeded")
    r = await world.call("POST", "/review/run")
    assert r.status_code == 409 and "run it again" in r.json()["detail"]
    world.dispatch.assert_not_called()


async def test_review_disabled_means_409_everywhere(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", False)
    world = World([_item(1)])
    calls = [("GET", "/review", {}),
             ("POST", "/review/decision",
              {"json": {"action": "leave", "item_id": str(uuid.UUID(int=1))}}),
             ("POST", "/review/run", {})]
    for method, path, kw in calls:
        assert (await world.call(method, path, **kw)).status_code == 409
    world.dispatch.assert_not_called()
    world.repo.record_decision.assert_not_called()
