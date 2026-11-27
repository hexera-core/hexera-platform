# Responsibility: Say back the CFD flow conditions the plan really took, when the user typed them (or changed them): one short sentence written by the intake's own model from the stored values, under a guard, with a code sentence as the fallback.
# Owns: when a plan says the conditions back, the facts the model is handed, its prompt blocks, the guard, the plain sentence.
# Boundaries: it changes nothing. The values are read back from the submitted plan's own sentence (contracts/flow_conditions.read) - never from the model's reading of the user's words; those words are only searched for the parts that were not taken. The model is the intake's own (contracts/model_inference.call_intake_model).
# Collaborates with: agents/intake/agent.py (puts the sentence before the plan line), agents/intake/flow.py (the question, its buttons and the reading of the answer), agents/intake/plan_reply.py (the voice, the language block, the guard's pieces and the time cap).
"""CFDREQ-37: THE CONDITIONS THE USER TYPED ARE SAID BACK.

The flow-conditions question proposes a fluid, a speed and a wall treatment, with two buttons. A user who typed
their own ("Air at 20 °C, 40 m/s, wall functions is fine.") used to get only the plan's fixed line, so they could
not tell whether 40 m/s was taken. Now the plan's message first says back what the plan carries, in one short
sentence the model writes ("Got it: air at 20 °C, 40 m/s, with wall functions."), then the plan line as before.

- The values are the STORED ones: the plan's conditions sentence, read back the way the run reads it.
- A part of what they typed that was not taken is said: a temperature the mesh is not sized for, a number that
  is no condition we read ("40" with no unit), a second speed; and a speed or fluid the first layer cannot be
  sized from (CFDREQ-8's `why`).
- A plain OK (the button or the words) adds nothing: the plan line is the answer. The "Resolve the wall" button
  changed the proposal, so it is said back too.
- Conditions given before the question would have come (in the first message) are said back once, with the
  first plan.
- The guard: every value said must be the stored one (the speed as stored, the fluid and its temperature, the
  wall treatment), no number the facts do not hold, every part not taken said as not taken, no question, no
  plan line, no rule id, no exclamation, at most 300 characters. Refused, failed or slower than the plan step's
  6 s: the plain sentence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

from meshpipeline.agents.intake import flow
from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.contracts import flow_conditions as FC

logger = logging.getLogger(__name__)

RULE = "CFDREQ-37"
#: the name the model call is logged under
MODEL_NAME = "ConditionsReply"
SOURCE_MODEL, SOURCE_FALLBACK = PR.SOURCE_MODEL, PR.SOURCE_FALLBACK

#: how the conditions came: typed as the answer (or as a change), the "Resolve the wall" button, or in words
#: before the question would have come
TYPED, CLICKED, EARLIER = "typed", "clicked", "from_earlier_words"

_WALL_SHORT = {"functions": "wall functions", "resolved": "a resolved wall (y⁺ ≈ 1)"}
#: a temperature as people type it (°C, °F, K, "degrees")
_TEMP = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(°\s*[CFK]|º\s*[CFK]|[Dd]eg(?:rees?)?\b(?:\s*(?:[CF]\b|[Cc]elsius|"
                   r"[Ff]ahrenheit))?|[Cc]elsius|[Ff]ahrenheit|C\b|F\b|K\b)")
#: where one typed answer splits into its parts ("2,5 m/s" keeps its decimal comma)
_PARTS = re.compile(r";|,\s|\band\b|\bwith\b|\bbut\b|\bplus\b", re.I)
_DIGIT = re.compile(r"\d")
#: a part said as not used
_NOT = re.compile(r"\bnot\b|n['’]t\b|\bonly\b|\binstead\b|\bignor\w*|\bleft out\b|\bunused\b", re.I)
_FUNCTIONS_SAID = re.compile(r"\bwall[- ]functions?\b", re.I)
_RESOLVED_SAID = re.compile(r"\bresolv\w*|\by\s*[+⁺]\s*(?:≈|~|=|of|about|around)?\s*1\b", re.I)
#: the plan's own line follows the sentence: the sentence never says it too
_PLAN_SAID = re.compile(r"\bproceed\b|\bhere['’]?s the plan\b|\bsay go\b", re.I)


# #
# when, and from which words
# #

def _users(messages: list[dict]) -> list[tuple[int, str]]:
    return [(i, str(m.get("content") or "").strip()) for i, m in enumerate(messages) if m.get("role") == "user"]


def _carries_conditions(text: str) -> bool:
    return any(flow._found(rx, text) is not None for rx in (FC.FLUIDS, FC.SPEED, flow._RESOLVED, flow._FUNCTIONS))


def how_given(messages: Iterable[Any], *, first_plan: bool) -> tuple[str, list[str]]:
    """(how, the words): how this turn's latest message gave the conditions - TYPED (the answer to the question, or
    a change at the plan), CLICKED (the "Resolve the wall" button), EARLIER (named before the question would have
    come, said back with the first plan) - or ("", []) when nothing is to be said back: a plain OK, a question, a
    message about something else. Pure."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    users = _users(msgs)
    if not users:
        return "", []
    at, latest = users[-1]
    if latest == flow.CONDITIONS_CHOICES[1][1]:
        return CLICKED, [latest]
    if latest in flow._step_buttons() or flow._found(flow._OK, latest) or flow.asks_something(latest):
        latest_counts = False
    else:
        latest_counts = latest in flow._condition_words(msgs)
    asked = [i for i, m in enumerate(msgs) if m.get("role") == "assistant" and flow.is_conditions_question(m.get("content"))]
    # the first message after the question is its answer - unless the question stood under a plan (CFDREQ-37,
    # rulebook 122): words typed at the plan card are an answer only when they give conditions themselves
    answering = (bool(asked) and [i for i, _t in users if i > asked[-1]] == [at]
                 and not flow.asked_after_plan(msgs, asked[-1]))
    if latest_counts and (answering or _carries_conditions(latest)):
        return TYPED, [latest]
    if not asked and first_plan:
        said = [t for t in flow._condition_words(msgs) if _carries_conditions(t)]
        if said:
            return EARLIER, said
    return "", []


