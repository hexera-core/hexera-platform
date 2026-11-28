# Responsibility: an answer to the engine question is honoured even when the customer was slow.
# Boundaries: `engine_selection._answerable` / `_is_the_direct_reply`, and `choice_payload`.
# Collaborates with: agents/intake/agent.py, which calls confirm_by_assent before the model runs.
"""Job f55ef23e, 2026-11-28: the customer answered and the product asked again, word for word.

    06:16:46  Intake: engine selection PROPOSED engine=cfmesh_conjugate
    06:32:50  (the customer sends "OK")
    06:33:13  WARNING confirm rejected - no engine selection is awaiting confirmation
    06:33:18  Intake: engine selection PROPOSED engine=cfmesh_conjugate

`PROPOSED_TTL_S` is 900 s, so the proposal died at 06:31:46 and the answer landed sixty-four
seconds late. `state_of` read it as NO_SELECTION, `_answerable` said no question was open, and the
model re-asked. Nothing in the reply said the answer had been thrown away, and `choice_payload`
returns None for an expired proposal, so the console served `engine_choice: null` and
`choices: []` under a question the customer could still read. The second answer, to the fresh
proposal, bound immediately - which is what makes this a clock bug and not a grammar bug:
`plain_assent("OK")` was True the whole time.

WHAT THESE TESTS PIN. The count test is the real staleness guard and it is untouched: a proposal
is stale when the user has SAID SOMETHING ELSE since it was put. The clock no longer gets to
overrule that for the message that directly answers the question. Session and owner are still
checked, and an expired CONFIRMED selection is still expired.
"""
from __future__ import annotations

import time

import pytest

from meshpipeline.agents.intake import engine_selection as es
from meshpipeline.agents.intake.measured_choice import choice_payload

ENGINE = "cfmesh_conjugate"
SID, OWNER = "791dc56a-c7d1-464f-8e20-4abd44332b91", "dev-user"

#: The measured gap: the question was put 964 s before the answer arrived, against a 900 s fuse.
LATE_BY_S = 964.0


def proposed(*, count: int = 4, engine: str = ENGINE, session: str = SID, owner: str = OWNER):
    return es.propose(engine, session_id=session, owner_id=owner, revision="r1",
                      user_msg_count=count)


def aged(sel: dict, *, by: float = LATE_BY_S) -> dict:
    """The same proposal, `by` seconds after it was put. 964 s reproduces the real run."""
    return {**sel, "expires_at": sel["expires_at"] - by}


def fits(engine: str = ENGINE) -> dict:
    return {"engine": engine, "shape": "a multi-region fluid and solid assembly",
            "table_version": "2026-10-20-0ddaa08f-85664054",
            "fits": [{"engine": engine, "label": "Conjugate (coolant + metal)", "fit": "best fit",
                      "reason": "no lab results yet for shapes like this", "recommended": True},
                     {"engine": "snappy_multiregion", "label": "snappyHexMesh multi-region",
                      "fit": "not suited", "reason": "no lab results yet for shapes like this",
                      "outside": True}]}


def gate(sel: dict) -> dict:
    return {"selection": {**sel, "recommendation": fits(str(sel.get("engine") or ENGINE))}}


# ------------------------------------------------------------------ the clock is expired

def test_the_proposal_really_is_expired():
    """Otherwise every test below passes for the wrong reason."""
    old = aged(proposed())
    assert old["expires_at"] < time.time()
    assert es.state_of(old) == es.NO_SELECTION
    assert es.PROPOSED_TTL_S == 900
    assert LATE_BY_S > es.PROPOSED_TTL_S, "the fixture must reproduce a LATE answer"


# ------------------------------------------------------------------ the answer is honoured

def test_a_late_answer_to_the_open_question_is_answerable():
    """THE REGRESSION. Before the fix this returned the "no engine selection is awaiting
    confirmation" that the 06:33:13 log line records."""
    ok, why = es._answerable(aged(proposed(count=4)), session_id=SID, owner_id=OWNER,
                             user_msg_count=5)
    assert ok, f"the customer's answer was refused: {why!r}"


