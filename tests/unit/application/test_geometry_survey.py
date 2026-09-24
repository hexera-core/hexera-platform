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


def _role_question(state) -> dict:
    """The one role question the survey raises. There is one: `ask.say.port_roles` puts ONE question naming
    every mouth nothing has placed, not one question per mouth."""
    (question,) = [v for v in gs.question_views(state) if v["about"] == "opening.role"]
    return question


def _say_role(state, mouth: str, role: str, *, words: str = "", said: str = "", principal: str = "") -> dict:
    """The customer giving ONE mouth ONE role, through the role question the survey raised.

    The question's options are the ROLES and the mouths it names are its subjects, so an answer is the role
    with the mouth beside it (`geometry_survey._role_answer`). Written once here because every test below
    gives a role and none of them is about the shape of the call.
    """
    words = words or f"{mouth} is the {role}"
    return gs.record_answer(state, question_id=_role_question(state)["id"], choice=role, subject=mouth,
                            words=words, latest_user_message=said or words, principal=principal)


#: A port row the way a customer's brief gives one: with something to bind it to a mouth by. A row with
#: neither a size nor a location binds to nothing, and this platform's own validation refuses the job for it
#: (`agents/intake/validation.py`), which is why the Surveyor now raises `q_dispatch` on one.
_DECLARED_IN = {"name": "in", "type": "inlet", "diameter_mm": 297.94, "near_mm": [0, 0, 0]}
_DECLARED_OUT = {"name": "out", "type": "outlet", "diameter_mm": 297.94, "near_mm": [790.64, 787.28, 0]}


# THE CHAIN'S ORDER IS NOT A PREFERENCE: THE BRIEF CHANGES WHAT GETS ASKED


def test_an_external_body_measured_for_the_assumed_purpose_is_not_named_until_the_customer_says():
    """ahmed_variant_001 is a bluff body with four flat faces the measurement found. Composed for the purpose
    the upload assumed, the Surveyor cannot say what the file is and names no representation; composed for
    what the customer said, it names it. That difference is why intake comes first. Neither composition asks
    which face is the inlet in the same words, and that is the whole point.

    READ AS INTERNAL the four faces ARE what the builder would cut: under an unknown representation
    `tools._builder_ports` falls back to every planar face, because that is what `tessellate_internal` takes,
    so the Surveyor asks which of them carries the flow rather than letting the widest one become the inlet
    behind the customer. READ AS WHAT THEY SAID there are no candidates at all: the external driver meshes
    the body in a far-field box and no measured face ever becomes a patch."""
    doc = _doc("ahmed_variant_001")
    assumed = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd"))
    said = _fresh("ahmed_variant_001", purpose="external_cfd")
    assert assumed["composed_for"]["representation"] == "unknown"
    role = _role_question(assumed)
    assert role["id"] == "q_port_roles" and len(role["subjects"]) == 4
    assert [v["id"] for v in gs.question_views(said) if v["about"] == "opening.role"] == []
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
    # AN STL DECLARES NO UNIT, and the fixture is a STEP whose header does, so the header goes with the format:
    # `ask.settle.unit_settled_by_file` reads the declaration, not the extension, and a document that says
    # "stl" and carries a step_header unit is a document no measurement produces
    doc["facts"]["source_format"] = "stl"
    doc["facts"]["units"] = {**(doc["facts"].get("units") or {}), "declared": None, "declared_source": "none"}
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd"))
    routes = {v["id"]: v["route"] for v in gs.question_views(state)}
    assert routes["q_unit"] == gs.ROUTE_APPLICATION
    assert "q_unit" not in _ids(gs.open_now(state))
    with pytest.raises(gs.SurveyError, match="unit itself"):
        gs.record_answer(state, question_id="q_unit", choice="mm", words="mm", latest_user_message="mm")


