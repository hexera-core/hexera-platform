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
