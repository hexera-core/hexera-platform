# Responsibility: Own the intake's standard questionnaire - the fixed steps of each route, in order, their fixed words and buttons, which of them comes next, and the plan the application submits itself - so the order of a chat is the application's, never the model's mood.
# Owns: the step ids, the reading of a conversation back into steps, the CFD flow-conditions question and the flow-kind question, the reading of their answers, the FEA and CFD plans built from what the user confirmed, and the line an off-script answer returns to.
# Boundaries: pure over the conversation, except ScriptedPlanTurn, which only ever calls the intake executor's own tools (every gate - selection, admission token, validation, approval - still applies), and geometry_flow_facts, which reads the stored geometry check once its naming has landed. It stores nothing.
# Collaborates with: agents/intake/agent.py (runs the turn), agents/intake/message.py (asks the application's own questions), application/purpose_map.py (the purpose and analysis questions), agents/intake/approval.py (the plan line).
"""THE STANDARD QUESTIONNAIRE.

Every chat is the same short questionnaire. The application asks every step in fixed words, reads the
answers, and submits the plan itself; the model only answers what the user asks off-script (and reads
a correction to a plan that stands):

    FEA           purpose -> stage -> confirmed -> analysis        -> plan -> started
    CFD external  purpose -> stage -> confirmed -> flow_conditions -> plan -> started
    CFD internal  purpose -> stage -> confirmed -> flow_conditions -> plan -> started

`purpose`         "Geometry received. What are you meshing this for?" (the upload's greeting)
`purpose_clarify` the one two-button question when the words did not say CFD or FEA (or cooling)
`flow_kind`       CFD, once the naming has landed, only when the flow-kind authority still says
                  "undecided" and no words of the user's decided it (application/flow_kind, FLOW-4):
                  "Is the flow through the inside or around the outside?" with two buttons, asked
                  beside the open stage. The answer sets the stage's "The fluid flows" before
                  Proceed, and the stage line follows (purpose -> stage -> flow_kind -> stage -> ...)
`stage`           "Checking your part - confirm it on the right and press Proceed." The panel shows the
                  part, its unit and (internal flow) its openings; the chat box is closed while it is up.
`confirmed`       the short "Confirmed: <part>" card the panel's Proceed leaves in the chat
`analysis`        FEA: "What will you analyze?" with its buttons
`flow_conditions` CFD: what flows and how fast, with the wall treatment, as one proposal - "OK" or
                  "Resolve the wall (y⁺ ≈ 1)" buttons, or the user's own words. The mesh uses exactly
                  these: the fluid and its speed size the first cell at the wall, and the wall
                  treatment (wall functions, y⁺ 30–300, or a resolved wall, y⁺ ≈ 1) sets the layers - the plan
                  carries them as one sentence the run reads back (contracts/flow_conditions, CFDREQ-8).
`plan`            the plan card - the mesh plan, the notes box, and Proceed - under one fixed line
`started`         the run starts

A step the user answered early is skipped (an analysis named in the first message, the fluid and its
speed named before the stage). Off-script words are answered briefly and the chat returns to the same
step in the same words (`return_line`). Nothing else is ever asked.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.application import flow_kind as _fk
from meshpipeline.contracts import flow_conditions as _FC

#: THE STEP IDS - what a test reads a conversation as, whatever prose the model wrote.
PURPOSE = "purpose"
PURPOSE_CLARIFY = "purpose_clarify"
FLOW_KIND = "flow_kind"
STAGE = "stage"
CONFIRMED = "confirmed"
ANALYSIS = "analysis"
FLOW_QUESTION = "flow_conditions"
PLAN = "plan"
READING = "reading_notes"
STARTED = "started"
UNIT = "unit"
#: the model's own words: a brief answer to an off-script message
ANSWER = "answer"

ROUTE_FEA, ROUTE_CFD = "fea", "cfd"
#: The standard sequence of each route (FEA; CFD external and internal share one shape).
STANDARD: dict[str, tuple[str, ...]] = {
    ROUTE_FEA: (PURPOSE, STAGE, CONFIRMED, ANALYSIS, PLAN, STARTED),
    ROUTE_CFD: (PURPOSE, STAGE, CONFIRMED, FLOW_QUESTION, PLAN, STARTED),
}

_DIGIT = re.compile(r"\d")
_SIZE = re.compile(r"Part size: (\d+) x (\d+) x (\d+) mm")
_UNIT = re.compile(r"The file is in ([a-z]+)")
_BODIES = re.compile(r"(\d+) bodies\.")


def _found(rx: re.Pattern, text: Any) -> re.Match | None:
    """The first place `rx` matches in `text` (None when nowhere)."""
    return next(rx.finditer(str(text or "")), None)


# ------------------------------------------------------------- the CFD questions, fixed words ----

#: THE FLOW-CONDITIONS QUESTION's two leads (one per flow kind) - fixed; only the proposal in it
#: follows a fluid or speed the user already named.
EXTERNAL_LEAD = "What flows around it, and how fast?"
INTERNAL_LEAD = "What flows through it, and how fast?"
_PROPOSAL = " I'd go with {fluid} at {speed}{inlet}, with {wall} - OK, or tell me what to change."
#: the wall treatments' words and the plan's conditions sentence are contracts/flow_conditions' (CFDREQ-8): the run
#: reads the same sentence back, so what the plan says is what the mesh uses
WALL_FUNCTIONS = _FC.WALL_FUNCTIONS
WALL_RESOLVED = _FC.WALL_RESOLVED
#: its buttons: (label, what a click sends)
CONDITIONS_CHOICES = (("OK", "OK"), ("Resolve the wall (y⁺ ≈ 1)", "Resolve the wall (y⁺ ≈ 1)"))
#: THE FLOW-KIND QUESTION, asked only when neither the geometry nor the words decide it - the
#: flow-kind authority's own words (FLOW-4), one string in one place. Whatever its buttons send is
#: read back there as the user's stated words (FLOW-1).
FLOW_KIND_QUESTION = _fk.QUESTION
FLOW_KIND_CHOICES = (("Through the inside", "The flow goes through the inside"),
                     ("Around the outside", "The flow goes around the outside"))

_DEFAULT_FLUID = {"external": "air at 20 °C", "internal": "water at 20 °C"}
_DEFAULT_SPEED = {"external": "10 m/s", "internal": "1 m/s"}
_FLUIDS = _FC.FLUIDS
_SPEED = _FC.SPEED
_RESOLVED = re.compile(r"y\s*\+?\s*⁺?\s*(?:≈|~|=|of|below|under|<=?|around)?\s*1\b|resolve(?:d)? (?:the )?wall|"
                       r"wall[- ]resolved|low[- ]re\b|no wall functions", re.I)
_FUNCTIONS = re.compile(r"wall functions?|y\s*\+?\s*⁺?\s*(?:≈|~|=|of)?\s*30", re.I)
_OK = re.compile(r"^\s*(ok|okay|yes|yep|fine|sure|sounds good|looks good|go ahead|that'?s fine|correct|"
                 r"default|use (?:the )?defaults?)\b[.! ]*$", re.I)


def conditions_question(messages: Iterable[Any]) -> str:
    """The flow-conditions question for this conversation, in its fixed words with its proposal."""
    kind = flow_kind_of(messages) or "external"
    got = flow_conditions(messages, answered_only=False)
    lead = INTERNAL_LEAD if kind == "internal" else EXTERNAL_LEAD
    return lead + _PROPOSAL.format(fluid=got["fluid"], speed=got["speed"],
                                   inlet=" at the inlet" if kind == "internal" else "",
                                   wall=WALL_RESOLVED if got["wall"] == "resolved" else WALL_FUNCTIONS)


def is_conditions_question(text: Any) -> bool:
    t = str(text or "").strip()
    return any(t.startswith(lead) or ("\n\n" + lead) in t for lead in (EXTERNAL_LEAD, INTERNAL_LEAD))


def step_choices(reply: Any) -> list[dict]:
    """The buttons under a step this module asks, by its words; [] for any other reply."""
    t = str(reply or "").strip()
    if is_conditions_question(t):
        return [{"label": label, "send": send, "soon": False} for label, send in CONDITIONS_CHOICES]
    if _ends_with(t, FLOW_KIND_QUESTION):
        return [{"label": label, "send": send, "soon": False} for label, send in FLOW_KIND_CHOICES]
    return []


def _upload_greeting() -> str:
    from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT
    return UPLOAD_ACKNOWLEDGEMENT


def plan_line() -> str:
    from meshpipeline.agents.intake.approval import PROCEED_ASK
    return PROCEED_ASK


def _ends_with(text: Any, line: str) -> bool:
    t = str(text or "").strip()
    return bool(line) and (t == line or t.endswith("\n\n" + line))


def step_of(text: Any) -> tuple[str, ...]:
    """The step(s) one assistant message is: one id, or (ANSWER, step) for a brief answer the
    application closed with a step's own words. Any other message is the model's answer."""
    from meshpipeline.agents.intake import message as msg
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK
    from meshpipeline.application.geometry_hold import DRAWING_MARK

    t = str(text or "").strip()
    if not t:
        return ()
    if t == _upload_greeting():
        return (PURPOSE,)
    if t.startswith(DRAWING_MARK):
        return (STAGE,)
    if t.startswith(CONFIRMED_MARK):
        return (CONFIRMED,)
    if t == msg.READING_FIRST_REPLY:
        return (READING,)
    from meshpipeline.agents.intake import plan_talk as PT
    if t in (PT.ASK_LINE, PT.HOLD_LINE):
        # NOTE-18: the one go question is the plan step asking again, in fewer words; "not starting yet" is too
        return (PLAN,)
    if is_conditions_question(t):
        return (FLOW_QUESTION,) if any(t.startswith(lead) for lead in (EXTERNAL_LEAD, INTERNAL_LEAD)) \
            else (ANSWER, FLOW_QUESTION)
    lines = [(q, PURPOSE_CLARIFY) for q in pm.QUESTIONS.values()]
    lines += [(FLOW_KIND_QUESTION, FLOW_KIND), (pm.ANALYSIS_QUESTION, ANALYSIS), (plan_line(), PLAN)]
    for line, step in lines:
        if t == line:
            return (step,)
        if _ends_with(t, line):
            head = t[: -len(line)].strip()
            # the plan's own lead lines (what changed, a coming-soon note) are part of the step
            return (step,) if step == PLAN and _plan_lead(head) else (ANSWER, step)
    return (ANSWER,)


def _plan_lead(head: str) -> bool:
    from meshpipeline.agents.intake import approval as ap
    from meshpipeline.application import purpose_map as pm

    h = head.strip()
    return (not h or h.startswith("Changed from the ") or h.startswith(ap.NOTHING_CHANGED)
            or any(h == pm.coming_soon_note(k) for k in pm.ANALYSIS_KEYS if pm.coming_soon_note(k)))


def latest_declaration(messages) -> str:
    """The stage's latest declaration in the conversation ("" when no part was confirmed)."""
    from meshpipeline.application.geometry_confirmation import _is_declaration

    for m in reversed([m for m in messages or () if isinstance(m, dict)]):
        if _is_declaration(m):
            return str(m.get("content") or "")
    return ""


