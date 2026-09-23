# Responsibility: Verify the survey reaches the mesh planner after the request cut only when it passes the package's own contract, and that a confirmed budget holds the mesh to it.
# Boundaries: the planner's allowlist, validator call and note, and the drivers' ceiling; composing the survey is the application's.
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from tests.unit.engines.test_planner_look_block import BLOCK, _captured

import meshpipeline.cad.regions as regions
import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.geometry_agent_block import confirmed_cell_cap
from meshpipeline.engines.snappy import drivers

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"


def _metrics(user: str) -> dict:
    start = user.index("MEASURED GEOMETRY (metres):\n") + len("MEASURED GEOMETRY (metres):\n")
    return json.JSONDecoder().raw_decode(user[start:])[0]


@pytest.fixture
def surveyed_block():
    """A real block with a real survey: bend_elbow_001, composed for the corpus brief, both roles
    answered by the customer, the way `application/geometry_survey.py` hands it to the planner."""
    pytest.importorskip("geometry_agent.contract.deliver",
                        reason="the measurement package is not on this interpreter's path")
    from meshpipeline.application import geometry_survey as gs

    doc = json.loads((FIXTURES / "bend_elbow_001.json").read_text(encoding="utf-8"))
    brief = (FIXTURES / "bend_elbow_001.brief.txt").read_text(encoding="utf-8")
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=brief))
    said = "o2 in, o1 out"
    state = gs.record_answer(state, question_id="role_inlet", choice="o2", words="o2 in", latest_user_message=said)
    state = gs.record_answer(state, question_id="role_outlet", choice="o1", words="o1 out", latest_user_message=said)
    return gs.builder_block(state)


def test_a_block_with_no_survey_sends_exactly_what_it_sent_before(monkeypatch, tmp_path):
    seen = _captured(monkeypatch, tmp_path, geometry_agent=dict(BLOCK))
    assert "survey" not in _metrics(seen["user"])["geometry_agent"]
    assert 'ABOUT "survey"' not in seen["user"]


def test_the_survey_rides_after_the_cut_and_carries_the_customers_answers(monkeypatch, tmp_path, surveyed_block):
    long_brief = "x" * 5000
    seen = _captured(monkeypatch, tmp_path, geometry_agent=surveyed_block, request_txt=long_brief)
    agent = _metrics(seen["user"])["geometry_agent"]
    assert agent["survey"]["confirmed"]["opening.role=inlet"]["applies_to"] == ["o2"]
    assert agent["customer_cell_cap"] == 2_000_000
    assert seen["user"].count("x" * 2000) == 1 and "x" * 2001 not in seen["user"]
    assert 'ABOUT "survey" INSIDE "geometry_agent"' in seen["user"]


def test_a_survey_that_breaks_the_contract_is_refused_and_the_measurement_still_arrives(monkeypatch, tmp_path,
                                                                                       surveyed_block):
    broken = copy.deepcopy(surveyed_block)
    broken["survey"]["seen"]["identity.name"] = {"kind": "seen", "value": "a 90 degree elbow",
                                                 "tier": "candidate"}
    seen = _captured(monkeypatch, tmp_path, geometry_agent=broken)
    agent = _metrics(seen["user"])["geometry_agent"]
    assert "survey" not in agent
    assert agent["inlet_bore_m"] == broken["inlet_bore_m"]
    assert 'ABOUT "survey"' not in seen["user"]


def test_an_oversized_survey_is_refused_rather_than_cut(monkeypatch, tmp_path, surveyed_block):
    broken = copy.deepcopy(surveyed_block)
    broken["survey"]["unsettled"] = [{"about": "opening.role", "subjects": ["o1"], "why": "y" * 5000}]
    seen = _captured(monkeypatch, tmp_path, geometry_agent=broken)
    assert "survey" not in _metrics(seen["user"])["geometry_agent"]


def test_with_no_validator_to_vouch_for_it_the_survey_is_refused(monkeypatch, tmp_path, surveyed_block):
    monkeypatch.setitem(sys.modules, "geometry_agent.contract.deliver", None)
    seen = _captured(monkeypatch, tmp_path, geometry_agent=copy.deepcopy(surveyed_block))
    assert "survey" not in _metrics(seen["user"])["geometry_agent"]


# cell_cap HAS A READER


def test_only_a_confirmed_budget_is_read_as_the_customers_cap():
    def block(mark):
        return {"survey": {"confirmed": {"cell_budget": mark}}}

    assert confirmed_cell_cap(block({"kind": "confirmed", "value": 750_000})) == 750_000
    assert confirmed_cell_cap(block({"kind": "stated", "value": 750_000})) is None
    assert confirmed_cell_cap(block({"kind": "assumed", "value": 750_000})) is None
    assert confirmed_cell_cap(block({"kind": "confirmed", "value": "hold 750,000"})) is None
    assert confirmed_cell_cap(block({"kind": "confirmed", "value": True})) is None
    assert confirmed_cell_cap({"status": "ok"}) is None
    assert confirmed_cell_cap(None) is None


def _ceiling(monkeypatch, block, *, hard=4_000_000):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", hard)

    async def agent_block(_state):
        return block

    # the step has no upload to read for this state, so the driver falls back to the block the
    # measurement composed, which is the one supplied here
    monkeypatch.setattr(regions, "agent_block_for_state", agent_block)
    # the ceiling is read off the inputs the driver already holds for this call, so it is asked of one
    # of those rather than of a free function: `_ForThisRun` is what every call site builds
    return asyncio.run(drivers._ForThisRun({}).ceiling())


def test_no_block_at_all_is_the_compute_limit(monkeypatch):
    assert _ceiling(monkeypatch, None) == 4_000_000


def test_a_budget_the_customer_confirmed_holds_the_mesh_to_it(monkeypatch):
    confirmed = {"survey": {"confirmed": {"cell_budget": {"kind": "confirmed", "value": 100_000}}}}
    assert _ceiling(monkeypatch, confirmed) == 100_000


def test_a_budget_the_customer_only_wrote_does_not_starve_the_mesh(monkeypatch):
    stated = {"survey": {"confirmed": {}, "measured": {"cell_budget": {"kind": "stated", "value": 100_000}}}}
    assert _ceiling(monkeypatch, stated) == 4_000_000


def test_a_confirmed_budget_above_the_compute_limit_never_raises_it(monkeypatch):
    confirmed = {"survey": {"confirmed": {"cell_budget": {"kind": "confirmed", "value": 9_000_000}}}}
    assert _ceiling(monkeypatch, confirmed) == 4_000_000


def test_a_ceiling_that_cannot_be_read_is_the_compute_limit(monkeypatch):
    async def boom(_state):
        raise RuntimeError("the row could not be read")

    monkeypatch.setattr(regions, "agent_block_for_state", boom)
    assert asyncio.run(drivers._ForThisRun({}).ceiling()) == polcfg.CELL_HARD_LIMIT
