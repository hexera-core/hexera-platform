# Responsibility: Accept one inbound user message and settle its consequence for the gate.
# Owns: the inbound shape, the message outcome, the gate transition, and the durable turn.
# Boundaries: the message and its gate consequence are committed together.
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any

import meshpipeline.settings.policy as polcfg
from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import unit_clarification as uc


@dataclass(frozen=True)
class InboundMessage:

    session_id: uuid.UUID
    owner_id: str
    content: str
    #: The TENANT the durable consequences of this message are stamped with. Carried on the
    #: inbound message rather than passed alongside it because every write this turn can reach -
    #: the geometry interpretation below, and the job the approval authority creates - belongs to
    #: the same tenant as the message itself. Empty for a principal that names no organisation,
    #: which stamps owner_id alone; see persistence/repositories/tenant_scope.
    organization_id: str = ""


class MessageStatus(str, enum.Enum):

    #: nothing gate-related happened - run the intake turn
    proceed = "proceed"
    #: the session's run is still in flight; the message was recorded and nothing else happens.
    #: Once that run ends the same session takes the next message as the start of another run.
    already_dispatched = "already_dispatched"
    #: an unambiguous approval - the route hands off to the approval authority
    approve = "approve"
    #: an ambiguous reply to a standing approval; asked once, deterministically
    approval_deferred = "approval_deferred"
    #: a correction or rejection; the snapshot can never dispatch again - then run the turn
    approval_invalidated = "approval_invalidated"
    #: the physical scale is unsettled; the question (or the refusal) is the whole reply
    unit_question = "unit_question"
    #: the user named a unit; it is durably bound - then run the turn
    unit_recorded = "unit_recorded"
    #: the user said the file is in another unit than the one settled: it is recorded, every size
    #: derived under the old one is re-read, and the reply says so - no model turn
    unit_changed = "unit_changed"
    #: the geometry check is drawing the part: the turn is answered with a holding line
    geometry_hold = "geometry_hold"
    #: the session does not exist for this owner
    not_found = "not_found"


class GateChange(str, enum.Enum):

    none = "none"
    approval_deferred = "approval_deferred"
    approval_invalidated = "approval_invalidated"
    unit_asked = "unit_asked"
    unit_answered = "unit_answered"
    unit_changed = "unit_changed"


@dataclass(frozen=True)
class GateTransition:

    change: GateChange = GateChange.none
    revision: str = ""
    approval_status: str = ""
    reason: str = ""

    @property
    def invalidated_approval(self) -> bool:
        return self.change is GateChange.approval_invalidated


@dataclass(frozen=True)
class PreviousRun:
    """The run this conversation already made, now over. Carried to the intake turn so the
    model can propose 'the same again' from what that run was approved with and say how it
    ended; it records nothing - every value is a proposal until the user confirms it and the
    approval authority takes a FRESH approval for the new run."""

    job_id: str
    #: the job's status value ("succeeded", "failed", ...); empty when the row is gone
    status: str = ""
    #: the verdict the user was shown for that run, as application code rendered it
    outcome: str = ""

    def as_state(self) -> dict:
        return {"job_id": self.job_id, "status": self.status, "outcome": self.outcome}


@dataclass(frozen=True)
class MessageOutcome:

    status: MessageStatus
    transition: GateTransition = field(default_factory=GateTransition)
    #: the refreshed session, for the caller that goes on to run the intake turn
    session: Any = None
    #: this turn handed the user's words to the geometry naming (so a failed commit withdraws it)
    queued_naming: bool = False
    #: a deterministic reply, when the authority answered without the model
    reply: str = ""
    job_id: Any = None
    awaiting_confirmation: bool = False
    #: set when this conversation has already run once and that run is over: the intake turn
    #: that follows is the start of another run on the same geometry
    previous_run: PreviousRun | None = None

    @property
    def answered(self) -> bool:
        return self.status in (MessageStatus.approval_deferred, MessageStatus.unit_question,
                               MessageStatus.already_dispatched, MessageStatus.geometry_hold,
                               MessageStatus.unit_changed)

    @property
    def continues_to_intake(self) -> bool:
        return self.status in (MessageStatus.proceed, MessageStatus.approval_invalidated,
                               MessageStatus.unit_recorded)


def _revision(messages) -> str:
    from meshpipeline.agents.intake import admission_token as at
    return at.revision_of(messages)