def route_of(messages: Iterable[Any]) -> str:
    """The route the conversation is on: FEA or CFD (by the stage's declaration, else the words)."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    if latest_declaration(msgs):
        return ROUTE_FEA if fea_declared(msgs) else ROUTE_CFD
    return ROUTE_FEA if pm.reading_for(msgs).route == pm.ROUTE_FEA else ROUTE_CFD


def read_messages(messages: Iterable[Any]) -> list[tuple[dict, tuple[str, ...]]]:
    """Each message with the step(s) it is: () for a user's."""
    out: list[tuple[dict, tuple[str, ...]]] = []
    said = ""
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            said = str(m.get("content") or "") if m.get("role") == "user" else said
            out.append((m, ()))
            continue
        got = step_of(m.get("content"))
        if got == (ANSWER, PLAN) and not asks_something(said) and "?" not in str(m.get("content") or ""):
            # CFDREQ-37: the conditions said back before the plan line, after words that asked nothing, are the
            # plan step's own lead (agents/intake/conditions_reply) - never an answer to a question
            got = (PLAN,)
        out.append((m, got))
    return out


def steps(messages: Iterable[Any]) -> list[str]:
    """The conversation read back as steps, in order - the assistant's messages only."""
    return [s for _m, got in read_messages(messages) for s in got]


