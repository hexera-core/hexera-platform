# Responsibility: Verify the Surveyor's chain on the platform against the measurement package itself: composition for the customer, the questions, the answers, the gate and the builder's block.
# Boundaries: pure functions over real stored measurements of three corpus parts; the database and the conversation are other tests.
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("geometry_agent.contract.deliver",
                    reason="the measurement package is not on this interpreter's path")

from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"


def _doc(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))


def _brief(case: str) -> str:
    return (FIXTURES / f"{case}.brief.txt").read_text(encoding="utf-8")


def _fresh(case: str, **kw) -> dict:
    kw.setdefault("purpose", "internal_cfd")
    kw.setdefault("brief", _brief(case))
    return gs.carry_answers(None, gs.compose(_doc(case), **kw))


def _ids(views) -> list[str]:
    return [v["id"] for v in views]


# THE CHAIN'S ORDER IS NOT A PREFERENCE: THE BRIEF CHANGES WHAT GETS ASKED


def test_an_external_body_measured_for_the_assumed_purpose_is_not_named_until_the_customer_says():
    """ahmed_variant_001 is a bluff body with four flat faces the measurement found. Composed for the purpose
    the upload assumed, the Surveyor cannot say what the file is and names no representation; composed for
    what the customer said, it names it. That difference is why intake comes first. Neither composition asks
    which face is the inlet: none of the four is a mouth the builder can cut (`catalog.port_openings`), so the
    question an internal reading used to put about them is gone with the miscount behind it."""
    doc = _doc("ahmed_variant_001")
    assumed = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd"))
    said = _fresh("ahmed_variant_001", purpose="external_cfd")
    assert assumed["composed_for"]["representation"] == "unknown"
    assert gs.open_now(assumed) == []
    assert gs.open_now(said) == []
    assert said["composed_for"]["representation"] == "external"
    assert doc["representation"] == "unknown"


def test_the_composition_is_the_packages_own_and_nothing_is_measured_again():
    doc = _doc("bend_elbow_001")
    state = _fresh("bend_elbow_001")
    block = state["planner_block"]
    assert block["schema"] == "geometry_agent.planner_block.v1"
    assert block["agent_forecast_cells"] == doc["planner_block"]["agent_forecast_cells"]
    assert block["inlet_bore_m"] == doc["planner_block"]["inlet_bore_m"]
    assert state["sha256"] == doc["source_sha256"]
    assert state["survey"]["schema_name"] == "geometry_agent.contract.survey.v1"


def test_the_customers_written_budget_reaches_the_block_that_had_no_reader():
    """`customer_cell_cap` was null on every job because nothing read the budget. Composed from the
    customer's words, it is the number they wrote."""
    doc = _doc("bend_elbow_001")
    assert doc["planner_block"]["customer_cell_cap"] is None
    state = _fresh("bend_elbow_001")
    assert state["planner_block"]["customer_cell_cap"] == 2_000_000
    assert state["composed_for"]["cell_cap_kind"] == "stated"


def test_a_measurement_that_did_not_succeed_is_never_composed():
    with pytest.raises(gs.SurveyError):
        gs.compose({"status": "measurement_failed", "facts": {}}, purpose="internal_cfd")
    doc = _doc("venturi_orifice_001")
    doc.pop("facts")
    with pytest.raises(gs.SurveyError, match="no facts"):
        gs.compose(doc, purpose="internal_cfd")


def test_the_package_surveys_an_upload_that_names_no_engine_and_marks_the_engine_assumed():
    """THE UPSTREAM FAULT THIS PINNED IS FIXED (work/z-chain, `contract.marks.OWNERS["engine"]` admits
    `assumed`), so the shim `_for_survey` is gone and the composed document goes to the survey whole.
    The engine rides as what it is: nobody named one, so it is assumed, never stated."""
    from geometry_agent.agent import hexera
    from geometry_agent.contract import marks
    from geometry_agent.contract.build import survey_from
    from geometry_agent.facts.schema import GeometryFacts

    doc = _doc("venturi_orifice_001")
    composed = hexera.report_measured(GeometryFacts.model_validate(doc["facts"]), None, None,
                                      purpose="internal_cfd")
    assert composed["engine_source"] == "assumed"
    survey = survey_from(composed)
    assert survey.source_sha256 == doc["source_sha256"]
    assert survey.mark("engine").kind == marks.ASSUMED
    assert not hasattr(gs, "_for_survey")


