# Responsibility: Verify the Surveyor's chain on the platform against the measurement package itself: composition for the customer, the questions, the answers, the gate and the builder's block.
# Boundaries: pure functions over real stored measurements of three corpus parts; the database and the conversation are other tests.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.contract.deliver", needs="the Surveyor's chain and the builder's block")

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


@pytest.fixture
def side_on(monkeypatch):
    """THERE IS NOTHING LEFT TO ARM. The side is read on every composition: the package deleted the reader
    behind GEOMETRY_AGENT_FLUID_SIDE and both platform settings that wrote it are retired. The crowded part
    below needs the side question, and it gets it unconditionally now."""
    from geometry_agent.agent import catalog
    monkeypatch.setenv("GEOMETRY_AGENT_FLUID_SIDE", "off")
    assert not hasattr(catalog, "FLUID_SIDE_ENV")


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


# THE FINDER'S OWN WORK, ON THE PRODUCT PATH: THE CAP, THE RANKING, THE TIE-BREAK, AND THE TWO MISSING KINDS


def _crowded(**kw) -> dict:
    """block_boss_sharp with six questions on it, which is one more than the cap puts.

    Every one of the six comes from the part or the brief and none is invented: the file would be refused by
    this platform's own pre-mesh rules (dispatch), two mouths need a role (boundary), the geometry reads the
    same both ways with the side switch on (domain), the look reports a mouth the measurement did not find
    (domain), the brief asks for a thousand cells (resolution) and NOBODY declares a unit (resolution).

    NOBODY means the brief too, which is why its unit sentence is removed here as well as the file's declared
    unit. `ask.settle` reads "Geometry is in metres" and settles the unit question on it, so a fixture that
    doctored only the facts was asking for a question the brief had already answered: five questions, and the
    cap this test is about never fired. Removing the sentence is what makes the fixture mean what it says.
    """
    doc = _doc("block_boss_sharp")
    doc["facts"]["units"] = {**(doc["facts"].get("units") or {}), "declared": None, "declared_source": "none"}
    doc["look"] = {"status": "ok", "impression": {
        "looks_like": "a pierced block",
        "openings_seen": [{"id": "unlabelled", "mouth": "clear"}, {"id": "o1", "mouth": "clear"}],
        "internal_features": []}}
    brief = (_brief("block_boss_sharp").replace("Geometry is in metres.", "")
             + "\nMesh budget: keep the total cell count under 1,000 cells.\n")
    assert "in metres" not in brief and "in millimetres" not in brief, brief
    return gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=brief, **kw))


def test_the_five_question_cap_is_a_fact_of_the_product_and_the_ranking_is_the_tier_order(side_on):
    """MAX_ASKED and the consequence ranking were built and ran nowhere near a customer: the platform composed
    its survey with the package's fallback finder, which has neither. Six questions, five put, in tier order.

    THE SIXTH IS THE BUDGET, and it used to be the unit. `ask.consequence` imported a name the units module
    does not define, so every file with no declared unit raised inside that branch, the handler swallowed it
    and landed the question on `resolution` - the ImportError's fallback, not a decision. With the crash fixed
    the unit sits at `domain`, where a question deciding what every length MEANS belongs, so the ranking is
    dispatch, boundary and three domain questions, and the budget trade at `resolution` is 6 of 6."""
    state = _crowded()
    views = {v["id"]: v for v in gs.question_views(state)}
    assert state["asking"]["max_asked"] == 5
    assert len(state["asking"]["put"]) == 5
    assert [views[q]["tier"] for q in state["asking"]["put"]] == [
        "dispatch", "boundary", "domain", "domain", "domain"]
    assert views["q_unit"]["tier"] == "domain", "the unit question is back on the ImportError fallback tier"
    (held,) = list(state["asking"]["held"])
    assert held == "q_budget" and views[held]["tier"] == "resolution"
    assert views[held]["put"] is False
    assert views[held]["held_because"] == "below_the_cap"
    assert "at most 5 are put at once" in views[held]["held_quote"]
    # and it is never put to anybody, which is the whole difference between a cap and a comment
    assert held not in _ids(gs.open_now(state))
    assert len(gs.open_now(state)) <= 5


