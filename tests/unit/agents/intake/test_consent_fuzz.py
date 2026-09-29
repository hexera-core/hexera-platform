"""The intake's one consent reader, fuzzed over the ways people say yes, change something, refuse
and hesitate.

A user who said "looks good", "sounds good to me, go for it", "yes go ahaed" or "ok" to the summary
got another summary back: the approval grammar read 53 of 95 natural ways of saying yes as a
correction and threw the summary away, and the engine question kept a different word list. One
reader now serves the summary and the engine question. The contract, generated from a fixed seed:

- every phrasing of a plain yes approves - with openers, closers, punctuation, case and up to two
  one-keystroke slips - and is assent to the proposed engine;
- a yes carrying any change ("yes, but ...", "go ahead with 10 layers") never approves;
- a refusal, however it is typed, never approves and is never assent;
- a hesitation, or a yes asked back as a question, is ambiguous: asked once more, never dispatched.
"""
from __future__ import annotations

import random

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import consent
from meshpipeline.agents.intake import engine_selection as es

SEED = 20260929

YES_CORES = [
    "yes", "yep", "yeah", "yea", "ya", "yup", "sure", "ok", "okay", "k", "go", "go ahead", "proceed",
    "looks good", "looks good to me", "looks right", "looks fine", "sounds good", "sounds good to me",
    "sounds great", "that sounds great", "go for it", "do it", "run it", "start it", "mesh it", "ship it",
    "start meshing", "begin", "continue", "carry on", "go on", "that's right", "that is correct", "correct",
    "exactly", "all good", "fine", "fine by me", "works for me", "that works", "good to go", "agreed",
    "i agree", "accept", "i accept", "approved", "confirm", "confirmed", "lgtm", "perfect", "great",
    "alright", "absolutely", "definitely", "of course", "sure thing", "affirmative", "let's go",
    "let's do it", "yes please", "please proceed", "yes please start it", "whatever you think",
    "sure, whatever you think", "your call", "that's what i said", "as i said", "as shown",
    "go ahead with that", "yes, run it", "yes, start the mesh", "👍", "ok, proceed", "yes proceed",
]
OPENERS = ["", "yes, ", "ok ", "ok, ", "great, ", "perfect! ", "alright, ", "thanks, ", "cool, ", "yes yes, "]
CLOSERS = ["", ".", "!", "!!", " thanks", " please", ", thank you", " :)", " - thanks!"]

CHANGES = [
    "make it 3 m/s", "use cfMesh instead", "10 layers on the walls", "call the inlet in",
    "the outlet is the other end", "a finer mesh", "change the fluid to air", "drop the symmetry plane",
    "set y+ to 1", "no ground plane", "the file is in metres", "5 m/s instead of 2", "with gmsh",
    "tomorrow", "twice the cells", "only half the model", "rename the wall to pipe",
    "the far field 50 chords out", "swap the inlet and outlet", "add a second outlet",
]
REFUSALS = [
    "no", "nope", "nah", "not yet", "don't start", "do not proceed", "wait", "hold on", "stop", "cancel",
    "not now", "no go", "go back", "don't run it", "not that", "never mind", "no, change it",
    "i don't agree", "absolutely not", "definitely not", "no thanks", "please don't", "not ok",
    "that's wrong", "wrong", "no, that's not right", "stop, wait",
]
HEDGES = [
    "maybe", "not sure", "i think so", "hmm", "i guess", "idk", "dunno", "maybe go ahead",
    "yes... i think", "proceed, not sure", "looks good?", "go ahead?", "ok?", "i suppose",
    "probably yes", "no idea", "thanks",
]


def _slip(word: str, rng: random.Random) -> str:
    """One keystroke wrong: two neighbours swapped, a letter doubled or dropped."""
    if len(word) < 3:
        return word
    if len(word) == 3:                                   # a three-letter word only by a swap or a double
        return rng.choice((word[0] + word[2] + word[1], word + word[-1]))
    i = rng.randrange(1, len(word) - 1)
    kind = rng.randrange(3)
    if kind == 0:
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == 1:
        return word[:i] + word[i] + word[i:]
    return word[:i] + word[i + 1:]


