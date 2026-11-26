# Responsibility: Write the answer to "Request a change" on a delivered mesh - by the intake's own model, under the plan step's guard, with a code fallback.
# Boundaries: it decides nothing about the run; it says what application/change_request already did, from those facts alone.
# Collaborates with: agents/intake/plan_reply (the voice, the model call and the guard it reuses), application/change_request (the facts and the plain sentence).
"""VER-12: THE REPLY TO A CHANGE REQUEST IS WRITTEN BY THE MODEL, UNDER THE PLAN STEP'S GUARD.

The same path as a requirement's reply at the plan step (NOTE-19): the intake's own model (`plan_reply._ask`,
`call_intake_model`), the same voice and language blocks, the same guard (`plan_reply.check`: no number the
facts do not hold, a place named when something is applied, no rule id, no exclamation, at most 300
characters) - plus what a change request must say: that a new run is starting (or, when nothing could be tied
to the part, that nothing started and where to mark), the marks that were left out, and never a verdict on the
old mesh ("meets your brief"). Refused, failed or slower than the plan step's 6 s: the plain sentence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from meshpipeline.agents.intake import plan_reply as PR

logger = logging.getLogger(__name__)

RULE = "VER-12"
SOURCE_MODEL = PR.SOURCE_MODEL
SOURCE_FALLBACK = PR.SOURCE_FALLBACK

_PLAN_BLOCKS = {name: build for name, _purpose, build in PR.PLAN_REPLY_PROMPT_BLOCKS}


def _block_task() -> str:
    return ("This time the engineer is looking at a FINISHED mesh. They marked spots on it and typed a change, and a "
            "new run of the same part is starting with that change as a requirement on the faces they marked, and "
            "nothing else changed. Say, in one or two short sentences: what you understood they want, where (the "
            "faces, in their natural short form), that nothing else changes, and that a new run is starting; if "
            "last_run_took_minutes is there, add how long the last run took ('the last one took 29 min'). Speak of "
            "what you'll do in the new run, never of a mesh already made. Never say the mesh meets their brief, "
            "passed or is fine: this is a change, not a review.\n")


def _block_exceptions() -> str:
    return ("If no_marks is there, no spot was marked: say you've noted it for the new run and that the result "
            "will say what came of it; never say it is set or applied. "
            "If marks_not_on_the_part is there, say plainly that those marks are not on the part, so you left them "
            "out. If one_place_words_several_spots is there, they named one place but marked several separate "
            "spots: say it goes on every spot they marked. If flow_limit is there, say in a few words that this "
            "flow mesher can't size one face on its own and the result will say what it did there. If "
            "what_happens says nothing starts, no new run starts: say you couldn't find the part under their marks "
            "and ask them to mark the spot on the part itself and send it again.\n")


def _block_examples() -> str:
    return ("Examples (the facts differ each time; never copy their numbers):\n"
            "They: 'make the edges around this hole finer, nowhere else' (Face 12 · round Ø15 mm marked; last run "
            "29 min) -> 'Got it: finer mesh on the edges of that hole only (Face 12), nothing else changes. "
            "Starting a new run; the last one took 29 min.'\n"
            "They: '2 mm here' (2 faces marked, one mark off the part) -> 'Okay, 2 mm on Face 4 and Face 9 and "
            "nothing else changes. One mark wasn't on the part, so I left it out. Starting a new run now.'\n")


#: THE CHANGE REPLY'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text it gets
CHANGE_REPLY_PROMPT_BLOCKS: tuple = (
    ("voice", "the plan step's own voice (plan_reply)", _PLAN_BLOCKS["voice"]),
    ("task", "a finished mesh, a change asked on marked faces: what, where, nothing else changes, a new run "
             "starting, the last run's time; never a verdict", _block_task),
    ("exceptions", "marks off the part, one place but several spots, a flow mesher that cannot size a face, nothing "
                   "started", _block_exceptions),
    ("examples", "two example exchanges in that voice", _block_examples),
    ("language", "the plan step's own language block (plan_reply)", _PLAN_BLOCKS["language"]),
)


def compose_system() -> str:
    return "".join(build() for _name, _purpose, build in CHANGE_REPLY_PROMPT_BLOCKS)


def messages_for(facts: Mapping) -> list[dict]:
    return [{"role": "system", "content": compose_system()},
            {"role": "user", "content": "Write the reply to this request.\n"
                                        + json.dumps({"facts": dict(facts)}, ensure_ascii=False)}]


_NEW_RUN = re.compile(r"\b(?:new run|re-?run|rebuild\w*|re-?mesh\w*|start(?:ing|ed|s)?\b[^.]{0,30}\b(?:run|mesh))",
                      re.I)
_VERDICT = re.compile(r"\b(?:meets? your brief|passed|looks? (?:fine|good)|is fine|no change (?:is )?needed)\b",
                      re.I)
_NOT_ON = re.compile(r"\b(?:not on|off|isn'?t on|wasn'?t on|weren'?t on|aren'?t on|outside|missed)\b", re.I)
_MARK_AGAIN = re.compile(r"\b(?:mark|click|pick|select)\w*\b", re.I)
_NOTHING_STARTED = re.compile(r"\b(?:nothing (?:has )?start|haven'?t started|didn'?t start|not started|no (?:new )?run)",
                              re.I)


def check(reply: str, facts: Mapping) -> str:
    """Why a model-written reply may not be said ("" when it may): the plan step's guard first, then a change
    request's own checks."""
    why = PR.check(reply, facts)
    if why:
        return why
    text = " ".join(str(reply or "").split())
    if _VERDICT.search(text):
        return "a verdict on the old mesh"
    asking = "nothing starts" in str(facts.get("what_happens") or "")
    if asking:
        if not _MARK_AGAIN.search(text):
            return "nothing tied, and no mark asked for"
        if _NEW_RUN.search(text) and not _NOTHING_STARTED.search(text):
            return "says a run starts when none does"
        return ""
    if not _NEW_RUN.search(text):
        return "no new run said"
    if facts.get("marks_not_on_the_part") and not _NOT_ON.search(text):
        return "a mark not on the part, not said"
    return ""


async def written(facts: Mapping, *, session_id: str = "", user_id: str = "", ask_fn=None,
                  timeout_s: float = PR.TIMEOUT_S) -> dict[str, Any]:
    """{"text": what is said, "source": model | fallback, "why": the guard's refusal or the failure}. Never
    raises."""
    plain = str(facts.get("plain_version_that_is_exactly_right") or "")
    try:
        said = await asyncio.wait_for((ask_fn or PR._ask)(messages_for(facts), job_id=f"change-reply-{session_id}",
                                                          user_id=user_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the plain sentence (VER-12)
        logger.info("change reply: the plain sentence (%s, %s) - session=%s", RULE, type(exc).__name__, session_id)
        return {"text": plain, "source": SOURCE_FALLBACK, "why": f"no answer ({type(exc).__name__})"}
    said = " ".join(str(said or "").strip().strip('"“”').split())
    why = check(said, facts)
    if why:
        logger.info("change reply: the model's reply refused by the check '%s' (%s) - session=%s", why, RULE,
                    session_id)
        return {"text": plain, "source": SOURCE_FALLBACK, "why": why}
    logger.info("change reply: written by the model (%s) - session=%s", RULE, session_id)
    return {"text": said, "source": SOURCE_MODEL, "why": ""}


__all__ = ["CHANGE_REPLY_PROMPT_BLOCKS", "RULE", "SOURCE_FALLBACK", "SOURCE_MODEL", "check", "compose_system",
           "messages_for", "written"]