# ---------------------------------------------------------------------------- the user's words ----

def _stage_continue() -> str:
    from meshpipeline.application.geometry_hold import CONTINUE_TEXT
    return CONTINUE_TEXT


def _analysis_buttons() -> set[str]:
    from meshpipeline.application import purpose_map as pm
    return {str(c.get("send") or "").strip() for c in pm.choices_for(pm.ANALYSIS_QUESTION)}


def _purpose_buttons() -> set[str]:
    from meshpipeline.application import purpose_map as pm
    return {c["send"] for q in pm.QUESTIONS.values() for c in pm.choices_for(q)}


def _step_buttons() -> set[str]:
    return {send for _l, send in CONDITIONS_CHOICES} | {send for _l, send in FLOW_KIND_CHOICES}


def _users(messages) -> list[str]:
    return [str(m.get("content") or "").strip() for m in messages or ()
            if isinstance(m, dict) and m.get("role") == "user"]


def latest_user(messages: Iterable[Any]) -> str:
    said = _users(messages)
    return said[-1] if said else ""


def asks_something(text: Any) -> bool:
    """Whether the user's message asks something (it then gets a brief answer first)."""
    t = str(text or "")
    return "?" in t or bool(re.match(r"\s*(what|why|how|which|when|where|who|can|could|does|do|is|are|will|"
                                     r"should|would)\b", t, re.IGNORECASE))


def analysis_said(messages: Iterable[Any]) -> str:
    """The FEA analysis the user has given (a key of purpose_map.ANALYSES), or "". The first message
    that names one gives it; a later one changes it only when it is an answer of its own ("Buckling",
    "make it modal") or names the analysis outright ("a modal analysis") - never a note that merely
    mentions a load. Never the words a purpose button sent, and never a question that names analyses
    ("what is the difference between modal and static?")."""
    from meshpipeline.application import purpose_map as pm

    skip = _purpose_buttons() | _step_buttons() | {_stage_continue()}
    picked = ""
    for content in _users(messages):
        if content in skip or asks_something(content):
            continue
        got = pm.analysis_of(content)
        if got and (not picked or plain_step_answer(content) or pm.named_analysis(content).analysis):
            picked = got
    return picked


#: Words that say something specific about the mesh or the simulation - a message carrying one is
#: kept as the user's note when the application submits the plan itself.
_SPECIFIC = re.compile(
    r"\d|\b(mm|cm|metres?|meters?|inch|element|elements|mesh|size|sizes|refine\w*|finer|coarser|layers?|"
    r"fixed|fix|load\w*|support\w*|material|steel|alumin\w*|solver|nastran|abaqus|ansys|calculix|femap|fluent|"
    r"openfoam|star-?ccm\+?|bolt\w*|holes?|fillets?|contact|bonded|second[- ]order|first[- ]order|quadratic|"
    r"linear|hex\w*|tet\w*|prism|wake|bend|gap|cells?|turbulen\w*|k-?(?:omega|epsilon|ω|ε)|sst)\b",
    re.IGNORECASE)


def plain_step_answer(text: Any) -> bool:
    """Whether a user message is only the answer to a standard step - the stage's Proceed, a step's
    button, or a few words naming the analysis - with nothing else for the model to read."""
    from meshpipeline.application import purpose_map as pm

    t = " ".join(str(text or "").split())
    if not t:
        return False
    if t == _stage_continue() or t in _analysis_buttons() or t in _step_buttons():
        return True
    words = re.findall(r"[\w'-]+", t)
    return bool(pm.analysis_of(t)) and len(words) <= 4 and "?" not in t and _found(_DIGIT, t) is None


# ------------------------------------------------------------------------- the CFD conditions ----

def flow_kind_of(messages: Iterable[Any]) -> str:
    """"internal" or "external": the confirmed declaration's, else the words' (the flow-kind question's
    answer included); "" when nothing says."""
    from meshpipeline.application import purpose_map as pm

    decl = latest_declaration(messages)
    if "; the fluid flows through it." in decl:
        return "internal"
    if "; the fluid flows around it." in decl:
        return "external"
    return pm.reading_for([m for m in messages or () if isinstance(m, dict)]).topology


def _condition_words(messages) -> list[str]:
    """The user's messages that can carry conditions: never a button the application offered for
    another step, the stage's own turn, or a question."""
    skip = _purpose_buttons() | _analysis_buttons() | {_stage_continue()} | {s for _l, s in FLOW_KIND_CHOICES}
    return [t for t in _users(messages) if t not in skip and not asks_something(t)]