def test_a_held_question_still_reaches_the_builder_as_unsettled(side_on):
    """NOTHING IS DELETED BY ANY OF THE FOUR CUTS. A question below the cap is not put, and it is still an
    uncertainty, so the builder is told it is unsettled rather than left to assume somebody settled it.

    THE ANSWER HALF IS ITS OWN TEST BELOW, because the question that now falls below the cap is the budget
    trade, and the trade has a SECOND gate: it is only put once every other question is settled. An answer
    volunteered to it is refused for that reason and not by the cap, and the two are worth telling apart.
    """
    state = _crowded()
    (held,) = list(state["asking"]["held"])
    assert held in {u["id"] for u in state["survey"]["uncertainties"]}, "a cut deleted the question"
    about = {u["about"] for u in state["survey"]["uncertainties"] if u["id"] == held}
    told = {r["about"] for r in gs.builder_block(state)["survey"]["unsettled"]}
    assert about <= told, f"the builder is not told {held} is unsettled: {sorted(told)}"


def test_volunteering_an_answer_to_the_held_budget_trade_is_refused_by_the_trades_own_gate(side_on):
    """And the refusal says which gate it is. The cap holds a question back; the trade additionally waits for
    every other question, because a budget answered before the roles and the unit is a budget priced against
    a part nobody has finished describing."""
    state = _crowded()
    (held,) = list(state["asking"]["held"])
    view = [v for v in gs.question_views(state) if v["id"] == held][0]
    assert view["route"] == gs.ROUTE_TRADE
    said = view["options"][0]
    with pytest.raises(gs.SurveyError, match="only put once every other survey question is settled"):
        gs.record_answer(state, question_id=held, choice=said, words=said, latest_user_message=said)


def test_the_tie_break_inside_a_tier_decides_the_order_a_customer_is_asked_in(side_on):
    """`asked_before` breaks ties INSIDE a tier, lowest count first, and never moves a question across one.

    Three questions sit at `domain` on this part, so the ledger's counts decide the order those three are put
    in: the topic with the highest count goes last in its tier. The tier itself never moves, and the budget
    is the one below the cap whatever the counts say, because it is a whole tier lower.
    """
    def _domain(state) -> list[str]:
        tiers = {v["id"]: v["tier"] for v in gs.question_views(state)}
        return [q for q in state["asking"]["put"] if tiers[q] == "domain"]

    plain = _crowded()
    assert _domain(plain) == ["q_fluid_side", "q_unit", "q_unlabelled_mouth"]
    asked_unit = _crowded(asked_before={"unit": 4})
    assert _domain(asked_unit) == ["q_fluid_side", "q_unlabelled_mouth", "q_unit"]
    asked_side = _crowded(asked_before={"fluid_side": 4})
    assert _domain(asked_side) == ["q_unit", "q_unlabelled_mouth", "q_fluid_side"]
    for state in (plain, asked_unit, asked_side):
        tiers = {v["id"]: v["tier"] for v in gs.question_views(state)}
        assert tiers["q_fluid_side"] == tiers["q_unit"] == tiers["q_unlabelled_mouth"] == "domain"
        assert tiers["q_budget"] == "resolution"
        assert list(state["asking"]["held"]) == ["q_budget"]


def test_the_counts_a_composition_used_are_replayed_by_every_later_composition(side_on):
    """The counts move the ORDER of the ranked list and therefore the order of the survey's uncertainties, so a
    recomposition made with different counts is a survey `geometry_step` cannot reproduce and refuses to plan
    against. They ride on the row and `composed_inputs` hands them back."""
    state = _crowded(asked_before={"budget_envelope": 4})
    assert gs.composed_inputs(state)["asked_before"] == {"budget_envelope": 4}
    again = gs.recomposed(state, _doc("block_boss_sharp"))
    assert gs.composed_inputs(again)["asked_before"] == {"budget_envelope": 4}
    # and a row composed with none is byte for byte what it was: the key is absent, not empty
    assert "asked_before" not in _fresh("bend_elbow_001")["composed_for"]


