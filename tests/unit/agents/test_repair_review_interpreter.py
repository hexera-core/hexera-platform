# Responsibility: Verify the redirect interpreter yields catalogue directives or honest refusals.
# Boundaries: the model seam is stubbed through set_model_router; nothing is executed.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meshpipeline.agents.repair_review.interpreter import interpret
from meshpipeline.contracts import model_inference


class _Router:
    def __init__(self, reply):
        self.reply = reply

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        return SimpleNamespace(ok=True, assistant_text=json.dumps(self.reply))


GROUP = [{"id": f"it{i}", "defect_code": "open_boundary", "fingerprint": f"fp{i}",
          "entity_type": "edge", "entity_index": i, "measurements": {}, "message": "m",
          "proposal": None} for i in range(3)]


@pytest.mark.asyncio
async def test_a_redirect_becomes_a_group_scoped_directive_with_playback():
    model_inference.set_model_router(_Router(
        {"kind": "directive", "op": "fill_planar_hole", "scope": "group",
         "playback": "I will cap every one of these holes."}))
    got = await interpret("fill all of these", GROUP[0], GROUP, job_id="j")
    assert got.kind == "directive" and got.scope == "group"
    assert got.playback == "I will cap every one of these holes."
    assert got.directive == {
        "op": "fill_planar_hole", "targets": ["fp0", "fp1", "fp2"],
        "defect_code": "open_boundary", "defect_fingerprint": "fp0",
        "source": "engineer", "item_id": "it0"}


@pytest.mark.asyncio
async def test_a_redirect_the_catalogue_cannot_express_is_an_honest_refusal():
    model_inference.set_model_router(_Router(
        {"kind": "refusal", "reason": "moving the edge is not something i can do"}))
    got = await interpret("move that edge 2mm left", GROUP[0], GROUP, job_id="j")
    assert got.kind == "refusal"
    assert "cannot move, redraw or delete geometry" in got.reason
    assert got.playback


@pytest.mark.asyncio
async def test_an_operation_outside_the_catalogue_is_a_refusal():
    model_inference.set_model_router(_Router(
        {"kind": "directive", "op": "delete_face", "scope": "item", "playback": "x"}))
    got = await interpret("delete it", GROUP[0], GROUP, job_id="j")
    assert got.kind == "refusal" and "delete_face" in got.reason


@pytest.mark.asyncio
async def test_a_directive_with_nothing_to_target_is_a_refusal():
    model_inference.set_model_router(_Router(
        {"kind": "directive", "op": "sew_faces", "scope": "item", "playback": "x"}))
    bare = {**GROUP[0], "fingerprint": ""}
    got = await interpret("sew it", bare, [bare], job_id="j")
    assert got.kind == "refusal"
