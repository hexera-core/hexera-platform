# Responsibility: Execute one intake tool call and track what the conversation has established.
# Boundaries: intake tools read and propose; none of them starts a run.
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.intake import admission_token as at
from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import engine_selection as es
from meshpipeline.agents.intake import measured_choice
from meshpipeline.agents.intake import recommendation as rec
from meshpipeline.agents.intake import vocabulary as _vocab
from meshpipeline.agents.intake.validation import preview_admission, validate_submission
from meshpipeline.contracts import rationale as _R
from meshpipeline.contracts import user_notes as un

logger = logging.getLogger(__name__)

INTAKE_CATEGORIES = {
    "web_search": "research",
    "recommend_compatible_engines": "recommendation",
    "propose_engine_selection": "selection",
    "confirm_engine_selection": "selection",
    "preview_selected_admission": "admission",
    "submit_requirements": "submission",
}

# Tools that can change authorization-relevant state. A call from this set AFTER an approval has
# been established may invalidate it; anything else is read-only for authorization purposes.
MUTATING_TOOLS = frozenset({
    "propose_engine_selection", "confirm_engine_selection",
    "preview_selected_admission", "submit_requirements",
})

MAX_SEARCH_CALLS = 2   # web_search budget per intake turn (RAG was removed)

# What the model is told with an impossible verdict. The finding used to END the turn as the
# user's reply, which left them holding a sentence about what the engine cannot do and no move
# to make - when the conflicting value was often the model's own (a wall patch the user never
# named). The finding goes back to the model instead, with what would pass: the model repairs
# what was its own and puts to the user, with a proposed answer, only what was theirs.
REFUSAL_GUIDANCE = (
    "This setup cannot be meshed as declared. NOTHING was recorded and no token was issued; every "
    "value the user declared is preserved. Do not end the turn on this finding - act on it. First "
    "decide WHOSE value conflicts. If it is yours - a patch the user never named, a role or "
    "dimensionality you assigned, a boundary that is not a surface of the uploaded part (a "
    "ground, a floor, a cut) - repair it now and call preview_selected_admission again in this "
    "same turn with the corrected payload, then tell the user in one sentence what you changed "
    "and why. If the USER declared it, do not change it: tell them plainly what the engine cannot "
    "do and what that means for them, propose the ONE revision that would pass (see "
    "what_would_pass) and ask whether to apply it, so that 'ok' is a complete answer. Name no "
    "engine other than the selected one; if they want alternatives they will ask."
)


#: Said under the summary when the user's own notes were captured with it: they are kept as typed.
NOTES_KEPT = ("Your notes are kept word for word, and every step that builds the mesh reads them "
              "as guidance.")


#: The cell-limit question's opening, as conversations from before its removal carry it (Areen, 14 Nov: the cell
#: count is the user's to state, never ours to ask) - its two buttons are still read as clicks, never as notes.
_RETIRED_CELL_QUESTION = "Full detail needs about "


def _offered_choices(reply: str) -> list[dict]:
    """The buttons the application offered under one of its messages (application/purpose_map.choices_for), so a
    click on one is never kept as the user's note (contracts/user_notes.capture)."""
    from meshpipeline.application.purpose_map import choices_for
    text = str(reply or "").strip()
    if text.startswith(_RETIRED_CELL_QUESTION) and " Keep " in text:
        limit = text.split(" Keep ", 1)[1].split(" (", 1)[0].strip()
        return [{"label": f"Keep {limit}", "send": f"Keep {limit}"},
                {"label": "Try full detail", "send": "Try full detail"}]
    return choices_for(text)


def _same_submission(a, b) -> bool:
    """The same submission: every value equal, and the same notes in the same words (when a note
    was captured is not part of what was submitted)."""
    def _plain(p):
        p = dict(p or {})
        p["user_notes"] = [n.get("text") for n in p.get("user_notes") or [] if isinstance(n, dict)]
        return p
    return _plain(a) == _plain(b)


def category_of(tool: str) -> str:
    return INTAKE_CATEGORIES.get(tool, "unknown")


def fea_path(st) -> bool:
    """Whether the session is an FEA part: the declared case is the structural purpose (the FEA
    route of the purpose mapping, confirmed on the FEA stage) on a solid body - or, before any
    stage was confirmed, the user's own words take it to the FEA path (application/purpose_map).
    The engine is never a question there, whatever the file's format or whether the stage held.
    The LATEST word on the analysis wins: a user who confirmed a flow on the stage and then asks for
    a stress analysis of the same part is on the FEA path (current_purpose), and the other way round."""
    case = tuple(getattr(st, "declared_case", None) or ())
    if case:
        now = current_purpose(st)
        if now and now != case[0] and (now == "structural") != (case[0] == "structural"):
            return now == "structural"
        return case[0] == "structural" and (len(case) < 2 or case[1] in ("solid-body", ""))
    from meshpipeline.application import purpose_map as pm

    said = [{"role": "user", "content": str(t)} for t in getattr(st, "user_texts", None) or ()]
    return bool(said) and pm.reading_for(said).route == pm.ROUTE_FEA


def said_after_stage(conversation) -> list[str] | None:
    """The user's own messages after the latest stage confirmation, oldest first; None when the conversation holds
    no stage confirmation. The composer is closed while the stage is up, so these are what the user wrote after
    they proceeded - where a correction of the stage can only be."""
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK

    msgs = [m for m in (conversation or ()) if isinstance(m, dict)]
    last = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"
                and str(m.get("content") or "").startswith(CONFIRMED_MARK)), default=None)
    if last is None:
        return None
    return [str(m.get("content") or "") for m in msgs[last + 1:]
            if m.get("role") == "user" and str(m.get("content") or "").strip()]


#: The purpose of a flow whose words say only "a flow" (neither through nor around it): not a PURPOSES key, so no
#: engine is checked against it - it only tells a flow from the FEA path.
SOME_FLOW = "flow"


def _flow_purpose(topology: str) -> str:
    return {"internal": "internal_cfd", "external": "external_cfd"}.get(topology, SOME_FLOW)


def current_purpose(st) -> str:
    """WHAT THE USER WANTS THE MESH FOR NOW: a purpose key, SOME_FLOW, or "" when nothing says. The latest word
    on it wins: the case the user confirmed on the stage, unless a later message of theirs NAMES the other path
    outright (purpose_map.named_analysis) - "now a static stress analysis of the same part" after a flow was
    confirmed is structural; "refine the bend" or "it sits in water" names no analysis and changes nothing.
    Words on the stage's side of the FEA line leave the stage's purpose standing. With no stage standing, all the
    user's words read together, as the stage reads them."""
    from meshpipeline.application import purpose_map as pm

    case = tuple(getattr(st, "declared_case", None) or ())
    if case:
        for text in reversed(said_after_stage(getattr(st, "conversation", ())) or []):
            reading = pm.named_analysis(text)
            if not reading.settled:
                continue
            if (reading.route == pm.ROUTE_FEA) == (case[0] == "structural"):
                return case[0]
            return "structural" if reading.route == pm.ROUTE_FEA else _flow_purpose(reading.topology)
        return case[0]
    said = [{"role": "user", "content": str(t)} for t in getattr(st, "user_texts", None) or ()]
    if not said:
        return ""
    reading = pm.reading_for(said)
    if reading.route == pm.ROUTE_FEA:
        return "structural"
    return _flow_purpose(reading.topology) if reading.settled else ""


def case_now(st) -> tuple[str, str] | None:
    """(purpose, input kind) of the case the user wants meshed now: the stage's while it is the latest word on the
    analysis, the part itself for a structural request made since, (purpose, "") for a flow named since; None when
    nothing says (current_purpose)."""
    case = tuple(getattr(st, "declared_case", None) or ())
    now = current_purpose(st)
    if case and now == case[0]:
        return case[0], (case[1] if len(case) > 1 else "")
    if now == "structural":
        return "structural", "solid-body"
    from meshpipeline.engines.purposes import PURPOSES

    if now in PURPOSES:
        return now, ""
    return (case[0], case[1]) if case and not now else None


#: How the APPLICATION chose an engine, with no word of the user's: the best fit, the engine that reads the file, the
#: FEA path's mesher. Such a choice is reconsidered when the case changes; a choice the user typed is not (it stays
#: theirs unless it cannot mesh the case at all).
AUTOMATIC_VIAS = (es.VIA_AUTO, es.VIA_THE_FILE, es.VIA_FEA_PATH)


