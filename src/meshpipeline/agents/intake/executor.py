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


def category_of(tool: str) -> str:
    return INTAKE_CATEGORIES.get(tool, "unknown")


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
    submit_summary: str | None = None
    submit_args: dict | None = None

    search_calls_used: int = 0
    search_events: list = field(default_factory=list)
    submit_attempts: int = 0
    submit_rejections: int = 0
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


    def _geometry_facts(self) -> dict:
        # What the uploaded CAD actually distinguishes, read from the staged file beside the
        # session. Without it a refusal can only describe the engine, so a user whose export named
        # nothing is told the system cannot do what they asked - when the missing part is theirs.
        # Never fatal and never blocking: no file, or a file this cannot describe, simply says
        # nothing and the verdict is what it always was.
        try:
            import meshpipeline.settings.runtime as rtcfg
            from meshpipeline.cad.regions import regions_for_session

            return regions_for_session(str(self.state.session_id or ""), rtcfg.JOBS_DIR).as_facts()
        except Exception as exc:
            logger.debug("Intake: geometry regions unavailable (%s)", exc)
            return {}

    def _staged_upload(self) -> str:
        # The upload as staged beside the session (the file cad/regions reads), or ''.
        return measured_choice.staged_upload(str(self.state.session_id or ""))

    def _geometry_form(self) -> str:
        # WHAT KIND OF FILE the user uploaded - a CAD solid or a surface mesh - asked of the one
        # reading (measured_choice.upload_form: the staged file first, then the source's name).
        # Every engine check below asks the engine whether it takes THIS file for the flow; ''
        # (no upload, or a name that says nothing) claims nothing, and nothing is refused on it.
        return measured_choice.upload_form(self._staged_upload(), self.state.source_ref)


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
        if st.admission_refusal is not None and not named_now:
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
        if not chosen_by_user and st.declared_case:
            # AN ENGINE THE USER DID NOT NAME MUST BE ABLE TO MESH WHAT THEY CONFIRMED. The model
            # proposed cfMesh for a fluid-volume file on its own; the admission then refused, and
            # its way out was to relabel the confirmed fluid volume as a body surface - a fact the
            # user never said - or to ask a user who had said "your call" which value to revise.
            # The engine is the model's to change, the confirmed geometry is not.
            purpose, kind = st.declared_case
            form = self._geometry_form()
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
                    f"Not proposed: {shown} cannot mesh what the user confirmed on the geometry "
                    f"stage ({_vocab.to_display(_vocab.INPUT_KIND, kind)}, "
                    f"{_vocab.to_display(_vocab.PURPOSE, purpose)}) from the file they uploaded, "
                    f"and the user did not ask for it. "
                    f"Engines that can: {names}. Propose one of those instead; never ask the user to "
                    "change the geometry they confirmed to suit an engine they did not choose."))
        # A NEW proposal invalidates the previous selection AND any admission preview or pending
        # canonical confirmation that the old selection authorized - including one obtained
        # EARLIER IN THIS SAME provider response.
        st.selection = es.propose(eng, session_id=st.session_id, owner_id=st.owner_id,
                                  revision=st.revision, user_msg_count=st.user_msg_count)
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
        if recd is not None and recd.fit_of(eng) is not None:
            st.selection_prompt = measured_choice.statement(recd, eng)
        else:
            st.selection_prompt = es.render_selection_statement(eng, str(args.get("reason") or ""))
        logger.info("Intake: engine selection PROPOSED engine=%s - job_id=%s", eng, self._job_id)
        swapped = (f" You suggested {_vocab.to_display(_vocab.ENGINE, model_pick)}; the measured "
                   f"recommendation for this geometry is {_vocab.to_display(_vocab.ENGINE, eng)}, "
                   "so that is the engine proposed." if model_pick != eng else "")
        return IntakeToolResult(tool="propose_engine_selection", accepted=True, advanced=True,
                                content=("Proposed." + swapped + " The application asks the user "
                                         "this one engine question in its own words, with the "
                                         "measured evidence and the alternatives; do not "
                                         "paraphrase it or ask it again. Await their answer."))

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
                                 geometry_form=self._geometry_form())
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
        val_errors = validate_submission(args)
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

        st.submit_args = args
        logger.info("Intake: submit_requirements AUTHORIZED - domain=%r - job_id=%s",
                    args.get("domain", "")[:60], self._job_id)
        canon_stored = (st.pending or {}).get("canonical", {})
        payload = {k: v for k, v in args.items() if k != "preview_token"}
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
                     and prior.get("payload") == payload)
        st.submit_summary = (at.CONFIRM_REQUIREMENTS_ASK + "\n\n"
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
        return IntakeToolResult(tool="submit_requirements", accepted=True, advanced=True, content=(
            "Authorized. The application will show the user the canonical confirmation summary; "
            "do not paraphrase it. Await their explicit approval."))


__all__ = ["INTAKE_CATEGORIES", "MAX_SEARCH_CALLS", "MUTATING_TOOLS", "REFUSAL_GUIDANCE",
           "IntakeExecutionState", "IntakeToolExecutor", "IntakeToolResult", "category_of"]
