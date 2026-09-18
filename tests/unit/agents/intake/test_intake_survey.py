# Responsibility: Verify intake runs the Surveyor's half of the chain only when armed: the prompt, the two tools, the recorded answers and the submission gate.
# Boundaries: the intake executor and prompt over the real measurement package; the database is an in-memory stand-in.
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import meshpipeline.agents.intake.executor as ex_mod
import meshpipeline.settings.policy as polcfg
from meshpipeline.agents.intake import geometry_brief as gb
from meshpipeline.agents.intake.agent import INTAKE_TOOLS, SURVEY_TOOLS
from meshpipeline.agents.intake.executor import (
    INTAKE_CATEGORIES,
    IntakeExecutionState,
    IntakeToolExecutor,
    category_of,
)
from meshpipeline.contracts.geometry_source import GeometrySourceRef

FIXTURES = Path(__file__).parents[3] / "fixtures" / "geometry_survey"


def _doc(case: str = "bend_elbow_001") -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))


def _brief(case: str = "bend_elbow_001") -> str:
    return (FIXTURES / f"{case}.brief.txt").read_text(encoding="utf-8")


# WITH THE SURVEY OFF, NOTHING MOVES


def test_the_survey_is_off_by_default_and_needs_both_gates_under_it():
    assert polcfg.GEOMETRY_SURVEY_ENABLED is False


def test_the_tool_list_intake_is_offered_is_untouched():
    names = [t["function"]["name"] for t in INTAKE_TOOLS]
    assert names == ["web_search", "recommend_compatible_engines", "propose_engine_selection",
                     "confirm_engine_selection", "preview_selected_admission", "submit_requirements"]
    assert not {t["function"]["name"] for t in SURVEY_TOOLS} & set(names)
    assert set(INTAKE_CATEGORIES) == set(names)
    assert category_of("survey_the_part") == "survey" == category_of("answer_survey_question")


def test_with_the_gate_off_the_survey_is_never_imported(monkeypatch):
    from meshpipeline.agents.intake.agent import _survey_for

    monkeypatch.setattr(polcfg, "GEOMETRY_SURVEY_ENABLED", False)
    name = "meshpipeline.application.geometry_survey"
    was = sys.modules.pop(name, None)
    try:
        armed, state = asyncio.run(_survey_for(object(), _doc()))
        assert (armed, state) == (False, None)
        assert name not in sys.modules
    finally:
        if was is not None:
            sys.modules[name] = was


def test_the_unarmed_block_is_the_block_that_shipped():
    doc = _doc()
    assert gb.render_block(doc) == gb.render_block(doc, armed=False)
    text = gb.render_block(doc)
    assert "Surveyor" not in text and "WHICH of two openings of the same size" in text


# ARMED: STEP 1 BEFORE STEP 2


def test_before_the_customer_says_what_it_is_for_the_representation_is_not_stated():
    """Measured at upload the file is `wall_shell` for the purpose the upload ASSUMED. Until the customer
    has said what it is for, that word is not theirs to be told, and the block says how to get the
    survey instead."""
    doc = _doc()
    assert doc["representation"] == "wall_shell"
    text = gb.render_block(doc, armed=True)
    assert "the file holds:" not in text
    assert "call survey_the_part" in text
    assert "WHICH of two openings of the same size" not in text


# THE WHOLE CONVERSATION HALF, THROUGH THE EXECUTOR


@pytest.fixture
def armed(monkeypatch):
    pytest.importorskip("geometry_agent.contract.deliver",
                        reason="the measurement package is not on this interpreter's path")
    from meshpipeline.application import geometry_survey as gs

    for gate in ("GEOMETRY_MEASUREMENT_ENABLED", "GEOMETRY_REPORT_READERS_ENABLED", "GEOMETRY_SURVEY_ENABLED"):
        monkeypatch.setattr(polcfg, gate, True)
    store: dict = {}

    async def load(owner_id, source_id, *, sha256):
        state = store.get((owner_id, source_id))
        return state if state and state.get("sha256") == sha256 else None

    async def save(owner_id, source_id, state, *, session_id=""):
        store[(owner_id, source_id)] = state
        return True

    async def interpretation(owner_id, source_id):
        return None, None, None

    monkeypatch.setattr(gs, "load", load)
    monkeypatch.setattr(gs, "save", save)
    monkeypatch.setattr(gs, "_interpretation", interpretation)
    monkeypatch.setattr(gs, "_queue_the_look", lambda *a, **k: "off")
    doc = _doc()
    ref = GeometrySourceRef(source_id="11111111-1111-4111-8111-111111111111", owner_id="owner-7f3a",
                            object_key="k", sha256=doc["source_sha256"], size_bytes=1,
                            original_filename="bend_elbow_001.step", suffix_hint=".step")
    st = IntakeExecutionState(session_id="22222222-2222-4222-8222-222222222222", owner_id="owner-7f3a",
                              revision="r1", user_msg_count=1, latest_user_msg=_brief(),
                              geometry_document=doc, survey_armed=True, survey_source_ref=ref,
                              customer_messages=(_brief(),))
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"],
                            search_tool=lambda *a, **k: "")
    return st, ex, store, gs