def _cannot_mesh(engine: str, case, form: str) -> bool:
    """Whether `engine` cannot mesh the case at all (the admission's impossible verdict); False when the case is
    not known well enough to say."""
    if not engine or not case or not case[0]:
        return False
    return preview_admission(engine, case[0], case[1], dimensionality="3D",
                             geometry_form=form).get("verdict") == "impossible"


def fea_part_kind(args):
    """A STRUCTURAL request's input kind is the part itself ("solid-body"), whatever the model wrote.
    The FEA path meshes the uploaded part as it is - a solid, or a surface file the shell route reads
    (engines/surface_run) - and no engine meshes a "body-surface" for structural work, so that word
    only ever refused a valid request. The model chose it now and then for a surface STEP (dev-areen,
    12 Nov 2026: one turn repaired itself, the next told the user "requires a Solid body or Planar
    domain"). A flat 2D "planar-domain" is a real structural input and is kept.
    The same held for "solid-assembly": the FEA path meshes a part of several bodies as it is (bonded or in
    contact, as the stage confirmed), and the only engine that takes a solid assembly makes a multi-region
    fluid+solid mesh, never a structural one. The model wrote it for a 5-body knee implant (dev-areen,
    13 Nov 2026) and the gate answered that the mesher "cannot mesh a Solid assembly for FEA". So the rule
    is general: a structural request keeps only "solid-body" and "planar-domain" (STRUCTURAL_KEPT)."""
    if (isinstance(args, dict) and str(args.get("purpose") or "") == "structural"
            and str(args.get("input_kind") or "") not in STRUCTURAL_KEPT):
        return {**args, "input_kind": "solid-body"}
    return args


#: The only input kinds a structural request keeps as written (fea_part_kind). EVERY other word the model
#: may choose - "body-surface", "solid-assembly", "fluid-domain", or one added later - is read as the part
#: itself: no engine meshes them for structural work, so each one only ever refused a valid FEA request,
#: and the next variant must not need its own fix. "" stays "" (not yet declared).
STRUCTURAL_KEPT = ("solid-body", "planar-domain", "")


#: The tool arguments that name the engine a payload is for (the preview and the submission).
_PAYLOAD_ENGINE_ARGS = ("selected_engine", "mesh_engine")


def _payload_engine(args: dict) -> str:
    for name in _PAYLOAD_ENGINE_ARGS:
        eng = str(args.get(name) or "").strip().lower()
        if eng:
            return eng
    return ""


def stage_part_kind(args, declared_case):
    """THE INPUT KIND THE USER CONFIRMED ON THE STAGE, over any other word the model wrote for it.
    (args, note): the arguments, and a line for the model when the kind was replaced.

    The stage's sentence says what the file is ("input_kind fluid-domain; the fluid flows through
    it") and the user clicked it; declared_case reads it back, and is None once the user restated
    what the file is or how the fluid moves in the chat. While it stands and the payload has the
    stage's purpose, a different kind is the model's word, not the user's, and the stage wins
    ALWAYS. Where the gate refused the model's word it refused a valid run (snappyHexMesh or cfMesh
    with "fluid-domain" for a body the air flows around, Gmsh with "solid-body" for a CAD solid of
    the fluid itself, the multi-region meshers with "solid-body" for a confirmed assembly). Where
    the gate accepted it, it was worse: Gmsh took "body-surface" for a confirmed fluid domain and
    would build a far-field box around the fluid box, and "fluid-domain" for a confirmed part and
    would mesh the metal as the fluid - with nothing said. A kind the engine cannot take as
    confirmed is still refused, with its real reason. A different purpose is never touched here: a
    change of analysis may be said in words the restate check does not catch."""
    if not isinstance(args, dict) or not declared_case:
        return args, ""
    from meshpipeline.engines.purposes import INPUT_KINDS, PURPOSES

    d_purpose, d_kind = (tuple(declared_case) + ("", ""))[:2]
    purpose = str(args.get("purpose") or "").strip()
    kind = str(args.get("input_kind") or "").strip()
    if (not kind or kind == d_kind or purpose != d_purpose or purpose not in PURPOSES
            or d_kind not in INPUT_KINDS):
        return args, ""
    if purpose == "structural" and kind in STRUCTURAL_KEPT:
        # A FLAT 2D PART (plane stress or strain) is a real structural input the FEA stage's sentence
        # cannot say - it always says the solid part - so it is kept, as fea_part_kind keeps it.
        return args, ""
    note = (f"input_kind is '{d_kind}', as the user confirmed on the geometry stage - the payload said "
            f"'{kind}', which the user never said. The stage's kind is what is meshed; keep it as the "
            "stage has it, and do not mention the change unless the user asks. Only if the USER said "
            f"otherwise in the chat after the stage, call again with input_kind '{kind}' and "
            "input_kind_user_words set to their words, copied exactly.")
    return {**args, "input_kind": d_kind}, note


#: The tool argument that carries the user's own words for an input kind other than the stage's (KIND-1).
KIND_WORDS_ARG = "input_kind_user_words"


def _plain_words(text) -> str:
    t = str(text or "").replace("’", "'").replace("‘", "'").casefold()
    return " ".join(t.split()).strip(" \"'.,;:!?")


def kind_said_by_user(args, declared_case, quote, said_after: list[str] | None) -> bool:
    """KIND-1: THE USER'S OWN CORRECTION OF THE STAGE WINS. The stage's input kind stands over the model's word
    (stage_part_kind) - but not over the user's: after proceeding, a user may write "the upload already includes
    the air box; mesh its contents directly", and the model that reads it submits "fluid-domain". That kind is
    kept when the model quotes the user's words for it (`quote`, the KIND_WORDS_ARG) and the words are in a
    message the user wrote AFTER the stage (said_after_stage) - a correction can be said in any words, so no word
    list decides it, and words from before the stage, or words the user never wrote, keep the stage's kind.
    Review on #465: the correction was replaced by the stage's kind and a second far-field box was built."""
    if not isinstance(args, dict) or not declared_case or not said_after:
        return False
    from meshpipeline.engines.purposes import INPUT_KINDS

    d_purpose, d_kind = (tuple(declared_case) + ("", ""))[:2]
    kind = str(args.get("input_kind") or "").strip()
    if str(args.get("purpose") or "").strip() != d_purpose or not kind or kind == d_kind or kind not in INPUT_KINDS:
        return False
    q = _plain_words(quote)
    if len(q) < 8 or len(q.split()) < 2:
        return False
    return any(q in _plain_words(text) for text in said_after)


#: Words a model writes for an engine parameter's declared values (EngineSpec.intake_params), folded
#: (lower case, letters and digits only). The parameter's own spelling is always accepted too.
_PARAM_WORDS: dict[str, tuple[str, ...]] = {
    "1": ("first", "firstorder", "1st", "linear", "tet4", "c3d4"),
    "2": ("second", "secondorder", "2nd", "quadratic", "tet10", "c3d10"),
    "on": ("yes", "true", "enabled", "enable"),
    "off": ("no", "false", "none", "disabled", "disable"),
    "internal": ("internalflow", "internalcfd", "through"),
    "external": ("externalflow", "externalcfd", "around"),
}


def _fold(value) -> str:
    return "".join(c for c in str(value).lower() if c.isalnum())


