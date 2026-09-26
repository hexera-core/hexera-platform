# Responsibility: the platform's side of a proposal that leaves a face the measurement calls an obstacle face out of the flow.
# Boundaries: pure functions over one stored measurement; the agent's own proposal is `geometry_agent/tests/test_ask_not_a_mouth.py`, the conversation and the database are other tests.
"""THE DEFECT, MEASURED LIVE. Two conversations on cht_block_serpentine (survey rows 9fe18cd7 and fcb5d648,
2026-09-26 11:17 and 11:40, `["internal cfd, air through it", "you decide everything", "go"]`) ended
`grounding_rejected` with NO plan on the geometry agent's own words:

    patch o23 is the end face of an inner body (a centre body) inside the wall shell; it is an obstacle face,
    not a port; the port is the ring's bore around it

Row 9fe18cd7's stored proposal places all 24 measured faces, `o23: outlet` and `o24: outlet` among them, and its
24 answers all carry `note: "accepted the setup the application proposed"`. Under `wall_shell`
`catalog.port_openings` names nothing on that part, so the candidate set falls back to every cap and ring and TEN
of the 24 are the end caps of the nested region r2. The agent is right, its checker is right, the PROPOSAL was
wrong.

WHAT THIS FILE HOLDS, and it is the platform's half of the fix rather than the fix:

  * the proposal may now place a role that is NOT a flow role, and the recording path has to carry it whole -
    into `confirmed_roles`, into the handoff, with the authority of a proposal and not of the customer's word;
  * a plan that reads those faces as walls has to pass `contract.given.check_plan`, because a refusal the model
    can satisfy only by calling a solid face a port is a refusal that costs three submissions and the plan;
  * and `role_problems`, the last gate before a boundary condition, has to refuse a port declared on one of them.

WHICH HALF IS LIVE. The proposal itself is `geometry_agent.ask`'s, the stack installs a VENDORED WHEEL
(hexera_geometry_agent 0.1.0+g1ce2925ddc) and it is vendored once at the end of a round, so today's wheel still
proposes `outlet` on those faces and no live run can show the change. Every test here therefore STATES the
proposal rather than asking the package for one: that is what makes them run today, and it is also what makes
them fail differently from the thing they check.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests._surveyor_package import require

require("geometry_agent.contract.given", needs="the authority of a role that crosses to the geometry agent")

from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE = "cht_enclosing_2region"

#: The brief of the live rows, not the fixture's own, and the representation those rows were composed for. On
#: this file the geometry reads the same both ways (`catalog.side_reading`), so the live conversation asked which
#: side is the fluid and the answer settled `wall_shell` - which is the representation the agent's checker calls
#: these faces obstacle faces under, and which the fixture's own brief does not reach.
BRIEF = "internal cfd, air through it"
REPRESENTATION = "wall_shell"


def _doc() -> dict:
    return json.loads((FIXTURES / f"{CASE}.json").read_text(encoding="utf-8"))


def _inner_body_end_caps(doc: dict) -> list[str]:
    """The faces the checker's criterion names, read straight off the fixture's own facts.

    THE CRITERION IS SPELLED OUT HERE ON PURPOSE - `o.kind == "cap"`, its region `nested_in` another - so this
    file never asks either side which faces it thinks are which. `agent.grounding._check_patches` is the reader
    that refuses them and `ask.uncertainty.obstacle_faces` is the reader that abstains on them; a test that
    called one of those would pass on a day both were wrong.
    """
    facts = doc.get("facts") or {}
    inner = {r["id"] for r in (facts.get("regions") or []) if r.get("nested_in")}
    return sorted(o["id"] for o in (facts.get("openings") or [])
                  if o.get("kind") == "cap" and o.get("region_id") in inner)


def _fresh() -> dict:
    return gs.carry_answers(None, gs.compose(_doc(), purpose="internal_cfd", brief=BRIEF,
                                             confirmed_representation=REPRESENTATION))


def _role_question(state: dict) -> dict:
    (question,) = [v for v in gs.question_views(state) if v["about"] == "opening.role"]
    return question


def _with_walls(state: dict, places: list[str]) -> tuple[dict, dict[str, str]]:
    """The state with the stored proposal placing `places` as `wall`, which is what the package proposes once
    the wheel carries `ask.uncertainty.obstacle_faces`. Returns the state and the proposal it now holds."""
    view = _role_question(state)
    proposal = {p: ("wall" if p in places else r) for p, r in view["proposal"].items()}
    asking = dict(state["asking"])
    asking["proposal"] = {**dict(asking.get("proposal") or {}), view["id"]: proposal}
    return {**state, "asking": asking}, proposal


def test_the_fixture_carries_the_shape_the_defect_needs():
    """One body nested inside another, with a flat end cap of its own, and the composed representation the
    checker's rule is written against. Without all three there is nothing for the rest of this file to be about.
    """
    doc = _doc()
    caps = _inner_body_end_caps(doc)
    assert caps == ["o10", "o12", "o14", "o16", "o18", "o8"]
    state = _fresh()
    assert state["composed_for"]["representation"] == REPRESENTATION
    view = _role_question(state)
    assert set(caps) <= set(view["subjects"]), "the builder cuts them today, so they are still asked about"
    assert "wall" in view["options"], "the answer the proposal needs is one of the question's own"


def test_the_platform_records_a_wall_the_proposal_places_and_it_stays_ours():
    """The recording path took the proposal's roles on trust and every one of them was a flow role. A `wall` has
    to travel the same way: through `_role_answer`'s own vocabulary into the row, into `confirmed_roles`, and
    across the handoff wearing `value_from: proposed` - our reading, accepted, never their word."""
    from geometry_agent.contract import intake

    caps = _inner_body_end_caps(_doc())
    state, proposal = _with_walls(_fresh(), caps)
    view = _role_question(state)
    state = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                             latest_user_message="you decide everything", accepted_proposal=True,
                             principal="owner-7f3a")
    assert gs.confirmed_roles(state) == proposal
    assert [gs.confirmed_roles(state)[c] for c in caps] == ["wall"] * len(caps)
    assert all(a["note"] == gs.ACCEPTED_THE_PROPOSAL for a in gs.live_answers(state))
    assert {v["id"]: v["status"] for v in gs.question_views(state)}[view["id"]] == "answered"

    handoff = gs.intake_handoff(state)
    by_mouth = {a.subject: a for a in handoff.answers}
    assert [by_mouth[c].value for c in caps] == ["wall"] * len(caps)
    assert {a.value_from for a in handoff.answers} == {intake.VALUE_PROPOSED}
    assert not any(a.their_own_value() for a in handoff.answers), "we proposed it; they named nothing"


def test_a_plan_that_reads_those_faces_as_walls_is_not_refused():
    """THE TRAP THIS CLOSES. The agent's own checker refuses an inlet or an outlet on one of these faces, so a
    plan that keeps our `outlet` cannot pass it; if the survey boundary then refused the plan for correcting us,
    there would be no submission that passes both, and three rounds later the step ends with no plan. Here the
    plan agrees with the proposal AND the plan that overrides it are both accepted, because nobody has said
    which role these mouths have - and every role nobody confirmed is still reported to the builder."""
    from geometry_agent.contract import given as given_mod

    caps = _inner_body_end_caps(_doc())
    state, proposal = _with_walls(_fresh(), caps)
    view = _role_question(state)
    state = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                             latest_user_message="you decide everything", accepted_proposal=True)
    given = given_mod.Given.of(gs.survey_of(state), gs.intake_handoff(state))
    for mouth in proposal:
        assert given.role_of(mouth) is None
        assert given.role_source(mouth)[0] == given_mod.PROPOSED

    agrees = {"patches": [{"id": m, "type": r} for m, r in sorted(proposal.items())], "unit": {"assumed": "mm"}}
    assert given_mod.check_plan(agrees, given) == []
    # the agent reading MORE of them as solid than we proposed, which is the correction that used to be fatal
    overrides = {"patches": [{"id": m, "type": ("wall" if m in caps or r == "outlet" else r)}
                             for m, r in sorted(proposal.items())], "unit": {"assumed": "mm"}}
    assert given_mod.check_plan(overrides, given) == []
    assert given_mod.DISPUTE_INSTRUCTION not in given_mod.check_plan(overrides, given)
    reported = given_mod.unconfirmed_roles(agrees, given)
    assert set(reported) == {m for m, r in proposal.items() if r in ("inlet", "outlet")}
    assert all(r["intake_from"] == given_mod.PROPOSED for r in reported.values())
    assert not (set(reported) & set(caps)), "a wall is not a flow role and is not reported as one"


def test_the_last_gate_refuses_a_port_declared_on_a_face_the_proposal_walled():
    """`role_problems` is the last place a role can be stopped before `engines/port_binding.py` binds a boundary
    condition to it. Once the proposal says `wall`, a submission that declares one of those faces an outlet
    disagrees with the record, and the refusal names the mouth and the role the record holds.

    THE SAME GATE HAS TO PASS THE PORTS THAT REMAIN, which is the second half and the one that makes the first
    mean something: a refusal that fires on every submission is not a check.
    """
    caps = _inner_body_end_caps(_doc())
    doc = _doc()
    state, proposal = _with_walls(_fresh(), caps)
    view = _role_question(state)
    state = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                             latest_user_message="you decide everything", accepted_proposal=True)

    on_the_lump = {"name": "outlet_on_the_centre_body", "type": "outlet", "opening_id": caps[0]}
    problems = gs.role_problems(state, doc, [on_the_lump])
    assert len(problems) == 1
    assert caps[0] in problems[0] and "the wall" in problems[0]
    # AND IT NAMES WHOEVER ACTUALLY SAID IT. They said "you decide everything"; the wall is our reading of that
    # mouth, and a refusal telling the model the CUSTOMER called it that sends it back to the wrong person.
    assert gs.roles_we_proposed(state) == set(proposal)
    assert "the setup you showed them placed as the wall" in problems[0]
    assert "the customer called" not in problems[0]

    real = next(m for m, r in sorted(proposal.items()) if r == "outlet")
    assert gs.role_problems(state, doc, [{"name": "outlet_1", "type": "outlet", "opening_id": real}]) == []