def flow_conditions(messages: Iterable[Any], *, answered_only: bool = True) -> dict:
    """The CFD conditions as the user gave them, with the proposal for what they left: {fluid,
    speed, wall, said_fluid, said_speed, accepted}. The latest word on each wins."""
    kind = flow_kind_of(messages) or "external"
    out = {"fluid": _DEFAULT_FLUID[kind], "speed": _DEFAULT_SPEED[kind], "wall": "functions",
           "said_fluid": False, "said_speed": False, "said_wall": False, "accepted": False}
    for t in _condition_words(messages):
        fluid, speed = _found(_FLUIDS, t), _found(_SPEED, t)
        if fluid:
            name = fluid.group(1).lower()
            out["fluid"], out["said_fluid"] = (f"{name} at 20 °C" if name in ("air", "water") else name), True
        if speed:
            out["speed"], out["said_speed"] = " ".join(speed.group(0).split()), True
        if _found(_RESOLVED, t):
            out["wall"], out["said_wall"] = "resolved", True
        elif _found(_FUNCTIONS, t):
            out["wall"], out["said_wall"] = "functions", True
        if _found(_OK, t):
            out["accepted"] = True
    return out


def conditions_answered(messages: Iterable[Any]) -> bool:
    """Whether the CFD conditions are settled: the fixed question was answered (any answer that asks
    nothing: "OK", a button, the user's own values), or the user named a fluid and its speed already."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    asked = False
    for m in msgs:
        if m.get("role") == "assistant" and is_conditions_question(m.get("content")):
            asked = True
        elif asked and m.get("role") == "user" and not asks_something(m.get("content")) \
                and str(m.get("content") or "").strip() != _stage_continue():
            return True
    got = flow_conditions(msgs)
    return got["said_fluid"] and got["said_speed"]


def conditions_due(messages: Iterable[Any]) -> bool:
    """The flow-conditions step is next: a CFD part (through or around it) was confirmed on the stage,
    the question was never asked, and the words do not already settle the conditions."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    return (cfd_declared(msgs) and not any(m.get("role") == "assistant" and is_conditions_question(m.get("content"))
                                           for m in msgs) and not conditions_answered(msgs))


def cfd_declared(messages: Iterable[Any]) -> bool:
    """A part confirmed on the flow stage for a flow through or around it - the questionnaire's CFD
    route. An assembly of regions (conjugate heat transfer) and a whole thermal model have their own."""
    decl = latest_declaration(messages)
    return _found(_FLOW_CASE, decl) is not None if decl else False


_FLOW_CASE = re.compile(r"input_kind (body-surface|fluid-domain|solid-body); the fluid flows (through|around) it\.")
_PLANNABLE = re.compile(r"The fluid travels along [+-][xyz]\.|; the fluid flows through it\.")


# ------------------------------------------------------------------------- the flow-kind seam ----

def geometry_flow_facts(session_id: str) -> dict:
    """THE SEAM to the geometry check: the stored check's facts ONCE THE NAMING HAS LANDED, else {}. The
    flow-kind authority's record (`facts["flow_kind"]`: kind, flow, best_guess, confidence, by, rule,
    reasons, question, input_kind, geometry) is rewritten at the naming with the user's first words and
    the pictures (application/geometry_check._name_after_wait), so the scout's earlier record is never
    the one asked about. A whole thermal model declares its regions (FLOW-5): nothing to ask."""
    try:
        from meshpipeline.application.geometry_check import read_check
        stored = read_check(session_id) or {}
    except Exception:  # noqa: BLE001 - a check we cannot read decides nothing
        return {}
    facts: dict = stored["facts"] if isinstance(stored.get("facts"), dict) else {}
    if stored.get("status") != "ready" or not stored.get("named") or not isinstance(facts.get("flow_kind"), dict):
        return {}
    return {} if facts.get("check_mode") == "thermal" else facts


def _flow_words(messages) -> list[str]:
    """The user's messages that can state a flow: never another step's button, the stage's own turn, or a
    question (the flow-kind question's own buttons are statements)."""
    skip = _purpose_buttons() | _analysis_buttons() | {_stage_continue()} | {s for _l, s in CONDITIONS_CHOICES}
    mine = {s for _l, s in FLOW_KIND_CHOICES}
    return [t for t in _users(messages) if t in mine or (t not in skip and not asks_something(t))]


def stated_flow_kind(facts: dict, messages: Iterable[Any]) -> dict | None:
    """A FLOW THE USER STATED BEATS THE STORED VERDICT (FLOW-1): the latest of the user's messages that
    states where the fluid goes - a button of the flow-kind question, or their own words at any time - is
    decided again over the same facts (application/flow_kind.answer). None when no message states one,
    or before the naming has landed."""
    record = facts.get("flow_kind") if isinstance(facts, dict) else None
    if not isinstance(record, dict):
        return None
    for text in reversed(_flow_words(messages)):
        if _fk.words_reading(text).tier == "said":
            return _fk.answer(record, facts, text)
    return None


def geometry_flow_kind(facts: dict, messages: Iterable[Any] = ()) -> dict:
    """The flow kind that stands for this conversation: a flow the user stated (stated_flow_kind), else
    the record the naming wrote; {} before the naming has landed."""
    record = facts.get("flow_kind") if isinstance(facts, dict) else None
    if not isinstance(record, dict):
        return {}
    return stated_flow_kind(facts, messages) or record


def stage_flow(record: dict) -> dict:
    """What the stage's form takes from a record: "The fluid flows" (`flow`) and "The file is"
    (`input_kind`) - set before Proceed, so the user confirms what they answered."""
    return {"flow": str(record.get("flow") or ""), "input_kind": str(record.get("input_kind") or "")}


def flow_kind_question_due(messages: Iterable[Any], record: dict) -> bool:
    """The flow-kind step is next: the route is CFD, the record the naming wrote (with any flow the user
    stated since) is "undecided" and no words of the user's decided it - `by` "words" never asks -, no
    part is confirmed yet, and the question was never asked."""
    from meshpipeline.application import purpose_map as pm

    msgs = [m for m in messages or () if isinstance(m, dict)]
    rec = record if isinstance(record, dict) else {}
    if rec.get("kind") != _fk.UNDECIDED or rec.get("by") == "words":
        return False
    reading = pm.reading_for(msgs)
    return (reading.route == pm.ROUTE_FLOW and not reading.topology and not latest_declaration(msgs)
            and not any(m.get("role") == "assistant" and _ends_with(m.get("content"), FLOW_KIND_QUESTION)
                        for m in msgs))


