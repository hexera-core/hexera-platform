# Responsibility: Execute one intake tool call and track what the conversation has established.
# Boundaries: intake tools read and propose; none of them starts a run.
from __future__ import annotations

import asyncio
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


#: How long the conversation waits for the look before carrying on without it. The look takes about
#: 25 seconds on this reader; the budget is generous so a slow one is still caught, and a slower one
#: is left to land on the row by itself rather than holding a customer.
LOOK_WAIT_SECONDS = 75.0
LOOK_POLL_SECONDS = 2.5


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
    choice_deferred: bool = False

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

    #: EVERY user message this session, oldest first, ARMED OR NOT. `customer_messages` above is the
    #: Surveyor's and is empty on every upload the survey is off for; who chose the engine, and whether
    #: they handed us the choice, are questions about the conversation and have nothing to do with the
    #: survey gate. Reading the survey's copy for them read an engine the customer named two turns ago
    #: as one we picked ourselves, and made a standing delegation invisible to the engine question - on
    #: exactly the uploads the Surveyor cannot compose for. `turn.py` already reads every user message
    #: for both facts (`_standing_deferral`), so this is the same history one field along, and what may
    #: be read from an earlier message is unchanged: a DEFERRAL only, never an affirmation or a refusal.
    user_messages: tuple[str, ...] = ()

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
    #: The Surveyor's receipt for the customer, composed once the look has landed. Shown ABOVE the
    #: model's own reply rather than instead of it: the questions still belong to the conversation,
    #: and what was measured and seen belongs to the application.
    surveyor_panel: str = ""
    #: The engine WE swapped out from under our own pick, and why, once per session. Durable
    #: because the confirmation that must tell the customer about it is composed on a LATER
    #: turn, and the swap was a local variable that died with the turn that made it.
    engine_swap: dict = field(default_factory=dict)
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
        """
        # Read before the loop, in `node_intake`. Absent means nothing was measured and nothing could
        # be read off the durable bytes, which is a legitimate outcome and not an error.
        return self.state.geometry_reading


    def _patch_binding(self, patches) -> dict:
        """Which declared flow boundaries resolved to a measured opening. Never fatal.

        With no measurement this is `{"checked": False, ...}` and the customer is told the geometry
        was not measured, which is a fact about the job and never a silence.
        """
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
        # ONE EXCEPTION, AND ONLY ONE: the customer handed us the choice in this very message.
        # The rule below exists so the model cannot select an engine merely because it just
        # recommended one. It does not fit a customer who said "you decide": they are told
        # "a recommendation is not a selection - ask which engine they want", which asks again
        # the one person who has already said they do not want to be asked. That cost a real
        # conversation three round trips for one choice. Deferring still selects NOTHING: the
        # application shows its own question and the customer answers it in their own words,
        # so house law 2 holds - a default is still not a confirmation.
        if st.recommended_this_turn and not st.choice_deferred:
            return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                "Not allowed in this turn: you compared engines for the user. A recommendation is "
                "not a selection - ask which engine they want and wait for their next message."))
        if eng not in self._engines:
            return IntakeToolResult(tool="propose_engine_selection", accepted=False, content=(
                f"{eng!r} is not a registered engine. Available: {', '.join(self._engines)}."))
        quote = str(args.get("user_named_verbatim") or "")
        shown = _vocab.to_display(_vocab.ENGINE, eng)
        prior = st.selection
        prior_state = es.state_of(prior)
        same_engine = isinstance(prior, dict) and prior.get("engine") == eng

        # ALREADY CHOSEN. Re-proposing the engine the user has already selected used to replace that
        # selection with a fresh question, discarding consent this application had already recorded
        # and verified. Nothing is learned by asking again, and the answer was already given.
        if same_engine and prior_state == es.CONFIRMED:
            logger.info("Intake: engine already selected, propose ignored engine=%s - job_id=%s",
                        eng, self._job_id)
            return IntakeToolResult(
                tool="propose_engine_selection", accepted=True, advanced=False,
                content=(f"{shown} is ALREADY SELECTED by the user - do not ask them again. Gather "
                         "the remaining requirements and call preview_selected_admission."))

        # ALREADY ASKED. Asking the identical question a second time is what made it unanswerable:
        # a fresh proposal replaced the pending one, so the user's answer was spent on re-asking and
        # the same question returned however many times they said yes.
        if same_engine and prior_state == es.PROPOSED:
            if es.answers_the_selection_question(eng, quote, st.latest_user_msg, outstanding=True,
                                                 earlier_user_messages=st.user_messages):
                st.selection = {**prior, "state": es.CONFIRMED,
                                "confirmed_revision": st.revision,
                                "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
                logger.info("Intake: engine selection CONFIRMED from the user's answer to the "
                            "standing question engine=%s - job_id=%s", eng, self._job_id)
                return IntakeToolResult(
                    tool="propose_engine_selection", accepted=True, advanced=True,
                    content=(f"The user answered the question already on screen, so {shown} is "
                             "SELECTED - do not ask them to confirm it again. Gather the remaining "
                             "requirements and call preview_selected_admission."))
            logger.info("Intake: engine already proposed, not re-asked engine=%s - job_id=%s",
                        eng, self._job_id)
            return IntakeToolResult(
                tool="propose_engine_selection", accepted=False,
                content=(f"{shown} is already proposed and that question is on the user's screen "
                         "unanswered. Do NOT ask it again - repeating it is how this conversation "
                         "gets stuck. Wait for their next message and call "
                         "confirm_engine_selection with the words they actually wrote."))

        # A NEW proposal invalidates the previous selection AND any admission preview or pending
        # canonical confirmation that the old selection authorized - including one obtained
        # EARLIER IN THIS SAME provider response.
        st.selection = es.propose(eng, session_id=st.session_id, owner_id=st.owner_id,
                                  revision=st.revision, user_msg_count=st.user_msg_count,
                                  # WHOSE CHOICE THIS IS, recorded at the one moment it is knowable.
                                  # Nothing on the row said, so `_do_preview_selected_admission` could
                                  # not tell an engine the customer insisted on from one we picked for
                                  # them - and treated both as theirs to revise.
                                  chosen_by=es.who_chose(eng, quote=quote,
                                                         latest_user_message=st.latest_user_msg,
                                                         user_messages=st.user_messages))
        st.pending = None
        st.invalidate_approval("engine selection replaced")
        # The confirmation question exists to prove the USER chose this engine. If their own latest
        # message already names it, that proof is in hand and asking again is a question with one
        # answer - so the selection is confirmed here instead of costing the user a round-trip.
        if es.answers_the_selection_question(eng, quote, st.latest_user_msg, outstanding=False,
                                             earlier_user_messages=st.user_messages):
            st.selection = {**st.selection, "state": es.CONFIRMED,
                            "confirmed_revision": st.revision,
                            "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
            logger.info("Intake: engine selection CONFIRMED from the user's own words engine=%s "
                        "- job_id=%s", eng, self._job_id)
            return IntakeToolResult(
                tool="propose_engine_selection", accepted=True, advanced=True,
                content=(f"The user named {shown} themselves, so it is SELECTED - "
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
            user_msg_count=st.user_msg_count, earlier_user_messages=st.user_messages)
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

    def _locate_named_ports(self, args: dict) -> None:
        """Give every port that NAMES a measured opening that opening's position, in place.

        WHY THIS EXISTS. `opening_id` lets a port say which mouth it is, which is what stopped this
        product interrogating a customer about two identical 439 mm outlets. But the BUILDER does not
        read it: `cad_tessellate.select_declared_openings` matches a declared port by size or by
        location and raises when it has neither - so accepting an id at intake without carrying its
        geometry downstream moved the failure from a question in chat to five failed build attempts.
        Measured: a submission that passed every gate died with "declared port 'inlet' matches none
        of the remaining flat faces".

        So the id is resolved to the measurement's own centroid here, and the port arrives at the
        builder locatable. Nothing is invented: the number comes from the measured opening the model
        named, and a port that already states a size or a position is left exactly as declared.

        RUN IN BOTH THE PREVIEW AND THE SUBMIT, and identical in each, because the admission token is
        canonicalised from these patches on both sides - enriching one and not the other would fail
        every submission on a token mismatch.
        """
        patches = args.get("patches")
        document = self.state.geometry_document
        if not isinstance(patches, list) or not isinstance(document, dict):
            return
        if document.get("status") != "ok":
            return
        rows = {str(r.get("id") or "").strip(): r
                for r in (document.get("openings") or []) if isinstance(r, dict) and r.get("id")}
        if not rows:
            return
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            if str(patch.get("type") or "").strip().lower() not in ("inlet", "outlet"):
                continue
            row = rows.get(str(patch.get("opening_id") or "").strip())
            if not row:
                continue        # named nothing measured: leave the declaration exactly as it is
            # THE MEASURED MOUTH'S OWN GEOMETRY WINS, because the model computes it wrong and the
            # builder matches on it. Measured: o1 is a RECTANGLE of 367,216 mm2. The model was told
            # "551 mm across", treated that as a circular diameter and submitted 238,528 mm2 -
            # pi*(551/2)^2 - and `cad_tessellate.select_declared_openings` checks size BEFORE
            # position, skipping every face outside the band. So a wrong area vetoed the face whose
            # centroid matched to the millimetre, and five build attempts died on "declared port
            # 'inlet' matches none of the remaining flat faces".
            #
            # Naming the opening is the claim; its size and position are then facts about that
            # opening, not the model's arithmetic. Only for a port that NAMES one - a port the
            # customer described themselves is untouched, because nothing here resolves to a row.
            centroid = row.get("centroid_mm")
            area = row.get("area_mm2")
            if isinstance(centroid, (list, tuple)) and len(centroid) == 3:
                try:
                    patch["near_mm"] = [float(c) for c in centroid]
                except (TypeError, ValueError):
                    pass
            if isinstance(area, (int, float)) and not isinstance(area, bool) and area > 0:
                patch["area_mm2"] = float(area)
                # exactly one size form, or the payload gate refuses the pair
                for k in ("diameter_mm", "width_mm", "height_mm"):
                    patch.pop(k, None)


    def _admission_of(self, engine: str, args: dict) -> dict:
        """The catalog's verdict on ONE engine for this payload, logged and traced. Changes no state.

        Lifted out of `_do_preview_selected_admission` so a turn that asks the catalog about a second
        engine records it the same way as the first. A second verdict that was neither logged nor traced
        would make the trace say the run was refused on an engine it did not use.
        """
        prev = preview_admission(engine, args.get("purpose", ""), args.get("input_kind", ""),
                                 dimensionality=args.get("dimensionality"),
                                 patches=args.get("patches"),
                                 engine_params=args.get("engine_params"),
                                 geometry_facts=self._geometry_facts())
        logger.info("Intake: preview_selected_admission(%s,%s,%s,patches=%d) -> %s(%s) - job_id=%s",
                    engine, args.get("purpose"), args.get("input_kind"),
                    len(args.get("patches") or []), prev["verdict"],
                    prev.get("blocking_rule_code", ""), self._job_id)
        _R.intake_compatibility(
            self._trace, engine=str(engine), purpose=str(args.get("purpose") or ""),
            input_kind=str(args.get("input_kind") or ""),
            supported=prev["verdict"] == "supported",
            explanation=str(prev.get("safe_user_message") or ""))
        return prev

    def _an_engine_that_can(self, failed: str, args: dict) -> str:
        """An engine the catalog admits for what was declared, when OUR OWN pick could not. "" otherwise.

        THREE THINGS HAVE TO BE TRUE and each of them alone makes this "", which is the turn ending
        exactly where it ended before:

          the customer handed us the choice   `st.choice_deferred`, which is their delegation in the
                                              latest message OR any earlier one (`turn._standing_deferral`)
                                              - a delegation does not expire.
          the engine on the row is OURS       `es.chosen_by(...) == CHOSEN_BY_US`, recorded when it was
                                              proposed. "" - a selection from before that field existed -
                                              is not "us" and changes nothing.
          they never named an engine at all   not the same question as the one above, and MEASURED live
                                              as a hole in it. A customer said "use snappyHexMesh" and
                                              then "you decide everything else"; the catalog refused
                                              snappyHexMesh for their file, the model was told "go" six
                                              times, and on the seventh it proposed cfMesh on its own
                                              authority. The row then said "ours" - truthfully - about an
                                              engine that had displaced one the CUSTOMER had named, and
                                              swapping on from there helped the model out of a value it
                                              was never allowed to change. A customer who has named an
                                              engine has not delegated THAT choice, whatever else they
                                              delegated, and the engine they named stays on screen with
                                              the mismatch explained - which is the whole point of the
                                              terminal below.
          some engine can actually do it      the catalog's answer, not ours.

        THE CUSTOMER'S OWN VALUES ARE NOT READ HERE AND CANNOT MOVE. The purpose, the geometry kind, the
        dimensionality and every patch go to `admissible_engines` exactly as declared and are what the
        candidate engines are judged against; the engine is the only field this can change, and it is
        the only one nobody chose.

        AND A SECOND OPINION IS NOT A SECOND SOURCE. This asks the catalog, which is what refused the
        first engine - the same authority the prompt file names as the only one that decides
        compatibility. It can still fail differently from the thing it checks, and does: the failing
        engine is excluded, so this returns "" precisely when nothing else can do the job either, and
        the dead end is then real rather than one we made.
        """
        st = self.state
        if not st.choice_deferred:
            return ""
        if es.chosen_by(st.selection) != es.CHOSEN_BY_US:
            return ""
        named = sorted({e for m in st.user_messages for e in _vocab.engines_named_in(str(m))})
        if named:
            logger.info("Intake: not changing the engine - the customer named %s themselves, so the "
                        "choice was never ours however much else they delegated - job_id=%s",
                        ", ".join(named), self._job_id)
            return ""
        candidates = rec.admissible_engines(
            args.get("purpose", ""), args.get("input_kind", ""),
            dimensionality=args.get("dimensionality"), patches=args.get("patches"),
            engine_params=args.get("engine_params"), geometry_facts=self._geometry_facts(),
            exclude=(failed,))
        candidates = [c for c in candidates if c in self._engines]
        if not candidates:
            logger.info("Intake: our own pick %s cannot do this and NO registered engine can either - "
                        "the impossibility is real, handing it back - job_id=%s", failed, self._job_id)
            return ""
        return candidates[0]

    async def _do_preview_selected_admission(self, args: dict) -> IntakeToolResult:
        self._locate_named_ports(args)
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
        prev = self._admission_of(eng, args)
        # AN IMPOSSIBILITY WE CREATED, UNDER A DELEGATION, IS OURS TO UNDO. Below this line the turn
        # ends in the application's own text and the model may not compose a reply or name a
        # replacement engine, which is right when the customer chose the engine and wrong when we did:
        # it asks them to revise a decision they never made and had explicitly handed over. MEASURED
        # over 23 driven conversations, shell_and_tube_7_unshared (0 of 3), cht_enclosing_2region
        # (0 of 2) and duct_bspline_inlet_shallow (0 of 1) died here, and turn 9 of one of them read
        # "snappyHexMesh still cannot do that. Nothing has changed, so nothing can run. Please change
        # the geometry type or the purpose" - to a customer who had said "you decide everything" and
        # then "go" eight times. `tee_fluid_filleted`, the same kind of file, submitted in four
        # messages because the model happened to pick the one engine that can.
        swap: dict = {}
        instead = (self._an_engine_that_can(eng, args)
                   if prev["verdict"] == "impossible" and not st.recommended_this_turn else "")
        if instead:
            swapped_from, refused_because = eng, str(prev.get("capability_reason") or "").strip()
            logger.info("Intake: our OWN engine pick cannot do this and the customer delegated the "
                        "choice - selecting %s instead of %s, nothing they declared is touched - "
                        "job_id=%s", instead, eng, self._job_id)
            # THE SAME CONSENT RULE AS EVERY OTHER PICK WE MAKE ON A DELEGATION, not a new one:
            # `propose_engine_selection` already confirms an engine straight off a standing deferral
            # (`answers_the_selection_question`), and `st.choice_deferred` is that same standing
            # deferral. So this is recorded exactly as if the model had proposed this engine instead
            # of the other one - ours, on their instruction - and it is still ours, so a SECOND
            # impossibility on it would be swapped again rather than handed back.
            st.selection = es.propose(instead, session_id=st.session_id, owner_id=st.owner_id,
                                      revision=st.revision, user_msg_count=st.user_msg_count,
                                      chosen_by=es.CHOSEN_BY_US)
            st.selection = {**st.selection, "state": es.CONFIRMED,
                            "confirmed_revision": st.revision,
                            "expires_at": es.time.time() + es.CONFIRMED_TTL_S}
            st.pending = None
            st.invalidate_approval("the engine we picked could not do the job")
            eng, prev = instead, self._admission_of(instead, args)
            st.engine_swap = {"from": _vocab.to_display(_vocab.ENGINE, swapped_from),
                              "to": _vocab.to_display(_vocab.ENGINE, instead),
                              "because": refused_because}
            swap = {
                "engine_changed_from": _vocab.to_display(_vocab.ENGINE, swapped_from),
                "engine_changed_to": _vocab.to_display(_vocab.ENGINE, instead),
                "why_it_changed": refused_because,
                "guidance": (
                    f"{_vocab.to_display(_vocab.ENGINE, swapped_from)} cannot do this, and YOU picked "
                    f"it - the customer handed you the choice and never named an engine, so it is "
                    f"yours to change and nothing of theirs has been touched. "
                    f"{_vocab.to_display(_vocab.ENGINE, instead)} is now SELECTED: the catalog admits "
                    "it for exactly the purpose, geometry and patches they declared. Do NOT ask them "
                    "to confirm it and do NOT ask them to revise anything. Say in ONE line which "
                    "mesher you changed to and why, carry on in this same turn, and use "
                    f"{_vocab.to_display(_vocab.ENGINE, instead)} as the engine in every call from "
                    "here - including mesh_engine on submit_requirements."),
            }
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
                advanced=st.authorization_signature() != _before or bool(swap),
                content=json.dumps({"verdict": "supported", "preview_token": st.pending["token"],
                                    "selected_engine": eng,
                                    "canonical_summary": at.CONFIRM_REQUIREMENTS_ASK, **swap}))
        # incomplete / malformed (or impossible inside a comparison turn): READ-ONLY - no
        # authorizing token, standing state untouched.
        return IntakeToolResult(
            tool="preview_selected_admission", accepted=True, advanced=bool(swap),
            content=json.dumps({**{k: prev[k] for k in
                                   ("verdict", "selected_engine", "missing_fields",
                                    "safe_user_message") if k in prev},
                                "authorizes_submission": False, **swap}))

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

    async def _current_document(self) -> dict:
        """The stored measurement AS IT IS NOW, not as it was when this turn began.

        `st.geometry_document` is read once in `node_intake`, before the model has said anything, and
        the LOOK lands about twenty-five seconds after the customer says what the part is for - so
        for the whole turn in which it arrives, the copy in memory says `not_attempted` while the row
        says `ok`. Every survey composed from that copy is a survey of a part nobody has looked at.

        That is not a stale display, it is a wrong plan: `geometry_step._inputs` recomposes the survey
        from the CURRENT row and refuses when the result does not match field for field, so a survey
        composed from the stale copy is one the builder cannot reproduce. Measured across driven runs,
        the guard named `looked, marks, openings, uncertainties` - all of them look-derived - and the
        job silently lost its plan.

        Cheap: a row read by primary key. Falls back to the snapshot on any failure, because a
        conversation never fails on this.
        """
        try:
            from meshpipeline.cad.regions import stored_document_for_source

            if self.state.survey_source_ref is not None:
                fresh = await stored_document_for_source(self.state.survey_source_ref)
                if isinstance(fresh, dict) and fresh.get("status") == "ok":
                    self.state.geometry_document = fresh
        except Exception as exc:                   # noqa: BLE001 - never a turn
            logger.debug("Intake: could not re-read the measurement (%s)", exc)
        doc = self.state.geometry_document
        return doc if isinstance(doc, dict) else {}

    async def _survey_with_the_look(self, survey: dict | None) -> dict | None:
        """The survey recomposed once the look has landed, or the one given if it does not.

        The look is queued by the composition itself and written by a worker, so this polls the row.
        `LOOK_WAIT_SECONDS` is the whole budget: a look that takes longer than this is one the
        conversation stops waiting for, not one that fails - it lands later and the row keeps it.
        """
        from meshpipeline.application import geometry_survey as gs

        if not isinstance(survey, dict) or self.state.survey_source_ref is None:
            return survey
        if gs.look_state(survey) != gs.LOOK_PENDING:
            return survey
        waited = 0.0
        while waited < LOOK_WAIT_SECONDS:
            await asyncio.sleep(LOOK_POLL_SECONDS)
            waited += LOOK_POLL_SECONDS
            document = await self._current_document()
            if str((document.get("look") or {}).get("status") or "") in ("ok", "failed"):
                logger.info("Intake: the look landed after %.0fs; recomposing the survey from it - "
                            "job_id=%s", waited, self._job_id)
                try:
                    fresh = gs.recomposed(survey, document)
                    await gs.save(self.state.owner_id, str(self.state.survey_source_ref.source_id),
                                  fresh, session_id=self.state.session_id)
                    return fresh
                except Exception as exc:           # noqa: BLE001 - never a turn
                    logger.warning("Intake: could not recompose for the look (%s) - job_id=%s",
                                   exc, self._job_id)
                    return survey
        logger.info("Intake: the look has not landed in %.0fs; carrying on without it - job_id=%s",
                    LOOK_WAIT_SECONDS, self._job_id)
        return survey

    async def _do_survey_the_part(self, args: dict) -> IntakeToolResult:
        """Step 1 to step 4: the customer has said what the part is for; compose and return the questions."""
        refused = self._survey_refusal("survey_the_part")
        if refused is not None:
            return refused
        from meshpipeline.application import geometry_survey as gs

        st = self.state
        # THE CURRENT ROW, not the snapshot this turn began with - see `_current_document`. Composing
        # from the stale copy is what let a look-aware survey be overwritten by a pre-look one when
        # the model surveyed again mid-conversation, and the builder then refused the plan.
        document = await self._current_document()
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
        # WAIT FOR THE LOOK, RATHER THAN COMPOSING AROUND IT. Composing queues the look, which takes
        # about 25 seconds. Everything downstream - the questions the customer is asked, the plan the
        # geometry agent makes, the survey the builder recomposes - is built from the survey, so a
        # survey composed before the look is a survey of a part nobody has seen, and every later
        # reader disagrees with it. Re-reading the row at each writer made that survivable; waiting
        # here makes it impossible, and it is one place instead of a rule every caller must keep.
        #
        # It also makes the questions better. On a real part the look said all four openings were
        # "not a port" - which is the difference between asking which one is the inlet and saying
        # this is an external body with no ports.
        #
        # Bounded, and never fatal: past the wait the conversation carries on with what it has, which
        # is exactly what it did before this existed.
        st.geometry_survey = await self._survey_with_the_look(st.geometry_survey)
        if not st.surveyor_panel:
            from meshpipeline.agents.intake.geometry_brief import surveyor_panel

            st.surveyor_panel = surveyor_panel(await self._current_document(), st.geometry_survey)

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
                role=str(args.get("role") or ""), subject=str(args.get("mouth") or ""),
                words=str(args.get("customer_words_verbatim") or ""),
                latest_user_message=st.latest_user_msg, skipped=bool(args.get("skipped")),
                took_default=bool(args.get("took_default")), document=st.geometry_document,
                # THE CUSTOMER ACCEPTED WHAT THE APPLICATION PROPOSED. The value recorded is then the
                # application's own proposal and never the model's option: see
                # `geometry_survey._the_proposal_recorded`.
                accepted_proposal=bool(args.get("accepted_proposal")),
                # their OWN earlier messages, so a delegation given two turns ago is still quotable.
                # Only a deferral may be quoted from these; see `said_by_customer`.
                earlier_customer_messages=st.customer_messages)
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

    def _dispatch_gate(self, args: dict) -> list[str]:
        """Why this file may not be dispatched at all: the customer was asked and said send it back.

        Empty with the survey off and empty on every file whose measurement predicted no refusal. It reads the
        row `_survey_gate` has just refreshed rather than composing a second one, which is also what keeps it
        honest: a recomposition that no longer raises the question retires the answer, and a retired answer is
        the record of what was said and not a refusal of this submission.

        AND ONLY WHEN THE ROW IS ABOUT THIS SUBMISSION'S PURPOSE. `hexera.triage` predicts a refusal against a
        purpose and an engine, so "send it back" is an answer about meshing this file for THAT analysis. When
        `for_submission` composed nothing - a purpose the Surveyor does not compose for - the row on the state
        is the previous purpose's, and refusing on it would hold a submission with an answer about another job.
        """
        st = self.state
        if not st.survey_armed or not isinstance(st.geometry_survey, dict):
            return []
        composed = (st.geometry_survey.get("composed_for") or {}).get("purpose") or ""
        if str(composed) != str(args.get("purpose") or ""):
            return []
        from meshpipeline.application import geometry_survey as gs

        try:
            return gs.dispatch_refusals(st.geometry_survey)
        except Exception as exc:                   # noqa: BLE001 - never a turn
            logger.warning("Intake: the dispatch gate could not run (%s) - job_id=%s", exc, self._job_id)
            return []

    async def _geometry_step_gate(self, args: dict) -> list[str]:
        """STEP 5 AND THE THIRD INTAKE, at the one moment both belong: after the customer's answers and
        before the builder. The geometry agent plans the part here (once per set of answers), and a
        question only its plan can raise is put before the submission goes through. Fails open: with the
        step unarmed or failing, this is empty and the submission is judged as it always was."""
        st = self.state
        if not st.survey_armed or st.survey_source_ref is None or not isinstance(st.geometry_document, dict):
            # SAID, NOT SILENT. This is the one step that plans the part, and skipping it used to
            # leave no trace at all - so a job that never planned and a job whose plan raised nothing
            # were the same empty list and the same silent log. That is the collapse the look's three
            # states exist to prevent, in the step that matters most, and it made "did the geometry
            # agent run?" unanswerable after the fact.
            logger.info("Intake: step 5 NOT RUN - armed=%s source_ref=%s document=%s - job_id=%s",
                        st.survey_armed, st.survey_source_ref is not None,
                        isinstance(st.geometry_document, dict), self._job_id)
            return []
        from meshpipeline.application import geometry_step as gst
        from meshpipeline.application import geometry_survey as gs

        # THE LOOK LANDS AFTER THE SURVEY IS COMPOSED, AND THE SURVEY HAS TO CATCH UP. The look is
        # queued once the customer says what the part is for and takes about 25 seconds; the survey
        # is composed immediately, so the stored one is the survey as it was BEFORE anything was
        # looked at. `look_state`'s own docstring calls this "the state the race produces".
        #
        # Nothing closed the race at submission, and the questioning rewrite made the customer faster
        # than the renderer: measured on a real run, the plan was built against the pre-look survey
        # and the builder then threw it away, because `geometry_step._inputs` recomposes and demands
        # the result match field for field - and by then the look was in the document. The refusal
        # named exactly this once it was asked to: "fields that differ: looked, marks, uncertainties",
        # all three of them look-derived.
        #
        # `recomposed` is built for this - its own docstring lists "the look landed" first - and it
        # carries the customer's answers across for every question the new survey still raises. Doing
        # it here means the plan is made from a survey that HAS been looked at, and that the builder
        # can reproduce.
        try:
            # AND THE DOCUMENT HAS TO BE RE-READ FIRST. `st.geometry_document` is the snapshot taken
            # once at the top of the turn, before the model said anything - so on the very turn the
            # look lands it still says `not_attempted`, the comparison below finds nothing to do, and
            # the survey is never refreshed. That is why this fix did nothing the first time: the row
            # said "ok" and the copy in memory said "not_attempted".
            _doc_look = str(((await self._current_document()).get("look") or {}).get("status") or "")
            _composed = str(((st.geometry_survey or {}).get("composed_for") or {}).get("look_status") or "")
            if _doc_look and _composed != _doc_look and isinstance(st.geometry_survey, dict):
                logger.info("Intake: the look landed after the survey was composed (%s -> %s); "
                            "recomposing so the plan is made from it - job_id=%s",
                            _composed or "none", _doc_look, self._job_id)
                st.geometry_survey = gs.recomposed(st.geometry_survey, st.geometry_document)
                await gs.save(st.owner_id, str(st.survey_source_ref.source_id), st.geometry_survey,
                              session_id=st.session_id)
        except Exception as exc:                   # noqa: BLE001 - never a turn
            logger.warning("Intake: could not refresh the survey for the look (%s) - job_id=%s",
                           exc, self._job_id)

        # THE PLAN NEEDS THE ANSWERS, SO THE QUESTIONS HAVE TO HAVE BEEN PUT. `at_submission` returns
        # the state untouched when the Surveyor still has a question nobody has been asked - it will
        # not plan against answers it does not have - and it stores nothing, so the builder is handed
        # no plan and runs on its own. That is silent, and it became the NORMAL case when intake got
        # fast enough to propose everything and submit on one "go": measured on a real run, step 5
        # returned unplanned and the worker logged "the geometry agent did not plan this job".
        #
        # Refusing here costs no round trip in the ordinary case. A question that was PUT and then
        # skipped, or left to its default, is already settled - `open_now` excludes it - so the model
        # satisfies this by recording the answers it has just proposed, in the same turn, which is
        # what the questioning rules already tell it to do.
        _unput = gst.not_yet(st.geometry_survey)
        if _unput:
            logger.info("Intake: submit held - the Surveyor has %d question(s) never put, so the "
                        "geometry agent cannot plan - job_id=%s", len(_unput), self._job_id)
            return ["the Surveyor still has question(s) nobody has been asked, and the geometry agent "
                    "plans from their answers - without them the builder gets no plan at all. Put them "
                    "now, or record what you have already proposed for them with answer_survey_question "
                    "(quoting the customer's own agreement, or took_default/skipped if they left it to "
                    "you), then submit again: " + "; ".join(_unput)]

        try:
            state = await gst.at_submission(
                owner_id=st.owner_id, session_id=st.session_id, source_ref=st.survey_source_ref,
                state=st.geometry_survey, document=st.geometry_document,
                fidelity=str(args.get("mesh_fidelity") or "standard").strip().lower())
            if state is None:
                logger.info("Intake: step 5 armed but the geometry agent produced no state - "
                            "job_id=%s", self._job_id)
                return []
            st.geometry_survey = state
            # SAY WHICH, because `at_submission` returns the state unchanged on several paths that
            # plan nothing, and this line used to call every one of them "planned". It sent me
            # looking for a missing plan in the builder when the agent had never run.
            _step = state.get("geometry_step") if isinstance(state, dict) else None
            _status = _step.get("status") if isinstance(_step, dict) else None
            logger.info("Intake: step 5 returned - plan status=%s - job_id=%s",
                        _status or "NO PLAN MADE", self._job_id)
            # inside the try as well: reading the question back is as much a place to fall over as
            # making it, and a submission that dies here is a turn lost to a step that is off by default
            return gst.submission_problems(state)
        except Exception as exc:                   # noqa: BLE001 - never a turn
            logger.warning("Intake: the geometry agent's step could not run (%s) - job_id=%s", exc, self._job_id)
            return []

    def _what_is_being_confirmed(self, args: dict) -> str:
        """THE REQUIREMENTS THE ASK POINTS AT, composed by the application from the payload it authorised.

        `admission_token.CONFIRM_REQUIREMENTS_ASK` is "Please confirm the requirements above before I mesh
        anything", and until this function nothing above it was the requirements. The model's own setup was
        meant to be, and `loop_policy.close_out` keeps it where the model writes one - but it can only write
        one in the SAME message as the submit_requirements call, because `loop.runner` closes the turn out at
        the end of that round, and MEASURED on the turn that carries the reading, the setup and the plan at
        once, the model spent all four rounds on tool calls and wrote no text at all. The customer was shown
        the Surveyor's receipt, "MESHING WITH snappyHexMesh", and a sentence pointing at nothing.

        So the one thing the ask must not depend on is whether the model wrote prose. This is the payload
        that was just authorised - the same values `submit_args` carries to the builder - and it is
        deliberately short: what is being meshed, from what, and which face is which. The roles are the part
        a customer corrects, and a correction here is what `_corrected` and the re-submit path exist for.
        """
        rows: list[tuple[str, str]] = []
        purpose = _vocab.to_display(_vocab.PURPOSE, args.get("purpose"))
        kind = _vocab.to_display(_vocab.INPUT_KIND, args.get("input_kind"))
        dim = str(args.get("dimensionality") or "").strip()
        made_of = ", ".join(p for p in (kind, dim) if p)
        if purpose:
            rows.append(("for", purpose + (f", from a {made_of}" if made_of else "")))
        elif made_of:
            rows.append(("from", made_of))
        named = []
        for patch in (args.get("patches") or []):
            if not isinstance(patch, dict):
                continue
            role = str(patch.get("type") or "").strip()
            mouth = str(patch.get("opening_id") or "").strip()
            named.append(f"{patch.get('name')} ({role}{' on ' + mouth if mouth else ''})")
        if named:
            rows.append(("faces", ", ".join(named)))
        if not rows:
            return ""
        width = max(len(k) for k, _ in rows)
        return (chr(10).join(f"  {k.ljust(width)}  {v}" for k, v in rows)) + (chr(10) * 2)

    async def _do_submit_requirements(self, args: dict) -> IntakeToolResult:
        self._locate_named_ports(args)
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

        # A FILE THE CUSTOMER ASKED US TO SEND BACK IS NOT DISPATCHED. The Surveyor's first question is
        # whether this file gets through the mesher at all (`ask.schema.TIERS`, the `dispatch` tier), and the
        # product's own words for it are "I can send it back to you now rather than spend the run finding
        # out". Their answer was recorded and nothing read it, so the run was spent anyway. Empty with the
        # survey off and on every file whose measurement predicted no refusal.
        _send_back = self._dispatch_gate(args)
        if _send_back:
            st.submit_rejections += 1
            logger.warning("Intake: submit rejected - the customer asked for this file to be sent back: %s "
                           "- job_id=%s", _send_back, self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                "Submission rejected - " + "; ".join(_send_back) + ". Nothing was saved."))

        # THE GEOMETRY AGENT PLANS HERE, after every answer and before the builder, and the one question
        # only its plan can raise is put once before this goes through. Empty with the step off.
        _late = await self._geometry_step_gate(args)
        if _late:
            st.submit_rejections += 1
            logger.info("Intake: submit held for the third intake's question - job_id=%s", self._job_id)
            return IntakeToolResult(tool="submit_requirements", accepted=False, content=(
                "Submission held - " + "; ".join(_late) + ". Nothing was saved."))

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
        # THE GEOMETRY AGENT'S OWN WORDS TO THE CUSTOMER, AT THE ONE MOMENT THEY ARE WORTH READING.
        # `plan.summary_for_user` says what it concluded, what it assumed, and what it still needs -
        # and on a real job that last part was "the outlet o6 has only 1.2 D of run - extend it or
        # accept the mesh and not quote the o6 pressure drop". Sound engineering, written for the
        # customer, stored on the row, and shown to nobody. This is the last turn before compute is
        # spent, so it is the only turn where a caveat is still worth something.
        _agent_words = ""
        _step = (st.geometry_survey or {}).get("geometry_step") if isinstance(st.geometry_survey, dict) else None
        _plan = _step.get("plan") if isinstance(_step, dict) else None
        if isinstance(_plan, dict):
            _said = str(_plan.get("summary_for_user") or "").strip()
            if _said:
                _agent_words = (chr(10) * 2) + "WHAT THE GEOMETRY AGENT FOUND" + chr(10) + _said
            # AND WHAT IT FLAGGED, which was going nowhere at all. On one real job the agent raised
            # FOUR risks at severity high, effect changes_bc - "your port list names 'outlet_o1',
            # and no opening in this file is that size or in that place" - correctly catching a
            # declared port that matched no measured mouth, which is a boundary condition landing
            # on the wrong face. Nobody was ever shown one of them. A warning nobody reads is not a
            # warning, and this is the turn before compute is spent.
            #
            # Only what CHANGES something: high severity, or an effect that moves a boundary
            # condition or the mesh. An advisory at info is real but it is not worth a line here,
            # and a wall of them would be read as furniture.
            _loud = []
            for _r in (_plan.get("risks") or []):
                if not isinstance(_r, dict):
                    continue
                _sev = str(_r.get("severity") or "").lower()
                _eff = str(_r.get("effect") or "").lower()
                if _sev == "high" or _eff in ("changes_bc", "changes_mesh"):
                    _text = str(_r.get("consequence") or "").strip()
                    _fix = str(_r.get("recommendation") or "").strip()
                    if _text:
                        _loud.append("  - " + _text + (" -> " + _fix if _fix else ""))
            if _loud:
                _agent_words += (chr(10) * 2) + "WORTH KNOWING BEFORE I RUN THIS" + chr(10) + chr(10).join(_loud[:5])
        # WHAT IS NEW GOES ABOVE THE ASK, not between the ask and the question. The customer was
        # told "confirm the requirements above", then handed findings they had not read yet, then
        # asked to proceed - so the one line telling them to check something pointed backwards,
        # past the very thing worth checking.
        # AND WHICH MESHER, WHICH THIS SCREEN NEVER SAID. `engine_selection.answers_the_selection_
        # question` accepts "you decide" against a FRESH proposal, deliberately - re-asking a
        # decision the customer just handed over is a question with one answer - and it justifies
        # itself in writing: "the engine is named in the setup block they confirm before anything is
        # submitted". It was not. MEASURED on a structural run: the engine was confirmed from "you
        # decide" with the application's own "Selected engine: X" never shown, the only place Gmsh
        # appeared was a sentence the MODEL wrote, and this screen - the last one before compute is
        # spent - named no mesher at all. So the guarantee that justifies skipping the question was
        # not being kept by anything.
        #
        # Read off the confirmed selection, not the arguments: this is the engine the platform will
        # actually run, so a model that wrote a different name upstream is contradicted here rather
        # than agreed with.
        _eng = str((st.selection or {}).get("engine") or "").strip()
        _head = ""
        if _eng:
            _head = "MESHING WITH " + es.engine_label(_eng)
            _fid = str(args.get("mesh_fidelity") or "").strip()
            if _fid:
                _head += f" at {_fid} fidelity"
            # AND IF WE CHANGED IT OUT FROM UNDER OUR OWN PICK, THE CUSTOMER IS TOLD SO HERE.
            #
            # `_do_preview_selected_admission` may replace an engine that cannot do the job when the
            # engine was ours and they had delegated the choice. That is right - it is what stops
            # three of twelve parts dead-ending on an impossibility we created - but it was announced
            # only in a `swap` dict handed to the MODEL as guidance, and "an instruction to a model is
            # a request; anything required must be enforced where we control it". A customer who read
            # "Go with cfMesh?" and said go would otherwise meet "MESHING WITH Gmsh" with nothing
            # anywhere saying it had changed, or why.
            #
            # It is on THIS line because this is the sentence that cannot be skipped: the last screen
            # before compute, composed by the application and not by the model.
            if st.engine_swap:
                _head += " (I changed this from {}: {})".format(
                    st.engine_swap.get("from") or "the engine I first picked",
                    st.engine_swap.get("because") or "it could not mesh this setup")
            _head += chr(10) * 2
        st.submit_summary = (_head + self._what_is_being_confirmed(args)
                             + _agent_words.lstrip() + (chr(10) * 2 if _agent_words else "")
                             + at.CONFIRM_REQUIREMENTS_ASK
                             + (chr(10) * 2) + "Shall I proceed with mesh generation?")
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
        # THE MODEL'S SETUP RIDES IN THE SAME MESSAGE AS THIS CALL OR NOT AT ALL, and that is a fact about
        # the loop rather than a preference: `loop.runner` calls `close_out` at the end of the round that
        # authorised the submission, which returns the terminal and ends the turn, so there is no later
        # round in which prose could be written. Telling the model to write it "now" would be telling it to
        # do something in a turn that is already over.
        return IntakeToolResult(tool="submit_requirements", accepted=True, advanced=True, content=(
            "Authorized. The application appends its own summary - which mesher, the setup it is "
            "submitting, what the geometry agent found and flagged, and the one ask - UNDER whatever you "
            "wrote in THIS message. There is no further round: anything you meant to say alongside this "
            "call had to be in the same message as it. Do not paraphrase the summary and do not ask a "
            "question of your own. Await their explicit approval."))


__all__ = ["INTAKE_CATEGORIES", "MAX_SEARCH_CALLS", "MUTATING_TOOLS", "SURVEY_CATEGORIES",
           "IntakeExecutionState", "IntakeToolExecutor", "IntakeToolResult", "category_of"]
