# Responsibility: hold the line between a role the customer TYPED and a role this application proposed and they waved through, across the handoff to the geometry agent.
# Boundaries: pure functions over one stored measurement; the conversation, the database and the agent's own loop are other tests.
"""THE DEFECT, MEASURED. On survey row 30660c74 (2026-09-26 06:12) the stored answers read

    o12 ~ outlet ~ customer ~ "you decide everything" ~ accepted the setup the application proposed
    o14 ~ outlet ~ customer ~ "you decide everything" ~ accepted the setup the application proposed
    o16 ~ outlet ~ customer ~ "you decide everything" ~ accepted the setup the application proposed

and the geometry step ended `failed` with no plan at all, because `contract.given.check_plan` had refused the
plan three times:

    "the plan calls o12 'wall' and the customer confirmed 'outlet': the agent decides what to do with a role,
     never which role a mouth has once a person has said"

The customer never said. `_the_proposal_recorded` wrote OUR reading of those mouths under their name when they
said "you decide everything", and the geometry agent - which on the sibling part says in as many words that such
a mouth "is the end face of an inner body ... an obstacle face, not a port" - was then forbidden from correcting
our own guess.

THE SYSTEM ALREADY DISAGREED WITH ITSELF. `geometry_agent.learn.schema.ANSWER_VERDICTS` scores a delegation
`not_a_label` and never `confirmed`, because counting one as a confirmation "would report 'the customer agreed
with us' on exactly the jobs where they said nothing about the substance, which is a fact that lies". This test
holds the two halves to the same reading, and holds a role the customer TYPED exactly where it was.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.contract.given", needs="the authority of a role that crosses to the geometry agent")

from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE = "bend_elbow_001"


def _package():
    from geometry_agent.contract import given, intake
    return given, intake


def _doc() -> dict:
    return json.loads((FIXTURES / f"{CASE}.json").read_text(encoding="utf-8"))


def _fresh() -> dict:
    brief = (FIXTURES / f"{CASE}.brief.txt").read_text(encoding="utf-8")
    return gs.carry_answers(None, gs.compose(_doc(), purpose="internal_cfd", brief=brief))


def _role_question(state) -> dict:
    (question,) = [v for v in gs.question_views(state) if v["about"] == "opening.role"]
    return question


def _the_field_exists() -> bool:
    """Does the INSTALLED package carry the field this round added?

    The stack installs a vendored wheel and it is vendored once, at the end of a round, so the platform has to
    run correctly against the version that predates the field. The test below that needs it says so and skips;
    the two that do not are the ones that hold the compatibility promise, and they always run.
    """
    _given, intake = _package()
    return "value_from" in (getattr(intake.Answer, "model_fields", {}) or {})


def test_the_handoff_is_built_whatever_version_of_the_package_is_installed():
    """THE COMPATIBILITY PROMISE, and it fails differently from the thing it checks: it constructs the answer
    rather than asking the schema what it would accept. A keyword the installed `Answer` has no room for is a
    validation error (`extra="forbid"`) on every job, and the geometry step would then fail for every customer
    until the wheel caught up."""
    state = _fresh()
    view = _role_question(state)
    state = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                             latest_user_message="you decide everything", accepted_proposal=True,
                             principal="owner-7f3a")
    _given, intake = _package()
    for row in gs.live_answers(state):
        extra = gs.whose_value(row)
        assert set(extra) <= {"value_from"}
        intake.Answer(question_id=row["question_id"], about=row["about"], subject=str(row.get("subject") or ""),
                      value=row.get("value"), answered_by=gs.CUSTOMER, at=str(row["at"]),
                      note=str(row.get("note") or ""), **extra)
    handoff = gs.intake_handoff(state)
    assert [a.question_id for a in handoff.answers] == [view["id"], view["id"]]


def test_the_row_still_records_the_acceptance_as_the_customers_answer():
    """WHAT DOES NOT MOVE. They did answer - they were shown a setup and said go - so the row is the customer's
    and the platform's own gates are untouched: `confirmed_roles` still has both mouths and `role_problems`
    still passes. Only the AUTHORITY over which role each mouth has changes, and only past the handoff."""
    state = _fresh()
    view = _role_question(state)
    state = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                             latest_user_message="you decide everything", accepted_proposal=True)
    assert gs.confirmed_roles(state) == view["proposal"]
    assert all(a["note"] == gs.ACCEPTED_THE_PROPOSAL for a in gs.live_answers(state))
    assert {v["id"]: v["status"] for v in gs.question_views(state)}[view["id"]] == "answered"


@pytest.mark.skipif(not _the_field_exists(),
                    reason="the installed geometry_agent wheel predates contract.intake.Answer.value_from; "
                           "this runs the moment the wheel is vendored")
def test_a_role_we_proposed_crosses_as_ours_and_a_role_they_typed_crosses_as_theirs():
    """THE FIX, both halves of it, on one part.

    A mouth they waved through: the agent may call it something else and the checker says nothing. A mouth they
    NAMED: the same plan is refused in the same words as before. The two answers differ in one field, and that
    field is the only thing between them.
    """
    given_mod, intake = _package()
    state = _fresh()
    view = _role_question(state)
    mouths = sorted(view["proposal"])
    waved = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                             latest_user_message="you decide everything", accepted_proposal=True)
    handoff = gs.intake_handoff(waved)
    assert {a.value_from for a in handoff.answers} == {intake.VALUE_PROPOSED}
    assert not any(a.their_own_value() for a in handoff.answers)

    given = given_mod.Given.of(gs.survey_of(waved), handoff)
    for mouth in mouths:
        assert given.role_of(mouth) is None, "our own proposal is not the customer's confirmation"
        assert given.role_source(mouth)[0] == given_mod.PROPOSED
        assert given.proposed_role_of(mouth).value == view["proposal"][mouth]
    other = {"inlet": "outlet", "outlet": "inlet"}
    swapped = {"patches": [{"id": m, "type": other[view["proposal"][m]]} for m in mouths],
               "unit": {"assumed": "mm"}}
    assert given_mod.check_plan(swapped, given) == [], "the agent corrects a role of ours"
    reported = given_mod.unconfirmed_roles(swapped, given)
    assert set(reported) == set(mouths) and all(r["intake_from"] == given_mod.PROPOSED
                                               for r in reported.values()), (
        "it is still reported: nobody confirmed it, and the builder is told so")

    # AND THE SAME ANSWER, TYPED, IS STILL UNTOUCHABLE.
    named = state
    for mouth, role in sorted(view["proposal"].items()):
        named = gs.record_answer(named, question_id=view["id"], words=f"{mouth} is the {role}",
                                 latest_user_message=f"{mouth} is the {role}", choice=role, subject=mouth)
    typed = gs.intake_handoff(named)
    assert {a.value_from for a in typed.answers} == {intake.VALUE_TYPED}
    assert all(a.their_own_value() for a in typed.answers)
    theirs = given_mod.Given.of(gs.survey_of(named), typed)
    problems = given_mod.check_plan(swapped, theirs)
    assert sum(1 for p in problems if "the customer confirmed" in p) == len(mouths)
    assert given_mod.DISPUTE_INSTRUCTION in problems, "the refusal says what the plan may do instead"
    condemning, per_patch = given_mod.plan_disputes(swapped, theirs)
    assert len(condemning) == len(mouths) and per_patch == {}, (
        "a role they typed condemns the plan; there is no partial shape that survives it")


def test_a_plan_the_survey_sent_back_leaves_the_row_saying_what_the_disagreement_WAS():
    """THE OTHER HALF OF THE MEASURED DEFECT. Three disputed mouths ended the step `failed`, and the row kept a
    count ("sent_back_by_the_survey: 3") and a reason that said "3 submissions rejected" - nothing that names
    the mouths. `survey_disagreement` carries the boundary's own sentences onto the row, whole and unparsed, so
    the disagreement survives the plan that was thrown away.

    It reads the trace and nothing else, which is why it can be checked here without a model: the trace is the
    loop's record of what each submission was refused for, and `rejection_kind: given` is the survey boundary's
    own kind (`agent.loop.GIVEN_KIND`).
    """
    from meshpipeline.application import geometry_step as gst

    said = ("the plan calls o12 'wall' and the customer confirmed 'outlet': the agent decides what to do with "
            "a role, never which role a mouth has once a person has said")
    trace = [{"round": 3, "accepted": False, "rejection_kind": "checker", "errors": ["a missing field"]},
             {"round": 5, "accepted": False, "rejection_kind": "given", "errors": ["an earlier disagreement"]},
             {"round": 7, "accepted": False, "rejection_kind": "given", "errors": [said, "and about o14"]}]
    assert gst.survey_disagreement(trace) == [said, "and about o14"], (
        "the last rejection is the one the run ended on, and the sentences are carried whole")
    assert gst.survey_disagreement([t for t in trace if t["rejection_kind"] != "given"]) == []
    assert gst.survey_disagreement(None) == [] and gst.survey_disagreement([]) == []
    assert gst.survey_disagreement([{"rejection_kind": "given", "errors": ["x" * 400]}]) == ["x" * 300]
    assert len(gst.survey_disagreement([{"rejection_kind": "given", "errors": [f"e{i}" for i in range(40)]}])) == 12
    # and a row that no longer carries a plan does not carry the last plan's disagreement either
    assert "sent_back_because" in gst.STALE_ON_A_FAILED_PLAN