def test_the_budget_trade_waits_for_every_step_four_question_and_is_put_once():
    brief = _brief("bend_elbow_001").replace("under 2 million cells", "under 100,000 cells")
    state = _fresh("bend_elbow_001", brief=brief)
    views = {v["id"]: v for v in gs.question_views(state)}
    assert views["q_budget"]["route"] == gs.ROUTE_TRADE
    assert "q_budget" not in _ids(gs.open_now(state))
    with pytest.raises(gs.SurveyError, match="only put once"):
        gs.record_answer(state, question_id="q_budget", choice=views["q_budget"]["options"][0],
                         words="hold", latest_user_message="hold it")
    said = "o2 is where it comes in and o1 is where it leaves"
    state = _say_role(state, "o2", "inlet", words="o2 is where it comes in", said=said)
    state = _say_role(state, "o1", "outlet", words="o1 is where it leaves", said=said)
    assert _ids(gs.open_now(state)) == ["q_budget"]
    assert gs.stage_of(state) == gs.STAGE_TRADE
    hold = views["q_budget"]["options"][0]
    state = gs.record_answer(state, question_id="q_budget", choice=hold, words="hold it",
                             latest_user_message="hold it at the hundred thousand")
    assert gs.open_now(state) == []
    assert gs.stage_of(state) == gs.STAGE_SETTLED
    assert gs.confirmed_cell_cap(state) == 100_000


def test_a_budget_default_is_put_once_confirms_nothing_and_rides_to_the_builder_unsettled():
    brief = _brief("bend_elbow_001").replace("under 2 million cells", "under 100,000 cells")
    state = _fresh("bend_elbow_001", brief=brief)
    said = "o2 in, o1 out, and use your default for the budget"
    state = _say_role(state, "o2", "inlet", words="o2 in", said=said)
    state = _say_role(state, "o1", "outlet", words="o1 out", said=said)
    state = gs.record_answer(state, question_id="q_budget", words="use your default for the budget",
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
    raise_to = [v for v in gs.question_views(state) if v["id"] == "q_budget"][0]["options"][1]
    said = "o2 in, o1 out, and raise it"
    state = _say_role(state, "o2", "inlet", words="o2 in", said=said)
    state = _say_role(state, "o1", "outlet", words="o1 out", said=said)
    state = gs.record_answer(state, question_id="q_budget", choice=raise_to, words="raise it",
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
        gs.record_answer(state, question_id="q_port_roles", choice="inlet", subject="o2", words="the left one",
                         latest_user_message="I think o2")


def test_a_choice_that_is_not_one_of_the_questions_options_is_refused():
    state = _fresh("bend_elbow_001")
    with pytest.raises(gs.SurveyError, match="not one of the options"):
        gs.record_answer(state, question_id="q_port_roles", choice="the big one", words="the big one",
                         latest_user_message="the big one")


def test_one_mouth_cannot_be_given_two_roles_without_asking_again():
    said = "o2 is the inlet"
    state = _say_role(_fresh("bend_elbow_001"), "o2", "inlet", said=said)
    with pytest.raises(gs.SurveyError, match="already said o2 is the inlet"):
        _say_role(state, "o2", "outlet", words=said, said=said)


def test_a_default_that_stood_is_not_a_confirmation_anywhere():
    """The ledger rule, end to end on the platform. A customer who lets the default stand has confirmed
    nothing: the row says `default_taken`, the question stays open, no role is confirmed, the
    IntakeHandoff lists it unanswered, and the builder is told it is unsettled."""
    said = "whatever you think is best"
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="q_port_roles", words=said,
                             latest_user_message=said, took_default=True)
    assert state["answers"][-1]["answered_by"] == gs.DEFAULT_TAKEN
    views = {v["id"]: v for v in gs.question_views(state)}
    assert views["q_port_roles"]["status"] == "defaulted"
    assert "q_port_roles" in _ids(gs.open_now(state))
    assert gs.confirmed_roles(state) == {}
    handoff = gs.intake_handoff(state)
    assert handoff.answers == [] and "q_port_roles" in handoff.unanswered
    block = gs.builder_block(state)
    assert block["survey"]["confirmed"] == {}
    assert any(row["about"] == "opening.role" for row in block["survey"]["unsettled"])


def test_a_skip_is_recorded_as_asked_and_not_answered():
    said = "skip that one"
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="q_port_roles", words=said,
                             latest_user_message=said, skipped=True)
    views = {v["id"]: v for v in gs.question_views(state)}
    assert views["q_port_roles"]["status"] == "skipped"
    assert "q_port_roles" not in _ids(gs.open_now(state))
    assert "q_port_roles" in gs.intake_handoff(state).unanswered