def test_the_two_kinds_the_old_finder_could_not_raise_are_raised_here():
    """`dispatch_refusal` is the TOP of the tier order and decides whether the job runs at all;
    `flow_direction` is the only question the external path has. `contract.asking.uncertainties_from` has no
    finder for either, so on the product path neither had ever been put."""
    dispatch = _fresh("block_boss_sharp")
    first = gs.open_now(dispatch)[0]
    assert first["id"] == "q_dispatch" and first["tier"] == "dispatch"
    assert "the answer decides whether the job runs at all" in first["tier_why"]
    # the platform's own rule, named, with the file and the line it would raise at
    assert "engines/snappy/drivers.py" in first["why"]
    assert "the mesher raises on this before it starts" in first["why"]

    # the external half. The fixture's own brief states the axis, and cut 1 settles the question from it, so
    # this asks with a brief that does not: an unstated flow axis on a blunt body is a real abstention
    outside = gs.carry_answers(None, gs.compose(
        _doc("ahmed_variant_001"), purpose="external_cfd",
        brief="External aero on this body. Mesh budget: 5 million cells."))
    (flow,) = [v for v in gs.open_now(outside) if v["about"] == "flow_direction"]
    assert flow["id"] == "q_flow_direction" and flow["tier"] == "domain"
    # every direction is offered, because the builder's own default with no declared axis is +x whatever the
    # shape, and a brief that says "flow is along +X" cannot settle a question that does not offer x
    assert [o for o in flow["options"] if o.startswith("along")] == [
        f"along {s}{a}" for a in "xyz" for s in "+-"]


def test_a_question_the_brief_already_answered_is_not_put_and_is_not_on_the_survey():
    """CUT 1 from the brief. ahmed_variant_001's own brief says which way the flow comes, so the external
    question is settled by their own words and never reaches the customer or the builder's unsettled list."""
    state = _fresh("ahmed_variant_001", purpose="external_cfd")
    assert [v for v in gs.question_views(state) if v["about"] == "flow_direction"] == []
    assert gs.open_now(state) == []


def test_the_sentence_a_customer_reads_is_the_finders_own_and_the_record_that_labels_it_is_there():
    """`ask.say` writes the question and `ask.record` writes the row that turns the answer into a label.
    Neither ran on this path before: the questions were rendered by `contract.asking.questions_from`, whose
    role sentence begins "I could not settle opening.role on"."""
    view = _role_question(_fresh("bend_elbow_001"))
    assert view["text"].startswith("I can see two openings on this part.")
    assert "I could not settle" not in view["text"]
    assert view["finder"] == gs.QUESTION_FINDER
    record = view["record"]
    assert record["kind"] == "port_role" and record["question_id"] == view["id"]
    assert [t["place"] for t in record["targets"]] == ["o1", "o2"]
    assert all(t["field"] == f"openings[{t['place']}].role" for t in record["targets"])


# ITEM 18: THE THIRD INTAKE