# #
# the facts
# #

def _celsius(value: str, unit: str) -> float:
    v = float(value.replace(",", "."))
    u = unit.replace("º", "°").replace(" ", "").lower()
    if u.endswith("f") or "fahrenheit" in u:
        return (v - 32.0) * 5.0 / 9.0
    if u.endswith("k") and "deg" not in u:
        return v - 273.15
    return v


def _not_taken(words: list[str], how: str, fluid_words: str, speed_words: str) -> list[dict]:
    """The parts of the user's words the plan did not take, each {"words", "why"}."""
    out: list[dict] = []
    sized_at_20 = "20 °C" in fluid_words
    stored_speed = " ".join(speed_words.split()).lower()
    for text in words:
        for part in _PARTS.split(text):
            part = part.strip(" .!")
            if not part or not _DIGIT.search(part):
                continue
            accounted = False
            for t in _TEMP.finditer(part):
                accounted = True
                said = " ".join(t.group(0).split())
                if not (sized_at_20 and abs(_celsius(t.group(1), t.group(2)) - 20.0) < 0.5):
                    out.append({"words": said, "why": (f"the mesh is sized for {fluid_words}, so this temperature is "
                                                       "not used" if sized_at_20 else
                                                       "the mesh does not use a temperature for this fluid")})
            for s in FC.SPEED.finditer(part):
                accounted = True
                said = " ".join(s.group(0).split())
                if said.lower() != stored_speed:
                    out.append({"words": said, "why": f"another speed than the one taken ({speed_words})"})
            if flow._found(flow._RESOLVED, part) or flow._found(flow._FUNCTIONS, part):
                accounted = True
            if not accounted and how == TYPED:
                out.append({"words": part, "why": "not read as a flow condition"})
    return out


def facts(messages: Iterable[Any], request_txt: str, patches: Iterable[Mapping] | None = None, *,
          first_plan: bool) -> dict[str, Any] | None:
    """What the model is told: how the conditions came, the values the plan really carries (`taken`), the parts not
    taken, and the plain sentence that says exactly that. None when nothing is to be said back, or the plan carries
    no conditions sentence. Pure."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    how, words = how_given(msgs, first_plan=first_plan)
    if not how:
        return None
    got = FC.read(request_txt, patches)
    if got is None:
        return None
    internal = flow.flow_kind_of(msgs) == "internal"
    fluid, speed = str(got["fluid_words"]), str(got["speed_words"])
    accepted = FC.accepted(request_txt)
    out: dict[str, Any] = {
        "how": how,
        "their_words": words,
        "taken": {"fluid": fluid, "speed": speed + (" at the inlet" if internal else ""),
                  "wall": _WALL_SHORT[got["wall"]]},
        "from_their_words": [w for w, key in (("fluid", "the fluid"), ("speed", "its speed"),
                                              ("wall treatment", "the wall treatment")) if key not in accepted],
        "not_taken": _not_taken(words, how, fluid, speed),
        # CFDREQ-8: why the first layer cannot be sized from them ("" when it can)
        "sizing": str(got.get("why") or ""),
    }
    out["plain_version_that_is_exactly_right"] = plain(out)
    return out


def plain(f: Mapping) -> str:
    """The code's own sentence: exactly the facts, in plain words."""
    t = f["taken"]
    values = f"{t['fluid']}, {t['speed']}, with {t['wall']}"
    if f["how"] == EARLIER:
        out = f"From what you said, I'm using {values}."
    elif not f.get("from_their_words") and f["how"] == TYPED:
        out = f"I took that as an OK to {values}."
    else:
        out = f"Got it: {values}."
    for n in f.get("not_taken") or ():
        if n["why"].startswith("another speed"):
            out += f" I took {t['speed']}, not {n['words']}."
        elif "temperature" in n["why"]:
            out += (f" The mesh is sized for {t['fluid']}, so the {n['words']} you gave isn't used."
                    if "20 °C" in t["fluid"] else f" The mesh doesn't use a temperature, so {n['words']} isn't used.")
        else:
            out += f" I couldn't read “{n['words']}” as a flow condition, so it isn't used."
    if f.get("sizing"):
        out += f" The cells at the wall keep the mesher's own size, because {f['sizing']}."
    return out