# ------------------------------------------------------------------------------- what is due ----

def fea_plan_due(messages: Iterable[Any], *, approval_live: bool) -> bool:
    """The FEA route's plan is the next step: the part is confirmed on the FEA stage, the analysis is
    known, and no plan stands yet."""
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    return fea_declared(msgs) and not approval_live and bool(analysis_said(msgs))


def cfd_plan_due(messages: Iterable[Any], *, approval_live: bool) -> bool:
    """The CFD route's plan is the next step: a flow part is confirmed on the stage, its conditions
    are settled, and no plan stands yet."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    return cfd_declared(msgs) and not approval_live and conditions_answered(msgs) \
        and _found(_PLANNABLE, latest_declaration(msgs)) is not None


def plan_due(messages: Iterable[Any], *, approval_live: bool) -> bool:
    return fea_plan_due(messages, approval_live=approval_live) or cfd_plan_due(messages, approval_live=approval_live)


def owned(messages: Iterable[Any]) -> bool:
    """Whether the conversation is on a step the questionnaire owns: a part confirmed for FEA or for a
    flow through or around it, or the purpose question still open before any route."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    if latest_declaration(msgs):
        return fea_declared(msgs) or cfd_declared(msgs)
    return not pm.reading_for(msgs).settled


def return_line(messages: Iterable[Any], *, approval_live: bool, silent: bool = False) -> str:
    """The fixed words an off-script answer closes with, so the chat returns to the step it left:
    the plan's when a plan stands; the analysis question while an FEA part's analysis is open; the
    flow-conditions question while a CFD part's conditions are open; the purpose question while no
    route is settled. "" when nothing is open. (`silent`: the model said nothing - the same line.)"""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    if approval_live:
        return plan_line()
    if fea_declared(msgs):
        return "" if analysis_said(msgs) else pm.ANALYSIS_QUESTION
    if cfd_declared(msgs):
        return "" if conditions_answered(msgs) else conditions_question(msgs)
    if not latest_declaration(msgs):
        reading = pm.reading_for(msgs)
        if not reading.settled:
            return pm.QUESTIONS.get(reading.ask, pm.DOMAIN_QUESTION)
    return ""


def closed_with(reply: str, line: str) -> str:
    """The reply closed with the step's line (once - never twice)."""
    r = str(reply or "").strip()
    if not line or _ends_with(r, line):
        return r or line
    if r.endswith(line):
        r = r[: -len(line)].rstrip()
    return f"{r}\n\n{line}" if r else line


# ------------------------------------------------------------------- the plan, by the application ----

def _declaration(messages) -> str:
    return latest_declaration(messages)


def fea_part_words(messages) -> dict:
    """What the declaration says of the part: its name, size and unit, as written there."""
    from meshpipeline.application.geometry_confirmation import FEA_LEAD

    text = _declaration(messages)
    name = ""
    if FEA_LEAD in text:
        m = re.match(r"\s*\(([^()]*)\)", text.split(FEA_LEAD, 1)[1])
        name = m.group(1).strip() if m else ""
    else:
        m = _found(re.compile(r"the file is [^,(]*\(([^()]*)\), input_kind"), text)
        name = m.group(1).strip() if m else ""
    size = _found(_SIZE, text)
    unit = _found(_UNIT, text)
    bodies = _found(_BODIES, text)
    return {"name": name, "size": "x".join(size.groups()) if size else "",
            "unit": unit.group(1) if unit else "", "bodies": int(bodies.group(1)) if bodies else 1}


def _quotes(messages) -> list[str]:
    skip = _purpose_buttons() | _analysis_buttons() | _step_buttons() | {_stage_continue()}
    return [t for t in _users(messages) if t not in skip and _found(_SPECIFIC, t) is not None
            and not _found(_OK, t)]


def _part_phrase(part: dict, default: str) -> tuple[str, str]:
    what = part["name"] or default
    size = f", {part['size'].replace('x', ' x ')} mm" if part["size"] else ""
    unit = f" (the file is in {part['unit']})" if part["unit"] else ""
    return what, f"{size}{unit}"


