# Responsibility: Verify the review proposer batches, caps, declines and degrades without raising.
# Boundaries: the model seam is stubbed through set_model_router; nothing is executed.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.agents.repair_review.proposer import propose_for_groups
from meshpipeline.contracts import model_inference


class _Router:
    def __init__(self, reply, delay=0.0):
        self.reply, self.delay, self.calls = reply, delay, 0

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(
            ok=True, assistant_text=self.reply if isinstance(self.reply, str)
            else json.dumps(self.reply))


def _items(groups: int, per: int = 1) -> list[dict]:
    return [{"group_key": f"c{g}:none", "defect_code": f"c{g}", "severity": "error",
             "entity_type": "edge", "entity_index": i, "fingerprint": f"fp{g}-{i}",
             "measurements": {}, "message": "m", "origin": "triage", "pairing_key": "",
             "proposal": None, "camera": None}
            for g in range(groups) for i in range(per + g % 2)]


@pytest.fixture
def declines(monkeypatch):
    rows = []
    import meshpipeline.capture.logger as cl
    monkeypatch.setattr(cl.TrainingLogger, "log",
                        lambda self, ev, payload, **kw: rows.append((ev, payload, kw)))
    return rows


@pytest.mark.asyncio
async def test_proposals_are_batched_per_group_and_capped(monkeypatch, declines):
    router = _Router({"op": "sew_faces", "rationale": "The Faces Look Unstitched."})
    model_inference.set_model_router(router)
    out = await propose_for_groups(_items(9), job_id="j")
    assert router.calls == rcfg.CAD_REPAIR_REVIEW_LLM_CALLS_MAX == 4
    filled = [i for i in out if i["proposal"]]
    assert {i["defect_code"] for i in filled} == {"c1", "c3", "c5", "c7"}
    p = filled[0]["proposal"]
    assert p["provenance"] == "llm" and p["op"] == "sew_faces"
    assert p["rationale"] == "the faces look unstitched." and len(p["rationale"]) <= 200


@pytest.mark.asyncio
async def test_an_unknown_operation_from_the_model_is_a_decline_not_a_proposal(declines):
    model_inference.set_model_router(_Router({"op": "delete_everything", "rationale": "x"}))
    items = _items(1)
    out = await propose_for_groups(items, job_id="j")
    assert all(i["proposal"] is None for i in out)
    assert declines and declines[0][0] == "repair_review"
    assert declines[0][2]["op_id"] == "review:propose"


@pytest.mark.asyncio
async def test_a_timeout_or_garbage_reply_is_a_decline(monkeypatch, declines):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_LLM_TIMEOUT_S", 0.01)
    model_inference.set_model_router(_Router({"op": "sew_faces"}, delay=0.5))
    out = await propose_for_groups(_items(1), job_id="j")
    assert all(i["proposal"] is None for i in out) and len(declines) == 1
    model_inference.set_model_router(_Router("not json"))
    await propose_for_groups(_items(1), job_id="j")
    assert len(declines) == 2


@pytest.mark.asyncio
async def test_items_with_a_rules_proposal_are_left_alone(declines):
    router = _Router({"op": "sew_faces", "rationale": "r"})
    model_inference.set_model_router(router)
    items = _items(1)
    for i in items:
        i["proposal"] = {"source": "rules", "op": "fix_small_edges"}
    out = await propose_for_groups(items, job_id="j")
    assert router.calls == 0 and out == items


@pytest.mark.asyncio
async def test_no_router_degrades_without_raising(declines):
    from meshpipeline.agents.repair_review.interpreter import interpret
    model_inference.set_model_router(None)
    items = _items(2)
    assert await propose_for_groups(items, job_id="j") == items
    got = await interpret("fill it", items[0], items, job_id="j")
    assert got.kind == "refusal" and "without a model" in got.reason


@pytest.mark.asyncio
async def test_parking_fills_missing_proposals_before_persisting(monkeypatch, declines):
    from meshpipeline.application import repair_review_park as rrp
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    model_inference.set_model_router(_Router({"op": "fill_planar_hole", "rationale": "a flat gap"}))
    state = {"repair_review_candidates": _items(1), "session_id": "s"}
    from meshpipeline.persistence.models import JobStatus
    plan = rrp.plan_review(state, status=JobStatus.failed)
    assert plan.park and all(i["proposal"] is None for i in plan.items)
    plan = await rrp.with_proposals(plan, "j")
    assert plan.park and all(i["proposal"]["op"] == "fill_planar_hole" for i in plan.items)

    class Boom:
        async def call_repair_review_model(self, *a, **k):
            raise RuntimeError("down")
    model_inference.set_model_router(Boom())
    same = await rrp.with_proposals(rrp.plan_review(state, status=JobStatus.failed), "j")
    assert same.park and all(i["proposal"] is None for i in same.items)
