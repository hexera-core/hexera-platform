# Responsibility: Say back what a typed answer to one of the questionnaire's own questions was taken as - the purpose ("What are you meshing this for?"), through or around (the flow-kind question), the FEA analysis ("What will you analyze?") - in one short sentence the intake's own model writes from the stored values, under a guard, with a code sentence as the fallback.
# Owns: when an answer is said back, the facts the model is handed, its prompt blocks, the guard, the plain sentence, the time cap.
# Boundaries: it changes nothing and decides nothing. The values are the ones the application already took (the purpose reading, the flow-kind record, the plan's analysis) - never the model's reading of the words. A click on one of our buttons says nothing back: the step's own line is the answer. The model is the intake's own (contracts/model_inference.call_intake_model).
# Collaborates with: agents/intake/message.py (the purpose and flow-kind answers, before the stage line), agents/intake/agent.py (the FEA analysis, before the plan line), agents/intake/plan_reply.py (the voice and language blocks and the guard's pieces).
"""CFDREQ-37 (amended): A TYPED ANSWER TO OUR OWN QUESTION IS SAID BACK.

A user who types "CFD, external aero over this car" to "What are you meshing this for?", "it's the flow around
it" to the flow-kind question, or "a modal one please" to "What will you analyze?" used to get only the next step's
fixed line: the panel changed, the chat said nothing. Now one short sentence says what was taken first ("Okay, a CFD
mesh with the flow around the part."), then the step's line as before.

- The values are the STORED ones (the purpose reading the stage is opened under, the flow-kind record the stage's
  form takes, the analysis the plan carries).
- A click on one of our buttons adds nothing.
- The guard: every value said is the stored one and no other (CFD never said FEA, around never said through), no
  number, no question, no step line (Proceed, the plan, the stage), no rule id, no exclamation, at most
  `MAX_CHARS`. Refused, failed or slower than `TIMEOUT_S`: the plain sentence. The cap is shorter than the plan
  step's 6 s because the stage waits behind it (the stage itself is already queued: it is never held by this).
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

RULE = "CFDREQ-37"
#: the name the model call is logged under
MODEL_NAME = "StepAck"
SOURCE_MODEL, SOURCE_FALLBACK = PR.SOURCE_MODEL, PR.SOURCE_FALLBACK
#: the longest the sentence may be, in characters
MAX_CHARS = 160
#: how long the model may take before the plain sentence is said (inside the plan step's own 6 s cap)
TIMEOUT_S = 3.0

#: the steps whose typed answer is said back
PURPOSE, FLOW_KIND, ANALYSIS = "purpose", "flow_kind", "analysis"

_ROUTE_WORDS = {"flow": "CFD", "fea": "FEA", "electronics": "electronics cooling"}
_FLOW_WORDS = {"internal": "through the part", "external": "around the part"}
#: how each value may be said, and what would say the other one
_SAYS = {
    "CFD": re.compile(r"\bCFD\b|\bflow\b|\bfluid\b", re.I),
    "FEA": re.compile(r"\bFEA\b|\bstructural\b|\bstrength\b|\bstress\b|\bvibration\b|\bmodal\b|\bbuckling\b", re.I),
    "electronics cooling": re.compile(r"\bcool\w*", re.I),
    "through the part": re.compile(r"\bthrough\b|\binside\b|\binternal\b", re.I),
    "around the part": re.compile(r"\baround\b|\boutside\b|\bexternal\b|\bover\b", re.I),
}
_ANALYSIS_SAYS = {
    "static": re.compile(r"\bstatic\b|\bstress\b", re.I),
    "modal": re.compile(r"\bmodal\b|\bvibration\b|\bfrequenc\w*", re.I),
    "buckling": re.compile(r"\bbuckl\w*", re.I),
    "thermal": re.compile(r"\bthermal\b|\bheat\b", re.I),
    "nonlinear": re.compile(r"\bnonlinear\b|\bnon-linear\b|\bcontact\b", re.I),
    "impact": re.compile(r"\bimpact\b|\bcrash\b", re.I),
    "fatigue": re.compile(r"\bfatigue\b", re.I),
}
#: the next step's own line follows the sentence: the sentence never says it too
_STEP_SAID = re.compile(r"\bproceed\b|\bpress\b|\bplan\b|\bconfirm\w*|\bon the right\b|\bpanel\b|\bsay go\b", re.I)
_DIGIT = re.compile(r"\d")


# #
# the facts
# #

def facts(step: str, *, words: str, route: str = "", flow: str = "", analysis: str = "",
          analysis_label: str = "") -> dict[str, Any]:
    """What the model is told: the step, their words, and the values taken (`taken`: route, flow, analysis - only
    the ones that were taken), with the plain sentence that says exactly that. Pure."""
    taken: dict[str, str] = {}
    if step == PURPOSE and route in _ROUTE_WORDS:
        taken["route"] = _ROUTE_WORDS[route]
    if step in (PURPOSE, FLOW_KIND) and flow in _FLOW_WORDS and taken.get("route", "CFD") == "CFD":
        taken["flow"] = _FLOW_WORDS[flow]
    if step in (PURPOSE, ANALYSIS) and analysis in _ANALYSIS_SAYS and taken.get("route", "FEA") == "FEA":
        taken["analysis"] = str(analysis_label or analysis).lower()
        taken["analysis_key"] = analysis
    out: dict[str, Any] = {"step": step, "their_words": str(words or ""), "taken": taken}
    out["plain_version_that_is_exactly_right"] = plain(out)
    return out


def analysis_facts(messages, submit_args: Mapping) -> dict[str, Any] | None:
    """The facts for an FEA plan whose turn's latest words typed the analysis ("a modal one please", "make it
    buckling") - the analysis the plan really carries (its summary names it) - or None: a click on an analysis
    button, the stage's own turn, words that name no analysis, a plan that does not carry the one named. Pure."""
    from meshpipeline.agents.intake import flow
    from meshpipeline.application import purpose_map as pm

    msgs = [m for m in messages or () if isinstance(m, Mapping)]
    users = [str(m.get("content") or "").strip() for m in msgs if m.get("role") == "user"]
    latest = users[-1] if users else ""
    buttons = flow._analysis_buttons() | flow._purpose_buttons() | {flow._stage_continue()}
    if not latest or latest in buttons or flow.asks_something(latest):
        return None
    key = pm.analysis_of(latest)
    if not key or key != flow.analysis_said(msgs):
        return None
    label = pm.ANALYSIS_LABELS.get(key, "")
    if not label or label.lower() not in str(submit_args.get("request_txt") or "").lower():
        return None
    return facts(ANALYSIS, words=latest, analysis=key, analysis_label=label)