def test_a_late_plain_yes_binds_the_engine():
    """What the customer actually did, end to end, as `agent.py` runs it before the model."""
    got = es.confirm_by_assent(aged(proposed(count=4)), session_id=SID, owner_id=OWNER,
                               revision="r2", latest_user_message="OK", user_msg_count=5)
    assert got is not None, "a plain yes to the open question was thrown away"
    assert es.state_of(got) == es.CONFIRMED
    assert got["engine"] == ENGINE


@pytest.mark.parametrize("word", ["OK", "ok", "yes", "Yes, go ahead.", "sure", "proceed"])
def test_every_plain_assent_survives_the_wait(word):
    """`plain_assent` was never the problem; these all read True before the fix too. They are
    here so a future narrowing of the clock fix cannot quietly drop one of them."""
    assert es.plain_assent(ENGINE, word) is True
    got = es.confirm_by_assent(aged(proposed(count=4)), session_id=SID, owner_id=OWNER,
                               revision="r2", latest_user_message=word, user_msg_count=5)
    assert got is not None and es.state_of(got) == es.CONFIRMED


def test_a_late_pick_of_another_listed_engine_is_honoured_too():
    """The question lists every engine that can take the file. Waiting must not take away the
    other buttons either."""
    sel = {**aged(proposed(count=4)), "recommendation": fits()}
    got = es.choose_listed(sel, session_id=SID, owner_id=OWNER, revision="r2",
                           latest_user_message="Use snappyHexMesh multi-region",
                           user_msg_count=5)
    assert got is not None and got["engine"] == "snappy_multiregion"
    assert es.state_of(got) == es.CONFIRMED


def test_the_model_path_honours_a_late_answer_as_well():
    """`confirm` is what the model calls when it quotes the user. Same question, same answer."""
    got, why = es.confirm(aged(proposed(count=4)), session_id=SID, owner_id=OWNER, revision="r2",
                          quote="OK", latest_user_message="OK", user_msg_count=5)
    assert got is not None and why == ""
    assert es.state_of(got) == es.CONFIRMED


# ------------------------------------------------------------------ what is NOT relaxed

def test_a_late_answer_is_still_stale_if_the_user_said_something_else():
    """THE GUARD THAT MATTERS, and it is untouched. The count test is what "stale" means.

    The REFUSAL is the subject here, not its wording: an expired proposal that is not the direct
    reply never reaches the count test, because `slow_but_direct` is what carried it past the
    closed-question branch and that is exactly what an intervening message takes away. Both
    sentences are honest about this case - no question is open AND the user moved on - so pinning
    which one comes back would pin the order of two branches rather than the behaviour. The
    wording of the count refusal is pinned on a FRESH proposal, in
    `test_a_fresh_proposal_behaves_exactly_as_before`, where it is the only branch that can fire.
    """
    ok, why = es._answerable(aged(proposed(count=4)), session_id=SID, owner_id=OWNER,
                             user_msg_count=7)
    assert not ok
    assert why, "a refusal has to say why"
    assert es._is_the_direct_reply(aged(proposed(count=4)), 7) is False
    # and the answer is not bound by any of the three paths a customer's words travel
    for fn, kw in ((es.confirm_by_assent, {}),
                   (es.choose_listed, {})):
        assert fn({**aged(proposed(count=4)), "recommendation": fits()}, session_id=SID,
                  owner_id=OWNER, revision="r2", latest_user_message="yes",
                  user_msg_count=7, **kw) is None
    got, _why = es.confirm(aged(proposed(count=4)), session_id=SID, owner_id=OWNER,
                           revision="r2", quote="yes", latest_user_message="yes",
                           user_msg_count=7)
    assert got is None


def test_an_expired_confirmed_selection_stays_expired():
    """`CONFIRMED_TTL_S` has its own job - stopping a selection outliving a long conversation -
    and `test_lifecycle_states_and_expiry` pins it. Only PROPOSED is read from the stored state."""
    conf = es.confirm_by_assent(proposed(count=4), session_id=SID, owner_id=OWNER, revision="r2",
                                latest_user_message="yes", user_msg_count=5)
    assert conf is not None
    old = {**conf, "expires_at": time.time() - 1}
    assert es.state_of(old) == es.NO_SELECTION
    ok, why = es._answerable(old, session_id=SID, owner_id=OWNER, user_msg_count=6)
    assert not ok and "awaiting confirmation" in why


