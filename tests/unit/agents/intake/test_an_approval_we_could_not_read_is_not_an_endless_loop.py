"""A customer said "good to go" and never got a mesh.

MEASURED 2026-09-26, 12 parts x 3 phrasings on a frozen HEAD. The two scripts ending in "go" submitted
24 of 24. The one ending in "good to go" stalled 12 of 12, every run to the 9-turn cap, with the same
confirmation re-shown five to seven times. The conversation was correct in every other respect: the
requirements were right, the customer's consent was plain, and the platform had no way to say so.

TWO HALVES, and the second is the one that matters.

The vocabulary was short. `_APPROVE` is a closed, whole-message set - deliberately, because "yes, but make
the far-field 50 chords" must never dispatch the stale requirements - and it held "go", "go ahead" and
"looks good proceed" but not "good to go". So the message fell through to CORRECTION_INTENT.

And a correction that changes nothing said nothing. `message._settle` invalidates the pending approval on a
correction and hands the turn back to intake for a fresh proposal. When the message really was an approval,
that fresh proposal is the SAME proposal: the customer sees the identical screen and repeats themselves. No
word list is ever complete, so the loop has to be closed on the shape of the failure rather than on the
phrase: an invalidated approval whose fingerprint equals the one about to be offered means the last message
changed nothing.
"""
from __future__ import annotations

from meshpipeline.agents.intake import approval as ap


def test_good_to_go_approves():
    """The measured phrase, and the ones next to it that a person actually types."""
    for said in ("good to go", "all good", "looks good", "sounds good", "go for it", "send it",
                 "ship it", "lgtm", "that works", "works for me", "fine", "fine by me",
                 "continue", "carry on", "go on", "mesh it", "no changes", "as is",
                 "happy with that", "we are good", "run the mesh"):
        assert ap.classify(said) == ap.APPROVE_INTENT, said


def test_the_whole_message_rule_still_holds():
    """The reason the set is closed: agreement WITH A CHANGE must not dispatch the stale requirements."""
    for said in ("good to go but make the far-field 50 chords",
                 "looks good, make it finer",
                 "all good, but o2 is the inlet",
                 "fine, use 3 million cells",
                 "continue with standard fidelity instead"):
        assert ap.classify(said) == ap.CORRECTION_INTENT, said


def test_a_refusal_is_still_a_refusal():
    for said in ("no", "not yet", "stop", "wait", "change the inlet to o2", "hold on"):
        assert ap.classify(said) != ap.APPROVE_INTENT, said


def test_a_hedge_still_asks():
    for said in ("maybe", "i think so", "not sure", "probably"):
        assert ap.classify(said) == ap.HEDGE_INTENT, said


def test_punctuation_and_filler_around_the_new_words():
    for said in ("ok, good to go!", "good to go, thanks", "Good To Go.", "good to go. proceed.",
                 "alright, looks good", "great, all good"):
        assert ap.classify(said) == ap.APPROVE_INTENT, said


def test_the_new_words_are_reachable_through_the_composition_path_too():
    """`_parses_as` composes over the same closed vocabulary, so a new phrase composes like the old ones."""
    assert ap.classify("good to go proceed with mesh generation") == ap.APPROVE_INTENT
    assert ap.classify("confirmed good to go") == ap.APPROVE_INTENT
    assert ap.classify("maybe good to go") == ap.HEDGE_INTENT, "a hedge dilutes consent: ask, never dispatch"


def _loops(previous: dict | None, fingerprint: str) -> bool:
    """The condition `_do_submit_requirements` reads, as a function of what it depends on."""
    return (isinstance(previous, dict)
            and str(previous.get("status") or "") == ap.INVALIDATED
            and str(previous.get("fingerprint") or "") == fingerprint)


def _an_approval(fingerprint: str) -> dict:
    return ap.create(owner_id="o", session_id="s", selection_id="sel", token_id="t",
                     canonical={"a": 1}, fingerprint=fingerprint, payload={}, summary="",
                     proposal_revision="r1", proposal_msg_count=1)


def test_a_correction_that_changed_nothing_is_the_signature_of_a_misread():
    live = _an_approval("fp-same")
    assert not _loops(live, "fp-same"), "a LIVE approval is not a loop, it is the normal path"
    dead = ap.invalidate(live, "user replied with a correction")
    assert _loops(dead, "fp-same"), "invalidated, and the next offer is identical: nothing changed"


def test_a_real_correction_is_left_alone():
    """The whole point of the correction path: the customer asked for something and got it."""
    dead = ap.invalidate(_an_approval("fp-before"), "user replied with a correction")
    assert not _loops(dead, "fp-after"), "the setup did change, so there is nothing to apologise for"


def test_a_first_confirmation_says_nothing_extra():
    assert not _loops(None, "fp-anything")
    assert not _loops({}, "fp-anything")

# -------------------------------------------------------------------------------------------------
# A bare delegation, which the survey already treated as consent while this called it a correction
# -------------------------------------------------------------------------------------------------

def test_a_bare_delegation_is_consent_to_the_thing_on_screen():
    """MEASURED 2026-09-27: "you decide" was a CORRECTION here, so the pending approval was invalidated
    and the requirements recomposed. The cost was not the extra turn. `accepts_a_proposal` reads the same
    message as acceptance AND RECORDS THE CUSTOMER'S PORT ROLES FROM IT - "deferring to a choice we have
    already made and shown is accepting it". One reader wrote boundary conditions from their words while
    the other threw their approval away."""
    for said in ("you decide", "you decide it", "you decide everything", "you decide the rest",
                 "decide it", "you pick", "you pick the rest", "you choose", "your call",
                 "up to you", "make the call", "whatever you think", "whatever you think is best",
                 "whatever you reckon", "as you see fit"):
        assert ap.classify(said) == ap.APPROVE_INTENT, said


def test_the_two_readers_of_consent_now_agree_on_those():
    """The disagreement was the defect, not either verdict on its own."""
    from meshpipeline.application.geometry_survey import accepts_a_proposal
    for said in ("you decide", "you decide everything", "whatever you think is best", "your call"):
        assert (ap.classify(said) == ap.APPROVE_INTENT) == accepts_a_proposal(said), said


def test_a_delegation_carrying_a_specification_is_still_a_correction():
    """THE CASE THAT KILLED THE FIRST ATTEMPT. A branch that asked "is this a delegation, and does it
    name a mouth" answered APPROVE for this, which is exactly what the closed vocabulary exists to
    refuse: a delegation with a number in it is not bare consent, and dispatching it would run the stale
    requirements. Putting the phrases INTO the vocabulary keeps the guarantee by construction."""
    for said in ("you decide the far field of 50 chords",
                 "you decide, but make o5 the outlet",
                 "you decide but keep it under 2 million cells",
                 "your call on everything except the inlet, that is o1"):
        assert ap.classify(said) == ap.CORRECTION_INTENT, said


def test_a_refusal_carrying_a_delegation_is_still_a_refusal():
    """"no, you pick the rest" keeps its denial. The survey reads the delegation half for the ROLE
    question, deliberately; dispatching compute off it is a different decision and this does not."""
    assert ap.classify("no, you pick the rest") == ap.CORRECTION_INTENT
    assert ap.classify("no, you decide") == ap.CORRECTION_INTENT


def test_the_verbose_delegation_is_knowingly_left_as_a_correction():
    """The owner's own third message. It does not compose from the closed vocabulary, and the fix for
    that would be loosening the rule that catches the test above. One extra turn is the right side to
    err on, and this records the choice so it is not mistaken for an oversight."""
    assert ap.classify("do the half minute read and decide it") == ap.CORRECTION_INTENT