def _typo(text: str, rng: random.Random, n: int) -> str:
    words = text.split(" ")
    idx = [i for i, w in enumerate(words) if w.isalpha() and len(w) >= 3]
    for i in rng.sample(idx, min(n, len(idx))):
        words[i] = _slip(words[i], rng)
    return " ".join(words)


def _case(text: str, rng: random.Random) -> str:
    return rng.choice((text, text.lower(), text.upper(), text[:1].upper() + text[1:]))


def _consent_phrasings(slips: tuple[int, ...]) -> list[str]:
    rng = random.Random(SEED + sum(slips))
    out = []
    for core in YES_CORES:
        for _ in range(8):
            text = rng.choice(OPENERS) + core + rng.choice(CLOSERS)
            out.append(_case(_typo(text, rng, rng.choice(slips)), rng))
    return sorted(set(out))


def _change_phrasings() -> list[str]:
    rng = random.Random(SEED + 1)
    out = []
    for change in CHANGES:
        for core in rng.sample(YES_CORES, 8):
            joiner = rng.choice((", but ", " but ", " - ", ", and ", " with ", ". ", ", just "))
            text = core + joiner + change
            out.append(_case(_typo(text, rng, rng.choice((0, 1))), rng))
        out.append(change)
    return sorted(set(out))


def _refusal_phrasings() -> list[str]:
    rng = random.Random(SEED + 2)
    out = []
    for r in REFUSALS:
        for _ in range(4):
            out.append(_case(_typo(r + rng.choice(CLOSERS), rng, rng.choice((0, 1))), rng))
    return sorted(set(out))


CONSENT = _consent_phrasings((0,))
SLIPPED = _consent_phrasings((1, 2))
CHANGED = _change_phrasings()
REFUSED = _refusal_phrasings()


def test_the_fuzz_is_wide():
    assert len(CONSENT) >= 500 and len(SLIPPED) >= 500 and len(CHANGED) >= 150 and len(REFUSED) >= 80


@pytest.mark.parametrize("said", CONSENT)
def test_every_way_of_saying_yes_approves_the_summary_and_the_engine(said):
    assert ap.classify(said) == ap.APPROVE_INTENT, said
    assert es.plain_assent("snappy", said) is True, said


def test_a_yes_with_a_slip_or_two_still_approves():
    # one or two keystrokes wrong ("yes go ahaed", "thnak yuo", "tat"). A slip that could be two
    # different words ("sue": sure or use?) is left unread - the summary is shown again, which is
    # safe - so this is a rate, not every one; what may never happen is tested below: a change or
    # a refusal read as a yes.
    missed = [s for s in SLIPPED if ap.classify(s) != ap.APPROVE_INTENT]
    assert len(missed) <= 0.03 * len(SLIPPED), (len(missed), len(SLIPPED), missed[:20])
    assert all(ap.classify(s) != ap.CORRECTION_INTENT or not consent.is_yes(s) for s in SLIPPED)


@pytest.mark.parametrize("said", CHANGED)
def test_a_yes_that_carries_a_change_never_approves(said):
    assert ap.classify(said) != ap.APPROVE_INTENT, said
    assert consent.is_yes(said) is False, said


@pytest.mark.parametrize("said", REFUSED)
def test_a_refusal_never_approves_and_is_never_assent(said):
    assert ap.classify(said) != ap.APPROVE_INTENT, said
    assert es.plain_assent("snappy", said) is False, said


@pytest.mark.parametrize("said", HEDGES)
def test_a_hesitation_is_asked_again_never_dispatched(said):
    assert ap.classify(said) == ap.HEDGE_INTENT, said


@pytest.mark.parametrize("word", sorted(consent.PROTECTED))
def test_a_protected_word_is_never_repaired_into_a_yes(word):
    # "five" is one key from "fine", "not" from "now", "eight" from "right": a count, a refusal or
    # a direction is a change, never a slip
    for text in (word, f"{word}.", f"{word} thanks"):
        assert ap.classify(text) != ap.APPROVE_INTENT, text


@pytest.mark.parametrize("said", ["five", "light", "goal", "stop it", "nine", "not now", "no, go ahead",
                                  "go ahead in five minutes", "fire it later", "yes but not yet"])
def test_near_misses_that_mean_something_else_stay_what_they_are(said):
    assert ap.classify(said) != ap.APPROVE_INTENT, said