def test_a_late_answer_from_another_session_is_still_refused():
    ok, why = es._answerable(aged(proposed(count=4, session="someone-else")),
                             session_id=SID, owner_id=OWNER, user_msg_count=5)
    assert not ok and "different session or owner" in why


def test_a_late_answer_from_another_owner_is_still_refused():
    ok, why = es._answerable(aged(proposed(count=4, owner="not-dev-user")),
                             session_id=SID, owner_id=OWNER, user_msg_count=5)
    assert not ok and "different session or owner" in why


def test_no_proposal_at_all_is_still_no_question():
    for sel in (None, {}, {"state": es.NO_SELECTION, "expires_at": time.time() + 900}):
        ok, why = es._answerable(sel, session_id=SID, owner_id=OWNER, user_msg_count=5)
        assert not ok, f"{sel!r} was treated as an open question"
        assert "awaiting confirmation" in why


def test_a_late_declining_answer_still_declines():
    """Honouring a slow answer means honouring a slow NO."""
    got = es.confirm_by_assent(aged(proposed(count=4)), session_id=SID, owner_id=OWNER,
                               revision="r2", latest_user_message="No, not that one",
                               user_msg_count=5)
    assert got is None


def test_a_proposal_with_no_count_is_not_rescued_by_the_clock_fix():
    """Without `proposed_msg_count` there is nothing to prove the message is the direct reply, so
    an expired proposal stays closed rather than being answerable forever."""
    sel = aged(proposed(count=4))
    sel.pop("proposed_msg_count")
    assert es._is_the_direct_reply(sel, 5) is False
    ok, _why = es._answerable(sel, session_id=SID, owner_id=OWNER, user_msg_count=5)
    assert not ok


def test_a_fresh_proposal_behaves_exactly_as_before():
    ok, why = es._answerable(proposed(count=4), session_id=SID, owner_id=OWNER, user_msg_count=5)
    assert ok and why == ""
    ok, why = es._answerable(proposed(count=4), session_id=SID, owner_id=OWNER, user_msg_count=9)
    assert not ok and "said something else since" in why


# ------------------------------------------------------------------ the buttons stay on screen

def test_the_console_still_draws_the_buttons_for_a_late_question():
    """THE OTHER HALF OF THE DEFECT. The customer could read the question while the API served
    `engine_choice: null` beside it, so there was nothing to click and no way to answer except by
    typing into what turned out to be a void."""
    got = choice_payload(gate(aged(proposed(count=4))))
    assert got is not None, "the open question was served with no engines to pick"
    assert got["proposed"] == ENGINE
    assert [e["engine"] for e in got["engines"]] == [ENGINE, "snappy_multiregion"]
    assert [e["reply"] for e in got["engines"]] == ["Use Conjugate (coolant + metal)",
                                                    "Use snappyHexMesh multi-region"]
    assert got["engines"][0]["recommended"] is True


def test_a_fresh_question_draws_the_same_buttons():
    fresh = choice_payload(gate(proposed(count=4)))
    late = choice_payload(gate(aged(proposed(count=4))))
    assert fresh == late, "waiting changed what the console draws"


def test_a_confirmed_selection_draws_no_buttons():
    """The question is answered; there is nothing to pick."""
    conf = es.confirm_by_assent(proposed(count=4), session_id=SID, owner_id=OWNER, revision="r2",
                                latest_user_message="yes", user_msg_count=5)
    assert conf is not None
    assert choice_payload({"selection": {**conf, "recommendation": fits()}}) is None


def test_no_recommendation_still_draws_nothing():
    """The payload exists to draw a MEASURED recommendation; without fits there is nothing."""
    assert choice_payload({"selection": aged(proposed(count=4))}) is None
    assert choice_payload({"selection": {**aged(proposed(count=4)),
                                         "recommendation": {"fits": []}}}) is None


def test_a_gate_that_is_not_a_mapping_draws_nothing():
    for bad in (None, [], "selection", 7):
        assert choice_payload(bad) is None
