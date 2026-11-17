# Responsibility: Own the intake's standard questionnaire - the fixed steps of each route, in order, and which of them comes next - so the order of a chat is the application's, never the model's mood.
# Owns: the step ids, the reading of a conversation back into steps, when the plan is due, the FEA plan the application submits itself when the user's answer leaves nothing to understand, and the line an off-script answer returns to.
# Boundaries: pure over the conversation, except ScriptedFeaTurn, which only ever calls the intake executor's own tools (every gate - selection, admission token, validation, approval - still applies). It stores nothing.
# Collaborates with: agents/intake/agent.py (runs the turn), agents/intake/message.py (the application's own questions), application/purpose_map.py (the purpose and analysis questions), agents/intake/approval.py (the plan line).
"""THE STANDARD QUESTIONNAIRE.

Every chat is the same short questionnaire. The application asks every fixed step in fixed words;
the model only reads what the user meant, and writes the one CFD flow question:

    FEA           purpose -> stage -> confirmed -> analysis -> plan -> started
    CFD external  purpose -> stage -> confirmed -> flow_question -> plan -> started
    CFD internal  purpose -> stage -> confirmed -> flow_question -> plan -> started

`purpose`        "Geometry received. What are you meshing this for?" (the upload's greeting)
`purpose_clarify` the one two-button question when the words did not say CFD or FEA (or cooling)
`stage`          "Checking your part - confirm it on the right and press Proceed." The panel shows
                 the part, its unit and (internal flow) its openings; the chat box is closed while
                 it is up.
`confirmed`      the short "Confirmed: <part>" card the panel's Proceed leaves in the chat
`analysis`       FEA only: "What will you analyze?" with its buttons
`flow_question`  CFD only: ONE question in the model's words - the fluid and its conditions, as a
                 proposal ("OK, or tell me what to change"). Skipped when the user already said them.
`plan`           the plan card - the mesh plan, the notes box, and Proceed - under one fixed line
`started`        the run starts

A step the user answered early is skipped (an analysis named in the first message, the fluid named
before the stage). Off-script words are answered briefly and the chat returns to the same next step
in the same fixed words (`return_line`). Nothing else is ever asked.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: THE STEP IDS - what a test reads a conversation as, whatever prose the model wrote.
PURPOSE = "purpose"
PURPOSE_CLARIFY = "purpose_clarify"
STAGE = "stage"
CONFIRMED = "confirmed"
ANALYSIS = "analysis"
FLOW_QUESTION = "flow_question"
PLAN = "plan"
READING = "reading_notes"
STARTED = "started"
UNIT = "unit"
#: the model's own words that are not a step: an answer to an off-script message
ANSWER = "answer"

ROUTE_FEA, ROUTE_CFD = "fea", "cfd"


_DIGIT = re.compile(r"\d")
_SIZE = re.compile(r"Part size: (\d+) x (\d+) x (\d+) mm")
_UNIT = re.compile(r"The file is in ([a-z]+)")
_BODIES = re.compile(r"(\d+) bodies\.")


def _found(rx: re.Pattern, text: str) -> re.Match | None:
    """The first place `rx` matches in `text` (None when nowhere)."""
    return next(rx.finditer(str(text or "")), None)
#: The standard sequence of each route (FEA; CFD external and internal share one shape).
STANDARD: dict[str, tuple[str, ...]] = {
    ROUTE_FEA: (PURPOSE, STAGE, CONFIRMED, ANALYSIS, PLAN, STARTED),
    ROUTE_CFD: (PURPOSE, STAGE, CONFIRMED, FLOW_QUESTION, PLAN, STARTED),
}


def _upload_greeting() -> str:
    from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT
    return UPLOAD_ACKNOWLEDGEMENT


def plan_line() -> str:
    from meshpipeline.agents.intake.approval import PROCEED_ASK
    return PROCEED_ASK


def _ends_with(text: str, line: str) -> bool:
    t = str(text or "").strip()
    return bool(line) and (t == line or t.endswith("\n\n" + line))


def step_of(text: Any) -> tuple[str, ...]:
    """The step(s) one assistant message is: one id, or (ANSWER, step) for a brief answer the
    application closed with a step's own line. A message no step owns is the model's: on the CFD
    route that is the flow question, anywhere else an answer (`steps` tells them apart)."""
    from meshpipeline.agents.intake import message as msg
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK
    from meshpipeline.application.geometry_hold import DRAWING_MARK

    t = str(text or "").strip()
    if not t:
        return ()
    if t == _upload_greeting():
        return (PURPOSE,)
    if t in pm.QUESTIONS.values():
        return (PURPOSE_CLARIFY,)
    if t.startswith(DRAWING_MARK):
        return (STAGE,)
    if t.startswith(CONFIRMED_MARK):
        return (CONFIRMED,)
    if t == msg.READING_FIRST_REPLY:
        return (READING,)
    for line, step in ((pm.ANALYSIS_QUESTION, ANALYSIS), (plan_line(), PLAN)):
        if t == line:
            return (step,)
        if _ends_with(t, line):
            head = t[: -len(line)].strip()
            # the plan's own lead lines (what changed, a coming-soon note) are part of the step
            return (step,) if _plan_lead(head) else (ANSWER, step)
    return (ANSWER,)


def _plan_lead(head: str) -> bool:
    from meshpipeline.agents.intake import approval as ap
    from meshpipeline.application import purpose_map as pm

    h = head.strip()
    return (not h or h.startswith("Changed from the ") or h.startswith(ap.NOTHING_CHANGED)
            or any(h == pm.coming_soon_note(k) for k in pm.ANALYSIS_KEYS if pm.coming_soon_note(k)))


def route_of(messages: Iterable[Any]) -> str:
    """The route the conversation is on: FEA or CFD (by the stage's declaration, else the words)."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    if latest_declaration(msgs):
        return ROUTE_FEA if fea_declared(msgs) else ROUTE_CFD
    return ROUTE_FEA if pm.reading_for(msgs).route == pm.ROUTE_FEA else ROUTE_CFD


#: What the CFD route's one flow question is about: the fluid and its conditions.
_FLOW_WORDS = re.compile(r"\b(fluid|air|water|oil|gas|speed|velocity|m/s|km/h|mph|flow rate|mass flow|"
                         r"conditions?|turbulen\w*|reynolds|mach|temperature|pressure)\b", re.IGNORECASE)


def is_flow_question(text: Any) -> bool:
    """Whether a model's message is the CFD route's flow question: it asks about the fluid and its
    conditions."""
    t = str(text or "")
    return "?" in t and _found(_FLOW_WORDS, t) is not None


def read_messages(messages: Iterable[Any]) -> list[tuple[dict, tuple[str, ...]]]:
    """Each message with the step(s) it is: () for a user's. On the CFD route the model's first
    question about the fluid and its conditions after the stage is the flow question; every other
    message of the model's is an answer."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    cfd = route_of(msgs) == ROUTE_CFD
    seen: list[str] = []
    out: list[tuple[dict, tuple[str, ...]]] = []
    for m in msgs:
        if m.get("role") != "assistant":
            out.append((m, ()))
            continue
        got = list(step_of(m.get("content")))
        for i, step in enumerate(got):
            if (step == ANSWER and cfd and CONFIRMED in seen and FLOW_QUESTION not in seen
                    and PLAN not in seen and is_flow_question(m.get("content"))):
                got[i] = FLOW_QUESTION
        seen.extend(got)
        out.append((m, tuple(got)))
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


def latest_user(messages: Iterable[Any]) -> str:
    for m in reversed([m for m in messages or () if isinstance(m, dict)]):
        if m.get("role") == "user":
            return str(m.get("content") or "").strip()
    return ""


def analysis_said(messages: Iterable[Any]) -> str:
    """The FEA analysis the user has given (a key of purpose_map.ANALYSES), or "". The first message
    that names one gives it; a later one changes it only when it is an answer of its own ("Buckling",
    "make it modal") or names the analysis outright ("a modal analysis") - never a note that merely
    mentions a load. Never the words a purpose button sent, and never a question that names analyses
    ("what is the difference between modal and static?")."""
    from meshpipeline.application import purpose_map as pm

    skip = _purpose_buttons() | {_stage_continue()}
    picked = ""
    for m in messages or ():
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = str(m.get("content") or "").strip()
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
    r"fixed|fix|load\w*|support\w*|material|steel|alumin\w*|solver|nastran|abaqus|ansys|calculix|femap|"
    r"bolt\w*|holes?|fillets?|contact|bonded|second[- ]order|first[- ]order|quadratic|linear|hex\w*|tet\w*)\b",
    re.IGNORECASE)


def plain_step_answer(text: str) -> bool:
    """Whether a user message is only the answer to a standard step - the stage's Proceed, an
    analysis button, or a few words naming the analysis - with nothing else for the model to read."""
    from meshpipeline.application import purpose_map as pm

    t = " ".join(str(text or "").split())
    if not t:
        return False
    if t == _stage_continue() or t in _analysis_buttons():
        return True
    words = re.findall(r"[\w'-]+", t)
    return bool(pm.analysis_of(t)) and len(words) <= 4 and "?" not in t and _found(_DIGIT, t) is None


def asks_something(text: Any) -> bool:
    """Whether the user's message asks something (it then gets a brief answer first)."""
    t = str(text or "")
    return "?" in t or bool(re.match(r"\s*(what|why|how|which|when|where|who|can|could|does|do|is|are|will|"
                                     r"should|would)\b", t, re.IGNORECASE))


# ------------------------------------------------------------------------------- what is due ----

def fea_plan_due(messages: Iterable[Any], *, approval_live: bool) -> bool:
    """The FEA route's plan is the next step: the part is confirmed on the FEA stage, the analysis is
    known, and no plan stands yet."""
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    return fea_declared(msgs) and not approval_live and bool(analysis_said(msgs))


def cfd_plan_due(messages: Iterable[Any], *, approval_live: bool) -> bool:
    """The CFD route's plan is the next step: the part is confirmed on the flow stage, the model has
    asked its one flow question since, and the user's latest message answers it (asks nothing)."""
    msgs = [m for m in messages or () if isinstance(m, dict)]
    if approval_live or route_of(msgs) != ROUTE_CFD or not latest_declaration(msgs):
        return False
    if not msgs or msgs[-1].get("role") != "user" or asks_something(msgs[-1].get("content")):
        return False
    # the message the user just answered is the conversation's flow question itself
    read = read_messages(msgs)
    last_assistant = next((s for m, s in reversed(read) if m.get("role") == "assistant"), ())
    return read[-1][0] is msgs[-1] and last_assistant == (FLOW_QUESTION,)


#: THE FLOW QUESTION IN FIXED WORDS, for the one turn the model said nothing where it was due (it is
#: the model's own words otherwise): what the fluid is and how fast it goes, as a proposal.
FLOW_FALLBACK = ("What is the fluid, and how fast does it flow? I'd go with air at 20 °C and 10 m/s - OK, or "
                 "tell me what to change.")


def return_line(messages: Iterable[Any], *, approval_live: bool, silent: bool = False) -> str:
    """The fixed line an off-script answer closes with, so the chat returns to the step it left:
    the plan's when a plan stands, the analysis question while an FEA part's analysis is open, and -
    only for a turn the model said nothing in (`silent`) - the CFD flow question when it is due; ""
    when the next step is the model's own or nothing is open."""
    from meshpipeline.application import purpose_map as pm
    from meshpipeline.application.geometry_confirmation import fea_declared

    msgs = [m for m in messages or () if isinstance(m, dict)]
    if approval_live:
        return plan_line()
    if fea_declared(msgs) and not analysis_said(msgs):
        return pm.ANALYSIS_QUESTION
    if silent and latest_declaration(msgs) and route_of(msgs) == ROUTE_CFD and FLOW_QUESTION not in steps(msgs) \
            and PLAN not in steps(msgs):
        return FLOW_FALLBACK
    return ""


def closed_with(reply: str, line: str) -> str:
    """The reply closed with the step's line (once - never twice)."""
    r = str(reply or "").strip()
    if not line or _ends_with(r, line):
        return r or line
    if r.endswith(line):
        r = r[: -len(line)].rstrip()
    return f"{r}\n\n{line}" if r else line


# ----------------------------------------------------------- the FEA plan, by the application ----

def latest_declaration(messages) -> str:
    """The stage's latest declaration in the conversation ("" when no part was confirmed)."""
    from meshpipeline.application.geometry_confirmation import _is_declaration

    for m in reversed([m for m in messages or () if isinstance(m, dict)]):
        if _is_declaration(m):
            return str(m.get("content") or "")
    return ""


def _declaration(messages) -> str:
    return latest_declaration(messages)


def fea_part_words(messages) -> dict:
    """What the FEA declaration says of the part: its name, size and unit, as written there."""
    from meshpipeline.application.geometry_confirmation import FEA_LEAD

    text = _declaration(messages)
    after = text.split(FEA_LEAD, 1)[1] if FEA_LEAD in text else ""
    name = ""
    m = re.match(r"\s*\(([^()]*)\)", after)
    if m:
        name = m.group(1).strip()
    size = _found(_SIZE, text)
    unit = _found(_UNIT, text)
    bodies = _found(_BODIES, text)
    return {"name": name, "size": "x".join(size.groups()) if size else "",
            "unit": unit.group(1) if unit else "", "bodies": int(bodies.group(1)) if bodies else 1}


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
    what = part["name"] or ("the assembly" if part["bodies"] > 1 else "the part")
    size = f", {part['size'].replace('x', ' x ')} mm" if part["size"] else ""
    unit = f" (the file is in {part['unit']})" if part["unit"] else ""
    shell = declared_shell(messages)
    mesh = (f"The mesh is {shell_summary(messages)}." if shell else
            "The mesh is a solid mesh of second-order 10-node elements (element_order 2) with every face kept as "
            "a named group.")
    analysis_words = (f"{label} analysis (coming soon - meshed as for static stress)" if soon
                      else f"{label.lower()} analysis")
    request = (f"Structural FEA of {what}{size}{unit}, confirmed by the user on the FEA stage. "
               f"The user will run a {analysis_words}. {mesh} "
               "No supports, loads or materials were stated in the chat: the user sets them in their own "
               "solver on the named faces. The user's own words are kept as notes, word for word.")
    review = ("The part is meshed whole, with no region missing. Every face of the part is present as a "
              "named group. Elements are well shaped, with no inverted or degenerate elements. Thin walls "
              "and small features are kept.")
    domain = " ".join(f"{part['name'] or ('assembly' if part['bodies'] > 1 else 'part')} {label} FEA".split()[:8])
    skip = _purpose_buttons() | _analysis_buttons() | {_stage_continue()}
    quotes = [str(m.get("content") or "") for m in messages or () if isinstance(m, dict)
              and m.get("role") == "user" and str(m.get("content") or "").strip() not in skip
              and _found(_SPECIFIC, str(m.get("content") or "")) is not None]
    out = {"domain": domain[:80], "request_txt": request, "review_brief_txt": review, "patches": [],
           "dimensionality": "3D", "purpose": "structural", "input_kind": "solid-body",
           "mesh_engine": engine, "engine_source": "suggested_confirmed",
           "engine_params": dict(engine_params)}
    if quotes:
        out["user_note_quotes"] = quotes
    return out


def _engine_defaults(engine: str) -> dict:
    try:
        from meshpipeline.engines.registry import get_spec
        return {p.key: p.default for p in get_spec(engine).intake_params if getattr(p, "default", None)}
    except Exception:  # noqa: BLE001 - an engine without declared defaults takes none
        return {}


@dataclass
class ScriptedFeaTurn:
    """THE FEA PLAN, SUBMITTED BY THE APPLICATION. When the FEA plan is due and the user's message
    leaves nothing to understand (the stage's Proceed, an analysis button), the turn's rounds are
    the application's: propose the FEA mesher, preview the exact payload, submit it - through the
    intake executor's own tools, so every gate applies. `takeover` starts the script when the model
    answers a due plan with prose instead. Any refusal hands the rest of the turn back to the model."""

    exec_state: Any
    messages: list
    model: Any
    active: bool = False
    takeover: bool = False
    rounds: int = 0
    handed_back: bool = False
    took_over: bool = False
    used: list = field(default_factory=list)

    def _resp(self, name: str, args: dict):
        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
        self.rounds += 1
        self.used.append(name)
        return ModelRoundResult(
            tool_calls=(ToolCallRequest(id=f"app-{self.rounds}-{name}", name=name,
                                        arguments=json.dumps(args)),),
            assistant_text="", finish_reason="tool_calls")

    @staticmethod
    def _last_tool(messages) -> dict:
        for m in reversed(list(messages or [])):
            if isinstance(m, dict) and m.get("role") == "tool":
                try:
                    got = json.loads(str(m.get("content") or "").split("\n\n")[-1])
                    return got if isinstance(got, dict) else {"text": str(m.get("content") or "")}
                except ValueError:
                    return {"text": str(m.get("content") or "")}
        return {}

    def _next(self, messages):
        from meshpipeline.agents.intake import engine_selection as es
        st = self.exec_state
        if not self.used:
            return self._resp("propose_engine_selection", {"engine": _vocab_display("gmsh")})
        last = self.used[-1]
        sel = st.selection or {}
        engine = str(sel.get("engine") or "")
        if last == "propose_engine_selection":
            if es.state_of(sel) != es.CONFIRMED or not engine:
                return None
            return self._resp("preview_selected_admission", {
                "selected_engine": _vocab_display(engine), "purpose": _display("PURPOSE", "structural"),
                "input_kind": _display("INPUT_KIND", "solid-body"), "dimensionality": "3D", "patches": [],
                "engine_params": _engine_defaults(engine)})
        if last == "preview_selected_admission":
            token = str(self._last_tool(messages).get("preview_token") or "")
            if not token:
                return None
            args = fea_submission(self.messages, engine=_vocab_display(engine),
                                  engine_params=_engine_defaults(engine))
            args.update(purpose=_display("PURPOSE", "structural"),
                        input_kind=_display("INPUT_KIND", "solid-body"), preview_token=token)
            return self._resp("submit_requirements", args)
        return None

    async def __call__(self, *, messages, tools, job_id, user_id, tool_choice="auto"):
        # the provider's own signature, never **kwargs: a round offers a reasoning sink only to a
        # provider that names one, and the intake's does not
        kw = {"messages": messages, "tools": tools, "job_id": job_id, "user_id": user_id,
              "tool_choice": tool_choice}
        if self.active:
            step = self._next(kw.get("messages") or [])
            if step is not None:
                return step
            # a gate refused: the rest of the turn is the model's, with every result in hand
            self.active, self.handed_back = False, True
            return await self.model(**kw)
        got = await self.model(**kw)
        if (self.takeover and not self.handed_back and not got.tool_calls
                and self.exec_state.submit_args is None and not _live(self.exec_state.approval)):
            # PROSE WHERE THE PLAN IS DUE: set aside - the application submits the plan itself
            self.active, self.took_over = True, True
            step = self._next(kw.get("messages") or [])
            if step is not None:
                return step
            self.active, self.handed_back = False, True
        return got


def _live(approval) -> bool:
    from meshpipeline.agents.intake import approval as ap
    return ap.is_live(approval)


def _display(kind: str, key: str) -> str:
    from meshpipeline.agents.intake import vocabulary as _vocab
    return _vocab.to_display(getattr(_vocab, kind), key)


def _vocab_display(engine_or_key: str) -> str:
    from meshpipeline.agents.intake import vocabulary as _vocab
    return _vocab.to_display(_vocab.ENGINE, engine_or_key)


__all__ = ["ANALYSIS", "ANSWER", "CONFIRMED", "FLOW_QUESTION", "PLAN", "PURPOSE", "PURPOSE_CLARIFY",
           "READING", "ROUTE_CFD", "ROUTE_FEA", "STAGE", "STANDARD", "STARTED", "UNIT", "ScriptedFeaTurn",
           "analysis_said", "asks_something", "cfd_plan_due", "closed_with", "fea_part_words", "fea_plan_due",
           "fea_submission", "is_flow_question", "latest_declaration", "latest_user", "plain_step_answer",
           "plan_line", "read_messages", "return_line", "route_of", "step_of", "steps"]