def test_raising_the_budget_never_un_asks_the_question_the_customer_just_answered():
    """THE LEAK, settled. `ask.uncertainty.budget_uncertainty` reads `forecast.over_cap`, which is computed
    against whatever cap the composition was given. Composing the finder's document with the CONFIRMED cap
    therefore un-asked the question the customer had just answered: the trade came off the survey,
    `carry_answers` retired the answer with it, the confirmed cap went back to None, and `earlier` in
    `chain.job.third_intake` no longer held `budget`, so the third intake was free to put the budget a second
    time. The questions are found against the budget the customer WROTE; only the builder hears the one they
    confirmed."""
    from geometry_agent.chain.schema import canonical_kind

    brief = _brief("bend_elbow_001").replace("under 2 million cells", "under 100,000 cells")
    doc = _doc("bend_elbow_001")
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=brief))
    said = "o2 in, o1 out, and raise it"
    state = _say_role(state, "o2", "inlet", words="o2 in", said=said)
    state = _say_role(state, "o1", "outlet", words="o1 out", said=said)
    raise_to = [v for v in gs.question_views(state) if v["about"] == "cell_budget"][0]["options"][1]
    state = gs.record_answer(state, question_id="q_budget", choice=raise_to, words="raise it",
                             latest_user_message=said)
    cap = gs.confirmed_cell_cap(state)
    assert cap and cap > 100_000
    state = gs.recomposed(state, doc, cell_cap=cap)

    # the question is still the survey's, the answer is still live, and the cap survived the recomposition
    assert [v for v in gs.question_views(state) if v["about"] == "cell_budget"], "the trade was un-asked"
    assert gs.confirmed_cell_cap(state) == cap
    assert state["planner_block"]["customer_cell_cap"] == cap
    assert gs.builder_block(state)["survey"]["confirmed"]["cell_budget"]["value"] == cap

    # and this is the exact set `chain.job.third_intake` reads to decide whether the budget was asked before
    earlier = {canonical_kind(u["about"]) for u in state["survey"]["uncertainties"]
               if u["about"] in ("cell_budget", "opening.role", "unit")}
    assert "budget" in earlier, "the third intake would put the budget a second time"


# THE BLOCK'S WHOLE CONTRACT, AND ITS CEILING


def test_a_builder_control_and_an_outcome_claim_are_refused_by_BOTH_checkers():
    """THIS TEST USED TO PROVE A GAP AND NOW PROVES IT IS SHUT, which is the only honest way to keep it.

    `deliver.check_survey_block` ran four of the block's six rules. The other two, the builder-key refusal and
    the outcome-claim refusal, were private to `contract/survey.py` and fired only inside a `SurveyHandoff`'s
    validator, and the look's free text is added to the block AFTER that validator ran. So the package's own
    checker accepted both of these and only the platform's refused them.

    The package now runs all six off the same two public functions (`survey.refuse_builder_keys` and
    `survey.refuse_outcome_claims`), so `check_the_survey_block` is a second call of the same rules rather
    than the only one. Both are asserted here because a gap that closes in one place is how it reopens in the
    other: if either checker stops refusing, this fails.
    """
    from geometry_agent.contract import survey as pkg_survey
    from geometry_agent.contract.deliver import check_survey_block
    from geometry_agent.contract.marks import ContractError

    # one implementation, not a copy: the platform's checker and the package's run the same object
    assert pkg_survey.refuse_builder_keys is not None and pkg_survey.refuse_outcome_claims is not None

    good = gs.builder_block(_answered_elbow())["survey"]
    gs.check_the_survey_block(good)
    check_survey_block(good)
    for hurt, what in (({"kind": "seen", "tier": "relied_on", "value": "a shoulder", "field": "marks_seen",
                         "source": "look", "n_layers": "three"}, "n_layers"),
                       ({"kind": "seen", "tier": "relied_on", "value": "the mesh will collapse here",
                         "field": "marks_seen", "source": "look"}, "the mesh will")):
        block = {**good, "seen": {**good["seen"], "shoulder": hurt}}
        with pytest.raises(ContractError) as pkg_refusal:
            check_survey_block(block)
        assert what in str(pkg_refusal.value)
        with pytest.raises(gs.SurveyError) as refusal:
            gs.check_the_survey_block(block)
        assert what in str(refusal.value)


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
        cap = gs.block_list_max()
        assert len(row["subjects"]) == cap
        assert f"stand for {mouths - cap} more" in row["why"], row["why"]
    # and the survey on the row still names every one of them: what is shortened is the block, said out loud
    assert len(big["survey"]["uncertainties"][0]["subjects"]) == 1300
    # a part small enough to name all its mouths is untouched
    assert small == len(json.dumps(gs.builder_block(_fresh("bend_elbow_001"))["survey"]))


