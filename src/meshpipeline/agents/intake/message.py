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


class MessageStatus(str, enum.Enum):

    #: nothing gate-related happened - run the intake turn
    proceed = "proceed"
    #: the session is already linked to a running job; the message was still recorded
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
    #: the session does not exist for this owner
    not_found = "not_found"


class GateChange(str, enum.Enum):

    none = "none"
    approval_deferred = "approval_deferred"
    approval_invalidated = "approval_invalidated"
    unit_asked = "unit_asked"
    unit_answered = "unit_answered"


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
class MessageOutcome:

    status: MessageStatus
    transition: GateTransition = field(default_factory=GateTransition)
    #: the refreshed session, for the caller that goes on to run the intake turn
    session: Any = None
    #: a deterministic reply, when the authority answered without the model
    reply: str = ""
    job_id: Any = None
    awaiting_confirmation: bool = False

    @property
    def answered(self) -> bool:
        return self.status in (MessageStatus.approval_deferred, MessageStatus.unit_question,
                               MessageStatus.already_dispatched)

    @property
    def continues_to_intake(self) -> bool:
        return self.status in (MessageStatus.proceed, MessageStatus.approval_invalidated,
                               MessageStatus.unit_recorded)


def _revision(messages) -> str:
    from meshpipeline.agents.intake import admission_token as at
    return at.revision_of(messages)


async def accept(inbound: InboundMessage, *, session_repo, db_factory, logger) -> MessageOutcome:
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

        # A dispatched session is terminal for chat. The message is still recorded - the user
        # said it, so the transcript shows it - but nothing else happens.
        if locked.job_id:
            await db.commit()
            return MessageOutcome(
                status=MessageStatus.already_dispatched, job_id=locked.job_id,
                transition=GateTransition(revision=revision),
                reply=("Mesh generation is already running for this session. "
                       f"Job ID: {locked.job_id}"))

        gate = dict(getattr(locked, "intake_gate", None) or {})
        outcome = await _settle(inbound, db, gate=gate, locked=locked, messages=messages,
                                revision=revision, session_repo=session_repo, logger=logger)
        await db.commit()

    if outcome.status is MessageStatus.approve:
        # Dispatch is the approval authority's, and it takes the lock itself. Hand back the
        # session it must operate on, read after this transaction committed.
        async with db_factory() as db:
            fresh = await session_repo.get_for_owner(db, inbound.session_id, inbound.owner_id)
        return MessageOutcome(status=MessageStatus.approve, session=fresh,
                              transition=outcome.transition)

    if outcome.continues_to_intake:
        async with db_factory() as db:
            fresh = await session_repo.get_for_owner(db, inbound.session_id, inbound.owner_id)
        if fresh is None:
            return MessageOutcome(status=MessageStatus.not_found)
        return MessageOutcome(status=outcome.status, session=fresh,
                              transition=outcome.transition)
    return outcome


async def _settle(inbound: InboundMessage, db, *, gate: dict, locked, messages: list,
                  revision: str, session_repo, logger) -> MessageOutcome:
    approval = gate.get("approval")
    if ap.is_live(approval):
        intent = ap.classify(inbound.content)
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
        return unit
    return MessageOutcome(status=MessageStatus.proceed,
                          transition=GateTransition(revision=revision))


async def _settle_unit(inbound: InboundMessage, db, *, gate: dict, locked, revision: str,
                       session_repo, logger) -> MessageOutcome | None:
    if not uc.needs_confirmation(locked):
        return None

    asked_already = uc.already_asked(gate)
    unit = uc.classify(inbound.content) if asked_already else None
    if unit is not None:
        recorded = await uc.record(db, owner_id=inbound.owner_id,
                                   geometry_source_id=locked.geometry_source_id, unit=unit)
        await session_repo.bind_geometry_interpretation(
            db, inbound.session_id, uuid.UUID(recorded.interpretation_id))
        await session_repo.set_intake_gate(db, inbound.session_id, uc.answered(gate))
        logger.info("intake message: geometry scale confirmed as %s - session=%s",
                    unit.value, inbound.session_id)
        return MessageOutcome(
            status=MessageStatus.unit_recorded,
            transition=GateTransition(change=GateChange.unit_answered, revision=revision))

    # Either the question has not been put yet, or the answer was not one of the four. Both mean
    # the same thing: ask, and do not proceed.
    reply = uc.REFUSAL if asked_already else uc.QUESTION
    await session_repo.set_intake_gate(db, inbound.session_id, uc.asked(gate))
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


__all__ = ["GateChange", "GateTransition", "InboundMessage", "MessageOutcome", "MessageStatus",
           "accept", "persist_turn"]