#: The answer while this session's run is still in flight. It says what the user CAN do; the
#: line it replaces ("already running") named nothing, and stood forever - it was the answer to
#: every message for the rest of the session's life, hours after the run had ended.
STILL_RUNNING_REPLY = (
    "Mesh generation is still running for this session (job {job_id}). You can watch it here. "
    "Once it finishes, tell me in this chat whether to run the same requirements again or what "
    "to change, and I will set up the next run. To mesh a different file, start a new session.")


@dataclass(frozen=True)
class _LastRun:
    """The run this session's last approval created, read under the row lock."""

    job_id: uuid.UUID
    status: str
    outcome: str

    @property
    def live(self) -> bool:
        from meshpipeline.persistence.job_state import is_active
        return is_active(self.status)

    @property
    def previous(self) -> PreviousRun:
        return PreviousRun(job_id=str(self.job_id), status=self.status, outcome=self.outcome)


def _dispatched_job_id(gate: dict) -> uuid.UUID | None:
    """The job the gate's approval snapshot dispatched, if it ever did. Only the approval
    transaction writes `job_id` onto a snapshot (agents/intake/approval.py), and a later
    invalidation keeps it, so its presence - not the status - is the record that a run was made.
    A fresh snapshot from a new submission carries none."""
    raw = (gate.get("approval") or {}).get("job_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _outcome_of(job) -> str:
    """What the user was told when that run ended, from its durable final result."""
    stored = getattr(job, "final_result", None) if job is not None else None
    if not stored:
        return ""
    try:
        from meshpipeline.application.final_result import FinalResult, render_message
        return render_message(FinalResult.from_dict(dict(stored)))[:1500]
    except Exception:                                # noqa: BLE001 - context, never a gate
        return str(stored.get("outcome_code") or "")


async def _last_run(db, *, locked, gate: dict, job_repo) -> _LastRun | None:
    """The session's last run: the one it is linked to while it holds a link, else the one its
    dispatched snapshot records (the link is released once that run has ended; see `accept`).
    Read by id, unscoped: the id comes off a session row already proven to be this owner's, and
    a job is only ever linked to the session that approved it - the link IS the scoping."""
    job_id = getattr(locked, "job_id", None) or _dispatched_job_id(gate)
    if not job_id:
        return None
    if job_repo is None:
        from meshpipeline.persistence.repositories.job_repository import JobRepository
        job_repo = JobRepository()
    job = await job_repo.get_internal(db, job_id)
    raw_status = getattr(job, "status", None) if job is not None else None
    status = str(getattr(raw_status, "value", raw_status) or "")
    return _LastRun(job_id=job_id, status=status, outcome=_outcome_of(job))


async def accept(inbound: InboundMessage, *, session_repo, db_factory, logger,
                 job_repo=None) -> MessageOutcome:
    async with db_factory() as db:
        locked = await session_repo.get_for_update(db, inbound.session_id)
        if locked is None or locked.owner_id != inbound.owner_id:
            # No write, no lock held for long, and deliberately indistinguishable from "no such
            # session": a caller must not learn that someone else's session exists.
            return MessageOutcome(status=MessageStatus.not_found)

        # Snapshot the transcript BEFORE the append. `append_message` re-reads the same row
        # through the identity map, so it mutates `locked.messages` in place - reading it
        # afterwards and appending again would count this message twice and produce a revision
        # that matches no conversation that ever existed.
        messages = list(getattr(locked, "messages", None) or [])
        await session_repo.append_message(db, inbound.session_id, "user", inbound.content)
        messages.append({"role": "user", "content": inbound.content})
        revision = _revision(messages)

        gate = dict(getattr(locked, "intake_gate", None) or {})
        # A SESSION WITH A RUN. While that run is in flight the session is closed to everything
        # but this answer: the message is still recorded - the user said it, so the transcript
        # shows it - but nothing else happens, and above all nothing can dispatch. Once the run
        # has ended the session moves on: the link to it is released, under this same lock and in
        # this same transaction, so the approval authority later sees a session with no run -
        # exactly what it saw before the first approval - and the next run needs a fresh
        # submission and a fresh approval like any other. The ended run is carried to the intake
        # turn as context, not as state: the model proposes from it and records nothing.
        previous: PreviousRun | None = None
        run = await _last_run(db, locked=locked, gate=gate, job_repo=job_repo)
        if run is not None and run.live:
            await db.commit()
            return MessageOutcome(
                status=MessageStatus.already_dispatched, job_id=run.job_id,
                transition=GateTransition(revision=revision),
                reply=STILL_RUNNING_REPLY.format(job_id=run.job_id))
        if run is not None:
            previous = run.previous
            if getattr(locked, "job_id", None):
                await session_repo.release_job(db, inbound.session_id)
                logger.info("intake message: run %s has ended (%s) - session %s moves on to "
                            "another run", run.job_id, run.status or "record gone",
                            inbound.session_id)

        outcome = await _settle(inbound, db, gate=gate, locked=locked, messages=messages,
                                revision=revision, session_repo=session_repo, logger=logger)
        try:
            await db.commit()
        except Exception:
            if outcome.queued_naming:
                # the naming was queued for a turn that will not exist: let the next one hold.
                # A wait-only turn queued nothing, and must not withdraw an earlier turn's request.
                from meshpipeline.application import geometry_hold as gh
                gh.withdraw_naming(str(inbound.session_id))
            raise

    if outcome.status is MessageStatus.approve:
        # Dispatch is the approval authority's, and it takes the lock itself. Hand back the
        # session it must operate on, read after this transaction committed.
        async with db_factory() as db:
            fresh = await session_repo.get_for_owner(db, inbound.session_id, inbound.owner_id)
        return MessageOutcome(status=MessageStatus.approve, session=fresh,
                              transition=outcome.transition, previous_run=previous)

    if outcome.continues_to_intake:
        async with db_factory() as db:
            fresh = await session_repo.get_for_owner(db, inbound.session_id, inbound.owner_id)
        if fresh is None:
            return MessageOutcome(status=MessageStatus.not_found)
        return MessageOutcome(status=outcome.status, session=fresh,
                              transition=outcome.transition, previous_run=previous)
    return outcome


async def _settle(inbound: InboundMessage, db, *, gate: dict, locked, messages: list,
                  revision: str, session_repo, logger) -> MessageOutcome:
    changed = await _settle_unit_change(inbound, db, locked=locked, messages=messages,
                                        revision=revision, session_repo=session_repo, logger=logger)
    if changed is not None:
        return changed
    approval = gate.get("approval")
    if ap.is_live(approval):
        intent = ap.classify(inbound.content)
        if uc.needs_confirmation(locked):
            # THE UNIT BEFORE THE RUN. A run cannot start on a file whose scale nobody has named
            # (application/dispatch_contract refuses the pair), so an approval given while the
            # question is open is held one turn on it: an answer in the same breath ("ok" to the
            # proposal) settles it and the approval stands; anything else is asked the question,
            # with the proposal, and the approval's expected turn moves along with it. A unit
            # named without a yes is recorded, and the approval waits one more turn for the yes.
            unit = await _settle_unit(inbound, db, gate=gate, locked=locked, revision=revision,
                                      session_repo=session_repo, logger=logger,
                                      insist=intent == ap.APPROVE_INTENT)
            if unit is not None and unit.status is MessageStatus.unit_question:
                return unit
            if unit is not None and intent != ap.APPROVE_INTENT:
                answered_gate = uc.answered(gate)
                answered_gate["approval"] = ap.defer(approval)
                await session_repo.set_intake_gate(db, inbound.session_id, answered_gate)
                named = uc.classify(inbound.content, uc.proposed(gate))
                reply = f"{uc.noted(named)} {ap.CLARIFICATION}" if named else ap.CLARIFICATION
                await session_repo.append_message(db, inbound.session_id, "assistant", reply)
                return MessageOutcome(
                    status=MessageStatus.approval_deferred, reply=reply, awaiting_confirmation=True,
                    transition=GateTransition(change=GateChange.approval_deferred, revision=revision,
                                              approval_status=ap.AWAITING))
        if intent == ap.APPROVE_INTENT:
            logger.info("intake message: application-owned approval - session=%s snapshot=%s",
                        inbound.session_id, (approval or {}).get("id"))
            return MessageOutcome(status=MessageStatus.approve,
                                  transition=GateTransition(revision=revision,
                                                            approval_status=ap.AWAITING))
        if intent == ap.HEDGE_INTENT:
            # Ambiguous: never dispatch, never silently invalidate - ask once, deterministically,
            # and move the snapshot's expected confirmation turn along with the question so the
            # user's next clear answer still lands on it.
            gate["approval"] = ap.defer(approval)
            await session_repo.set_intake_gate(db, inbound.session_id, gate)
            await session_repo.append_message(db, inbound.session_id, "assistant",
                                              ap.CLARIFICATION)
            return MessageOutcome(
                status=MessageStatus.approval_deferred, reply=ap.CLARIFICATION,
                awaiting_confirmation=True,
                transition=GateTransition(change=GateChange.approval_deferred, revision=revision,
                                          approval_status=ap.AWAITING))
        # A correction or rejection: the stored snapshot can never dispatch again. Keep it as
        # audit evidence, marked invalidated, and hand the turn back to intake for a fresh
        # proposal -> preview -> summary -> approval cycle.
        reason = f"user replied with a {intent}"
        gate["approval"] = ap.invalidate(approval, reason)
        await session_repo.set_intake_gate(db, inbound.session_id, gate)
        logger.info("intake message: pending approval invalidated by a %s - session=%s",
                    intent, inbound.session_id)
        settled = GateTransition(change=GateChange.approval_invalidated, revision=revision,
                                 approval_status=ap.INVALIDATED, reason=reason)
        unit = await _settle_unit(inbound, db, gate=gate, locked=locked, revision=revision,
                                  session_repo=session_repo, logger=logger)
        if unit is not None:
            return unit
        return MessageOutcome(status=MessageStatus.approval_invalidated, transition=settled)

    unit = await _settle_unit(inbound, db, gate=gate, locked=locked, revision=revision,
                              session_repo=session_repo, logger=logger)
    if unit is not None:
        if unit.status is MessageStatus.unit_recorded:
            # the unit answer may be the first answer the geometry check waits for
            hold = await _settle_geometry_hold(inbound, db, locked=locked, messages=messages,
                                               revision=revision, session_repo=session_repo,
                                               logger=logger)
            if hold is not None:
                return hold
        return unit
    if uc.needs_confirmation(locked):
        # THE NAMING WAITS FOR THE UNIT. The user's words go to the model only once the file's
        # scale is known: the naming re-reads every measurement in that unit, and the stage
        # confirms nothing without it. Until then the intake answers as it always did.
        return MessageOutcome(status=MessageStatus.proceed,
                              transition=GateTransition(revision=revision))
    hold = await _settle_geometry_hold(inbound, db, locked=locked, messages=messages,
                                       revision=revision, session_repo=session_repo, logger=logger)
    if hold is not None:
        return hold
    return MessageOutcome(status=MessageStatus.proceed,
                          transition=GateTransition(revision=revision))


async def _settle_geometry_hold(inbound: InboundMessage, db, *, locked, messages: list,
                                revision: str, session_repo, logger) -> MessageOutcome | None:
    """THE GEOMETRY CHECK'S TURN, taken here because it must be decided under the same row lock
    as every other turn: two answers sent at once must not both find 'nobody asked the model
    yet' and queue two namings with two holding lines. The first answer that reaches the intake
    hands the user's words to the naming step; the conversation holds while the part is drawn,
    and the intake resumes when the user proceeds on the stage."""
    from meshpipeline.application import geometry_hold as gh

    session_id = str(inbound.session_id)
    decision = gh.hold_applies(session_id)
    if not decision:
        return None
    if decision == "wait":
        # THE NAMING IS ALREADY RUNNING: a message sent meanwhile is answered with the wait line
        # and nothing else, so the intake cannot ask past the stage. Bounded by WAIT_GRACE_S.
        reply = gh.WAIT_REPLY
        await session_repo.append_message(db, inbound.session_id, "assistant", reply)
        logger.info("intake message: still held for the geometry check - session=%s", inbound.session_id)
        return MessageOutcome(status=MessageStatus.geometry_hold, reply=reply,
                              transition=GateTransition(revision=revision))
    interpretation = await gh.interpretation_payload(db, locked, inbound.owner_id,
                                                     inbound.organization_id)
    if not gh.queue_naming(session_id, inbound.owner_id, gh.purpose_from(messages), interpretation):
        return None
    try:
        await session_repo.append_message(db, inbound.session_id, "assistant", gh.HOLD_REPLY)
    except Exception:
        # the turn will not be stored: withdraw the marker so the next turn can hold again
        gh.withdraw_naming(session_id)
        raise
    logger.info("intake message: held for the geometry check - session=%s", inbound.session_id)
    return MessageOutcome(status=MessageStatus.geometry_hold, reply=gh.HOLD_REPLY, queued_naming=True,
                          transition=GateTransition(revision=revision))


async def _settle_unit_change(inbound: InboundMessage, db, *, locked, messages: list, revision: str,
                              session_repo, logger) -> MessageOutcome | None:
    """A UNIT CHANGED AFTER IT WAS SETTLED. "The file is in metres", said once the sizes were
    confirmed as millimetres, is recorded as the user's unit and every length derived under the
    old one is re-read by the application - the declaration the intake reads, the declared
    patches - and a run proposed with the old sizes is withdrawn. Nothing is left to the model
    to convert, which is where a unit named in the chat used to spiral: the model relabelled
    the numbers itself while the geometry kept the old scale. None when the message states no
    unit, or the one already held."""
    if uc.needs_confirmation(locked) or not getattr(locked, "geometry_source_id", None):
        return None
    unit = uc.stated_unit(inbound.content)
    if unit is None:
        return None
    from meshpipeline.application.unit_change import change_unit

    change = await change_unit(db, locked, owner_id=inbound.owner_id,
                               organization_id=inbound.organization_id, unit=unit,
                               session_repo=session_repo, where="chat")
    if change is None:
        return None
    if change.gate is not None:
        # the proposal made with the old sizes can never dispatch; the gate is this authority's
        await session_repo.set_intake_gate(db, inbound.session_id, change.gate)
    logger.info("intake message: the file's unit changed %s -> %s - session=%s",
                change.old_unit, change.new_unit, inbound.session_id)
    if not change.reread:
        # nothing was confirmed on the picture yet: the geometry check may be waiting for this
        # very answer, and the naming now reads the part in the unit just named
        hold = await _settle_geometry_hold(inbound, db, locked=locked, messages=messages,
                                           revision=revision, session_repo=session_repo, logger=logger)
        if hold is not None:
            return hold
    reply = change.reply()
    await session_repo.append_message(db, inbound.session_id, "assistant", reply)
    return MessageOutcome(
        status=MessageStatus.unit_changed, reply=reply,
        transition=GateTransition(
            change=GateChange.approval_invalidated if change.approval_withdrawn else GateChange.unit_changed,
            revision=revision,
            approval_status=ap.INVALIDATED if change.approval_withdrawn else "",
            reason="the file's unit changed" if change.approval_withdrawn else ""))


async def _settle_unit(inbound: InboundMessage, db, *, gate: dict, locked, revision: str,
                       session_repo, logger, insist: bool = False) -> MessageOutcome | None:
    """The unit turn. `insist` is the approval's: the run cannot start without the unit, so a
    reply that names none is asked the question again rather than let through."""
    if not uc.needs_confirmation(locked):
        return None

    asked_already = uc.already_asked(gate)
    proposal = uc.proposed(gate) if asked_already else None
    unit = uc.classify(inbound.content, proposal) if asked_already else None
    if unit is not None:
        # STAMPED WITH BOTH, exactly as api/v1/upload.py stamps the interpretation it writes for a
        # file-declared unit. The two paths write the same table for the same reason; an
        # interpretation recorded here without its organisation is invisible to the org-scoped
        # read that the dispute route makes of it later.
        recorded = await uc.record(db, owner_id=inbound.owner_id,
                                   geometry_source_id=locked.geometry_source_id, unit=unit,
                                   organization_id=inbound.organization_id)
        await session_repo.bind_geometry_interpretation(
            db, inbound.session_id, uuid.UUID(recorded.interpretation_id))
        await session_repo.set_intake_gate(db, inbound.session_id, uc.answered(gate))
        logger.info("intake message: geometry scale confirmed as %s - session=%s",
                    unit.value, inbound.session_id)
        return MessageOutcome(
            status=MessageStatus.unit_recorded,
            transition=GateTransition(change=GateChange.unit_answered, revision=revision))

    if asked_already and not insist:
        # A REPLY THAT NAMES NO UNIT IS AN ORDINARY TURN - a question about the question, "not
        # sure", anything else - and the intake answers it; asking again trapped the
        # conversation. The question stays open on the gate: the next reply that names a unit
        # settles it, and an approval given before then is held on the question (see _settle),
        # because the run cannot start without it.
        return None

    # THE QUESTION PROPOSES when the part has been measured: millimetres, with the part's size
    # in every candidate unit beside it, so the user can see which one is theirs and "ok" is an
    # answer. Without a measurement it is the plain question, and nothing is proposed that the
    # user could not check.
    from meshpipeline.application import geometry_hold as gh

    size = gh.measured_size_mm(str(inbound.session_id))
    if size:
        # millimetres unless the part would then be implausible - by its size, or by what the
        # user has said it is (a 1.04-unit car, a 229-unit city block)
        proposal = uc.proposal_for(size, gh.purpose_from(getattr(locked, "messages", None)))
    reply = uc.before_run(size, proposal) if insist else uc.question_for(size, proposal)
    if insist and ap.is_live(gate.get("approval")):
        # the approval waits one turn on the question: the next clear yes still lands on it
        gate = dict(gate, approval=ap.defer(gate.get("approval")))
    await session_repo.set_intake_gate(db, inbound.session_id, uc.asked(gate, proposal))
    await session_repo.append_message(db, inbound.session_id, "assistant", reply)
    return MessageOutcome(
        status=MessageStatus.unit_question, reply=reply, awaiting_confirmation=True,
        transition=GateTransition(change=GateChange.unit_asked, revision=revision))


#: The turn's durable requirements: result key -> repository setter. A table rather than eleven
#: near-identical `if` blocks, so adding a requirement cannot silently skip its write.
_REQUIREMENT_WRITES: tuple[tuple[str, str], ...] = (
    ("request_txt", "set_request_txt"),
    ("review_brief_txt", "set_review_brief_txt"),
    ("intake_patches", "set_intake_patches"),
    ("dimensionality", "set_dimensionality"),
    ("requested_mesh_fidelity", "set_requested_mesh_fidelity"),
    ("purpose", "set_purpose"),
    ("input_kind", "set_input_kind"),
    ("mesh_engine", "set_mesh_engine"),
    ("domain", "set_task_label"),
    ("engine_params", "set_engine_params"),
)

#: Event channels the turn can produce, in the order they must be appended.
_EVENT_KEYS = ("_intake_training_event", "_intake_agent_run_event")


async def persist_turn(inbound: InboundMessage, result: dict, *, session_repo,
                       db_factory) -> None:
    async with db_factory() as db:
        # A FAILED TURN SAID NOTHING, so it leaves no assistant message. The authority returns a
        # classified marker and no reply, and appending one anyway stored a blank turn that the
        # history read served back and that the next attempt handed to the model as conversation.
        # Every other assistant append here carries real text, and an approval system failure
        # appends nothing at all. The user's own message stays either way - `accept` committed it
        # before the turn ran, deliberately, because they said it.
        if not result.get("api_failure"):
            await session_repo.append_message(db, inbound.session_id, "assistant",
                                              _reply_of(result))
        for key, setter in _REQUIREMENT_WRITES:
            value = result.get(key)
            if value:
                await getattr(session_repo, setter)(db, inbound.session_id, value)
        if "intake_gate" in result:
            # Empty/None clears consumed or invalidated authorization; a dict stores the current
            # engine selection and admission preview token.
            await session_repo.set_intake_gate(db, inbound.session_id, result.get("intake_gate"))
        # Training material, not run state: the approval snapshot carries everything dispatch
        # and resume need, so a deployment that collects nothing stores nothing here.
        if polcfg.MODES.data_collection_enabled:
            for key in _EVENT_KEYS:
                event = result.get(key)
                if event and event.get("payload"):
                    await session_repo.append_intake_event(db, inbound.session_id, event)
        # THE TURN'S PUBLIC TRACE. Already projected for this deployment's mode, so a safe-mode
        # server never writes reasoning text onto a session in the first place. Stored on the same
        # channel as Intake's other records - no second storage path, and it is re-projected on
        # the way out (see project_all).
        for event in result.get("_public_trace", []) or []:
            await session_repo.append_intake_event(db, inbound.session_id, event)
        for event in result.get("_intake_search_events", []) or []:
            # web_search runs during intake happen before a job exists; they ride the same channel
            # as intake events and land in events.jsonl at dispatch.
            event.setdefault("payload", {})["phase"] = "intake"
            await session_repo.append_intake_event(db, inbound.session_id, event)
        await db.commit()


def _reply_of(result: dict) -> str:
    from meshpipeline.agents.intake.agent import _extract_reply
    return _extract_reply(result)


__all__ = ["STILL_RUNNING_REPLY", "GateChange", "GateTransition", "InboundMessage",
           "MessageOutcome", "MessageStatus", "PreviousRun", "accept", "persist_turn"]
