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
                           started_at=_T0, ended_at=_T0 + timedelta(minutes=20),
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
        self.job_repo.reset_for_review_rerun = AsyncMock()
        self.dispatch = AsyncMock()
        self.repo.reopen_park = AsyncMock(return_value=True)
        self.repo.restore_open = AsyncMock(return_value=0)
        self.repo.reopen_covered = AsyncMock(return_value=0)
        self.get_job = AsyncMock(return_value=self.job)
        self.interpret = AsyncMock()
        self.logged = []

    async def call(self, method, path, **kw):
        stack = [
            patch.object(rr, "get_db", _db), patch.object(rr, "repo", self.repo),
            patch.object(rr, "job_repo", self.job_repo),
            patch.object(rr.svc, "get_job", new=self.get_job),
            patch("meshpipeline.application.pipeline_run.dispatch", self.dispatch),
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


async def test_every_route_scopes_the_job_by_owner_and_organization():
    items = [_item(1)]
    calls = [("GET", "/review", {}),
             ("POST", "/review/decision",
              {"json": {"action": "leave", "item_id": str(uuid.UUID(int=1))}}),
             ("POST", "/review/run", {})]
    for method, path, kw in calls:
        world = World(items)
        await world.call(method, path, **kw)
        world.get_job.assert_awaited_once()
        args, kwargs = world.get_job.call_args
        assert args[1:3] == (_JOB, args[2]) and args[2]          # the caller's owner id
        assert "organization_id" in kwargs


async def test_a_stale_open_park_under_a_moved_on_job_is_served_closed():
    body = (await World([_item(1)], job="pending").call("GET", "/review")).json()
    assert body["status"] == "closed"


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
    assert r.json() == {"kind": "directive", "playback": "i will sew the faces", "covered": []}
    _a, args, _k = world.repo.record_decision.mock_calls[0]
    assert args[2] == "redirected" and args[3]["directive"]["op"] == "sew_faces"

    refusing = World([_item(1)])
    refusing.interpret.return_value = SimpleNamespace(
        kind="refusal", playback="cannot move geometry", reason="cannot move geometry",
        scope="", directive={})
    r = await refusing.call("POST", "/review/decision", json=body)
    assert r.json()["kind"] == "refusal" and r.json()["reason"] == "cannot move geometry"
    refusing.repo.record_decision.assert_not_called()
    refusing.repo.record_group_decision.assert_not_called()
    (ev, payload, _kw), = refusing.logged
    assert ev == "repair_review" and payload["action"] == "redirect_refused"
    assert payload["text"] == "sew it"


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


async def test_a_failed_dispatch_puts_the_job_back_in_review_and_a_retry_works():
    items = [_item(1, state="accepted", decided=1), _item(2)]
    world = World(items)
    world.dispatch.side_effect = RuntimeError("broker down")
    r = await world.call("POST", "/review/run")
    assert r.status_code == 503
    # forward flip, then the compensation: back to pending_review, park reopened, items restored
    targets = [c.args[2] for c in world.job_repo.transition.mock_calls]
    assert targets == [JobStatus.pending, JobStatus.pending_review]
    world.repo.reopen_park.assert_awaited_once_with(world.repo.reopen_park.call_args.args[0],
                                                    world.park.id)
    assert world.repo.restore_open.call_args.args[1] == [uuid.UUID(int=2)]
    world.job_repo.mark_launch_failed.assert_not_called()

    world.dispatch.side_effect = None                       # the retry
    world.dispatch.reset_mock()
    retry = await world.call("POST", "/review/run")
    assert retry.status_code == 202
    assert world.dispatch.call_args.args[2]["review_continuation"] is True


async def test_a_park_closed_under_the_unreviewed_marking_is_a_409():
    from meshpipeline.persistence.repositories.repair_review_repository import ParkClosedError
    world = World([_item(1)])
    world.repo.record_decision.side_effect = ParkClosedError("closed")
    assert (await world.call("POST", "/review/run")).status_code == 409
    world.dispatch.assert_not_called()


async def test_run_resets_the_reused_row_beside_the_status_cas():
    world = World([_item(1, state="accepted", decided=1)])
    order = []
    world.job_repo.transition.side_effect = lambda *a, **k: order.append("cas") or \
        TransitionResult.applied
    world.job_repo.reset_for_review_rerun.side_effect = lambda *a, **k: order.append("reset")
    assert (await world.call("POST", "/review/run")).status_code == 202
    assert order == ["cas", "reset"]
    kw = world.job_repo.reset_for_review_rerun.call_args.kwargs
    assert kw["started_at"] == _T0 and kw["ended_at"] == _T0 + timedelta(minutes=20)


async def test_the_compensation_can_only_pull_back_a_job_nobody_claimed():
    world = World([_item(1, state="accepted", decided=1)])
    world.dispatch.side_effect = RuntimeError("broker down")
    assert (await world.call("POST", "/review/run")).status_code == 503
    back = world.job_repo.transition.mock_calls[-1]
    assert back.args[2] == JobStatus.pending_review
    assert set(back.kwargs["allow"]) == {JobStatus.pending}, "a running job could be pulled back"


def _fill_interpretation(world, targets):
    world.interpret.return_value = SimpleNamespace(
        kind="directive", playback="i will fill every one of these holes", reason="",
        scope="group", directive={"op": "fill_planar_hole", "targets": list(targets),
                                  "defect_code": "open_shell", "defect_fingerprint": targets[0],
                                  "source": "engineer", "item_id": ""})


async def test_a_group_redirect_never_targets_a_paired_member_and_says_so():
    g = "open_shell:fill_planar_hole"
    items = [_item(1, group=g), _item(2, group=g), _item(3, group=g, pairing="p"),
             _item(4, group=g, pairing="p")]
    world = World(items)
    _fill_interpretation(world, [i.fingerprint for i in items])
    r = await world.call("POST", "/review/decision",
                         json={"action": "redirect", "text": "fill all of them",
                               "item_id": str(uuid.UUID(int=1))})
    body = r.json()
    assert body["kind"] == "directive"
    assert "2 paired defects" in body["playback"] and "own review" in body["playback"]
    recorded = {c.args[1]: (c.args[2], c.args[3]) for c in world.repo.record_decision.mock_calls}
    state, decision = recorded[uuid.UUID(int=1)]
    assert state == "redirected"
    assert decision["directive"]["targets"] == ["edge:0001", "edge:0002"]
    # no fill directive anywhere names a paired member
    paired = {"edge:0003", "edge:0004"}
    for _state, d in recorded.values():
        assert not paired & set((d.get("directive") or {}).get("targets") or ())
    assert uuid.UUID(int=3) not in recorded and uuid.UUID(int=4) not in recorded
    # the covered member is recorded as answered by the group redirect, with no directive of its own
    member_state, member = recorded[uuid.UUID(int=2)]
    assert member_state == "redirected" and member["covered_by"] == str(uuid.UUID(int=1))
    assert "directive" not in member
    assert body["covered"] == [str(uuid.UUID(int=2))]


async def test_a_group_redirect_given_on_a_paired_defect_is_refused_and_records_nothing():
    g = "open_shell:fill_planar_hole"
    items = [_item(3, group=g, pairing="p"), _item(4, group=g, pairing="p")]
    world = World(items)
    _fill_interpretation(world, [i.fingerprint for i in items])
    r = await world.call("POST", "/review/decision",
                         json={"action": "redirect", "text": "fill both",
                               "item_id": str(uuid.UUID(int=3))})
    assert r.json()["kind"] == "refusal" and "pair" in r.json()["playback"]
    world.repo.record_decision.assert_not_called()


async def test_the_covered_members_go_through_as_answered_not_unreviewed_and_add_no_directive():
    g = "small_edge:fix_small_edges"
    redirect = {"op": "sew_faces", "targets": ["edge:0001", "edge:0002"],
                "defect_code": "small_edge", "defect_fingerprint": "edge:0001",
                "source": "engineer", "item_id": str(uuid.UUID(int=1))}
    items = [_item(1, group=g, state="redirected", decided=1, decision={"directive": redirect}),
             _item(2, group=g, state="redirected", decided=1,
                   decision={"covered_by": str(uuid.UUID(int=1))})]
    world = World(items)
    assert (await world.call("POST", "/review/run")).status_code == 202
    directives = world.dispatch.call_args.args[2]["repair_directives"]
    assert [d["targets"] for d in directives] == [["edge:0001", "edge:0002"]]
    assert not [c for c in world.repo.record_decision.mock_calls if c.args[2] == "unreviewed"]


async def test_a_new_answer_on_the_redirected_item_reopens_the_members_it_covered():
    g = "small_edge:fix_small_edges"
    items = [_item(1, group=g, state="redirected", decided=1, decision={"directive": {}}),
             _item(2, group=g, state="redirected", decided=1,
                   decision={"covered_by": str(uuid.UUID(int=1))})]
    world = World(items)
    world.repo.reopen_covered = AsyncMock(return_value=1)
    await world.call("POST", "/review/decision",
                     json={"action": "leave", "item_id": str(uuid.UUID(int=1))})
    world.repo.reopen_covered.assert_awaited_once()
    assert world.repo.reopen_covered.call_args.args[1] == [uuid.UUID(int=2)]


async def test_apply_all_after_a_sample_redirect_repeats_that_redirect_on_each_remaining_member():
    sew = {"op": "sew_faces", "targets": ["edge:0002"], "defect_code": "small_edge",
           "defect_fingerprint": "edge:0002", "source": "engineer", "item_id": "x"}
    items = [_item(1, state="accepted", decided=1),
             _item(2, state="redirected", decided=5, decision={"directive": sew}),
             _item(3), _item(4)]
    world = World(items)
    r = await world.call("POST", "/review/decision",
                         json={"action": "apply_all", "group_key": items[0].group_key})
    assert r.json() == {"kind": "applied", "changed": 2, "direction": "redirect"}
    world.repo.record_group_decision.assert_not_called()      # never the proposal
    recorded = {c.args[1]: (c.args[2], c.args[3]) for c in world.repo.record_decision.mock_calls}
    assert set(recorded) == {uuid.UUID(int=3), uuid.UUID(int=4)}
    for n in (3, 4):
        state, decision = recorded[uuid.UUID(int=n)]
        assert state == "redirected" and decision["directive"]["op"] == "sew_faces"
        assert decision["directive"]["targets"] == [f"edge:{n:04d}"]
        assert decision["directive"]["item_id"] == str(uuid.UUID(int=n))


async def test_apply_all_after_an_all_leave_sample_leaves_the_rest():
    items = [_item(1, state="left", decided=1), _item(2, state="left", decided=2),
             _item(3), _item(4)]
    world = World(items)
    r = await world.call("POST", "/review/decision",
                         json={"action": "apply_all", "group_key": items[0].group_key})
    assert r.json()["direction"] == "leave"
    _db, _job, _group, state, decision = world.repo.record_group_decision.call_args.args
    assert state == "left" and decision["direction"] == "leave"


async def test_apply_all_after_an_accepted_sample_accepts_the_proposal_and_needs_a_sample():
    items = [_item(1, state="left", decided=1), _item(2, state="accepted", decided=2), _item(3)]
    world = World(items)
    r = await world.call("POST", "/review/decision",
                         json={"action": "apply_all", "group_key": items[0].group_key})
    assert r.json()["direction"] == "accept"
    assert world.repo.record_group_decision.call_args.args[3] == "accepted"
    unanswered = World([_item(1), _item(2), _item(3)])
    r = await unanswered.call("POST", "/review/decision",
                              json={"action": "apply_all", "group_key": items[0].group_key})
    assert r.status_code == 409
    unanswered.repo.record_group_decision.assert_not_called()


def test_an_approved_fill_survives_the_next_cycles_replay(monkeypatch):
    # cycle 1: the engineer approved a fill on one rim of a pairing and it was kept. cycle 2
    # replays the park's kept operations first - the fill must replay as the engineer's.
    import meshpipeline.cad.repair.directives as directives
    from meshpipeline.application.repair_review_minting import directive_outcomes
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect
    from meshpipeline.cad.repair.plan import RunHistory
    approved = str(uuid.UUID(int=7))
    kept = [{"name": "fill_planar_hole", "status": "kept", "targets": ["rim:a"],
             "defect_code": "open_shell", "source": "engineer", "item_id": approved},
            {"name": "sew_faces", "status": "kept", "targets": ["edge:x"],
             "defect_code": "open_shell", "source": "directive"}]
    replayed = rr._replay_directives(kept)
    assert [(d["source"], d["item_id"]) for d in replayed] == [("engineer", approved),
                                                               ("replay", "")]
    monkeypatch.setattr(directives, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"rim:a", "rim:b"}))
    rims = tuple(EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                              entity_type="edge", entity_index=i, message="rim",
                              fingerprint=fp, measurements={"boundary_edges": 4})
                 for i, fp in enumerate(("rim:a", "rim:b")))

    class _Nothing:
        def propose(self, *a, **k): return None
    planner = directives.DirectedPlanner(replayed[:1], _Nothing())
    candidate = planner.propose(rims, RunHistory(), shape="shape")
    assert candidate is not None and planner.unplayable == [], planner.unplayable
    assert candidate.item_id == approved and candidate.source == "engineer"
    # and its outcome writes back onto the item that approved it
    ops = [{"name": "fill_planar_hole", "status": "kept", "targets": ["rim:a"],
            "item_id": approved, "source": "engineer"}]
    (outcome,) = directive_outcomes(replayed[:1], ops, [])
    assert outcome["item_id"] == approved and outcome["outcome"] == "kept"


async def test_a_run_delivered_after_a_park_serves_its_offered_items_not_the_closed_park():
    closed = SimpleNamespace(**{**vars(_park()), "status": "closed"})
    world = World([], park=closed, job="succeeded")
    offered = [_item(9, state="open")]
    asked = []

    async def _items(db, job_id, park_id):
        asked.append(park_id)
        return offered if park_id is None else [_item(1, state="left")]
    world.repo.items_for = AsyncMock(side_effect=_items)
    body = (await world.call("GET", "/review")).json()
    assert asked == [None]
    assert body["status"] == "offered" and body["open"] == 1
    assert body["groups"][0]["items"][0]["id"] == str(uuid.UUID(int=9))


async def test_the_review_says_how_many_the_mint_cap_left_out():
    world = World([_item(1), _item(2)])
    world.job.final_result = {"review_parked": 2, "review_truncated": 700}
    body = (await world.call("GET", "/review")).json()
    assert (body["truncated"], body["total"]) == (700, 702)
    plain = (await World([_item(1)]).call("GET", "/review")).json()
    assert (plain["truncated"], plain["total"]) == (0, 1)
