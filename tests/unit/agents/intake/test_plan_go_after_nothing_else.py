# Responsibility: Verify NOTE-18's go reading for "nope, nothing else. go": a no that only says nothing more is wanted,
#       then a clear go, starts the run - the model's go is not held back by the negation guard, and the phrase reading
#       starts it with no model - while "don't go yet", "nothing else to go on", "go finer", "no go" never start.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.agents.intake import consent as C
from meshpipeline.agents.intake import plan_go as PG
from meshpipeline.agents.intake import plan_talk as PT

LAST = "Got it. Anything else, or shall I start the mesh?"

DONE_THEN_GO = [
    "nope, nothing else. go",                 # the one miss of 63 on the real model
    "no, nothing else, go ahead",
    "nope that's all, mesh it",
    "no thanks, nothing more. proceed",
    "nah, no more changes. start the mesh",
    "nope nothing else. lets go",
    "No. Nothing else from me. Go!",
]
NEVER_STARTS = [
    "don't go yet", "nothing else to go on", "go finer", "no go", "nope, go finer at the bore",
    "no, nothing else. don't go yet", "nope", "no, wait. go", "nope, nothing else?", "no, nothing else. go later",
]


def _answers(label):
    async def ask(messages, *, job_id, user_id):
        return label
    return ask


async def _fails(*_a, **_k):
    raise RuntimeError("provider down")


def _read(text, ask_fn):
    return asyncio.run(PG.read(text, consent=C.reading(text), last_line=LAST, ask_fn=ask_fn))


@pytest.mark.parametrize("text", DONE_THEN_GO)
def test_nothing_more_then_a_go_starts_the_run(text):
    assert _read(text, _answers("go")).reading == PT.GO, text      # the model's go is no longer held back
    assert _read(text, _fails).reading == PT.GO, text              # and the phrase reading starts it with no model


@pytest.mark.parametrize("text", NEVER_STARTS)
def test_these_never_start_the_run(text):
    assert _read(text, _fails).reading != PT.GO, text
    assert PT.reading(text, consent=C.reading(text)) != PT.GO, text


@pytest.mark.parametrize("text", ["don't go yet", "go finer", "no go", "nope, go finer at the bore",
                                  "no, nothing else. don't go yet", "no, wait. go", "nope, nothing else?",
                                  "no, nothing else. go later"])
def test_these_never_start_even_when_the_model_says_go(text):
    assert _read(text, _answers("go")).reading != PT.GO, text