def test_the_role_question_takes_a_role_per_mouth_and_is_settled_only_when_every_mouth_has_one():
    """bend_elbow_001's two bores are ONE question naming both, and it is settled only when every one has a
    role. The flange shoulders beside them are wall and are not asked about. A role with no mouth named is
    refused: the options are the roles, the mouth is the other half of the answer, and neither alone settles
    anything."""
    state = _fresh("bend_elbow_001")
    (question,) = gs.open_now(state)
    assert question["id"] == "q_port_roles" and sorted(question["subjects"]) == ["o1", "o2"]
    assert question["options"][:2] == ["inlet", "outlet"]
    said = "o2 in, o1 out"
    with pytest.raises(gs.SurveyError, match="say which mouth"):
        gs.record_answer(state, question_id="q_port_roles", choice="inlet", words="o2 in",
                         latest_user_message=said)
    for oid, role, words in (("o2", "inlet", "o2 in"), ("o1", "outlet", "o1 out")):
        assert gs.open_now(state), "settled before every mouth had a role"
        state = _say_role(state, oid, role, words=words, said=said)
    assert gs.open_now(state) == []
    assert gs.confirmed_roles(state) == {"o1": "outlet", "o2": "inlet"}


def test_a_port_row_the_customer_placed_settles_its_mouth_and_one_that_binds_to_nothing_does_not():
    """CUT 1 ON THE PLATFORM PATH: a declared port row AT a mouth settles that mouth and nothing is asked
    about it (`ask.settle.bind_declared`, one row to one mouth). A row with neither a size nor a location
    binds to no mouth, so it settles none, AND this platform's own validation would refuse the job for it, so
    the Surveyor puts the dispatch question FIRST instead of asking about roles as though the job would run.

    The old finder counted instead of binding: two roled rows against two mouths asked nothing whatever the
    rows carried, so a row that binds to nothing passed for an answer about a mouth, and nothing anywhere
    said the job would be refused before it was meshed."""
    placed = _fresh("bend_elbow_001", declared=[_DECLARED_IN, _DECLARED_OUT])
    assert gs.open_now(placed) == []
    # and it is not merely unasked: a question SOMEBODY ELSE answered is not an uncertainty any more, so it is
    # off the survey altogether and the builder is never told a role is unsettled that the customer placed
    assert [v for v in gs.question_views(placed) if v["about"] == "opening.role"] == []
    assert [r for r in gs.builder_block(placed)["survey"]["unsettled"]
            if r["about"] == "opening.role"] == []
    assert placed["asking"]["put"] == [] and placed["asking"]["held"] == {}

    unplaceable = _fresh("bend_elbow_001", declared=[{"name": "in", "type": "inlet"},
                                                     {"name": "out", "type": "outlet"}])
    put = gs.open_now(unplaceable)
    assert _ids(put)[0] == "q_dispatch", "the answer that decides whether the job runs comes first"
    assert put[0]["tier"] == "dispatch"
    assert "neither a size nor a location" in put[0]["text"]


# HANDOFF 3: THE BUILDER'S BLOCK


