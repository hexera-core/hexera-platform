# Responsibility: Whether a short message at the plan step means "start the mesh" (NOTE-18): code decides the safe
#       cases, the intake's own model is asked one small question for the rest, and the phrase reading stands in when
#       the model does not answer in time.
# Owns: the one-word question to the model (go / wait / question / requirement / other), its prompt blocks, and how
#       its answer and the code's reading combine.
# Boundaries: a reading out, nothing written. The model is the intake's own (contracts/model_inference.
#       call_intake_model); the phrase reading and the requirement test are agents/intake/plan_talk's; what a GO
#       starts is the approval authority's (agents/intake/message).
""""GO" READ ROBUSTLY (NOTE-18).

A phrase table cannot know every way of saying "I'm done here, mesh it". So:

1. CODE DECIDES THE SAFE CASES, no model: a message that carries a face, a picture or a file, or whose words hold a
   number, a unit, a size or a mesh requirement word, is a requirement (never a go); the Proceed button's own text is
   a go; a word about the plan itself (engine, flow kind, unit) is the intake's; a message with no words is the
   consent reader's.
2. THE REST (short words, no requirement in them) is asked of the intake's model, ONE small question with a strict
   answer set - go / wait / question / requirement / other - given our last line and the user's message.
3. A GO STARTS THE RUN only when the model says go AND code found no requirement content, no negation ("not", "but",
   "first") and no plain "ok" to some other question. "wait" is held. A question goes to the intake (or, for a go
   asked back, "Start the mesh now?"). Anything else is asked "Start the mesh now?" once - never "I couldn't read that
   as a mesh requirement".
4. NO ANSWER (error, or slower than `TIMEOUT_S`, or an answer outside the set): the phrase reading
   (plan_talk.reading) decides; where it is unsure the go question is asked once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from meshpipeline.agents.intake import plan_talk as PT

logger = logging.getLogger(__name__)

RULE = "NOTE-18"

#: how long the one-word question may take before the phrase reading decides
TIMEOUT_S = 3.0
#: the model's whole answer set
LABELS = ("go", "wait", "question", "requirement", "other")
#: how a reading was reached (logged, and printed by devtools/try_plan_talk.py; never shown)
BY_CODE, BY_MODEL, BY_FALLBACK = "code", "model", "fallback"


def _block_task() -> str:
    return ("You sort one chat message from an engineer at the last step before a mesh is generated. The plan is on "
            "screen; they can still add mesh requirements, ask questions, or say to start.\n")


def _block_labels() -> str:
    return ("Answer with exactly one word from this set and nothing else:\n"
            "go - they mean: start meshing now / I'm done with this step (\"ok I'm good to mesh\", \"yep run it\", "
            "\"that's everything\", \"looks good, start\", \"send it\").\n"
            "wait - they mean: not yet, hold on, don't start.\n"
            "question - they are asking something (\"is it good to mesh like this?\", \"what happens if I proceed?\").\n"
            "requirement - they ask for something about the mesh (\"go finer on the bore\", \"proceed with 2 mm\").\n"
            "other - anything else, or you are not sure. A plain \"ok\" or \"yes\" that answers a question we asked "
            "that was not about starting is other.\n")


def _block_rules() -> str:
    return ("Use the assistant's last line to understand short replies. Typos and lower case are normal. If in "
            "doubt between go and anything else, answer other.\n")


#: THE GO QUESTION'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text it gets
PLAN_GO_PROMPT_BLOCKS: tuple = (
    ("task", "what is being sorted: one message at the step before the mesh starts", _block_task),
    ("labels", "the strict answer set, one word, with everyday examples of each", _block_labels),
    ("rules", "the last line gives context; typos are normal; in doubt, other", _block_rules),
)


def compose_system() -> str:
    return "".join(build() for _name, _purpose, build in PLAN_GO_PROMPT_BLOCKS)


def messages_for(text: str, last_line: str) -> list[dict]:
    body = {"assistant_last_line": str(last_line or "")[-600:], "user_message": str(text or "")}
    return [{"role": "system", "content": compose_system()},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False)}]


def label_of(answer: str) -> str:
    """The one label in the model's answer, or "" when it is not exactly one of the set."""
    words = re.findall(r"[a-z]+", str(answer or "").casefold())
    return words[0] if len(words) == 1 and words[0] in LABELS else ""


