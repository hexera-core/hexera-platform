"""A role the customer typed lost its protection the moment they delegated afterwards.

Two defects, mirror images of each other, both found by adversarial review and both reproduced on
bend_elbow_001 before being fixed. Between them they decide whether a boundary condition belongs to
the customer or to us, which is the only thing standing between the geometry agent and a role it may
not touch.

  1. THE PROPOSAL OVERWROTE THEIR WORD. `_the_proposal_recorded` writes the platform's map for EVERY
     place the question names, and `_corrected` retires whatever stood there first. So a customer who
     typed "o1 is the inlet" and then said "you decide the rest" had their own answer retired and
     replaced by ours, wearing the note that says they accepted a proposal. They accepted a proposal
     about the OTHER mouths. `_mentions` guards the mouths named in the latest message and cannot
     guard one named three turns ago.

  2. THEIR WORD LEFT THE PROPOSAL LIVE BESIDE IT. Restating a role we had already proposed retired
     nothing, because `_corrected`'s guard was `value != value`. The mouth then held two live rows
     and `intake_handoff.answer_for` returned the first, so the agent was licensed off the proposal.

A delegation does not withdraw an answer already given. It settles what is still open.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from meshpipeline.application import geometry_survey as gs

FIXTURE = pathlib.Path("tests/fixtures/geometry_survey/bend_elbow_001.json")


@pytest.fixture
def state():
    doc = json.loads(FIXTURE.read_text())
    return gs.compose(doc, purpose="internal_cfd", brief="internal cfd, air through it")


def _role_view(st):
    return next(v for v in gs.question_views(st) if v["about"] == "opening.role")


def _rows(st, subject):
    return [a for a in st["answers"]
            if a.get("about") == "opening.role" and a.get("subject") == subject]


def _live(st, subject):
    return [a for a in _rows(st, subject) if not a.get("retired")]


def test_a_typed_role_is_not_overwritten_by_a_later_delegation(state):
    view = _role_view(state)
    mine, other = view["subjects"][0], view["subjects"][1]
    st = gs.record_answer(state, question_id=view["id"], choice="inlet", subject=mine,
                          words=f"{mine} is the inlet", latest_user_message=f"{mine} is the inlet",
                          principal="cust")
    st = gs.record_answer(st, question_id=view["id"], choice="outlet", subject=other,
                          words="you decide the rest", latest_user_message="you decide the rest",
                          principal="cust", accepted_proposal=True)
    live = _live(st, mine)
    assert len(live) == 1, "their typed answer was retired by a delegation about other mouths"
    assert live[0]["value"] == "inlet", "our proposal replaced the role they typed"
    assert str(live[0].get("note") or "") != gs.ACCEPTED_THE_PROPOSAL, (
        "their own word was relabelled as an accepted proposal")


def test_a_restatement_retires_the_proposal_rather_than_standing_beside_it(state):
    view = _role_view(state)
    mouth = view["subjects"][0]
    st = gs.record_answer(state, question_id=view["id"], choice="outlet", subject=mouth,
                          words="good to go", latest_user_message="good to go",
                          principal="cust", accepted_proposal=True)
    st = gs.record_answer(st, question_id=view["id"], choice="outlet", subject=mouth,
                          words=f"{mouth} is the outlet",
                          latest_user_message=f"{mouth} is the outlet", principal="cust")
    live = _live(st, mouth)
    assert len(live) == 1, (
        "the mouth held two live rows and answer_for returns the first, which was the proposal")
    assert str(live[0].get("note") or "") != gs.ACCEPTED_THE_PROPOSAL


def test_the_retirement_says_the_value_did_not_change(state):
    """A record claiming the mesh changed when only the authority did is a fact that lies."""
    view = _role_view(state)
    mouth = view["subjects"][0]
    st = gs.record_answer(state, question_id=view["id"], choice="outlet", subject=mouth,
                          words="good to go", latest_user_message="good to go",
                          principal="cust", accepted_proposal=True)
    st = gs.record_answer(st, question_id=view["id"], choice="outlet", subject=mouth,
                          words=f"{mouth} is the outlet",
                          latest_user_message=f"{mouth} is the outlet", principal="cust")
    retired = [a for a in _rows(st, mouth) if a.get("retired")]
    assert len(retired) == 1
    assert retired[0]["retired_because"] == gs.SAID_IT_THEMSELVES


def test_a_genuine_change_of_mind_still_reads_as_superseded(state):
    view = _role_view(state)
    mouth = view["subjects"][0]
    st = gs.record_answer(state, question_id=view["id"], choice="outlet", subject=mouth,
                          words=f"{mouth} is the outlet",
                          latest_user_message=f"{mouth} is the outlet", principal="cust")
    st = gs.record_answer(st, question_id=view["id"], choice="inlet", subject=mouth,
                          words=f"no, {mouth} is the inlet",
                          latest_user_message=f"no, {mouth} is the inlet", principal="cust")
    retired = [a for a in _rows(st, mouth) if a.get("retired")]
    assert retired and retired[0]["retired_because"] == gs.SUPERSEDED
    assert _live(st, mouth)[0]["value"] == "inlet"


def test_an_unsettled_mouth_still_takes_the_proposal(state):
    """The change must not stop a delegation settling what is actually open."""
    view = _role_view(state)
    mouth = view["subjects"][0]
    st = gs.record_answer(state, question_id=view["id"], choice="outlet", subject=mouth,
                          words="you decide everything",
                          latest_user_message="you decide everything",
                          principal="cust", accepted_proposal=True)
    live = _live(st, mouth)
    assert len(live) == 1 and str(live[0].get("note") or "") == gs.ACCEPTED_THE_PROPOSAL