def _a(words: str) -> str:
    return f"{'an' if str(words)[:1].lower() in 'aeiou' else 'a'} {words}"


def plain(f: Mapping) -> str:
    """The code's own sentence: exactly the facts, in plain words ("" when nothing was taken)."""
    t = f.get("taken") or {}
    if f.get("step") == FLOW_KIND and t.get("flow"):
        return f"Okay, flow {t['flow']}."
    if f.get("step") == ANALYSIS and t.get("analysis"):
        return f"Okay, {_a(t['analysis'])} analysis."
    route = t.get("route")
    if not route:
        return ""
    if route == "CFD":
        return "Okay, a CFD mesh" + (f" with the flow {t['flow']}" if t.get("flow") else "") + "."
    if route == "FEA":
        return "Okay, an FEA mesh" + (f" for {_a(t['analysis'])} analysis" if t.get("analysis") else "") + "."
    return "Okay, an electronics cooling case."


# #
# the model's sentence
# #

def _block_task() -> str:
    return ("The engineer has just answered one of the setup questions by typing, and the app's own next line comes "
            "right after your words (never mention the plan, the panel, Proceed or what comes next). In ONE short "
            "sentence, say back what you took from their answer - exactly the values in `taken`, in natural short "
            "words: the route (CFD, FEA or electronics cooling), the flow (through or around the part), the "
            "analysis. Use those values as given, never your own reading of their words, and say nothing that is "
            "not in `taken`. No numbers. Never ask a question.\n")