# THE QUESTIONS ARE THE SURVEY'S UNCERTAINTIES, ONE FOR ONE


def test_every_uncertainty_is_a_question_view_and_none_is_filtered():
    state = _fresh("bend_elbow_001")
    survey = gs.survey_of(state)
    assert _ids(gs.question_views(state)) == [u.id for u in survey.uncertainties]


def test_the_unit_is_the_applications_question_and_an_identity_is_never_put():
    doc = _doc("venturi_orifice_001")
    doc["facts"]["source_format"] = "stl"
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd"))
    routes = {v["id"]: v["route"] for v in gs.question_views(state)}
    assert routes["unit"] == gs.ROUTE_APPLICATION
    assert "unit" not in _ids(gs.open_now(state))
    with pytest.raises(gs.SurveyError, match="unit itself"):
        gs.record_answer(state, question_id="unit", choice="mm", words="mm", latest_user_message="mm")


def test_the_budget_trade_waits_for_every_step_four_question_and_is_put_once():
    brief = _brief("bend_elbow_001").replace("under 2 million cells", "under 100,000 cells")
    state = _fresh("bend_elbow_001", brief=brief)
    views = {v["id"]: v for v in gs.question_views(state)}
    assert views["budget_trade"]["route"] == gs.ROUTE_TRADE
    assert "budget_trade" not in _ids(gs.open_now(state))
    with pytest.raises(gs.SurveyError, match="only put once"):
        gs.record_answer(state, question_id="budget_trade", choice=views["budget_trade"]["options"][0],
                         words="hold", latest_user_message="hold it")
    said = "o2 is where it comes in and o1 is where it leaves"
    state = gs.record_answer(state, question_id="role_inlet", choice="o2", words="o2 is where it comes in",
                             latest_user_message=said)
    state = gs.record_answer(state, question_id="role_outlet", choice="o1", words="o1 is where it leaves",
                             latest_user_message=said)
    assert _ids(gs.open_now(state)) == ["budget_trade"]
    assert gs.stage_of(state) == gs.STAGE_TRADE
    hold = views["budget_trade"]["options"][0]
    state = gs.record_answer(state, question_id="budget_trade", choice=hold, words="hold it",
                             latest_user_message="hold it at the hundred thousand")
    assert gs.open_now(state) == []
    assert gs.stage_of(state) == gs.STAGE_SETTLED
    assert gs.confirmed_cell_cap(state) == 100_000


def test_a_budget_default_is_put_once_confirms_nothing_and_rides_to_the_builder_unsettled():
    brief = _brief("bend_elbow_001").replace("under 2 million cells", "under 100,000 cells")
    state = _fresh("bend_elbow_001", brief=brief)
    said = "o2 in, o1 out, and use your default for the budget"
    state = gs.record_answer(state, question_id="role_inlet", choice="o2", words="o2 in", latest_user_message=said)
    state = gs.record_answer(state, question_id="role_outlet", choice="o1", words="o1 out", latest_user_message=said)
    state = gs.record_answer(state, question_id="budget_trade", words="use your default for the budget",
                             latest_user_message=said, took_default=True)
    assert gs.open_now(state) == [], "the trade is asked once"
    assert gs.stage_of(state) == gs.STAGE_SETTLED
    assert gs.confirmed_cell_cap(state) is None
    block = gs.builder_block(state)
    assert "cell_budget" not in block["survey"]["confirmed"]
    assert any(row["about"] == "cell_budget" for row in block["survey"]["unsettled"])
    assert block["survey"]["measured"]["cell_budget"]["kind"] == "stated"