def fea_submission(messages, *, engine: str, engine_params: dict) -> dict:
    """The FEA plan's submit_requirements arguments, written by the application from the
    declaration and the analysis - the FEA path asks nothing else and invents nothing: no support,
    load or material is stated; every face is a named group for the user's solver. The user's own
    specific words are quoted, so they are kept as notes word for word."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.shell_check import declared_shell, shell_summary

    part = fea_part_words(messages)
    analysis = analysis_said(messages) or "static"
    label = pm.ANALYSIS_LABELS.get(analysis, "Static stress")
    soon = analysis not in pm.V1_ANALYSES
    what, where = _part_phrase(part, "the assembly" if part["bodies"] > 1 else "the part")
    mesh = (f"The mesh is {shell_summary(messages)}." if declared_shell(messages) else
            "The mesh is a solid mesh of second-order 10-node elements (element_order 2) with every face kept as "
            "a named group.")
    analysis_words = (f"{label} analysis (coming soon - meshed as for static stress)" if soon
                      else f"{label.lower()} analysis")
    request = (f"Structural FEA of {what}{where}, confirmed by the user on the FEA stage. "
               f"The user will run a {analysis_words}. {mesh} "
               "No supports, loads or materials were stated in the chat: the user sets them in their own "
               "solver on the named faces. The user's own words are kept as notes, word for word.")
    review = ("The part is meshed whole, with no region missing. Every face of the part is present as a "
              "named group. Elements are well shaped, with no inverted or degenerate elements. Thin walls "
              "and small features are kept.")
    domain = " ".join(f"{part['name'] or ('assembly' if part['bodies'] > 1 else 'part')} {label} FEA".split()[:8])
    out = {"domain": domain[:80], "request_txt": request, "review_brief_txt": review, "patches": [],
           "dimensionality": "3D", "purpose": _display("PURPOSE", "structural"),
           "input_kind": _display("INPUT_KIND", "solid-body"),
           "mesh_engine": engine, "engine_source": "suggested_confirmed", "engine_params": dict(engine_params)}
    quotes = _quotes(messages)
    if quotes:
        out["user_note_quotes"] = quotes
    return out


_AXIS = re.compile(r"The fluid travels along ([+-][xyz])\.")
_REF = re.compile(r"Reference length: (\d+(?:\.\d+)?) mm")
_MARGINS = re.compile(r"Far-field margins in reference lengths: ([\d.]+) upstream, ([\d.]+) downstream, "
                      r"([\d.]+) to each side, ([\d.]+) above\.")
_KIND = re.compile(r"input_kind (body-surface|fluid-domain|solid-body); the fluid flows (through|around) it\.")


def cfd_case(messages, intake_patches) -> dict | None:
    """What the CFD plan is built from: the purpose, the kind, the patches and (external flow) the
    far field, all as the user confirmed them on the stage. None when the declaration does not carry
    what a plan needs (a flow direction left open, an internal flow with no opening) - the model then
    reads the conversation, as it always did."""
    text = _declaration(messages)
    kind = _found(_KIND, text)
    if not kind:
        return None
    if kind.group(2) == "through":
        patches = [dict(p) for p in intake_patches or () if isinstance(p, dict)]
        if not any(p.get("type") == "inlet" for p in patches) or not any(p.get("type") == "outlet" for p in patches):
            return None
        return {"purpose": "internal_cfd", "input_kind": kind.group(1), "patches": patches, "extra": {}}
    axis, ref, margins = _found(_AXIS, text), _found(_REF, text), _found(_MARGINS, text)
    if not axis:
        return None
    patches = [{"name": "wall", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
    if "The part stands on the ground." in text:
        patches.insert(1, {"name": "ground", "type": "wall"})
    extra: dict = {"flow_axis": axis.group(1)}
    if ref and margins:
        extra.update(reference_length_m=float(ref.group(1)) / 1000.0, requested_extents=dict(zip(
            ("upstream", "downstream", "lateral", "vertical"), (float(v) for v in margins.groups()), strict=True)))
    return {"purpose": "external_cfd", "input_kind": kind.group(1), "patches": patches, "extra": extra}


def cfd_submission(messages, case: dict, *, engine: str, engine_params: dict) -> dict:
    """The CFD plan's submit_requirements arguments, written by the application from the stage's
    declaration and the conditions the user gave (or accepted). Nothing is invented: a value the
    user left to the proposal is said to be the proposal; their own words are kept as notes."""
    part = fea_part_words(messages)
    ext = case["purpose"] == "external_cfd"
    got = flow_conditions(messages)
    what, where = _part_phrase(part, "the part")
    band = "y⁺ ≈ 1" if got["wall"] == "resolved" else "y⁺ 30–300"
    left = [w for w, said in (("the fluid", got["said_fluid"]), ("its speed", got["said_speed"]),
                              ("the wall treatment", got["said_wall"])) if not said]
    names = ", ".join(str(p.get("name")) for p in case["patches"])
    ports = ""
    if not ext:
        ports = " Openings: " + "; ".join(
            f"{p['name']} ({p['type']}" + (f", {float(p['diameter_mm']):.0f} mm across" if p.get("diameter_mm") else "")
            + ")" for p in case["patches"] if p.get("type") in ("inlet", "outlet")) + "."
    far = ""
    if ext:
        x = case["extra"]
        far = f" The flow travels along {x['flow_axis']}."
        if x.get("requested_extents"):
            e = x["requested_extents"]
            far += (f" Far-field margins in reference lengths of {x['reference_length_m']:g} m: {e['upstream']:g} upstream, "
                    f"{e['downstream']:g} downstream, {e['lateral']:g} to each side, {e['vertical']:g} above.")
    # CFDREQ-8: the conditions as ONE sentence the run reads back (contracts/flow_conditions) - the first layer is
    # sized from them, or the sentence says why it keeps the mesher's own sizing
    said = _FC.sentence(got["fluid"], got["speed"], got["wall"], internal=not ext, patches=case["patches"],
                        wall_said=bool(got["said_wall"]))
    request = (f"{'External' if ext else 'Internal'} flow {'around' if ext else 'through'} {what}{where}, confirmed "
               f"by the user on the geometry stage. {said}{ports}{far} "
               + (f"{_FC.ACCEPTED_LEAD} {', '.join(left)}. " if left else "")
               + "The user's own words are kept as notes, word for word.")
    review = (f"The fluid region is meshed whole. Every boundary patch is present: {names}. Near-wall prism "
              f"layers cover the walls for {band}. Cell sizes grade smoothly from the walls into the "
              + ("flow and out to the far field, which encloses the body without clipping it." if ext
                 else "core of the passage, with the openings resolved."))
    domain = " ".join(f"{part['name'] or 'part'} {'external' if ext else 'internal'} flow".split()[:8])
    out = {"domain": domain[:80], "request_txt": request, "review_brief_txt": review,
           "patches": case["patches"], "dimensionality": "3D",
           "purpose": _display("PURPOSE", case["purpose"]), "input_kind": _display("INPUT_KIND", case["input_kind"]),
           "mesh_engine": engine, "engine_source": "suggested_confirmed", "engine_params": dict(engine_params),
           **case["extra"]}
    if not ext and any("near_mm" in p for p in case["patches"]):
        out["port_details_note"] = "captured"
    quotes = _quotes(messages)
    if quotes:
        out["user_note_quotes"] = quotes
    return out


def route_question(text: Any) -> bool:
    """Whether the application's message is one of the route's own questions - what the mesh is for, CFD or FEA,
    the analysis, through or around: an answer to it chooses the route and is no note (contracts/user_notes.capture)."""
    from meshpipeline.application import purpose_map as pm

    t = str(text or "")
    return any(q and q in t for q in ("What are you meshing this for?", pm.DOMAIN_QUESTION, pm.ANALYSIS_QUESTION,
                                      FLOW_KIND_QUESTION))


def with_conditions_sentence(messages, args: dict) -> dict:
    """CFDREQ-8: A SUMMARY REDONE IN THE MODEL'S OWN WORDS KEEPS THE CONDITIONS. The application's plan carries the
    conditions as one fixed sentence the run reads back (contracts/flow_conditions); a note at the plan card ("use
    snappyHexMesh") had the model submit a new summary in its own words, the sentence was gone, and the run sized
    the layers as if no conditions step had been answered (job 9a7fcd14: relative layers, no conditions row). When
    the conversation answered the conditions and the request carries no such sentence, it is added - the user's own
    answer, as cfd_submission writes it. `args` itself otherwise."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    request = str(args.get("request_txt") or "")
    if not cfd_declared(msgs) or not conditions_answered(msgs):
        return args
    patches = [p for p in args.get("patches") or () if isinstance(p, dict)]
    if _FC.read(request, patches) is not None:
        return args
    got = flow_conditions(msgs)
    said = _FC.sentence(got["fluid"], got["speed"], got["wall"], internal=flow_kind_of(msgs) == "internal",
                        patches=patches, wall_said=bool(got["said_wall"]))
    return {**args, "request_txt": f"{request.rstrip()} {said}{_left_to_proposal(got)}".strip()}


def _left_to_proposal(got: dict) -> str:
    """The plan's words for what the user left to the proposal (cfd_submission's own clause, which the run reads: an
    accepted wall treatment is no ask of theirs - engines/cfd_requirements.with_conditions, CFDREQ-8)."""
    left = [w for w, said in (("the fluid", got["said_fluid"]), ("its speed", got["said_speed"]),
                              ("the wall treatment", got["said_wall"])) if not said]
    return f" {_FC.ACCEPTED_LEAD} {', '.join(left)}." if left else ""


def _engine_defaults(engine: str) -> dict:
    try:
        from meshpipeline.engines.registry import get_spec
        return {p.key: p.default for p in get_spec(engine).intake_params if getattr(p, "default", None)}
    except Exception:  # noqa: BLE001 - an engine without declared defaults takes none
        return {}


#: The turn's modes (agents/intake/agent.py picks one; ScriptedPlanTurn runs it).
MODE_FREE, MODE_PLAN, MODE_ANSWER, MODE_ANSWER_THEN_PLAN, MODE_READ_THEN_PLAN = (
    "free", "plan", "answer", "answer_then_plan", "read_then_plan")
#: What the model may call while it answers a question: tools that read, or settle the engine the plan
#: names (the engine is chosen, never asked) - never the admission check or the submission, which put
#: a step in front of the user
READ_ONLY_TOOLS = frozenset({"web_search", "recommend_compatible_engines", "propose_engine_selection",
                             "confirm_engine_selection"})


def turn_mode(messages, *, approval_live: bool, plan_was_shown: bool) -> str:
    """Who does what this turn. On a route the questionnaire owns:
    - the plan is due and the user asked nothing: the APPLICATION submits it, no model (MODE_PLAN) -
      unless a plan was already shown and the user's own words change it on the CFD route: the model
      reads the change first, and the application submits if it did not (MODE_READ_THEN_PLAN);
    - the user asked something: the model answers in words only, with no tool that changes anything,
      and the plan follows when it is due (MODE_ANSWER_THEN_PLAN) or the open step's own words close
      the answer (MODE_ANSWER);
    - an FEA part's analysis is still open and the words did not give it: the model answers in words
      and the analysis question follows (MODE_ANSWER).
    Anything else is the model's turn as it always was (MODE_FREE)."""
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    if not owned(msgs):
        return MODE_FREE
    latest = latest_user(msgs)
    due = plan_due(msgs, approval_live=approval_live)
    if asks_something(latest):
        return MODE_ANSWER_THEN_PLAN if due else MODE_ANSWER
    if due:
        if plan_was_shown and cfd_declared(msgs) and not plain_step_answer(latest) and not _found(_OK, latest):
            return MODE_READ_THEN_PLAN
        return MODE_PLAN
    if fea_declared(msgs) and not analysis_said(msgs) and not approval_live:
        return MODE_ANSWER
    return MODE_FREE


@dataclass
class ScriptedPlanTurn:
    """THE PLAN, SUBMITTED BY THE APPLICATION. In MODE_PLAN the turn's rounds are the application's:
    propose the route's mesher, preview the exact payload, submit it - through the intake executor's
    own tools, so every gate applies; a refusal hands the rest of the turn back to the model, with
    every result in hand. In an answer mode the model is offered no tool that changes anything.
    `run_plan` submits the plan directly after a turn the model answered in (agent.py)."""

    exec_state: Any
    messages: list
    model: Any
    mode: str = MODE_FREE
    intake_patches: list = field(default_factory=list)
    active: bool = False
    rounds: int = 0
    handed_back: bool = False
    used: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.active = self.mode == MODE_PLAN

    def _call(self, name: str, args: dict):
        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
        self.rounds += 1
        self.used.append(name)
        return ModelRoundResult(
            tool_calls=(ToolCallRequest(id=f"app-{self.rounds}-{name}", name=name, arguments=json.dumps(args)),),
            assistant_text="", finish_reason="tool_calls")

    def _case(self) -> dict | None:
        from meshpipeline.application.geometry_confirmation import fea_declared
        if fea_declared(self.messages):
            return {"purpose": "structural", "input_kind": "solid-body", "patches": [], "extra": {}}
        return cfd_case(self.messages, self.intake_patches)

    def next_call(self, last_result: str) -> tuple[str, dict] | None:
        """The application's next tool call (name, arguments), from the last tool result; None when the
        plan is submitted or a gate refused it."""
        from meshpipeline.agents.intake import engine_selection as es
        st = self.exec_state
        case = self._case()
        if case is None:
            return None
        last = self.used[-1] if self.used else ""
        if not last:
            return "propose_engine_selection", {
                "engine": _vocab_display("gmsh" if case["purpose"] == "structural" else "snappy")}
        if (last == "propose_engine_selection" and "Engines that can:" in last_result
                and self.used.count("propose_engine_selection") < 2):
            able = last_result.split("Engines that can:", 1)[1].split(".")[0].split(",")[0].strip()
            return "propose_engine_selection", {"engine": able}
        sel = st.selection or {}
        engine = str(sel.get("engine") or "")
        if last == "propose_engine_selection":
            if es.state_of(sel) != es.CONFIRMED or not engine:
                return None
            return "preview_selected_admission", {
                "selected_engine": _vocab_display(engine), "purpose": _display("PURPOSE", case["purpose"]),
                "input_kind": _display("INPUT_KIND", case["input_kind"]), "dimensionality": "3D",
                "patches": case["patches"], "engine_params": _engine_defaults(engine)}
        if last == "preview_selected_admission":
            try:
                token = str(json.loads(last_result.split("\n\n")[-1]).get("preview_token") or "")
            except (ValueError, AttributeError):
                token = ""
            if not token:
                return None
            params = _engine_defaults(engine)
            args = (fea_submission(self.messages, engine=_vocab_display(engine), engine_params=params)
                    if case["purpose"] == "structural" else
                    cfd_submission(self.messages, case, engine=_vocab_display(engine), engine_params=params))
            return "submit_requirements", {**args, "preview_token": token}
        return None

    async def run_plan(self, executor) -> bool:
        """THE PLAN, SUBMITTED DIRECTLY through the executor's own tools (after a turn the model only
        answered in). True when it was submitted."""
        self.used, result = [], ""
        while True:
            call = self.next_call(result)
            if call is None:
                return self.exec_state.submit_args is not None
            name, args = call
            self.used.append(name)
            result = (await executor.run(name, args)).content
            if name == "submit_requirements":
                return self.exec_state.submit_args is not None

    async def __call__(self, *, messages, tools, job_id, user_id, tool_choice="auto"):
        # the provider's own signature, never **kwargs: a round offers a reasoning sink only to a
        # provider that names one, and the intake's does not
        if self.active:
            last = next((str(m.get("content") or "") for m in reversed(list(messages or []))
                         if isinstance(m, dict) and m.get("role") == "tool"), "")
            call = self.next_call(last)
            if call is not None:
                return self._call(*call)
            self.active, self.handed_back = False, True
        if self.mode in (MODE_ANSWER, MODE_ANSWER_THEN_PLAN) and not self.handed_back:
            # AN ANSWER IN WORDS: no tool that changes anything is offered - the order is not the model's
            tools = [t for t in tools or () if (t.get("function") or {}).get("name") in READ_ONLY_TOOLS]
            got = await self.model(messages=messages, tools=tools, job_id=job_id, user_id=user_id,
                                   tool_choice=tool_choice)
            kept = tuple(c for c in got.tool_calls if c.name in READ_ONLY_TOOLS)
            if len(kept) != len(got.tool_calls):
                # a call to a tool it was not offered is never run: its words are the answer
                import dataclasses
                return dataclasses.replace(got, tool_calls=kept, finish_reason=got.finish_reason if kept else "stop")
            return got
        return await self.model(messages=messages, tools=tools, job_id=job_id, user_id=user_id,
                                tool_choice=tool_choice)


def _live(approval) -> bool:
    from meshpipeline.agents.intake import approval as ap
    return ap.is_live(approval)


def _display(kind: str, key: str) -> str:
    from meshpipeline.agents.intake import vocabulary as _vocab
    return _vocab.to_display(getattr(_vocab, kind), key)


def _vocab_display(engine_or_key: str) -> str:
    from meshpipeline.agents.intake import vocabulary as _vocab
    return _vocab.to_display(_vocab.ENGINE, engine_or_key)


__all__ = ["ANALYSIS", "ANSWER", "CONDITIONS_CHOICES", "CONFIRMED", "EXTERNAL_LEAD", "FLOW_KIND",
           "FLOW_KIND_CHOICES", "FLOW_KIND_QUESTION", "FLOW_QUESTION", "INTERNAL_LEAD", "MODE_ANSWER",
           "MODE_ANSWER_THEN_PLAN", "MODE_FREE", "MODE_PLAN", "MODE_READ_THEN_PLAN", "PLAN", "PURPOSE",
           "PURPOSE_CLARIFY", "READING", "ROUTE_CFD", "ROUTE_FEA", "STAGE", "STANDARD", "STARTED", "UNIT",
           "ScriptedPlanTurn", "analysis_said", "asks_something", "cfd_case", "cfd_declared", "cfd_plan_due",
           "cfd_submission", "closed_with", "conditions_answered", "conditions_due", "conditions_question",
           "fea_part_words", "fea_plan_due", "fea_submission", "flow_conditions", "flow_kind_of",
           "flow_kind_question_due", "geometry_flow_facts", "geometry_flow_kind", "is_conditions_question",
           "latest_declaration", "latest_user", "owned", "plain_step_answer", "plan_due", "plan_line",
           "read_messages", "return_line", "route_of", "stage_flow", "stated_flow_kind", "step_choices", "step_of",
           "steps", "turn_mode"]