# #
# the model's sentence
# #

def _block_task() -> str:
    return ("The engineer has just told you the flow conditions for their CFD mesh, and the plan card comes right "
            "after your words (the app adds its own line about the plan: never mention the plan, Proceed or what "
            "comes next). In ONE short sentence, say back the conditions you took - exactly the values in `taken`: "
            "the fluid (with its temperature when it has one), the speed, the wall treatment - in their natural "
            "short form ('air at 20 °C, 40 m/s, with wall functions'). Use those values as given, never your own "
            "reading of their words. If how is 'from_earlier_words', they gave them earlier in the chat: say these "
            "are what you took from what they said. If from_their_words is empty, you took their words as an OK to "
            "the proposal: say so in a few words. For each item in not_taken add a short sentence saying plainly "
            "that part isn't used, and why, in everyday words. If sizing is there, say in a few words that the "
            "cells at the wall keep the mesher's own size, and why. Never ask a question.\n")


def _block_examples() -> str:
    return ("Examples (the facts differ each time; never copy their numbers):\n"
            "They: 'Air at 20 °C, 40 m/s, wall functions is fine.' -> 'Got it: air at 20 °C, 40 m/s, with wall "
            "functions.'\n"
            "They: 'water, 3 m/s, resolve the wall' (through a pipe) -> 'Okay, water at 20 °C, 3 m/s at the inlet, "
            "with a resolved wall (y⁺ ≈ 1).'\n"
            "They: 'air at 35 °C, 25 m/s' -> 'All right, air at 20 °C, 25 m/s, with wall functions. The mesh is "
            "sized for air at 20 °C, so the 35 °C isn't used.'\n")


#: THE SAID-BACK SENTENCE'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text
_PLAN_BLOCKS = {name: build for name, _purpose, build in PR.PLAN_REPLY_PROMPT_BLOCKS}
CONDITIONS_REPLY_PROMPT_BLOCKS: tuple = (
    ("voice", "the plan step's own voice (plan_reply)", _PLAN_BLOCKS["voice"]),
    ("task", "say back the stored conditions in one short sentence; the parts not taken, and why; never the plan "
             "line or a question", _block_task),
    ("examples", "three example exchanges in that voice", _block_examples),
    ("language", "the plan step's own language block (plan_reply)", _PLAN_BLOCKS["language"]),
)


def compose_system() -> str:
    return "".join(build() for _name, _purpose, build in CONDITIONS_REPLY_PROMPT_BLOCKS)


def messages_for(f: Mapping) -> list[dict]:
    return [{"role": "system", "content": compose_system()},
            {"role": "user", "content": "Write the sentence for this answer.\n"
                                        + json.dumps({"facts": dict(f)}, ensure_ascii=False)}]


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def check(reply: str, f: Mapping) -> str:
    """Why a model-written sentence may not be said ("" when it may), against the facts `f`."""
    text = " ".join(str(reply or "").split())
    if not text:
        return "empty"
    if len(text) > PR.MAX_CHARS:
        return "too long"
    if "?" in text:
        return "asks something"
    if PR._RULE_ID.search(text):
        return "a rule id"
    if PR._BANNED.search(text):
        return "an exclamation, filler or emoji"
    if _PLAN_SAID.search(text):
        return "says the plan line"
    t = f["taken"]
    allowed = PR._numbers(json.dumps({k: f.get(k) for k in ("taken", "not_taken", "sizing")}, ensure_ascii=False))
    allowed |= {"30", "300", "1"}  # the wall treatments' own y+ bands
    stray = sorted(PR._numbers(text) - allowed)
    if stray:
        return f"a number not in the facts ({', '.join(stray)})"
    speed = str(t["speed"]).removesuffix(" at the inlet")
    if _squash(speed) not in _squash(text):
        return f"the speed taken ({speed}) not said"
    key = FC.fluid_key(t["fluid"])
    if key and not re.search(r"\b" + re.escape(key) + r"\b", text, re.I):
        return f"the fluid taken ({key}) not said"
    if not PR._numbers(t["fluid"]) <= PR._numbers(text):
        return "the fluid's temperature not said"
    other = {m.lower() for m in FC.FLUIDS.findall(text)} - {key}
    if other:
        return f"another fluid said ({', '.join(sorted(other))})"
    if t["wall"] == _WALL_SHORT["resolved"]:
        if not _RESOLVED_SAID.search(text) or _FUNCTIONS_SAID.search(text):
            return "the wall treatment taken (a resolved wall) not said"
    elif not _FUNCTIONS_SAID.search(text) or _RESOLVED_SAID.search(text):
        return "the wall treatment taken (wall functions) not said"
    for n in f.get("not_taken") or ():
        if not PR._numbers(n["words"]) <= PR._numbers(text) or not _NOT.search(text):
            return f"a part not taken ({n['words']}) not said"
    if f.get("sizing") and not (_NOT.search(text) or re.search(r"\bown\b", text, re.I)):
        return "the wall cells' own size not said"
    return ""