def _block_examples() -> str:
    return ("Examples (the facts differ each time):\n"
            "They: 'CFD, external aero over this car' (route CFD, flow around the part) -> 'Okay, a CFD mesh with "
            "the flow around the part.'\n"
            "They: 'it goes through it, it is a pipe' (flow through the part) -> 'Got it, the flow goes through the "
            "part.'\n"
            "They: 'a modal one please' (analysis vibration (modal)) -> 'Sure, a vibration (modal) analysis.'\n"
            "They: 'stress analysis of this bracket' (route FEA, analysis static stress) -> 'All right, an FEA "
            "mesh for a static stress analysis.'\n")


def _block_language() -> str:
    return ("Answer in the language the user wrote in. Plain text only: no markdown, no quotes around the reply. At "
            f"most {MAX_CHARS} characters.\n")


_PLAN_BLOCKS = {name: build for name, _purpose, build in PR.PLAN_REPLY_PROMPT_BLOCKS}
#: THE SAID-BACK SENTENCE'S PROMPT BLOCKS (name, purpose, build), composed in this order - the only system text
STEP_ACK_PROMPT_BLOCKS: tuple = (
    ("voice", "the plan step's own voice (plan_reply)", _PLAN_BLOCKS["voice"]),
    ("task", "say back what the typed answer was taken as, in one short sentence; never the next step's line, a "
             "number or a question", _block_task),
    ("examples", "four example exchanges in that voice", _block_examples),
    ("language", "the user's language; plain text; the sentence's own length cap", _block_language),
)


def compose_system() -> str:
    return "".join(build() for _name, _purpose, build in STEP_ACK_PROMPT_BLOCKS)


def messages_for(f: Mapping) -> list[dict]:
    shown = {k: v for k, v in dict(f).items() if k != "plain_version_that_is_exactly_right"}
    shown["taken"] = {k: v for k, v in (f.get("taken") or {}).items() if k != "analysis_key"}
    shown["plain_version_that_is_exactly_right"] = f.get("plain_version_that_is_exactly_right")
    return [{"role": "system", "content": compose_system()},
            {"role": "user", "content": "Write the sentence for this answer.\n"
                                        + json.dumps({"facts": shown}, ensure_ascii=False)}]


def check(reply: str, f: Mapping) -> str:
    """Why a model-written sentence may not be said ("" when it may), against the facts `f`."""
    text = " ".join(str(reply or "").split())
    if not text:
        return "empty"
    if len(text) > MAX_CHARS:
        return "too long"
    if "?" in text:
        return "asks something"
    if PR._RULE_ID.search(text):
        return "a rule id"
    if PR._BANNED.search(text):
        return "an exclamation, filler or emoji"
    if _STEP_SAID.search(text):
        return "says the next step's line"
    if _DIGIT.search(text):
        return "a number"
    t = f.get("taken") or {}
    route = t.get("route")
    if route:
        if not _SAYS[route].search(text):
            return f"the route taken ({route}) not said"
        for other in ("CFD", "FEA"):
            if other != route and re.search(rf"\b{other}\b", text):
                return f"another route said ({other})"
    flow = t.get("flow")
    if flow:
        other = next(v for v in _FLOW_WORDS.values() if v != flow)
        if not _SAYS[flow].search(text):
            return f"the flow taken ({flow}) not said"
        if _SAYS[other].search(text):
            return f"the other flow said ({other})"
    key = t.get("analysis_key")
    if key:
        if not _ANALYSIS_SAYS[key].search(text):
            return f"the analysis taken ({t.get('analysis')}) not said"
        # (the taken label's own words never count as another analysis: "thermal or thermal stress")
        rest = re.sub(re.escape(str(t.get("analysis") or "")), " ", text, flags=re.I)
        others = sorted(k for k in _ANALYSIS_SAYS if k != key and _ANALYSIS_SAYS[k].search(rest))
        if others:
            return f"another analysis said ({', '.join(others)})"
    elif route == "CFD" or (not route and flow):
        if any(_ANALYSIS_SAYS[k].search(text) for k in _ANALYSIS_SAYS if k != "thermal"):
            return "an analysis said that was not taken"
    return ""


