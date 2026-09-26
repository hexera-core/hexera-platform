"""The corrected payload was dispatched and announced, and the row still held the role we had proposed.

`_apply_the_corrections` rewrites the submitted patches and the confirmation announces the change.
`role_problems` compares every patch's role against `confirmed_roles`, which reads the answer rows - and
those were left at our original proposal, so the dispatched payload was in conflict with the row from the
moment it was sent.

MEASURED by the adversarial reviewer on live row bff327ee against the dispatched payload of job a429c5fb:
`confirmed_roles` = {o1: inlet, o4: outlet, o6: outlet}; `role_problems` on the uncorrected payload is [];
on the corrected payload the customer was SHOWN it is three refusals. The customer reads a confirmation
naming three corrected roles, asks for anything that needs a re-submit, and the submission carrying the
roles they were just shown is refused before the correction code runs again.

A second reading of our own guess is still our guess: the row keeps its own note, `roles_we_proposed`
counts it, and the agent may re-read it again. What changes is which of OUR readings the row holds.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from meshpipeline.application import geometry_survey as gs

FIXTURE = pathlib.Path("tests/fixtures/geometry_survey/bend_elbow_001.json")


@pytest.fixture
def proposed():
    """A row whose role for the first mouth is OURS, waved through with a delegation."""
    doc = json.loads(FIXTURE.read_text())
    st = gs.compose(doc, purpose="internal_cfd", brief="internal cfd, air through it")
    view = next(v for v in gs.question_views(st) if v["about"] == "opening.role")
    mouth = view["subjects"][0]
    st = gs.record_answer(st, question_id=view["id"], choice="outlet", subject=mouth,
                          words="you decide everything", latest_user_message="you decide everything",
                          principal="cust", accepted_proposal=True)
    return st, mouth


def test_the_row_carries_the_agents_reading_after_it_corrects_our_proposal(proposed):
    st, mouth = proposed
    assert gs.confirmed_roles(st)[mouth] == "outlet"
    st = gs.the_agent_corrected(st, subject=mouth, value="wall", principal="cust")
    assert gs.confirmed_roles(st)[mouth] == "wall", (
        "the payload was rewritten to wall and the row still said outlet, so the next submission of the "
        "roles the customer was shown is refused")


def test_it_is_still_ours_afterwards(proposed):
    """A second reading of our own guess is our guess. The agent must be free to read it again."""
    st, mouth = proposed
    st = gs.the_agent_corrected(st, subject=mouth, value="wall", principal="cust")
    assert mouth in gs.roles_we_proposed(st)


def test_it_refuses_outright_on_a_role_the_customer_typed(proposed):
    """The third door into the room the other two authority defects were found in today."""
    doc = json.loads(FIXTURE.read_text())
    st = gs.compose(doc, purpose="internal_cfd", brief="internal cfd, air through it")
    view = next(v for v in gs.question_views(st) if v["about"] == "opening.role")
    mouth = view["subjects"][0]
    st = gs.record_answer(st, question_id=view["id"], choice="outlet", subject=mouth,
                          words=f"{mouth} is the outlet",
                          latest_user_message=f"{mouth} is the outlet", principal="cust")
    with pytest.raises(gs.SurveyError, match="their own word|theirs alone"):
        gs.the_agent_corrected(st, subject=mouth, value="wall", principal="cust")
    assert gs.confirmed_roles(st)[mouth] == "outlet", "their word survived the attempt"


def test_the_earlier_reading_is_retired_not_deleted(proposed):
    st, mouth = proposed
    st = gs.the_agent_corrected(st, subject=mouth, value="wall", principal="cust")
    rows = [a for a in st["answers"]
            if a.get("about") == "opening.role" and a.get("subject") == mouth]
    retired = [a for a in rows if a.get("retired")]
    assert len(retired) == 1 and retired[0]["value"] == "outlet"
    assert retired[0]["retired_because"] == gs.AGENT_CORRECTED_THE_PROPOSAL
    assert len([a for a in rows if not a.get("retired")]) == 1


def test_a_mouth_with_no_live_row_is_left_alone(proposed):
    st, _mouth = proposed
    before = json.dumps(st["answers"], sort_keys=True)
    st = gs.the_agent_corrected(st, subject="o_not_here", value="wall", principal="cust")
    assert json.dumps(st["answers"], sort_keys=True) == before