def _run(ex, tool, **args):
    return asyncio.run(ex.run(tool, args))


def test_the_tools_refuse_when_the_survey_is_not_armed():
    st = IntakeExecutionState(session_id="s", owner_id="u")
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"], search_tool=lambda *a, **k: "")
    for tool in ("survey_the_part", "answer_survey_question"):
        result = _run(ex, tool, question_id="x", customer_words_verbatim="x")
        assert result.accepted is False and "not available" in result.content


def test_survey_the_part_needs_the_customers_own_words(armed):
    _st, ex, store, _gs = armed
    result = _run(ex, "survey_the_part", purpose="Internal CFD",
                  customer_words_verbatim="an internal flow study of a manifold")
    assert result.accepted is False and "not in anything the customer wrote" in result.content
    assert store == {}


def test_step_one_to_step_four_composes_stores_and_hands_intake_the_surveyors_questions(armed):
    st, ex, store, gs = armed
    result = _run(ex, "survey_the_part", purpose="Internal CFD",
                  customer_words_verbatim="Internal flow through a circular elbow duct")
    assert result.accepted is True
    assert "[role_inlet]" in result.content and "[role_outlet]" in result.content
    assert "options: o1, o2, o3, o4" in result.content
    (saved,) = store.values()
    assert saved["composed_for"]["purpose"] == "internal_cfd"
    assert saved["asked"] == ["role_inlet", "role_outlet"]
    assert st.geometry_survey is saved
    # the next turn's system prompt carries the same questions and the representation composed for them
    text = gb.render_block(st.geometry_document, survey=saved, armed=True)
    assert "the file holds: the solid WALL" in text and "[role_inlet]" in text
    assert "

## THE SURVEYOR'S QUESTIONS" in text


def test_an_answer_is_recorded_with_who_gave_it_and_the_next_question_follows(armed):
    st, ex, store, gs = armed
    _run(ex, "survey_the_part", purpose="Internal CFD",
         customer_words_verbatim="Internal flow through a circular elbow duct")
    st.latest_user_msg = "o2 is where the water comes in"
    refused = _run(ex, "answer_survey_question", question_id="role_inlet", option="o2",
                   customer_words_verbatim="o2 is the inlet")
    assert refused.accepted is False and "Not recorded" in refused.content
    ok = _run(ex, "answer_survey_question", question_id="role_inlet", option="o2",
              customer_words_verbatim="o2 is where the water comes in")
    assert ok.accepted is True and "o2 is the inlet" in ok.content and "[role_outlet]" in ok.content
    (saved,) = store.values()
    row = saved["answers"][-1]
    assert (row["answered_by"], row["principal"], row["subject"], row["value"]) == (
        "customer", "owner-7f3a", "o2", "inlet")


def test_submission_is_refused_until_the_customer_has_named_the_mouths(armed, monkeypatch):
    st, ex, _store, _gs = armed
    _run(ex, "survey_the_part", purpose="Internal CFD",
         customer_words_verbatim="Internal flow through a circular elbow duct")
    patches = [{"name": "inlet", "type": "inlet", "diameter_mm": 297.94, "near_mm": [0, 0, 0]},
               {"name": "outlet", "type": "outlet", "diameter_mm": 297.94, "near_mm": [790.64, 787.28, 0]},
               {"name": "wall", "type": "wall"}]
    args = {"purpose": "internal_cfd", "patches": patches}
    assert len(asyncio.run(ex._survey_gate(args))) == 2
    st.latest_user_msg = "o2 in, o1 out"
    _run(ex, "answer_survey_question", question_id="role_inlet", option="o2", customer_words_verbatim="o2 in")
    _run(ex, "answer_survey_question", question_id="role_outlet", option="o1", customer_words_verbatim="o1 out")
    assert asyncio.run(ex._survey_gate(args)) == []