def test_a_confirmed_budget_is_composed_into_the_block_the_builder_reads():
    brief = _brief("bend_elbow_001").replace("under 2 million cells", "under 100,000 cells")
    doc = _doc("bend_elbow_001")
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=brief))
    raise_to = [v for v in gs.question_views(state) if v["id"] == "budget_trade"][0]["options"][1]
    said = "o2 in, o1 out, and raise it"
    state = gs.record_answer(state, question_id="role_inlet", choice="o2", words="o2 in", latest_user_message=said)
    state = gs.record_answer(state, question_id="role_outlet", choice="o1", words="o1 out", latest_user_message=said)
    state = gs.record_answer(state, question_id="budget_trade", choice=raise_to, words="raise it",
                             latest_user_message=said)
    cap = gs.confirmed_cell_cap(state)
    assert cap == state["planner_block"]["agent_forecast_cells"]
    state = gs.recomposed(state, doc, cell_cap=cap)
    assert state["planner_block"]["customer_cell_cap"] == cap
    block = gs.builder_block(state)
    assert block["survey"]["confirmed"]["cell_budget"]["kind"] == "confirmed"
    assert block["survey"]["confirmed"]["cell_budget"]["value"] == cap


# AN ANSWER IS THE CUSTOMER'S, OR IT IS NOT RECORDED


def test_an_answer_the_customer_did_not_write_is_refused():
    state = _fresh("bend_elbow_001")
    with pytest.raises(gs.SurveyError, match="not in the customer's latest message"):
        gs.record_answer(state, question_id="role_inlet", choice="o2", words="the left one",
                         latest_user_message="I think o2")


def test_a_choice_that_is_not_one_of_the_questions_options_is_refused():
    state = _fresh("bend_elbow_001")
    with pytest.raises(gs.SurveyError, match="not one of the options"):
        gs.record_answer(state, question_id="role_inlet", choice="the big one", words="the big one",
                         latest_user_message="the big one")


def test_one_mouth_cannot_be_given_two_roles_without_asking_again():
    said = "o2 is the inlet"
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="role_inlet", choice="o2", words=said,
                             latest_user_message=said)
    with pytest.raises(gs.SurveyError, match="already said o2 is the inlet"):
        gs.record_answer(state, question_id="role_outlet", choice="o2", words=said, latest_user_message=said)


def test_a_default_that_stood_is_not_a_confirmation_anywhere():
    """The ledger rule, end to end on the platform. A customer who lets the default stand has confirmed
    nothing: the row says `default_taken`, the question stays open, no role is confirmed, the
    IntakeHandoff lists it unanswered, and the builder is told it is unsettled."""
    said = "whatever you think is best"
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="role_inlet", words=said,
                             latest_user_message=said, took_default=True)
    assert state["answers"][-1]["answered_by"] == gs.DEFAULT_TAKEN
    views = {v["id"]: v for v in gs.question_views(state)}
    assert views["role_inlet"]["status"] == "defaulted"
    assert "role_inlet" in _ids(gs.open_now(state))
    assert gs.confirmed_roles(state) == {}
    handoff = gs.intake_handoff(state)
    assert handoff.answers == [] and "role_inlet" in handoff.unanswered
    block = gs.builder_block(state)
    assert block["survey"]["confirmed"] == {}
    assert any(row["about"] == "opening.role" for row in block["survey"]["unsettled"])


def test_a_skip_is_recorded_as_asked_and_not_answered():
    said = "skip that one"
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="role_inlet", words=said,
                             latest_user_message=said, skipped=True)
    views = {v["id"]: v for v in gs.question_views(state)}
    assert views["role_inlet"]["status"] == "skipped"
    assert "role_inlet" not in _ids(gs.open_now(state))
    assert "role_inlet" in gs.intake_handoff(state).unanswered


