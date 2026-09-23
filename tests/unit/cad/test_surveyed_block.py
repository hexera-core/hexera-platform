# Responsibility: Verify step 7 of the chain: the planner gets the block composed for the customer with the survey in it, and the measurement's own block whenever there is no survey to give.
# Boundaries: cad.regions.agent_block_for_state over stand-in rows; the survey's contents are the application's tests.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meshpipeline.cad import regions

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
REF = {"source_id": "11111111-1111-4111-8111-111111111111", "owner_id": "owner-7f3a", "object_key": "k",
       "size_bytes": 1, "original_filename": "bend_elbow_001.step", "suffix_hint": ".step"}


def _doc() -> dict:
    return json.loads((FIXTURES / "bend_elbow_001.json").read_text(encoding="utf-8"))


def _state_for(doc: dict) -> dict:
    return {"geometry": {"ref": {**REF, "sha256": doc["source_sha256"]}}}


@pytest.fixture
def rows(monkeypatch):
    pytest.importorskip("geometry_agent.contract.deliver",
                        reason="the measurement package is not on this interpreter's path")
    from meshpipeline.application import geometry_survey as gs

    doc = _doc()
    stored: dict = {"survey": None}

    async def document(_ref, **_kw):
        return doc

    async def load(owner_id, source_id, *, sha256):
        state = stored["survey"]
        return state if state and state["sha256"] == sha256 else None

    monkeypatch.setattr(regions, "stored_document_for_source", document)
    monkeypatch.setattr(gs, "load", load)
    return doc, stored, gs


def _answered(gs, doc) -> dict:
    brief = (FIXTURES / "bend_elbow_001.brief.txt").read_text(encoding="utf-8")
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=brief))
    said = "o2 in, o1 out"
    state = gs.record_answer(state, question_id="role_inlet", choice="o2", words="o2 in", latest_user_message=said)
    return gs.record_answer(state, question_id="role_outlet", choice="o1", words="o1 out", latest_user_message=said)


def test_the_planner_gets_the_block_composed_for_the_customer(rows):
    doc, stored, gs = rows
    stored["survey"] = _answered(gs, doc)
    block = asyncio.run(regions.agent_block_for_state(_state_for(doc)))
    assert block["survey"]["confirmed"]["opening.role=inlet"]["applies_to"] == ["o2"]
    assert block["customer_cell_cap"] == 2_000_000
    assert doc["planner_block"]["customer_cell_cap"] is None


def test_with_no_survey_stored_the_planner_gets_the_measurements_own_block(rows):
    """A row measured but never surveyed is an absence, not a failure: the planner is handed the
    measurement's own block and no `survey` key, exactly as it was before the survey existed."""
    doc, _stored, _gs = rows
    block = asyncio.run(regions.agent_block_for_state(_state_for(doc)))
    assert block == doc["planner_block"]
    assert "survey" not in block


def test_a_look_that_landed_after_the_last_composition_is_composed_in_before_the_planner_reads(rows):
    doc, stored, gs = rows
    stored["survey"] = _answered(gs, doc)
    assert stored["survey"]["composed_for"]["look_status"] != "ok"
    doc["look"] = {**(doc.get("look") or {}), "status": "ok",
                   "impression": {"attachments": ["a flange at each end"], "internal_features": [],
                                  "openings_seen": []}}
    block = asyncio.run(regions.agent_block_for_state(_state_for(doc)))
    assert block["survey"]["looked"] is True
    assert block["survey"]["seen"]["attachments"]["value"] == ["a flange at each end"]
    assert block["survey"]["confirmed"]["opening.role=outlet"]["applies_to"] == ["o1"]