async def _ask_model(messages: list[dict], *, job_id: str, user_id: str) -> str:
    from meshpipeline.contracts import model_inference as llm_router
    got = await llm_router.call_intake_model(messages=messages, job_id=job_id, user_id=user_id, name=MODEL_NAME)
    if not getattr(got, "ok", True):
        raise RuntimeError(str(getattr(got, "failure_marker", "") or "no answer"))
    return str(getattr(got, "assistant_text", "") or "").strip()


#: the model call (the unit tier replaces it: no live model there)
_ask = _ask_model


async def written(f: Mapping, *, session_id: str = "", user_id: str = "", ask_fn=None,
                  timeout_s: float = TIMEOUT_S) -> dict[str, Any]:
    """{"text": what is said, "source": model | fallback, "why": the guard's refusal or the failure}. Never
    raises. "" when nothing was taken (no model is asked)."""
    plain_text = str(f.get("plain_version_that_is_exactly_right") or plain(f))
    if not plain_text:
        return {"text": "", "source": SOURCE_FALLBACK, "why": "nothing taken"}
    try:
        said = await asyncio.wait_for((ask_fn or _ask)(messages_for(f), job_id=f"step-ack-{session_id}",
                                                       user_id=user_id), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a slow or failed model: the plain sentence (CFDREQ-37)
        logger.info("step ack: the plain sentence (%s, %s, %s) - session=%s", RULE, f.get("step"),
                    type(exc).__name__, session_id)
        return {"text": plain_text, "source": SOURCE_FALLBACK, "why": f"no answer ({type(exc).__name__})"}
    said = " ".join(str(said or "").strip().strip('"“”').split())
    why = check(said, f)
    if why:
        logger.info("step ack: the model's sentence refused by the check '%s' (%s) - session=%s", why, RULE,
                    session_id)
        return {"text": plain_text, "source": SOURCE_FALLBACK, "why": why}
    logger.info("step ack: written by the model (%s, %s) - session=%s", RULE, f.get("step"), session_id)
    return {"text": said, "source": SOURCE_MODEL, "why": ""}


async def sentence(f: Mapping | None, *, session_id: str = "", user_id: str = "", ask_fn=None) -> str:
    """The said-back sentence for these facts ("" for none). Never raises."""
    if not f:
        return ""
    try:
        return str((await written(f, session_id=session_id, user_id=user_id, ask_fn=ask_fn))["text"] or "")
    except Exception:  # noqa: BLE001 - nothing extra is said; the step's own line still stands
        logger.exception("step ack: not said (%s) - session=%s", RULE, session_id)
        return ""


def before_stage_line(reply: str, said: str) -> str:
    """The stage line with the sentence first, inside its drawing mark (the console reads the mark at the start)."""
    from meshpipeline.application.geometry_hold import DRAWING_MARK
    r = str(reply or "")
    if not said:
        return r
    if r.startswith(DRAWING_MARK):
        rest = r[len(DRAWING_MARK):].strip()
        return f"{DRAWING_MARK} {said} {rest[:1].upper()}{rest[1:]}"
    return f"{said}\n\n{r}" if r else said


def before_plan_line(reply: str, said: str) -> str:
    """The plan's message with the sentence before its last line (the plan line)."""
    if not said:
        return reply
    head, sep, line = str(reply or "").rpartition("\n\n")
    return f"{head}\n\n{said}\n\n{line}" if sep and head.strip() else f"{said}\n\n{reply}"


__all__ = ["ANALYSIS", "FLOW_KIND", "MAX_CHARS", "MODEL_NAME", "PURPOSE", "RULE", "SOURCE_FALLBACK", "SOURCE_MODEL",
           "STEP_ACK_PROMPT_BLOCKS", "TIMEOUT_S", "before_plan_line", "before_stage_line", "check", "compose_system",
           "facts", "messages_for", "plain", "sentence", "written"]