def test_a_long_confirmed_list_is_shortened_with_its_remainder_stated_and_never_goes_over_the_ceiling():
    """THE HALF OF ITEM 23, CLOSED IN THE PACKAGE, and this pins the behaviour that closed it.

    WHAT THIS TEST USED TO SAY, and why it had to be rewritten rather than deleted. It pinned the cost of the
    half that was open: `applies_to` was one entry per mouth one answer placed, so a customer who confirmed
    251 mouths pushed the block past `SURVEY_BLOCK_MAX` and `builder_block` handed over NOTHING - the right
    outcome of two bad ones, because a block that can be cut can lose what it was carrying. It said the fix
    belonged in `deliver.survey_block`. The fix is there now, in the wheel this repository vendors, so the old
    assertion was pinning a behaviour the product no longer has: it FAILED on platform main against the
    vendored wheel, which is the wheel CI installs, so the unit lane was red on it.

    WHAT THE PACKAGE DOES INSTEAD, measured on bend_elbow_001 with every mouth confirmed as wall, through the
    wheel in vendor/wheels/:

        mouths confirmed     2      20     21    100    250    300   1000
        block characters  1750    1868   1976   1977   1978   1978   1978
        names in applies_to  2      20     20     20     20     20     20

    so the list is shortened at `hexera.SURVEY_SUBJECTS_MAX` and the block is flat from there on. THE
    SHORTENING IS NOT A SILENT CUT, which is the whole difference and the only reason this is acceptable: the
    entry's own `why` says "the first 20 are named here and they stand for 280 more, and this answer is about
    every one of them". A builder that binds patches from what it is handed is told that the customer's answer
    covers mouths it cannot see, so it cannot read twenty as all of them.

    THE TWO THINGS PINNED HERE are the ones that would hurt if they changed: the block never goes over the
    ceiling however many mouths are confirmed, and a shortened list always states how many it stands for. A
    cut with no remainder is the failure the old assertion was protecting against, and it is still refused.
    """
    from geometry_agent.contract.deliver import SURVEY_BLOCK_MAX

    base = _fresh("bend_elbow_001")
    (role,) = [v for v in gs.question_views(base) if v["about"] == "opening.role"]
    sizes, named = {}, {}
    for mouths in (2, 250, 300, 1000):
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
        assert block is not None, (
            f"{mouths} confirmed mouths cost the builder the whole survey; the package shortens the list "
            f"instead of overflowing now, so there is nothing left for this to refuse")
        survey = block["survey"]
        sizes[mouths] = len(json.dumps(survey))
        # NEVER OVERSIZED. A block that can be cut is a block that can lose what it was carrying.
        assert sizes[mouths] <= SURVEY_BLOCK_MAX
        gs.check_the_survey_block(survey)
        (entry,) = [v for v in survey["confirmed"].values() if v.get("field") == "opening.role"]
        named[mouths] = len(entry["applies_to"])
        if named[mouths] < mouths:
            # A SHORTENED LIST SAYS SO. Twenty names read as twenty confirmed mouths unless the entry states
            # what they stand for, and the builder binds patches from what it is handed.
            assert str(mouths - named[mouths]) in str(entry.get("why") or ""), (
                f"{named[mouths]} of {mouths} confirmed mouths are named and the entry does not say how many "
                f"more they stand for: {entry.get('why')!r}. A cut with no remainder is a block that claims "
                f"fewer confirmed mouths than the customer gave.")
    assert sizes[250] > sizes[2], "a longer answer produces no larger a block at all, so nothing is carried"
    assert named[1000] == named[300] == named[250] < 250, (
        "the names are not being shortened, so the ceiling is reachable again and this test no longer "
        "measures what it says")


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