def test_the_whole_submission_stops_at_an_unconfirmed_role(armed, monkeypatch):
    st, ex, _store, _gs = armed
    monkeypatch.setattr(ex_mod, "validate_submission", lambda args: [])
    monkeypatch.setattr(ex_mod.at, "verify_for_submit", lambda *a, **k: (True, ""))
    patches = [{"name": "inlet", "type": "inlet", "diameter_mm": 297.94, "near_mm": [0, 0, 0]},
               {"name": "outlet", "type": "outlet", "diameter_mm": 297.94, "near_mm": [790.64, 787.28, 0]},
               {"name": "wall", "type": "wall"}]
    result = _run(ex, "submit_requirements", purpose="internal_cfd", patches=patches, mesh_engine="snappy",
                  input_kind="body-surface", dimensionality="3D", engine_params={}, request_txt="r",
                  review_brief_txt="b", domain="d", engine_source="user", preview_token="t")
    assert result.accepted is False and "a port role must be the customer's" in result.content
    assert st.submit_args is None and st.approval is None


def test_a_submission_that_never_surveyed_is_surveyed_at_the_gate(armed):
    """The model may skip survey_the_part. The gate composes the survey from the submission itself, with
    the ports as the customer's declaration, so the chain cannot be skipped by forgetting a tool."""
    st, ex, store, _gs = armed
    assert store == {}
    args = {"purpose": "internal_cfd", "patches": [
        {"name": "inlet", "type": "inlet", "diameter_mm": 297.94, "near_mm": [0, 0, 0]},
        {"name": "wall", "type": "wall"}]}
    problems = asyncio.run(ex._survey_gate(args))
    (saved,) = store.values()
    assert saved["composed_for"]["declared"][0]["name"] == "inlet"
    assert problems and all("role_count" in p for p in problems)


# THE LOOK'S PLACED FINDINGS REACH THE CONVERSATION


def _looked(at_places):
    doc = _doc()
    doc["look"] = {"status": "ok", "impression": {"looks_like": "a pipe elbow"}}
    doc["planner_block"] = {**doc["planner_block"], "look": {
        "relied_on": {"inside_is_plain": True}, "candidates": {}, "at_places": at_places}}
    return doc


def test_the_findings_the_look_placed_are_shown_at_the_place_the_measurement_drew():
    rows = [
        {"at": "o2", "kind": "passage_end", "the_look_says": "an open bore", "declined": False,
         "where_m": [0.0, 0.0, 0.0], "where_from": "opening_centroid"},
        {"at": "A", "kind": "marked_place", "the_look_says": "a flange ring around the bore", "declined": False,
         "where_m": [0.139, 0.0, 0.0], "where_from": "renderer"},
        {"at": "B", "kind": "marked_place", "the_look_says": "cannot tell", "declined": True, "where_m": None,
         "where_from": "", "where_missing": "no_point"},
    ]
    text = "\n".join(gb.look_lines(_looked(rows)))
    assert "WHAT IT SAW AT MEASURED PLACES" in text
    assert "passage end o2" in text and "an open bore [(0, 0, 0) mm]" in text
    assert "marked place A" in text and "[(139, 0, 0) mm]" in text
    assert "could not read" in text and "B" in text


def test_a_placed_finding_with_no_coordinate_says_so_rather_than_inventing_one():
    rows = [{"at": "C", "kind": "marked_place", "the_look_says": "a step in the bore", "declined": False,
             "where_m": None, "where_from": "", "where_missing": "no_scale"}]
    text = "\n".join(gb.look_lines(_looked(rows)))
    assert "a step in the bore [no coordinate for this file]" in text


def test_a_look_with_no_placed_findings_renders_exactly_as_before():
    assert gb.placed_lines({"relied_on": {}}) == []
    assert "MEASURED PLACES" not in "\n".join(gb.look_lines(_looked([])))