async def _ask(messages: list[dict], *, job_id: str, user_id: str) -> str:
    from meshpipeline.contracts import model_inference as llm_router
    got = await llm_router.call_intake_model(messages=messages, job_id=job_id, user_id=user_id, name=MODEL_NAME)
    if not getattr(got, "ok", True):
        raise RuntimeError(str(getattr(got, "failure_marker", "") or "no answer"))
    return str(getattr(got, "assistant_text", "") or "").strip()


async def written(f: Mapping, *, session_id: str = "", user_id: str = "", ask_fn=None,
                  timeout_s: float = PR.TIMEOUT_S) -> dict[str, Any]:
    """{"text": what is said, "source": model | fallback, "why": the guard's refusal or the failure}. Never
    raises."""
    plain_text = str(f.get("plain_version_that_is_exactly_right") or plain(f))
    try:
        said = await asyncio.wait_for((ask_fn or _ask)(messages_for(f), job_id=f"conditions-reply-{session_id}",
                                                       user_id=user_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the plain sentence (CFDREQ-37)
        logger.info("conditions reply: the plain sentence (%s, %s) - session=%s", RULE, type(exc).__name__,
                    session_id)
        return {"text": plain_text, "source": SOURCE_FALLBACK, "why": f"no answer ({type(exc).__name__})"}
    said = " ".join(str(said or "").strip().strip('"“”').split())
    why = check(said, f)
    if why:
        logger.info("conditions reply: the model's sentence refused by the check '%s' (%s) - session=%s", why, RULE,
                    session_id)
        return {"text": plain_text, "source": SOURCE_FALLBACK, "why": why}
    logger.info("conditions reply: written by the model (%s) - session=%s", RULE, session_id)
    return {"text": said, "source": SOURCE_MODEL, "why": ""}


async def said_back(messages: Iterable[Any], submit_args: Mapping, reply: str, *, first_plan: bool,
                    session_id: str = "", user_id: str = "", ask_fn=None) -> str:
    """The plan's message with the conditions said back before its last line (the plan line), when this turn's
    words gave them; `reply` itself otherwise. Never raises."""
    try:
        f = facts(messages, str(submit_args.get("request_txt") or ""), submit_args.get("patches"),
                  first_plan=first_plan)
    except Exception:  # noqa: BLE001 - a reading that fails says nothing extra; the plan line still stands
        logger.exception("conditions reply: the facts could not be read (%s) - session=%s", RULE, session_id)
        return reply
    if f is None:
        return reply
    got = await written(f, session_id=session_id, user_id=user_id, ask_fn=ask_fn)
    head, sep, line = str(reply or "").rpartition("\n\n")
    from meshpipeline.agents.intake import approval as ap
    if head.strip() in {ap.changed_line(["the written requirements"], since_run=s) for s in (False, True)}:
        # a plan redone for the conditions alone: the sentence says what changed, in plain words - never
        # "Changed from the last summary: the written requirements." above it
        return f"{got['text']}\n\n{line}"
    return (f"{head}\n\n{got['text']}\n\n{line}" if sep and head.strip() else f"{got['text']}\n\n{reply}")


__all__ = ["CLICKED", "CONDITIONS_REPLY_PROMPT_BLOCKS", "EARLIER", "MODEL_NAME", "RULE", "SOURCE_FALLBACK",
           "SOURCE_MODEL", "TYPED", "check", "compose_system", "facts", "how_given", "messages_for", "plain",
           "said_back", "written"]