def _param_value(value, allowed: tuple):
    if isinstance(value, bool):
        words = ("on", "yes", "true") if value else ("off", "no", "false")
        return next((a for a in allowed if a in words), value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    folded = _fold(value)
    for a in allowed:
        if folded == _fold(a) or folded in _PARAM_WORDS.get(a, ()):
            return a
    return value


def engine_param_words(args):
    """THE ENGINE'S OWN WORD for a parameter value the model wrote another way: `true` for "on",
    `2.0` or "quadratic" for element order "2", "internal flow" for "internal". Each is a value the
    engine declares, so a different spelling only ever held the run back ("param must be one of
    ['on', 'off'], got 'true'"). A value that means none of them is left as written, for the gate
    to refuse; an unknown parameter is not touched."""
    if not isinstance(args, dict) or not isinstance(args.get("engine_params"), dict):
        return args
    from meshpipeline.engines.registry import engine_names, get_spec

    eng = _payload_engine(args)
    if eng not in engine_names():
        return args
    specs = {p.key: p for p in get_spec(eng).intake_params}
    params = args["engine_params"]
    fixed = {k: (_param_value(v, tuple(specs[k].values)) if k in specs else v)
             for k, v in params.items()}
    return args if fixed == params else {**args, "engine_params": fixed}


def _normalize_declared_patches(args: dict):
    """The declared patches as every later layer will read them. The PURPOSE decides which roles a
    word may mean and whether a ground plane exists (only an external flow has a far-field box
    with a floor); an unknown purpose applies the name rules alone and leaves types to the
    validator."""
    from meshpipeline.contracts.patch_names import normalize_declaration
    from meshpipeline.engines.ground_plane import ground_rule
    from meshpipeline.engines.purposes import PURPOSES, topology_of

    purpose = str(args.get("purpose") or "").strip()
    known = purpose in PURPOSES
    return normalize_declaration(
        args.get("patches"),
        allowed_roles=PURPOSES[purpose].boundary_roles if known else None,
        ground=ground_rule() if known and topology_of(purpose) == "external" else None)


@dataclass(frozen=True)
class IntakeToolResult:

    tool: str
    content: str = ""
    accepted: bool = False          # a well-formed call the executor actually ran
    malformed: bool = False         # arguments did not parse - nothing was dispatched
    advanced: bool = False          # application state genuinely improved
    signature: str = ""             # stable identity of the state this call produced


@dataclass
class IntakeExecutionState:

    session_id: str = ""
    owner_id: str = ""
    revision: str = ""
    user_msg_count: int = 0
    latest_user_msg: str = ""
    #: Every message the user wrote in this conversation, oldest first (the application's synthetic
    #: nudges excluded) - where an engine they chose before this turn is found.
    user_texts: tuple = ()
    #: (purpose, input kind) the user confirmed on the geometry stage, or None: what an engine the
    #: model proposes on its own must be able to mesh.
    declared_case: tuple | None = None
    #: The conversation as the user and the assistant said it, oldest first (the application's
    #: synthetic nudges excluded) - where the notes question and the user's own words are found.
    conversation: tuple = ()
    #: Whether that confirmation is a whole thermal model's (an ECXML upload confirmed on its
    #: panel): its engine is the one that reads the file, selected without a question, and every
    #: reply is held to the thermal vocabulary (application/thermal_wording).
    thermal: bool = False
    # The approved SOURCE, not a file. Intake binds which bytes were approved; it never
    # opens them, so it holds the reference and no path.
    source_ref: object | None = None
    rec_authorized: bool = False
    #: THE MEASURED RECOMMENDATION for the uploaded geometry (engines/fitness.Recommendation),
    #: or None when there is nothing to rank yet. The engine question proposes ITS engine.
    recommendation: Any = None

    pending: dict | None = None       # the issued admission token record
    selection: dict | None = None
    approval: dict | None = None

    # TURN-SCOPED, never round-scoped. Once a recommendation happens in this invocation, nothing
    # may escalate to selection or submission for the REST of the invocation - not merely for the
    # rest of the round. A comparison the user asked for can never become a choice they did not
    # make, however many provider rounds the model takes to get there.
    recommended_this_turn: bool = False

    #: The standing admission refusal: the impossible verdict of the LATEST preview this turn, or
    #: None once a later preview passed or found only gaps. The model holds it as a tool result
    #: and is expected to act on it; the turn's reply is checked against it after the loop
    #: (agent.py), and a proposal of an engine the user did not name is refused while it stands.
    admission_refusal: dict | None = None
    # Application-rendered terminals, resolved after the whole ordered batch.
    selection_prompt: str | None = None
    #: THE NOTES QUESTION (contracts/user_notes, NOTE-1): asked once, by the application, in place
    #: of the first summary when the user has not said anything specific yet.
    notes_prompt: str | None = None
    submit_summary: str | None = None
    submit_args: dict | None = None
    #: the user's own words captured with the submission - verbatim, never the model's rewrite
    user_notes: list = field(default_factory=list)
    #: THE TURN ONLY ANSWERS: the user asked a question about the run that has ended, so no new
    #: submission is taken in it (agents/intake/chat_facts.asks_about_last_run)
    answer_only: bool = False

    search_calls_used: int = 0
    search_events: list = field(default_factory=list)
    submit_attempts: int = 0
    submit_rejections: int = 0
    #: a shell run's summary that promised tetrahedra was sent back once for a rewrite (never twice)
    shell_words_sent_back: bool = False
    val_errors_seen: list[str] = field(default_factory=list)
    tools_invoked: list[str] = field(default_factory=list)

    def authorization_signature(self) -> str:
        pending = self.pending or {}
        return "|".join([
            str((self.selection or {}).get("id") or ""),
            str((self.selection or {}).get("confirmed") or ""),
            str(pending.get("fingerprint") or ""),      # the payload, not the token for it
            str(pending.get("revision") or ""),
            str(pending.get("verdict") or ""),
            str((self.approval or {}).get("id") or ""),
            str((self.approval or {}).get("status") or ""),
        ])

    def invalidate_approval(self, why: str) -> None:
        self.approval = ap.invalidate(self.approval, why)
        # An approval that no longer stands cannot render a submission confirmation.
        self.submit_summary = None
        self.submit_args = None


class IntakeToolExecutor:

    def __init__(self, *, state: IntakeExecutionState, job_id: str,
                 implemented_engines: list[str], search_tool: Any,
                 trace: Any = None) -> None:
        self.state = state
        self._job_id = job_id
        self._engines = implemented_engines
        self._search_tool = search_tool
        # the PUBLIC trace sink for this turn. Rationale is published from real
        # gate outcomes below; None simply means nothing is listening.
        self._trace = trace
        # a line for the model about the engine, put in front of the call's result (run)
        self._engine_note = ""

    def _choice_is_stale(self, sel: dict, case, form: str) -> bool:
        """Whether an engine the APPLICATION chose was chosen for another case than the one the user wants now:
        the analysis crossed the FEA line, the case it was chosen for is not this one (the user corrected the
        stage, or asked for another analysis), or it cannot mesh this case at all. A selection stored before the
        case was kept with it is judged by the last alone."""
        now = current_purpose(self.state)
        was = str(sel.get("for_purpose") or "")
        if was and now and (was == "structural") != (now == "structural"):
            return True
        if "for_case" in sel and tuple(sel.get("for_case") or ()) != tuple(case or ()):
            return True
        return _cannot_mesh(str(sel.get("engine") or ""), case, form)

    def _analysis_changed_since_naming(self, engine: str) -> bool:
        """Whether the user wants another case now than when they last named `engine` in their own words: the
        purpose then (current_purpose over the conversation up to that message) is not the purpose now. False
        when the naming is not found in the conversation - the user's word then stands as it always did."""
        from types import SimpleNamespace

        from meshpipeline.application.geometry_confirmation import declared_case

        conv = [m for m in (self.state.conversation or ()) if isinstance(m, dict)]
        at = max((i for i, m in enumerate(conv) if m.get("role") == "user"
                  and engine in _vocab.engines_named_in(str(m.get("content") or ""), "")), default=None)
        if at is None:
            return False
        then_conv = conv[:at + 1]
        then = current_purpose(SimpleNamespace(
            declared_case=declared_case(then_conv), conversation=tuple(then_conv),
            user_texts=tuple(str(m.get("content") or "") for m in then_conv if m.get("role") == "user")))
        return then != current_purpose(self.state)


    def _geometry_facts(self) -> dict:
        # What the uploaded CAD actually distinguishes, read from the staged file beside the
        # session. Without it a refusal can only describe the engine, so a user whose export named
        # nothing is told the system cannot do what they asked - when the missing part is theirs.
        # Never fatal and never blocking: no file, or a file this cannot describe, simply says
        # nothing and the verdict is what it always was.
        try:
            from meshpipeline.cad.regions import regions_for_file

            return regions_for_file(self._staged_upload()).as_facts()
        except Exception as exc:
            logger.debug("Intake: geometry regions unavailable (%s)", exc)
            return {}

    def _measured_setup_errors(self, args: dict) -> list[str]:
        # WHAT THE MEASURED PART CANNOT CARRY, said before the summary is put to the user: a
        # symmetry plane on a whole body, the ground taken for a half model's cut, a reference
        # length a thousand times off (agents/intake/measured_setup). The check's stored facts and
        # the confirmed form are the evidence; unreadable, nothing is said and nothing is refused.
        sid = str(self.state.session_id or "")
        if not sid:
            return []
        try:
            import json

            from meshpipeline.agents.intake.measured_setup import setup_errors
            from meshpipeline.application.geometry_check import check_object_key, read_check
            from meshpipeline.contracts.object_storage import get_object_store

            facts = (read_check(sid) or {}).get("facts") or {}
            try:
                confirmed = json.loads(get_object_store().get_bytes(
                    object_key=check_object_key(sid, "confirmed.json")))
            except Exception:  # noqa: BLE001 - nothing confirmed: no size to judge a length by
                confirmed = {}
            return setup_errors(args, facts, confirmed if isinstance(confirmed, dict) else {})
        except Exception as exc:  # noqa: BLE001 - a check we cannot read refuses nothing
            logger.debug("Intake: measured setup check unavailable (%s)", exc)
            return []

    def _confirmed_up_axis(self) -> str:
        # WHICH WAY IS UP, as the user confirmed it on the picture (INS-9): the ground lies under
        # it, so a flow along z is along the ground of a Y-up file. Unreadable: +z, as before.
        try:
            from meshpipeline.application.geometry_confirmation import confirmed_up_axis

            return confirmed_up_axis(str(self.state.session_id or ""))
        except Exception:  # noqa: BLE001 - unknown is +z up, never a refusal
            return ""

    def _staged_upload(self) -> str:
        # The session's upload as a local file (staged, or fetched back from its stored source), or ''.
        return measured_choice.staged_upload(str(self.state.session_id or ""),
                                             self.state.source_ref)

    def _geometry_form(self) -> str:
        # WHAT KIND OF FILE the user uploaded - a CAD solid or a surface mesh - asked of the one
        # reading (measured_choice.upload_form: the staged file first, then the source's name).
        # Every engine check below asks the engine whether it takes THIS file for the flow; ''
        # (no upload, or a name that says nothing) claims nothing, and nothing is refused on it.
        return measured_choice.upload_form(self._staged_upload(), self.state.source_ref)

    def _source_patches(self, engine: str) -> tuple[str, ...]:
        # THE FILE'S OWN PATCHES, for an engine that writes them under the file's names
        # (EngineSpec.patch_names_from_source): read from the confirmed geometry, so a rename or a
        # merge is refused at the preview - before the summary is approved - never by the patch
        # contract after a full mesh. Only read when the engine needs them.
        try:
            from meshpipeline.engines.registry import get_spec
            if not get_spec(engine).patch_names_from_source:
                return ()
            from meshpipeline.application.geometry_confirmation import confirmed_opening_names
            return confirmed_opening_names(str(self.state.session_id or ""))
        except Exception as exc:  # noqa: BLE001 - unknown claims nothing
            logger.debug("Intake: confirmed patches unavailable (%s)", exc)
            return ()

    async def run(self, tool: str, args: dict | None) -> IntakeToolResult:
        st = self.state
        st.tools_invoked.append(tool)
        if args is None:
            # MALFORMED. Nothing is dispatched and nothing is inferred: a tool that never ran
            # produced no result, changed no requirement, and authorized nothing. The old loop
            # turned this into `{}` and ran the handler anyway, which let a submission be
            # attempted with every field silently absent.
            return IntakeToolResult(
                tool=tool, malformed=True, accepted=False,
                content=(f"[SYSTEM] The arguments for {tool} were not valid JSON, so NOTHING was "
                         "executed and no value was recorded. Call the tool again with well-formed "
                         "JSON arguments. Do not assume anything was saved."))

        # THE VOCABULARY BOUNDARY. Intake speaks display names and is never shown an internal key
        # (see agents/intake/vocabulary.py); everything past this line routes on keys, as it always
        # has. Converting here - once, before any handler, validator or store sees the arguments -
        # is what keeps that split from leaking into the rest of the system.
        from meshpipeline.agents.intake.vocabulary import normalize_tool_args
        args = normalize_tool_args(args)
        args = fea_part_kind(args)
        # THE STAGE'S KIND and THE ENGINE'S OWN PARAMETER WORDS, at the same boundary: the preview
        # and the submission read the same arguments, so the token bound to one matches the other.
        _kind_note = ""
        _kind_words = ""
        if isinstance(args, dict) and KIND_WORDS_ARG in args:
            # the user's words for the kind are the executor's to check; no handler, token or payload sees them
            _kind_words = str(args.get(KIND_WORDS_ARG) or "")
            args = {k: v for k, v in args.items() if k != KIND_WORDS_ARG}
        if tool in ("preview_selected_admission", "submit_requirements"):
            args = engine_param_words(args)
            if st.declared_case and kind_said_by_user(args, st.declared_case, _kind_words,
                                                     said_after_stage(st.conversation)):
                # KIND-1: the user corrected the stage in their own words - their kind is the case from here on in
                # this turn (the submission after this preview reads it too, and an engine is judged by it)
                logger.info("Intake: input_kind %s kept over the stage's %s - the user's own words - job_id=%s",
                            args.get("input_kind"), st.declared_case[1], self._job_id)
                st.declared_case = (st.declared_case[0], str(args.get("input_kind")))
                _kind_note = (f"input_kind '{args.get('input_kind')}' is kept: the user corrected the geometry "
                              "stage in their own words. Use it in submit_requirements too.")
            elif st.declared_case:
                args, _kind_note = stage_part_kind(args, st.declared_case)
        # THE DECLARED PATCHES, at the same boundary and for the same reason: a name a person typed
        # ("car wall") becomes the spelling a mesher can write ("car_wall"), a name a mesher keeps
        # for itself ("outer") becomes the user's own, a boundary word becomes the role it means
        # ("velocity inlet" -> inlet), and in an external flow a wall called "ground plane" or
        # "floor" becomes the ground the domain lays - before the preview, the preview token, the
        # approved snapshot, the builder payload and the manifest check see any of it, so none of
        # them can disagree. The model is told every change in the tool result.
        _changes = None
        if isinstance(args, dict) and "patches" in args:
            _changes = _normalize_declared_patches(args)
            args = {**args, "patches": _changes.patches}

        before = st.authorization_signature()
        handler = getattr(self, f"_do_{tool}", None)
        if handler is None:
            logger.warning("Intake: unknown tool %r - job_id=%s", tool, self._job_id)
            return IntakeToolResult(
                tool=tool, accepted=False,
                content=f"Unknown tool {tool!r}. Available: {', '.join(INTAKE_CATEGORIES)}.")
        result = await handler(args)
        after = st.authorization_signature()
        content = result.content
        if _changes is not None and _changes.changed:
            from meshpipeline.contracts.patch_names import declaration_note
            _note = declaration_note(_changes)
            content = f"{_note}\n\n{content}" if content else _note
        if _kind_note:
            content = f"{_kind_note}\n\n{content}" if content else _kind_note
        if self._engine_note:
            content = f"{self._engine_note}\n\n{content}" if content else self._engine_note
            self._engine_note = ""
        return IntakeToolResult(
            tool=result.tool, content=content, accepted=result.accepted,
            advanced=result.advanced or (after != before), signature=after)

    # the seven tools
    async def _do_web_search(self, args: dict) -> IntakeToolResult:
        import asyncio
        st = self.state
        st.search_calls_used += 1
        if st.search_calls_used > MAX_SEARCH_CALLS:
            return IntakeToolResult(tool="web_search", accepted=False,
                                    content="web_search limit reached for this turn.")
        text = await asyncio.to_thread(self._search_tool, "web_search", args, st.search_events)
        logger.info("Intake: web_search - query=%s result_len=%d - job_id=%s",
                    (args.get("query", "") or args.get("intent", ""))[:80], len(text), self._job_id)
        # Research informs the model; it changes no requirement and is never progress.
        return IntakeToolResult(tool="web_search", accepted=True, content=text)

    async def _do_recommend_compatible_engines(self, args: dict) -> IntakeToolResult:
        st = self.state
        recs = rec.recommend_compatible_engines(
            args.get("purpose", ""), args.get("input_kind", ""),
            dimensionality=args.get("dimensionality"), patches=args.get("patches"),
            engine_params=args.get("engine_params"), authorized=st.rec_authorized,
            geometry_form=self._geometry_form())
        if st.rec_authorized and st.recommendation is not None:
            # THE COMPARISON THE USER ASKED FOR CARRIES THE MEASURED RANKING: every engine that can
            # take this file, best lab record on shapes like it first, with its plain reason.
            recs["measured_ranking"] = measured_choice.ranking_rows(st.recommendation)
        if st.rec_authorized:
            # TURN-SCOPED latch. Set once, cleared only by a new Intake invocation.
            st.recommended_this_turn = True
        logger.info("Intake: recommend_compatible_engines(%s,%s) authorized=%s -> %s - job_id=%s",
                    args.get("purpose"), args.get("input_kind"), st.rec_authorized,
                    recs.get("compatible_engines"), self._job_id)
        return IntakeToolResult(tool="recommend_compatible_engines", accepted=True,
                                content=json.dumps(recs))

    async def _do_propose_engine_selection(self, args: dict) -> IntakeToolResult:
        st = self.state
        eng = str(args.get("engine") or "").strip().lower()
        if st.recommended_this_turn:
            return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                "Not allowed in this turn: you compared engines for the user. A recommendation is "
                "not a selection - ask which engine they want and wait for their next message."))
        if eng not in self._engines:
            return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                f"{eng!r} is not a registered engine. Available: {', '.join(self._engines)}."))
        sel = st.selection or {}
        if (es.state_of(sel) == es.CONFIRMED and sel.get("engine") == eng
                and sel.get("session") == st.session_id and sel.get("owner") == st.owner_id):
            # THE ENGINE IS ASKED ONCE. The user already confirmed this very engine - by naming
            # it or by saying yes to the one question - so proposing it again is a question with
            # one answer. It changes nothing: the selection, its preview and any summary stand.
            return IntakeToolResult(tool="propose_engine_selection", accepted=True, content=(
                f"{_vocab.to_display(_vocab.ENGINE, eng)} is already selected - the user confirmed "
                "it. Do not ask about it or mention it again; carry on with the next thing you "
                "need."))
        quote = str(args.get("user_named_verbatim") or "")
        named_now = es.user_named_engine(eng, quote, st.latest_user_msg)
        asked = eng
        case = case_now(st)
        form = self._geometry_form() if case else ""
        ours = (es.state_of(sel) == es.CONFIRMED and sel.get("session") == st.session_id
                and sel.get("owner") == st.owner_id)
        # THE APPLICATION'S CHOICE FOLLOWS THE CASE (review on #465). An engine the application chose itself was
        # chosen for the case of its day; once the user asks for another analysis of the part ("now a stress
        # analysis of it" after a flow) or corrects what the file is, it is chosen again - it never holds the user
        # to an engine they did not pick, which only the name of another one could undo.
        stale = ours and sel.get("via") in AUTOMATIC_VIAS and self._choice_is_stale(sel, case, form)
        if stale:
            logger.info("Intake: the application's %s was chosen for another case (%s, now %s) - chosen again - "
                        "job_id=%s", sel.get("engine"), sel.get("for_case"), case, self._job_id)
        if st.admission_refusal is not None and not named_now and not stale:
            # A REFUSAL IS NOT ROUTED AROUND. With the finding in hand, the model may not switch
            # the user to another engine on its own: the application's "I'd mesh this with X"
            # question would name a replacement the user never asked about, in the very turn
            # that refused their setup. Only an engine the user named themselves goes forward.
            shown = _vocab.to_display(_vocab.ENGINE, eng)
            return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                f"Not allowed: the selected engine was just refused for this setup and the user "
                f"has not named {shown} themselves. Do not switch engines for them - repair a "
                "value that was your own and check again, or put the finding to the user with "
                "the one revision that would pass; if they want alternatives they will ask."))
        chosen_by_user = named_now or es.user_chose(eng, st.user_texts)
        if not chosen_by_user:
            # THE USER'S OWN LAST WORD ON THE ENGINE WINS over the model's proposal: with no question
            # asked any more, a model that proposes snappyHexMesh after "actually, cfMesh" would
            # otherwise select it over the user's words
            theirs = next((e for e in self._engines if e != eng and es.user_leans_to(e, st.user_texts)), None)
            if theirs is not None:
                logger.info("Intake: the model proposed %s; the user chose %s - job_id=%s",
                            eng, theirs, self._job_id)
                eng, chosen_by_user = theirs, True
        if (chosen_by_user and not named_now and _cannot_mesh(eng, case, form)
                and self._analysis_changed_since_naming(eng)):
            # THE USER'S ENGINE STAYS THEIRS - unless it cannot mesh what they ask for SINCE they named it (an
            # engine they typed for a flow, then a stress analysis of the part). Then it is said plainly, and an
            # engine that can is used. Named for this very case, or again now, it goes to the admission, which
            # says why it cannot.
            able = [e for e in self._engines if not _cannot_mesh(e, case, form)]
            pick = asked if asked in able else (able[0] if able else "")
            if pick:
                purpose_words = _vocab.to_display(_vocab.PURPOSE, case[0]) if case else "this analysis"
                self._engine_note = (
                    f"{_vocab.to_display(_vocab.ENGINE, eng)}, the engine the user chose earlier, cannot mesh this "
                    f"part for {purpose_words}, so {_vocab.to_display(_vocab.ENGINE, pick)} is used. Tell the user "
                    "that in one plain sentence.")
                logger.info("Intake: the user's %s cannot mesh %s - %s is used - job_id=%s", eng, case, pick,
                            self._job_id)
                eng, chosen_by_user, stale = pick, False, True
        if (ours and sel.get("engine") != eng and not chosen_by_user and not stale):
            # THE ENGINE IS SETTLED and nobody asked for another: only the user's own words change
            # it ("use cfMesh"), never the model's second thoughts
            shown = _vocab.to_display(_vocab.ENGINE, str(sel.get("engine") or ""))
            return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                f"Not changed: {shown} is the selected engine and the user did not ask for "
                f"{_vocab.to_display(_vocab.ENGINE, eng)}. Keep {shown}; do not mention the "
                "engine. Carry on with the next thing you need."))
        # THE MEASURED RECOMMENDATION IS THE ENGINE PROPOSED. The model presents the system's
        # recommendation, it does not pick its own: when the user named no engine and the
        # geometry was measured and ranked, the question proposes the best-ranked engine, with its
        # evidence, whatever the model suggested. The user's answer still decides.
        recd = st.recommendation
        model_pick = eng
        if (not chosen_by_user and recd is not None and recd.engine
                and recd.engine in self._engines and recd.engine != eng
                and es.state_of(sel) != es.CONFIRMED):
            logger.info("Intake: the model proposed %s; the measured recommendation %s is "
                        "proposed instead - job_id=%s", eng, recd.engine, self._job_id)
            eng = recd.engine
        # THE ENGINE THAT READS THE UPLOAD ITSELF goes first, measured or not (fitness.recommend
        # ranks it so when the geometry was measured; this is the same rule when it was not). An
        # ECXML upload was proposed snappyHexMesh multi-region - a conversion of the model - while
        # the snap-grid mesher, which meshes the model the file describes, went unmentioned
        # (dev-areen set_top_box, 2026-10-11). Read from the engines' own declarations
        # (EngineSpec.reads_source_formats) and only among engines that can take the confirmed case.
        own_reason = ""
        if not chosen_by_user and (es.state_of(sel) != es.CONFIRMED or stale):
            fmt = measured_choice.source_format_of(
                self._staged_upload(), getattr(st.source_ref, "suffix_hint", ""),
                getattr(st.source_ref, "original_filename", ""))
            native = measured_choice.own_format_engines(self._engines, fmt)
            if native and case:
                purpose, kind = case
                native = [e for e in native
                          if preview_admission(e, purpose, kind, dimensionality="3D",
                                               geometry_form=form).get("verdict") != "impossible"]
            if native and eng not in native:
                logger.info("Intake: %s reads this %s upload itself - proposed instead of %s - "
                            "job_id=%s", native[0], fmt, eng, self._job_id)
                eng = native[0]
            if native and eng == native[0]:
                from meshpipeline.contracts.intake_formats import format_for_key
                label = getattr(format_for_key(fmt), "label", "") or fmt.upper()
                own_reason = f"it reads the {label} itself and meshes the model the file describes"
        if not chosen_by_user and case:
            # AN ENGINE THE USER DID NOT NAME MUST BE ABLE TO MESH WHAT THEY CONFIRMED. The model
            # proposed cfMesh for a fluid-volume file on its own; the admission then refused, and
            # its way out was to relabel the confirmed fluid volume as a body surface - a fact the
            # user never said - or to ask a user who had said "your call" which value to revise.
            # The engine is the model's to change, the confirmed geometry is not. The case is the
            # one the user wants now (case_now): the stage's, or the analysis they asked for since.
            purpose, kind = case
            verdict = preview_admission(eng, purpose, kind, dimensionality="3D",
                                        geometry_form=form)
            if verdict.get("verdict") == "impossible":
                able = [e for e in self._engines
                        if preview_admission(e, purpose, kind, dimensionality="3D",
                                             geometry_form=form).get("verdict") != "impossible"]
                shown = _vocab.to_display(_vocab.ENGINE, eng)
                if not able:
                    # NO ENGINE can take this file for this flow: proposing any of them would only
                    # be refused again (the aorta STL was offered VMTK, which cannot take an STL
                    # either). The way on is the file - say so, and propose nothing.
                    return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                        f"Not proposed: {verdict.get('capability_reason') or shown + ' cannot mesh this.'} "
                        "No engine here can mesh what the user confirmed from the file they "
                        "uploaded, so do not propose any engine. Tell the user plainly, in one or "
                        "two sentences, and give them the way on: "
                        + " ".join(verdict.get("what_would_pass") or [])))
                names = ", ".join(_vocab.to_display(_vocab.ENGINE, e) for e in able)
                return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                    f"Not proposed: {shown} cannot mesh what the user "
                    f"{'confirmed on the geometry stage' if tuple(st.declared_case or ()) == case else 'asked for'} "
                    f"({_vocab.to_display(_vocab.INPUT_KIND, kind)}, "
                    f"{_vocab.to_display(_vocab.PURPOSE, purpose)}) from the file they uploaded, "
                    f"and the user did not ask for it. "
                    f"Engines that can: {names}. Propose one of those instead; never ask the user to "
                    "change the geometry they confirmed to suit an engine they did not choose."))
        # A NEW proposal invalidates the previous selection AND any admission preview or pending
        # canonical confirmation that the old selection authorized - including one obtained
        # EARLIER IN THIS SAME provider response.
        st.selection = es.propose(eng, session_id=st.session_id, owner_id=st.owner_id,
                                  revision=st.revision, user_msg_count=st.user_msg_count)
        # the case this engine is chosen for: an automatic choice is chosen again once the case is another
        st.selection["for_case"] = list(case or ())
        st.selection["for_purpose"] = current_purpose(st)
        if recd is not None:
            st.selection["recommendation"] = measured_choice.compact(recd)
        st.pending = None
        st.invalidate_approval("engine selection replaced")
        # A refusal was about the engine the user has just left behind; the reply about the one
        # they named is theirs to read in full.
        st.admission_refusal = None
        # The confirmation question exists to prove the USER chose this engine. If their own words
        # already name it - this message, or the latest earlier one that named an engine - that
        # proof is in hand and asking again is a question with one answer, so the selection is
        # confirmed here instead of costing the user a round-trip. (A refusal still needs the name
        # in THIS message, above: an earlier naming is what the refusal was about.)
        if st.thermal and own_reason and not chosen_by_user:
            # A WHOLE THERMAL MODEL is meshed by the engine that reads its file itself: nothing is
            # asked about it (the chat after "Mesh this model" asks nothing). The job sheet the
            # user approves names it, so that approval is where they say yes to it - or name
            # another engine, which the conversation takes as their choice as always.
            st.selection = {**st.selection, "state": es.CONFIRMED, "via": es.VIA_THE_FILE,
                            "confirmed_revision": st.revision,
                            "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
            logger.info("Intake: engine selection CONFIRMED for a thermal model - %s reads the "
                        "file itself - job_id=%s", eng, self._job_id)
            return IntakeToolResult(
                tool="propose_engine_selection", accepted=True, advanced=True,
                content=(f"{_vocab.to_display(_vocab.ENGINE, eng)} is SELECTED: {own_reason}. "
                         "The job sheet the user approves names it - do not ask about it or "
                         "mention it. Call preview_selected_admission and submit_requirements "
                         "now (engine_source suggested_confirmed)."))
        if fea_path(st) and not chosen_by_user:
            # AN FEA PART (the purpose the user's words mapped to, confirmed on the FEA stage) is
            # meshed on the FEA path: version 1 has one solid mesher and quadratic tetrahedra, so
            # neither the engine nor the element order is a question. The real-path test of
            # 2026-10-22 asked both, with CFD words ("no reliable near-wall layers", "blade-row
            # passages"). The job sheet the user approves names the engine; naming another one in
            # the chat is still their choice, as always.
            st.selection = {**st.selection, "state": es.CONFIRMED, "via": es.VIA_FEA_PATH,
                            "confirmed_revision": st.revision,
                            "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
            st.selection_prompt = None
            logger.info("Intake: engine selection CONFIRMED for an FEA part - %s on the FEA path - "
                        "job_id=%s", eng, self._job_id)
            from meshpipeline.application.shell_check import declared_shell, shell_summary
            if declared_shell(st.conversation):
                shell_said = shell_summary(st.conversation)   # SHSTAGE-8: triangles when the stage settled them
                # SHSTAGE-5 / SURF-6: the stage settled a SHELL - the delivered mesh is plates, never tetrahedra
                return IntakeToolResult(
                    tool="propose_engine_selection", accepted=True, advanced=True,
                    content=(f"{_vocab.to_display(_vocab.ENGINE, eng)} is SELECTED for this FEA part. The stage "
                             f"settled a SHELL: the delivered mesh is {shell_said}. request_txt and "
                             "review_brief_txt say that shell and never a solid or tetrahedral mesh, element "
                             "order or tetrahedra. Do not ask about the engine, the element order, near-wall "
                             "layers or a detail level, and do not mention them. Call preview_selected_admission "
                             "and submit_requirements now (engine_source suggested_confirmed)."))
            return IntakeToolResult(
                tool="propose_engine_selection", accepted=True, advanced=True,
                content=(f"{_vocab.to_display(_vocab.ENGINE, eng)} is SELECTED for this FEA part "
                         "(the FEA path: a solid mesh of quadratic 10-node tetrahedra, element_order "
                         "2, every face named). Do not ask about the engine, the element order, "
                         "near-wall layers or a detail level, and do not mention them. Call "
                         "preview_selected_admission and submit_requirements now (engine_source "
                         "suggested_confirmed)."))
        if chosen_by_user:
            st.selection = {**st.selection, "state": es.CONFIRMED,
                            "confirmed_revision": st.revision,
                            "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
            logger.info("Intake: engine selection CONFIRMED from the user's own words engine=%s "
                        "- job_id=%s", eng, self._job_id)
            return IntakeToolResult(
                tool="propose_engine_selection", accepted=True, advanced=True,
                content=(f"The user named {_vocab.to_display(_vocab.ENGINE, eng)} themselves, so it is SELECTED - "
                         "do not ask them to confirm it. Gather the remaining requirements and call "
                         "preview_selected_admission."))
        # THE ENGINE IS CHOSEN, NOT ASKED (Areen, 2026-11-13; it replaced "recommend, the user
        # picks"): the best engine for this part - the measured recommendation, an engine that reads
        # the upload itself, else the model's capable pick - is selected here, with no question and
        # no lab numbers in the chat. The plan card names it; "use cfMesh" in the user's own words
        # still chooses another (chosen_by_user above).
        st.selection = {**st.selection, "state": es.CONFIRMED, "via": es.VIA_AUTO,
                        "confirmed_revision": st.revision,
                        "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
        st.selection_prompt = None
        logger.info("Intake: engine selection CONFIRMED automatically engine=%s (model said %s) - "
                    "job_id=%s", eng, model_pick, self._job_id)
        shown = _vocab.to_display(_vocab.ENGINE, eng)
        # NO CELL-COUNT QUESTION (Areen, 14 Nov: "the person is specifying how many cells they want - why
        # does the system ask 8 M or 21 M?"). A count the user states is theirs, on the machine it needs
        # (engines/cfd_requirements CFDREQ-3); none stated, the run sizes the mesh itself within the
        # standard machine (engines/gap_sizing) - nothing is asked either way.
        return IntakeToolResult(
            tool="propose_engine_selection", accepted=True, advanced=True,
            content=(f"{shown} is SELECTED for this part (engine_source suggested_confirmed). Do not "
                     "ask about the engine, do not compare engines, and do not mention it or any lab "
                     "evidence - the plan card names it. Gather what is still missing and call "
                     "preview_selected_admission and submit_requirements."))

    async def _do_confirm_engine_selection(self, args: dict) -> IntakeToolResult:
        st = self.state
        quote = str(args.get("user_agreed_verbatim") or "").strip()
        if (es.state_of(st.selection) == es.CONFIRMED and st.selection
                and st.selection.get("confirmed_revision") == st.revision):
            # Already confirmed from this very message - by the user's plain yes, read by the
            # application before the model ran (agent.py), or by their own naming of it. The
            # call changes nothing and is not a fault; refusing it as "already confirmed" sent
            # the model back to ask the user a question they had just answered.
            return IntakeToolResult(tool="confirm_engine_selection", accepted=True,
                                    content=self._confirmed_text(st.selection["engine"]))
        new_sel, reason = es.confirm(
            st.selection, session_id=st.session_id, owner_id=st.owner_id, revision=st.revision,
            quote=quote, latest_user_message=st.latest_user_msg,
            user_msg_count=st.user_msg_count)
        if new_sel is None:
            logger.warning("Intake: engine selection confirm rejected - %s - job_id=%s",
                           reason, self._job_id)
            return IntakeToolResult(tool="confirm_engine_selection", accepted=False,
                                    content=f"Not confirmed: {reason}.")
        st.selection = new_sel
        logger.info("Intake: engine selection CONFIRMED engine=%s - job_id=%s",
                    st.selection["engine"], self._job_id)
        return IntakeToolResult(tool="confirm_engine_selection", accepted=True, advanced=True,
                                content=self._confirmed_text(st.selection["engine"]))

    @staticmethod
    def _confirmed_text(engine: str) -> str:
        return (f"Confirmed: the user selected {_vocab.to_display(_vocab.ENGINE, engine)}. You "
                "may now gather the remaining requirements and call preview_selected_admission.")

    async def _do_preview_selected_admission(self, args: dict) -> IntakeToolResult:
        st = self.state
        eng = str(args.get("selected_engine") or "").strip().lower()
        sel_ok, reason = es.verify_confirmed(st.selection, eng, session_id=st.session_id,
                                             owner_id=st.owner_id)
        pre_confirmed = (st.selection or {}).get("confirmed_revision") != st.revision
        if not sel_ok:
            logger.warning("Intake: selected preview refused - %s - job_id=%s", reason, self._job_id)
            return IntakeToolResult(tool="preview_selected_admission", accepted=False, content=(
                f"Cannot check admission: {reason}. Nothing was checked or saved."))
        if st.recommended_this_turn and not pre_confirmed:
            return IntakeToolResult(tool="preview_selected_admission", accepted=False, content=(
                "Not allowed in this turn: the engine was confirmed during a turn in which you "
                "also compared engines. Ask the user to confirm their choice in their next "
                "message."))
        prev = preview_admission(eng, args.get("purpose", ""), args.get("input_kind", ""),
                                 dimensionality=args.get("dimensionality"),
                                 patches=args.get("patches"),
                                 engine_params=args.get("engine_params"),
                                 geometry_facts=self._geometry_facts(),
                                 geometry_form=self._geometry_form(),
                                 source_patches=self._source_patches(eng))
        logger.info("Intake: preview_selected_admission(%s,%s,%s,patches=%d) -> %s(%s) - job_id=%s",
                    eng, args.get("purpose"), args.get("input_kind"),
                    len(args.get("patches") or []), prev["verdict"],
                    prev.get("blocking_rule_code", ""), self._job_id)
        _R.intake_compatibility(
            self._trace, engine=str(eng), purpose=str(args.get("purpose") or ""),
            input_kind=str(args.get("input_kind") or ""),
            supported=prev["verdict"] == "supported",
            explanation=str(prev.get("safe_user_message") or ""))
        if prev["verdict"] == "impossible" and not st.recommended_this_turn:
            # A RESULT FOR THE MODEL, not the user's reply. The verdict is already on the trace
            # (above) and this result stays in the transcript, so the refusal is on record either
            # way; what changes is that the turn goes on. Any standing authorization is voided
            # all the same, and the reply the model then writes is checked against this record
            # after the loop (agent.py) - one naming another engine is replaced by the rendered
            # finding, exactly as the application's own text used to be delivered.
            st.admission_refusal = prev
            st.pending = None
            st.invalidate_approval("admission became impossible")
            logger.info("Intake: admission refused (%s) - handed back to the model to repair or "
                        "put to the user - job_id=%s", prev.get("blocking_rule_code", ""),
                        self._job_id)
            return IntakeToolResult(
                tool="preview_selected_admission", accepted=True,
                content=json.dumps({
                    "verdict": "impossible",
                    "blocking_rule_codes": prev.get("blocking_rule_codes", []),
                    "finding": prev.get("capability_reason") or prev["safe_user_message"],
                    "what_would_pass": prev.get("what_would_pass", []),
                    "recorded": False, "authorizes_submission": False,
                    "guidance": REFUSAL_GUIDANCE}))
        # A verdict from a payload the gate did NOT refuse supersedes a refusal recorded earlier
        # this turn: the model repaired its request. "incomplete" counts only when the gate ran -
        # a call missing the engine, purpose or input kind was never checked, and a malformed one
        # proves nothing either; both leave the refusal standing for the reply check.
        if prev["verdict"] == "supported" or (
                prev["verdict"] == "incomplete"
                and not {"engine", "purpose", "input_kind"} & set(prev.get("missing_fields") or ())):
            st.admission_refusal = None
        if prev["verdict"] == "supported":
            # ISSUE the submission-authorizing token, bound to this exact canonical payload, the
            # current user-message revision, AND the confirmed selection.
            canon = at.canonical_payload(eng, args.get("purpose"), args.get("input_kind"),
                                         args.get("dimensionality"), args.get("patches"),
                                         args.get("engine_params"))
            # MATERIAL change only: establishing an authorization that did not exist, or one for
            # a different payload/revision. Re-previewing the SAME requirements mints a new token
            # (the authorization contract requires a fresh nonce) but advances nothing.
            _before = st.authorization_signature()
            st.pending = at.issue(session_id=st.session_id, owner_id=st.owner_id,
                                  revision=st.revision, canonical=canon, verdict="supported",
                                  mode=at.SELECTED,
                                  selection_id=str((st.selection or {}).get("id") or ""))
            return IntakeToolResult(
                tool="preview_selected_admission", accepted=True,
                advanced=st.authorization_signature() != _before,
                content=json.dumps({"verdict": "supported", "preview_token": st.pending["token"],
                                    "canonical_summary": at.CONFIRM_REQUIREMENTS_ASK}))
        # incomplete / malformed (or impossible inside a comparison turn): READ-ONLY - no
        # authorizing token, standing state untouched.
        return IntakeToolResult(
            tool="preview_selected_admission", accepted=True,
            content=json.dumps({**{k: prev[k] for k in
                                   ("verdict", "selected_engine", "missing_fields",
                                    "safe_user_message") if k in prev},
                                "authorizes_submission": False}))

    async def _do_submit_requirements(self, args: dict) -> IntakeToolResult:
        st = self.state
        st.submit_attempts += 1
        if st.recommended_this_turn:
            # SAME-TURN ESCALATION BLOCK, turn-scoped: a recommendation turn can never become a
            # submission, in this round or any later round of the same invocation.
            logger.warning("Intake: submit rejected - recommendation turn - job_id=%s", self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                "Not allowed in this turn: you compared engines for the user, who has not selected "
                "one. Tell them which engines are compatible and ask which they want. Nothing was "
                "saved."))
        if st.answer_only:
            # A QUESTION ABOUT THE RUN THAT ENDED is answered, never turned into another run: the
            # exhaust header's "How did the mesh turn out?" was answered with a fresh summary and
            # "Shall I proceed?" (dry run, 2 Nov)
            logger.info("Intake: submit rejected - the user asked about the finished run - job_id=%s",
                        self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                "Not in this turn: the user asked a question about the run that has ended. Answer it "
                "from the LAST RUN facts in your instructions - the mesh exists - and set up nothing. "
                "Nothing was saved."))
        _up = self._confirmed_up_axis()
        val_errors = ((validate_submission(args, up_axis=_up) if _up else validate_submission(args))
                      or self._measured_setup_errors(args))
        # STRUCTURAL GATE: authorized ONLY by a matching, unexpired, single-engine SUPPORTED
        # preview TOKEN for the EXACT payload, in this session/owner and the current revision.
        canon = at.canonical_payload(args.get("mesh_engine"), args.get("purpose"),
                                     args.get("input_kind"), args.get("dimensionality"),
                                     args.get("patches"), args.get("engine_params"))
        tok_ok, tok_reason = at.verify_for_submit(
            st.pending, str(args.get("preview_token") or ""), session_id=st.session_id,
            owner_id=st.owner_id, revision=st.revision, submitted_canonical=canon,
            selection=st.selection)
        if val_errors:
            st.submit_rejections += 1
            st.val_errors_seen = [str(e).split(" ")[0] for e in val_errors]
            _R.intake_submission(self._trace, authorized=False,
                                 reason="the submitted requirements are not yet complete")
            logger.warning("Intake: submit rejected - missing fields: %s - job_id=%s",
                           val_errors, self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                "Submission rejected - required values missing or invalid: "
                f"{'; '.join(val_errors)}. Gather these before submitting."))
        if not tok_ok:
            # A refusal names THAT authorization failed, never the token or its contents.
            _R.intake_submission(self._trace, authorized=False,
                                 reason="the submission did not match the configuration "
                                        "you confirmed")
            st.submit_rejections += 1
            logger.warning("Intake: submit rejected - no valid preview token (%s) - job_id=%s",
                           tok_reason, self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                f"Submission NOT authorized: {tok_reason}. Nothing was saved."))

        # A SHELL RUN'S SUMMARY NEVER PROMISES TETRAHEDRA (SHSTAGE-5 / SURF-6): the card shows request_txt to the user,
        # and a surface file meshed as a shell was summarised as "a quadratic tetrahedral structural mesh". Sent back
        # ONCE for a rewrite; the card's own Mesh-as and engine rows say the shell either way
        if fea_path(st) and not st.shell_words_sent_back:
            from meshpipeline.application.shell_check import TET_WORDS, declared_shell, shell_summary
            shell_said = shell_summary(st.conversation)   # SHSTAGE-8: triangles when the stage settled them
            said = sorted({w.lower() for w in TET_WORDS.findall(
                f"{args.get('request_txt') or ''} {args.get('review_brief_txt') or ''}")})
            if said and declared_shell(st.conversation):
                st.shell_words_sent_back = True
                logger.info("Intake: a shell run's summary promised a solid mesh (%s) - sent back once for a "
                            "rewrite - job_id=%s", ", ".join(said), self._job_id)
                return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                    "Not saved: this part is meshed as a SHELL (the stage settled it), and request_txt or "
                    f"review_brief_txt speak of a solid mesh ({', '.join(said)}). The delivered mesh is "
                    f"{shell_said}. Rewrite both to say that shell - no tetrahedra, no element order - and "
                    "call submit_requirements again with the same preview_token."))

        # THE USER'S OWN WORDS (contracts/user_notes, NOTE-1): their answer to the notes question
        # and every message the model quoted as saying something specific, kept whole and verbatim -
        # never a click on a button the application offered (its words are ours, not a note)
        notes, unmatched = un.capture(st.conversation, args.get("user_note_quotes") or (),
                                       consumer="intake", offered=_offered_choices)
        if not notes and not un.asked(st.conversation):
            # ASKED ONCE, before the first summary, and only when the user has said nothing
            # specific yet. It holds nothing: the next answer - details or a plain no - goes
            # straight on to the summary.
            st.notes_prompt = un.QUESTION
            logger.info("Intake: submission held one turn for the notes question - job_id=%s",
                        self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=True, content=(
                "Not submitted yet: the application now asks the user its one optional question "
                "about anything specific for this simulation, in its own words - do not ask it or "
                "paraphrase it. Nothing was saved. After their answer, re-run "
                "preview_selected_admission and call submit_requirements again."))
        st.submit_args = args
        st.user_notes = notes
        logger.info("Intake: submit_requirements AUTHORIZED - domain=%r notes=%d - job_id=%s",
                    args.get("domain", "")[:60], len(notes), self._job_id)
        canon_stored = (st.pending or {}).get("canonical", {})
        payload = {k: v for k, v in args.items() if k != "preview_token"}
        if notes:
            payload["user_notes"] = notes
        intent_canon = at.approved_intent_canonical(
            engine=args.get("mesh_engine"), purpose=args.get("purpose"),
            input_kind=args.get("input_kind"), dimensionality=args.get("dimensionality"),
            patches=args.get("patches"), engine_params=args.get("engine_params"),
            requested_mesh_fidelity=args.get("mesh_fidelity"),
            requested_extents=args.get("requested_extents"),
            reference_length_m=args.get("reference_length_m"),
            flow_axis=args.get("flow_axis"),
            requirements_strict=bool(args.get("requirements_strict") or False),
            request_txt=args.get("request_txt"), source_ref=st.source_ref)
        # the approval this one replaces: withdrawn by the user's last message, and if that message
        # changed nothing at all, the summary says so instead of reappearing unexplained
        prior = st.approval or {}
        # the WHOLE submission the same - requirements, review brief, label and every value - not
        # only the run-determining intent: a changed brief is a change the user should see
        unchanged = (prior.get("status") == ap.INVALIDATED
                     and prior.get("intent_fingerprint") == at.fingerprint(intent_canon)
                     and _same_submission(prior.get("payload"), payload))
        # ...and when it did change, the summary says WHAT, so the user sees their correction taken
        changed = (ap.changed_line(ap.changes_between(prior.get("payload"), payload),
                                   since_run=prior.get("status") == ap.DISPATCHED)
                   if prior.get("payload") and not unchanged else "")
        st.submit_summary = (at.CONFIRM_REQUIREMENTS_ASK + "\n\n"
                             + (f"{changed}\n\n" if changed else "")
                             + (f"{NOTES_KEPT}\n\n" if notes else "")
                             + (f"{ap.NOTHING_CHANGED} " if unchanged else "") + ap.PROCEED_ASK)
        st.approval = ap.create(
            owner_id=st.owner_id, session_id=st.session_id,
            selection_id=str((st.selection or {}).get("id") or ""),
            token_id=str((st.pending or {}).get("token") or ""),
            canonical=canon_stored, fingerprint=at.fingerprint(canon_stored),
            intent_canonical=intent_canon, intent_fingerprint=at.fingerprint(intent_canon),
            payload=payload, summary=st.submit_summary,
            proposal_revision=st.revision, proposal_msg_count=st.user_msg_count)
        logger.info("Intake: pending approval %s created - job_id=%s",
                    st.approval["id"], self._job_id)
        _R.intake_requirements_finalized(
            self._trace, patches=len(args.get("patches") or []),
            dimensionality=str(args.get("dimensionality") or ""))
        _R.intake_submission(self._trace, authorized=True)
        _not_kept = (" These quotes matched nothing the user wrote, so they were not kept as "
                     "notes: " + "; ".join(repr(q[:80]) for q in unmatched[:5]) + "."
                     if unmatched else "")
        return IntakeToolResult(tool="submit_requirements", accepted=True, advanced=True, content=(
            "Authorized. The application will show the user the canonical confirmation summary; "
            "do not paraphrase it. Await their explicit approval." + _not_kept))


__all__ = ["INTAKE_CATEGORIES", "MAX_SEARCH_CALLS", "MUTATING_TOOLS", "REFUSAL_GUIDANCE",
           "IntakeExecutionState", "IntakeToolExecutor", "IntakeToolResult", "category_of"]