def test_the_role_count_question_takes_a_role_per_mouth_from_the_customers_vocabulary():
    """With ONE port declared, bend_elbow_001's two bores are one question naming both, and it is settled
    only when every one has a role. The flange shoulders beside them are wall and are not asked about:
    two declared ports against the two bores ask nothing, which is `contract.asking`'s rule that a
    declaration matching the mouths is the customer's own statement."""
    assert gs.open_now(_fresh("bend_elbow_001", declared=[{"name": "inlet", "type": "inlet"},
                                                          {"name": "outlet", "type": "outlet"}])) == []
    state = _fresh("bend_elbow_001", declared=[{"name": "inlet", "type": "inlet"}])
    (question,) = gs.open_now(state)
    assert question["id"] == "role_count" and sorted(question["subjects"]) == ["o1", "o2"]
    said = "o2 in, o1 out"
    with pytest.raises(gs.SurveyError, match="say which role"):
        gs.record_answer(state, question_id="role_count", choice="o2", words="o2 in", latest_user_message=said)
    for oid, role, words in (("o2", "inlet", "o2 in"), ("o1", "outlet", "o1 out")):
        assert gs.open_now(state), "settled before every mouth had a role"
        state = gs.record_answer(state, question_id="role_count", choice=oid, role=role, words=words,
                                 latest_user_message=said)
    assert gs.open_now(state) == []
    assert gs.confirmed_roles(state) == {"o1": "outlet", "o2": "inlet"}


# HANDOFF 3: THE BUILDER'S BLOCK


def _answered_elbow() -> dict:
    said = "o2 is the inlet and o1 is the outlet"
    state = _fresh("bend_elbow_001")
    state = gs.record_answer(state, question_id="role_inlet", choice="o2", words=said, latest_user_message=said,
                             principal="owner-7f3a")
    return gs.record_answer(state, question_id="role_outlet", choice="o1", words=said, latest_user_message=said,
                            principal="owner-7f3a")


def test_the_builder_gets_the_survey_and_it_passes_the_packages_own_validator():
    from geometry_agent.contract.deliver import SURVEY_BLOCK_MAX, check_survey_block

    block = gs.builder_block(_answered_elbow())
    check_survey_block(block["survey"])
    assert len(json.dumps(block["survey"])) <= SURVEY_BLOCK_MAX
    confirmed = block["survey"]["confirmed"]
    # one entry per answer, the mouths it placed beside it (work/z-chain grouped them to hold the ceiling)
    assert confirmed["opening.role=inlet"]["value"] == "inlet" and confirmed["opening.role=inlet"]["applies_to"] == ["o2"]
    assert confirmed["opening.role=outlet"]["applies_to"] == ["o1"]
    assert "answered by customer" in confirmed["opening.role=inlet"]["source"]
    assert block["survey"]["unsettled"] == []


def test_who_answered_is_the_customer_and_their_account_never_rides_to_a_provider():
    state = _answered_elbow()
    assert {a["principal"] for a in state["answers"]} == {"owner-7f3a"}
    assert "owner-7f3a" not in json.dumps(gs.builder_block(state))


def test_a_survey_for_other_bytes_does_not_reach_the_builder():
    state = _answered_elbow()
    state["facts_sha256"] = "0" * 64
    assert gs.builder_block(state) is None


# THE GATE: A PORT ROLE THE CUSTOMER DID NOT CONFIRM NEVER REACHES port_declaration

_INLET = {"name": "inlet", "type": "inlet", "diameter_mm": 297.94, "near_mm": [0, 0, 0]}
_OUTLET = {"name": "outlet", "type": "outlet", "diameter_mm": 297.94, "near_mm": [790.64, 787.28, 0]}
_WALL = {"name": "wall", "type": "wall"}


def test_a_role_question_nobody_answered_refuses_the_submission():
    problems = gs.role_problems(_fresh("bend_elbow_001"), _doc("bend_elbow_001"), [_INLET, _OUTLET, _WALL])
    assert len(problems) == 2 and all("has not answered" in p for p in problems)