def _answered_elbow() -> dict:
    said = "o2 is the inlet and o1 is the outlet"
    state = _say_role(_fresh("bend_elbow_001"), "o2", "inlet", words=said, said=said, principal="owner-7f3a")
    return _say_role(state, "o1", "outlet", words=said, said=said, principal="owner-7f3a")


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
    assert [r for r in block["survey"]["unsettled"] if r["about"] != "look"] == []


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
    """ONE question names both mouths, so there is one refusal naming both rather than one per role."""
    problems = gs.role_problems(_fresh("bend_elbow_001"), _doc("bend_elbow_001"), [_INLET, _OUTLET, _WALL])
    assert len(problems) == 1 and "has not answered" in problems[0]
    assert "o1" in problems[0] and "o2" in problems[0]


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
    state = gs.record_answer(_fresh("bend_elbow_001"), question_id="q_port_roles", words=said,
                             latest_user_message=said, took_default=True)
    problems = gs.role_problems(state, _doc("bend_elbow_001"), [_INLET, _OUTLET, _WALL])
    assert any("did not confirm survey question q_port_roles" in p for p in problems)


def test_the_briefs_blanket_settles_the_remaining_mouths_only_once_every_named_port_is_placed():
    """CUT 1 IS ONE ROW TO ONE MOUTH, and the brief's blanket is gated on that.

    venturi_orifice_001's brief ends "All remaining surfaces are the wall". With no port row placed, "remaining"
    could mean either mouth, so the blanket settles neither and the Surveyor asks about both. With their inlet
    row placed at o1, "remaining" means o2, the blanket settles it as wall, and nothing is asked at all. Without
    that gate the blanket settled every mouth as wall including the inlet the brief names in prose, which is the
    R9 class of mistake, and the old finder had no gate because it had no blanket: it counted rows instead."""
    ports = [{"name": "in", "type": "inlet", "diameter_mm": 469.66, "near_mm": [0, 0, 0]}]
    both = _role_question(_fresh("venturi_orifice_001"))
    assert sorted(both["subjects"]) == ["o1", "o2"]
    assert both["put"] is True

    placed = _fresh("venturi_orifice_001", declared=ports)
    assert [v for v in gs.question_views(placed) if v["about"] == "opening.role"] == []
    assert gs.open_now(placed) == []


def test_an_external_purpose_raises_no_role_gate():
    state = _fresh("ahmed_variant_001", purpose="external_cfd")
    assert gs.role_problems(state, _doc("ahmed_variant_001"), [_WALL, {"name": "far", "type": "farfield"}]) == []


# THE BLOCK'S WHOLE CONTRACT, AND ITS CEILING


def test_the_platform_refuses_a_builder_control_and_an_outcome_claim_the_packages_checker_accepts():
    """`deliver.check_survey_block` runs four of the block's six rules. The other two,
    `_refuse_builder_keys` and `_refuse_outcome_claims`, are private to `contract/survey.py` and fire only
    inside a `SurveyHandoff`'s validator, and the look's free text is added to the block AFTER that validator
    ran. So the checker accepts both of these, and the platform's own check does not."""
    from geometry_agent.contract.deliver import check_survey_block

    good = gs.builder_block(_answered_elbow())["survey"]
    gs.check_the_survey_block(good)
    for hurt, what in (({"kind": "seen", "tier": "relied_on", "value": "a shoulder", "field": "marks_seen",
                         "source": "look", "n_layers": "three"}, "builder control 'n_layers'"),
                       ({"kind": "seen", "tier": "relied_on", "value": "the mesh will collapse here",
                         "field": "marks_seen", "source": "look"}, "says 'the mesh will'")):
        block = {**good, "seen": {**good["seen"], "shoulder": hurt}}
        check_survey_block(block)  # the package's own checker accepts it, which is the finding
        with pytest.raises(gs.SurveyError, match="never predicts how the mesh turns out") as refusal:
            gs.check_the_survey_block(block)
        assert what.split()[-1].strip("'") in str(refusal.value)