async def _ask(messages: list[dict], *, job_id: str, user_id: str) -> str:
    from meshpipeline.contracts import model_inference as llm_router
    got = await llm_router.call_intake_model(messages=messages, job_id=job_id, user_id=user_id, name="PlanGo")
    if not getattr(got, "ok", True):
        raise RuntimeError(str(getattr(got, "failure_marker", "") or "no answer"))
    return str(getattr(got, "assistant_text", "") or "").strip()


@dataclass(frozen=True)
class Read:
    reading: str        # plan_talk's GO / ASK / HOLD / NOTE / BOTH / INTAKE
    by: str             # BY_CODE / BY_MODEL / BY_FALLBACK
    raw: str = ""       # the model's answer as given ("" when not asked or failed)
    why: str = ""       # what decided it, in a few words (logged)


def _safe_go(text: str, last_line: str) -> bool:
    """No question mark ("is it good to mesh like this?"), no negation or condition, and not a plain "ok" to some
    other question of ours."""
    return "?" not in str(text or "") and not PT.never_go(text) and not PT.answers_another_question(text, last_line)


async def read(text: str, *, carries: bool = False, asked: bool = False, consent: str = "", engine: str = "",
               last_line: str = "", user_id: str = "", session_id: str = "", ask_fn=None,
               timeout_s: float = TIMEOUT_S) -> Read:
    """What one message at the plan step means (the module's docstring). Never raises."""
    code = PT.reading(text, carries=carries, asked=asked, consent=consent, engine=engine)
    if PT.requirement_content(text, carries=carries):
        return Read(code, BY_CODE, why="requirement content")
    if PT.exact_go(text):
        return Read(PT.GO, BY_CODE, why="the button's text")
    if not PT.words_of(text) or PT.plan_change(text):
        return Read(code, BY_CODE, why="no words" if not PT.words_of(text) else "a plan word")
    raw = ""
    try:
        raw = await asyncio.wait_for((ask_fn or _ask)(messages_for(text, last_line), job_id=f"plan-go-{session_id}",
                                                      user_id=user_id), timeout=timeout_s)
        label = label_of(raw)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the phrase reading decides (NOTE-18)
        label = ""
        logger.info("plan go: the model did not answer (%s, %s) - session=%s", RULE, type(exc).__name__, session_id)
    if PT.answers_another_question(text, last_line):
        # "ok" to a question of ours that was not "Start the mesh now?": the intake takes it as that answer
        other = PT.INTAKE
    else:
        other = PT.INTAKE if asked or code == PT.INTAKE else PT.ASK
    if not label:
        # with no requirement in the words, a NOTE would answer "couldn't read that as a mesh requirement": the go
        # question is asked instead, once (asked already, or an answer to another question: the intake answers)
        got = other if code == PT.NOTE else code
        if got == PT.GO and not _safe_go(text, last_line):
            got = other
        return Read(got, BY_FALLBACK, raw=raw, why="the phrase reading")
    if label == "go":
        if code == PT.HOLD:
            return Read(PT.HOLD, BY_MODEL, raw=raw, why="go, but the words say not yet")
        if not _safe_go(text, last_line):
            return Read(other, BY_MODEL, raw=raw, why="go, but a negation or another question's answer")
        return Read(PT.GO, BY_MODEL, raw=raw, why="go")
    if label == "wait":
        return Read(PT.HOLD, BY_MODEL, raw=raw, why="wait")
    if label == "question":
        return Read(PT.ASK if code == PT.ASK else PT.INTAKE, BY_MODEL, raw=raw, why="question")
    if label == "requirement":
        return Read(PT.NOTE, BY_MODEL, raw=raw, why="requirement")
    # other: never a start; the go question once, else the intake answers
    return Read(other, BY_MODEL, raw=raw, why="other")


__all__ = ["BY_CODE", "BY_FALLBACK", "BY_MODEL", "LABELS", "PLAN_GO_PROMPT_BLOCKS", "RULE", "Read", "TIMEOUT_S",
           "compose_system", "label_of", "messages_for", "read"]