def test_ports_on_the_mouths_the_customer_named_are_submitted():
    assert gs.role_problems(_answered_elbow(), _doc("bend_elbow_001"), [_INLET, _OUTLET, _WALL]) == []


def test_a_port_on_a_mouth_the_customer_called_something_else_is_refused():
    swapped = [{**_INLET, "type": "outlet"}, {**_OUTLET, "type": "inlet"}, _WALL]
    problems = gs.role_problems(_answered_elbow(), _doc("bend_elbow_001"), swapped)
    assert len(problems) == 2 and "which the customer called the inlet" in " ".join(problems)


def test_twin_bores_with_no_position_are_undecidable_and_refused_rather_than_guessed():
    """o1 and o2 of bend_elbow_001 are the same bore to 0.0001 mm. A port declared by size alone could be
    either, so it binds to neither, and a role that cannot be tied to a mouth cannot be tied to the
    customer's answer about that mouth."""
    by_size = [{"name": "inlet", "type": "inlet", "diameter_mm": 297.94},
               {"name": "outlet", "type": "outlet", "diameter_mm": 297.94}, _WALL]
    problems = gs.role_problems(_answered_elbow(), _doc("bend_elbow_001"), by_size)
    assert len(problems) == 2 and all("does not bind to a measured mouth" in p for p in problems)


def test_a_default_role_refuses_the_submission_it_would_otherwise_have_guessed():
    said = "you pick"
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="role_inlet", words=said,
                             latest_user_message=said, took_default=True)
    problems = gs.role_problems(state, _doc("bend_elbow_001"), [_INLET, _OUTLET, _WALL])
    assert any("did not confirm survey question role_inlet" in p for p in problems)


def test_ports_the_brief_named_one_for_one_are_the_customers_and_nothing_is_asked():
    ports = [_INLET, _OUTLET]
    state = _fresh("venturi_orifice_001", declared=[{"name": "in", "type": "inlet"},
                                                    {"name": "out", "type": "outlet"}])
    assert not [v for v in gs.question_views(state) if v["about"] == "opening.role"]
    assert gs.role_problems(state, _doc("venturi_orifice_001"), ports) == []


def test_an_external_purpose_raises_no_role_gate():
    state = _fresh("ahmed_variant_001", purpose="external_cfd")
    assert gs.role_problems(state, _doc("ahmed_variant_001"), [_WALL, {"name": "far", "type": "farfield"}]) == []


# RECOMPOSITION KEEPS WHAT IS STILL BOUND AND DROPS WHAT IS NOT


def test_the_look_landing_keeps_every_answer():
    doc = _doc("bend_elbow_001")
    state = _answered_elbow()
    doc["look"] = {**(doc.get("look") or {}), "status": "ok", "impression": {"looks_like": "a pipe elbow",
                                                                             "openings_seen": [],
                                                                             "internal_features": []}}
    again = gs.recomposed(state, doc)
    assert again["composed_for"]["look_status"] == "ok"
    assert gs.confirmed_roles(again) == gs.confirmed_roles(state)
    assert gs.survey_of(again).looked is True


def test_a_new_purpose_retires_the_role_questions_and_their_answers_stay_on_the_record():
    doc = _doc("bend_elbow_001")
    before = _answered_elbow()
    again = gs.recomposed(before, doc, purpose="external_cfd")
    assert gs.confirmed_roles(again) == {}
    assert gs.intake_handoff(again).answers == []
    assert len(again["answers"]) == len(before["answers"]), "nothing the customer said is deleted"
    assert {a["question_id"] for a in again["answers"] if a.get("retired")} == {"role_inlet", "role_outlet"}
    # and composed back for the purpose they had, the retired answers do not come back to life
    back = gs.recomposed(again, doc, purpose="internal_cfd")
    assert gs.confirmed_roles(back) == {}
    assert _ids(gs.open_now(back)) == ["role_inlet", "role_outlet"]