def test_no_row_of_the_block_names_more_mouths_than_the_ceiling_has_room_for():
    """ITEM 23, measured. One role question names every unplaced mouth, so ONE unsettled row carried every id:
    the block was 2,117 characters with two mouths and 8.0 longer per mouth, crossing the 4,000 ceiling between
    200 and 300. Past it the package refused, `builder_block` returned None, and the planner got
    `hexera.planner_block`'s intake-less survey instead, losing every confirmed role. No answer is needed to
    reach this, which is what made it the reachable half."""
    from geometry_agent.contract.deliver import SURVEY_BLOCK_MAX

    state = _fresh("bend_elbow_001")
    small = len(json.dumps(gs.builder_block(state)["survey"]))
    for mouths in (200, 500, 1300):
        big = json.loads(json.dumps(state))
        for u in big["survey"]["uncertainties"]:
            if u["about"] == "opening.role":
                u["subjects"] = [f"o{i}" for i in range(mouths)]
        block = gs.builder_block(big)
        assert block is not None, f"{mouths} mouths lost the survey altogether"
        survey = block["survey"]
        gs.check_the_survey_block(survey)
        assert len(json.dumps(survey)) <= SURVEY_BLOCK_MAX
        (row,) = [r for r in survey["unsettled"] if r["about"] == "opening.role"]
        assert len(row["subjects"]) == gs.BLOCK_LIST_MAX
        assert f"stand for {mouths - gs.BLOCK_LIST_MAX} more" in row["why"], row["why"]
    # and the survey on the row still names every one of them: what is shortened is the block, said out loud
    assert len(big["survey"]["uncertainties"][0]["subjects"]) == 1300
    # a part small enough to name all its mouths is untouched
    assert small == len(json.dumps(gs.builder_block(_fresh("bend_elbow_001"))["survey"]))


def test_a_block_over_the_ceiling_is_never_handed_over_and_the_other_list_is_still_uncapped():
    """THE HALF OF ITEM 23 THAT IS NOT CLOSED HERE, with the number, so nobody has to guess at it again.

    `applies_to` is one entry per mouth ONE answer placed, and capping it would make the block claim fewer
    confirmed mouths than the customer gave, which is worse than a long list: the builder binds patches from
    what it is handed. So it is left alone, and what is pinned instead is that an oversized block is never
    handed over. Measured on bend_elbow_001 with every mouth confirmed as wall: 1,921 characters at 2 mouths,
    3,399 at 200, 3,799 at 250, and over the 4,000 ceiling by 300. Reaching it takes 251 separate answers from
    one customer on one part. The fix belongs in `deliver.survey_block`, which should shorten the list and say
    how many more there are, the way it already caps the number of rows.
    """
    from geometry_agent.contract.deliver import SURVEY_BLOCK_MAX

    base = _fresh("bend_elbow_001")
    (role,) = [v for v in gs.question_views(base) if v["about"] == "opening.role"]
    sizes = {}
    for mouths in (2, 250, 300):
        state = json.loads(json.dumps(base))
        for u in state["survey"]["uncertainties"]:
            if u["about"] == "opening.role":
                u["subjects"] = [f"o{i}" for i in range(mouths)]
        state["answers"] = [
            {"question_id": role["id"], "about": "opening.role", "at": "2026-09-23T00:00:00+00:00",
             "words": "all wall", "principal": "p", "via": "intake_conversation",
             "answered_by": gs.CUSTOMER, "subject": f"o{i}", "value": "wall", "option": "wall", "note": ""}
            for i in range(mouths)]
        block = gs.builder_block(state)
        sizes[mouths] = None if block is None else len(json.dumps(block["survey"]))
        if block is not None:
            # NEVER OVERSIZED. A block that can be cut is a block that can lose what it was carrying, so the
            # only two outcomes are a block under the ceiling or no survey key at all
            assert sizes[mouths] <= SURVEY_BLOCK_MAX
            gs.check_the_survey_block(block["survey"])
    assert sizes[250] is not None and sizes[250] > sizes[2]
    assert sizes[300] is None, "the survey is refused rather than truncated, and that is the remaining cost"


# THE LOOK: FOUR STATES, NOT A BOOLEAN


