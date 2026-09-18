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


#: The Surveyor's two tools, offered only when the survey is armed for an upload. Kept apart from
#: `INTAKE_CATEGORIES` so the conversation with the survey off names exactly the tools it always has,
#: down to the list an unknown tool is answered with.
SURVEY_CATEGORIES = {
    "survey_the_part": "survey",
    "answer_survey_question": "survey",
}


def category_of(tool: str) -> str:
    return INTAKE_CATEGORIES.get(tool) or SURVEY_CATEGORIES.get(tool, "unknown")


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
    # The approved SOURCE, not a file. Intake binds which bytes were approved; it never
    # opens them, so it holds the reference and no path.
    source_ref: object | None = None
    rec_authorized: bool = False

    pending: dict | None = None       # the issued admission token record
    selection: dict | None = None
    approval: dict | None = None

    # WHAT THE FILE IS, read once per turn before the loop starts and handed in here rather than
    # fetched by a handler. Two readers want it - the admission preview wants five keys, the system
    # prompt wants the opening table - and a second fetch inside a tool call would read the same row
    # twice in one turn and could disagree with the table the model is already holding.
    #
    # `None` means the measured phase did not run, which is the common case and never an error. A
    # dict always carries `status`: see `cad/regions.py`. Nothing downstream may infer a verdict
    # from an absent key.
    geometry_reading: dict | None = None
    #: The whole stored measurement, for what the conversation says. None when there is none.
    geometry_document: dict | None = None

    # THE SURVEYOR, for this upload. `survey_armed` is false unless the survey gate is on and there is
    # a successful measurement; everything below is then unused and every tool behaves as before.
    survey_armed: bool = False
    #: The stored survey state, updated in place by the two survey tools so a later call in the same
    #: turn, and the submission gate, read what was just recorded.
    geometry_survey: dict | None = None
    #: The upload the survey belongs to.
    survey_source_ref: object | None = None
    #: The customer's own messages this session, oldest first. The survey is composed from their words
    #: and never from intake's write-up of them.
    customer_messages: tuple[str, ...] = ()

    # TURN-SCOPED, never round-scoped. Once a recommendation happens in this invocation, nothing
    # may escalate to selection or submission for the REST of the invocation - not merely for the
    # rest of the round. A comparison the user asked for can never become a choice they did not
    # make, however many provider rounds the model takes to get there.
    recommended_this_turn: bool = False

    # Application-rendered terminals, resolved after the whole ordered batch.
    admission_block: str | None = None
    #: The verdict behind admission_block, kept so the refusal can be put in the user's terms
    #: after the loop. The rendered text alone cannot be checked against what was declared.
    admission_facts: dict | None = None
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


    def _geometry_facts(self) -> dict | None:
        """What the uploaded geometry is, for the engine's admission rules. Never fatal, never blocking.

        `None` IS A VALUE HERE, and it was not before. This used to return `{}` on every path - no
        file, an unreadable file, any exception - and `engines/base.py:430-434` tests only
        `surface_analysis is not None`, so `{}` passed the measured gate carrying nothing and three
        situations arrived as one: never measured, measurement failed, and measured with nothing
        unusual about it. `None` now means the measured phase did not run and a dict always carries
        `status`, so no rule infers a verdict from an absent key.

        With `GEOMETRY_REPORT_READERS_ENABLED` off this is exactly what it was, including the empty
        dict, because that is what every engine rule on this path has always been handed.
        """
        import meshpipeline.settings.policy as polcfg

        if polcfg.GEOMETRY_REPORT_READERS_ENABLED:
            # Read before the loop, in `node_intake`. Absent means nothing was measured and nothing
            # could be read off the durable bytes, which is a legitimate outcome and not an error.
            return self.state.geometry_reading
        try:
            import meshpipeline.settings.runtime as rtcfg
            from meshpipeline.cad.regions import regions_for_session

            return regions_for_session(str(self.state.session_id or ""), rtcfg.JOBS_DIR).as_facts()
        except Exception as exc:
            logger.debug("Intake: geometry regions unavailable (%s)", exc)
            return {}


    def _patch_binding(self, patches) -> dict:
        """Which declared flow boundaries resolved to a measured opening. Never fatal.

        Off, or with no measurement, this is `{"checked": False, ...}` and the customer is told the
        geometry was not measured - which is what has been true on every job ever run.
        """
        import meshpipeline.settings.policy as polcfg

        if not polcfg.GEOMETRY_REPORT_READERS_ENABLED:
            return {"checked": False}
        try:
            from meshpipeline.agents.intake.geometry_brief import bind_patches

            bound = bind_patches(self.state.geometry_document, patches)
            if bound.get("unbound"):
                logger.info("Intake: %d declared boundary/boundaries did not bind to a measured "
                            "opening - job_id=%s: %s", len(bound["unbound"]), self._job_id,
                            [b["name"] for b in bound["unbound"]])
            return bound
        except Exception as exc:                   # noqa: BLE001 - a claim is never worth a turn
            logger.debug("Intake: patch binding unavailable (%s)", exc)
            return {"checked": False}

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

        before = st.authorization_signature()
        handler = getattr(self, f"_do_{tool}", None)
        if handler is None:
            logger.warning("Intake: unknown tool %r - job_id=%s", tool, self._job_id)
            return IntakeToolResult(
                tool=tool, accepted=False,
                content=f"Unknown tool {tool!r}. Available: {', '.join(INTAKE_CATEGORIES)}.")
        result = await handler(args)
        after = st.authorization_signature()
        return IntakeToolResult(
            tool=result.tool, content=result.content, accepted=result.accepted,
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
            engine_params=args.get("engine_params"), authorized=st.rec_authorized)
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
        # A NEW proposal invalidates the previous selection AND any admission preview or pending
        # canonical confirmation that the old selection authorized - including one obtained
        # EARLIER IN THIS SAME provider response.
        st.selection = es.propose(eng, session_id=st.session_id, owner_id=st.owner_id,
                                  revision=st.revision, user_msg_count=st.user_msg_count)
        st.pending = None
        st.invalidate_approval("engine selection replaced")
        # The confirmation question exists to prove the USER chose this engine. If their own latest
        # message already names it, that proof is in hand and asking again is a question with one
        # answer - so the selection is confirmed here instead of costing the user a round-trip.
        quote = str(args.get("user_named_verbatim") or "")
        if es.user_named_engine(eng, quote, st.latest_user_msg):
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
        st.selection_prompt = es.render_selection_statement(eng)
        logger.info("Intake: engine selection PROPOSED engine=%s - job_id=%s", eng, self._job_id)
        return IntakeToolResult(tool="propose_engine_selection", accepted=True, advanced=True,
                                content=("Proposed. The application will ask the user to confirm "
                                         "this engine; do not paraphrase it. Await their explicit "
                                         "answer."))

    async def _do_confirm_engine_selection(self, args: dict) -> IntakeToolResult:
        st = self.state
        quote = str(args.get("user_agreed_verbatim") or "").strip()
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
                                content=(f"Confirmed: the user selected "
                                         f"{_vocab.to_display(_vocab.ENGINE, st.selection['engine'])}. "
                                         "You may now gather the remaining requirements and call "
                                         "preview_selected_admission."))

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
                                 geometry_facts=self._geometry_facts())
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
            # TERMINAL: the application's message is the final reply for this turn; the model does
            # not compose it. It also invalidates any standing authorization.
            st.admission_block = prev["safe_user_message"]
            st.admission_facts = prev
            st.pending = None
            st.invalidate_approval("admission became impossible")
            return IntakeToolResult(tool="preview_selected_admission", accepted=True,
                                    advanced=True, content=prev["safe_user_message"])
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

    # the Surveyor's two tools
    def _survey_refusal(self, tool: str) -> IntakeToolResult | None:
        st = self.state
        if st.survey_armed and st.survey_source_ref is not None and isinstance(st.geometry_document, dict):
            return None
        return IntakeToolResult(tool=tool, accepted=False, content=(
            "The Surveyor is not available for this upload. Nothing was recorded; carry on as usual."))

    def _survey_text(self) -> str:
        from meshpipeline.agents.intake.geometry_brief import survey_lines
        return "\n".join(survey_lines(self.state.geometry_survey)).strip()

    async def _do_survey_the_part(self, args: dict) -> IntakeToolResult:
        """Step 1 to step 4: the customer has said what the part is for; compose and return the questions."""
        refused = self._survey_refusal("survey_the_part")
        if refused is not None:
            return refused
        from meshpipeline.application import geometry_survey as gs

        st = self.state
        # _survey_refusal returned None, so this is the stored document; the else is for the type checker
        document = st.geometry_document if isinstance(st.geometry_document, dict) else {}
        quote = str(args.get("customer_words_verbatim") or "")
        if not any(gs.said_by_customer(quote, said) for said in st.customer_messages):
            return IntakeToolResult(tool="survey_the_part", accepted=False, content=(
                "Not surveyed: that quote is not in anything the customer wrote. Quote their own words "
                "exactly. Nothing was recorded."))
        ports = [p for p in (args.get("ports") or []) if isinstance(p, dict)]
        try:
            st.geometry_survey = await gs.survey_the_part(
                owner_id=st.owner_id, session_id=st.session_id, source_ref=st.survey_source_ref,
                document=document, purpose=str(args.get("purpose") or ""),
                messages=[{"role": "user", "content": said} for said in st.customer_messages],
                declared=ports or None, engine=self._confirmed_engine() or None)
        except gs.SurveyError as exc:
            logger.info("Intake: survey_the_part declined (%s) - job_id=%s", exc, self._job_id)
            return IntakeToolResult(tool="survey_the_part", accepted=True, content=(
                f"No survey for this upload: {exc}. Carry on as usual."))
        logger.info("Intake: survey composed for %s, stage=%s - job_id=%s", args.get("purpose"),
                    st.geometry_survey.get("stage"), self._job_id)
        return IntakeToolResult(tool="survey_the_part", accepted=True, content=self._survey_text())

    async def _do_answer_survey_question(self, args: dict) -> IntakeToolResult:
        """Step 4 and step 6: one answer, the customer's, with their words."""
        refused = self._survey_refusal("answer_survey_question")
        if refused is not None:
            return refused
        from meshpipeline.application import geometry_survey as gs

        st = self.state
        try:
            st.geometry_survey = await gs.answer(
                owner_id=st.owner_id, source_ref=st.survey_source_ref,
                question_id=str(args.get("question_id") or ""), choice=str(args.get("option") or ""),
                role=str(args.get("role") or ""), words=str(args.get("customer_words_verbatim") or ""),
                latest_user_message=st.latest_user_msg, skipped=bool(args.get("skipped")),
                took_default=bool(args.get("took_default")), document=st.geometry_document)
        except gs.SurveyError as exc:
            return IntakeToolResult(tool="answer_survey_question", accepted=False,
                                    content=f"Not recorded: {exc}.")
        return IntakeToolResult(tool="answer_survey_question", accepted=True,
                                content="Recorded.\n" + self._survey_text())

    def _confirmed_engine(self) -> str:
        st = self.state
        return (str((st.selection or {}).get("engine") or "")
                if es.state_of(st.selection) == es.CONFIRMED else "")

    async def _survey_gate(self, args: dict) -> list[str]:
        """Why a submission's port roles are not the customer's, or nothing. Fails open.

        With the survey off, or for an upload it cannot compose, this is empty and the submission is
        judged exactly as it was before the survey existed.
        """
        st = self.state
        if not st.survey_armed or st.survey_source_ref is None or not isinstance(st.geometry_document, dict):
            return []
        from meshpipeline.application import geometry_survey as gs

        try:
            state = await gs.for_submission(
                owner_id=st.owner_id, session_id=st.session_id, source_ref=st.survey_source_ref,
                document=st.geometry_document, purpose=str(args.get("purpose") or ""),
                messages=[{"role": "user", "content": said} for said in st.customer_messages],
                state=st.geometry_survey, engine=self._confirmed_engine() or None)
        except gs.SurveyError as exc:
            logger.info("Intake: no survey to check the submission against (%s) - job_id=%s", exc,
                        self._job_id)
            return []
        except Exception as exc:                   # noqa: BLE001 - never a turn
            logger.warning("Intake: the survey gate could not run (%s) - job_id=%s", exc, self._job_id)
            return []
        if state is None:
            return []
        st.geometry_survey = state
        return gs.role_problems(state, st.geometry_document, args.get("patches"))

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

        # A PORT ROLE THE CUSTOMER DID NOT CONFIRM STOPS HERE. `admission_token.py` copies every
        # declared inlet and outlet into `port_declaration` and the engine binds a boundary condition
        # from each, so this is the last place a guessed role can be refused. Empty with the survey off.
        _unconfirmed = await self._survey_gate(args)
        if _unconfirmed:
            st.submit_rejections += 1
            logger.warning("Intake: submit rejected - port roles the customer has not confirmed: %s "
                           "- job_id=%s", _unconfirmed, self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                "Submission rejected - a port role must be the customer's: " + "; ".join(_unconfirmed)
                + ". Nothing was saved."))

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
        st.submit_summary = (at.CONFIRM_REQUIREMENTS_ASK
                             + "\n\nShall I proceed with mesh generation?")
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
        # WHAT WAS ACTUALLY CHECKED. `geometry_checked` is true only when a measurement exists AND
        # every declared inlet and outlet resolved to an opening in it. Until this phase nothing
        # could ever bind, so the sentence the customer read was false on every job; the foundations
        # phase made it say what happened, and this is what lets it say the true version - and only
        # then. A partial binding is false: a check of some of the patches is not a check.
        _bound = self._patch_binding(args.get("patches"))
        _R.intake_requirements_finalized(
            self._trace, patches=len(args.get("patches") or []),
            dimensionality=str(args.get("dimensionality") or ""),
            geometry_checked=bool(_bound.get("checked")))
        _R.intake_submission(self._trace, authorized=True)
        return IntakeToolResult(tool="submit_requirements", accepted=True, advanced=True, content=(
            "Authorized. The application will show the user the canonical confirmation summary; "
            "do not paraphrase it. Await their explicit approval."))


__all__ = ["INTAKE_CATEGORIES", "MAX_SEARCH_CALLS", "MUTATING_TOOLS", "SURVEY_CATEGORIES",
           "IntakeExecutionState", "IntakeToolExecutor", "IntakeToolResult", "category_of"]