def _external(look=None, **row) -> dict:
    doc = _doc("ahmed_variant_001_external_looked")
    doc.pop("look", None)
    if look is not None:
        doc["look"] = look
    state = gs.carry_answers(None, gs.compose(doc, purpose="external_cfd",
                                             brief=_brief("ahmed_variant_001")))
    return {**state, **row}


def _look_row(state) -> dict | None:
    rows = [r for r in gs.builder_block(state)["survey"]["unsettled"] if r["about"] == "look"]
    return rows[0] if rows else None


def test_a_look_that_landed_says_nothing_about_itself_and_the_seen_half_carries_the_reading():
    looked = gs.carry_answers(None, gs.compose(_doc("ahmed_variant_001_external_looked"),
                                               purpose="external_cfd", brief=_brief("ahmed_variant_001")))
    assert gs.look_state(looked) == gs.LOOK_OK
    block = gs.builder_block(looked)["survey"]
    assert block["looked"] is True and block["seen"]
    assert _look_row(looked) is None, "there is nothing unsettled about a look that landed"


def test_the_three_states_that_reach_the_builder_as_looked_false_are_told_apart_in_words():
    """`survey.looked` is one boolean over four states, so never taken, still running and FAILED all read as
    False beside an empty `seen`, which is indistinguishable from a look that looked and found nothing. The
    product rule is that a failed look is never a clear passage, so the block says which it was."""
    never = _external()
    pending = _external(look=None, look_queued="queued")
    failed = _external(look={"status": "failed", "reason": "the reader returned nothing"})
    assert (gs.look_state(never), gs.look_state(pending), gs.look_state(failed)) == (
        gs.LOOK_NONE, gs.LOOK_PENDING, gs.LOOK_FAILED)
    whys = {}
    for name, state in (("never", never), ("pending", pending), ("failed", failed)):
        assert gs.builder_block(state)["survey"]["looked"] is False
        row = _look_row(state)
        assert row is not None, f"{name} says nothing to the builder"
        whys[name] = row["why"]
    assert len(set(whys.values())) == 3, "three states, three sentences"
    assert "FAILED" in whys["failed"] and "not a clear passage" in whys["failed"]
    assert "has not written" in whys["pending"]


def test_the_look_state_row_never_takes_the_block_over_its_own_ceiling():
    from geometry_agent.contract.deliver import SURVEY_BLOCK_ITEMS, SURVEY_BLOCK_MAX, check_survey_block

    block = gs.builder_block(_external(look_queued="queued"))["survey"]
    check_survey_block(block)
    assert len(json.dumps(block)) <= SURVEY_BLOCK_MAX
    assert len(block["unsettled"]) <= SURVEY_BLOCK_ITEMS
    assert block["unsettled"][0]["about"] == "look", "it goes first: it changes how every other row reads"


def test_the_queue_answer_survives_a_recomposition_so_a_pending_look_stays_pending():
    """The row records that a look is on the way, and a recomposition for a new answer must not lose it: the
    stored document carries no look until the worker writes one, so without the note pending reads as never."""
    doc = _doc("bend_elbow_001")
    state = {**_answered_elbow(), "look_queued": "queued"}
    assert gs.look_state(state) == gs.LOOK_PENDING
    assert gs.look_state(gs.recomposed(state, doc)) == gs.LOOK_PENDING
    landed = {**(doc.get("look") or {}), "status": "ok",
              "impression": {"looks_like": "a pipe elbow", "openings_seen": [], "internal_features": []}}
    assert gs.look_state(gs.recomposed(state, {**doc, "look": landed})) == gs.LOOK_OK


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
    assert {a["question_id"] for a in again["answers"] if a.get("retired")} == {"q_port_roles"}
    # and composed back for the purpose they had, the retired answers do not come back to life
    back = gs.recomposed(again, doc, purpose="internal_cfd")
    assert gs.confirmed_roles(back) == {}
    assert _ids(gs.open_now(back)) == ["q_port_roles"]
