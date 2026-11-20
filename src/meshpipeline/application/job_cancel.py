# Responsibility: End a job on its owner's request, before the worker would have.
# Owns: what may be cancelled, the eviction of the running worker's ownership, the terminal
#       `cancelled` record with its announcement, and the withdrawal of a launch still queued.
# Boundaries: it writes the ONE terminal transaction of a cancelled job; from that commit on the
#             worker's own terminal path is refused by the fence and by the transition table. It runs
#             no pipeline and stops no native process - a worker mid-mesh learns at its next fence,
#             and one waiting on a Cloud Run execution notices the cleared token within about half a
#             minute and cancels that execution itself (application/native_submission).
from __future__ import annotations

import asyncio
import enum
import logging
import uuid
from dataclasses import dataclass

from meshpipeline.application import final_result as _fr
from meshpipeline.contracts.pipeline_state import checkpoint_thread
from meshpipeline.persistence.job_state import TERMINAL_STATES, TransitionResult
from meshpipeline.persistence.lease import LeaseRepository
from meshpipeline.persistence.models import JobStatus
from meshpipeline.persistence.repositories.job_repository import JobRepository
from meshpipeline.persistence.repositories.terminal_outbox_repository import TerminalOutboxRepository

log = logging.getLogger(__name__)

#: How much of the owner's reason is kept. The route's schema refuses more; this is the durable
#: bound the column was sized for, applied again here so no caller can exceed it.
REASON_MAX = 500

#: How long the checkpoint read below may take, and WHY IT IS BOUNDED AT ALL. It runs inside
#: the cancel's own transaction, while this job's row is locked - that placement is forced,
#: because the generation it must read comes off the locked row and the record it feeds is
#: written in the same commit. So an unreachable checkpoint store must not be able to hold a
#: user's row lock for as long as its own connect timeout: the gate's name is worth a few
#: seconds and never worth the cancel.
CHECKPOINT_READ_TIMEOUT_S = 5.0


async def read_checkpoint_state(job_id, *, generation: int = 0) -> dict:
    """What this generation's durable checkpoint holds, as a plain dict.

    A SEPARATE SAVER AND NOT THE RUN'S OWN GRAPH. The worker that wrote this checkpoint is
    another process - being evicted by the caller - so there is no live binding to borrow;
    the facts are in PostgreSQL and this opens its own reader for them. No `cp.setup()`: a
    cancel is no time for migration DDL, and absent tables simply read as nothing.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    import meshpipeline.settings.providers as provcfg
    from meshpipeline.pipeline.graph import build_graph

    dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    config = {"configurable": {"thread_id": checkpoint_thread(str(job_id), generation)}}
    async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
        snap = await build_graph(checkpointer=cp).aget_state(config)
    return dict(getattr(snap, "values", None) or {})


class CancelResult(str, enum.Enum):
    cancelled = "cancelled"                    # this call ended the job
    already_cancelled = "already_cancelled"    # a repeat: nothing changed, the same answer
    already_finished = "already_finished"      # succeeded or failed first - nothing to cancel
    not_found = "not_found"                    # no such job for this owner; a foreign one reads the same


@dataclass(frozen=True)
class CancelOutcome:
    result: CancelResult
    status: JobStatus | None
    cancel_reason: str | None = None


async def cancel_job(job_id: uuid.UUID, *, owner_id: str, organization_id: str = "",
                     reason: str = "", session_factory=None,
                     job_repo: JobRepository | None = None,
                     lease_repo: LeaseRepository | None = None,
                     outbox_repo: TerminalOutboxRepository | None = None) -> CancelOutcome:
    if session_factory is None:
        from meshpipeline.persistence.session import get_db as session_factory  # noqa: N813
    job_repo = job_repo or JobRepository()
    lease_repo = lease_repo or LeaseRepository()
    outbox_repo = outbox_repo or TerminalOutboxRepository()
    reason = (reason or "").strip()[:REASON_MAX]

    async with session_factory() as db:
        # SCOPED AND LOCKED. The status this decides on is held until the commit, so a worker
        # finalizing at the same moment either takes the lock first (the job is then terminal and
        # this refuses it) or waits, and then finds a cancelled row its CAS cannot overwrite.
        row = await job_repo.lock_for_owner(db, job_id, owner_id, organization_id=organization_id)
        if row is None:
            return CancelOutcome(CancelResult.not_found, None)
        if row.status == JobStatus.cancelled:
            return CancelOutcome(CancelResult.already_cancelled, row.status, row.cancel_reason)
        if row.status in TERMINAL_STATES:
            return CancelOutcome(CancelResult.already_finished, row.status)
        # WHAT THIS MODULE LOGS AND PASSES ON: the row's OWN id, read back from the database, never
        # the id the caller sent. They are equal by the WHERE clause, but only the first is a value
        # this code owns - the second arrived in a request and has no business in an operator log.
        row_id = uuid.UUID(str(row.id))

        # 1. EVICT THE WORKER. Revoke the mirror its token authorises and clear the token, under
        #    the lock, so from this commit on every fence check the running worker makes fails:
        #    its checkpoint writes, its native accept, its artifact delivery, and the row lock at
        #    its atomic terminal transaction. Nothing it does after this reaches the user.
        evicted = await lease_repo.evict_owner(db, row_id)
        was_parked = row.status == JobStatus.pending_review

        # 2. THE TERMINAL CAS, through the one transition table. It is what refuses the worker's
        #    late succeeded/failed afterwards: a terminal state is never a legal source.
        tr = await job_repo.transition(db, row_id, JobStatus.cancelled)
        if tr is not TransitionResult.applied:
            # The row was locked and non-terminal a moment ago, so this cannot happen without a
            # defect in the table; refuse loudly rather than record a cancel that did not apply.
            raise RuntimeError(f"cancel of job {row_id} did not apply (transition={tr.value})")
        row.cancel_reason = reason or None
        if was_parked:
            # A cancelled parked run has nothing left to decide: close its park in the same commit
            # so no decision can land on it afterwards.
            from meshpipeline.persistence.repositories.repair_review_repository import (
                RepairReviewRepository,
            )
            _reviews = RepairReviewRepository()
            park = await _reviews.open_park(db, row_id)
            if park is not None:
                await _reviews.close_park(db, park.id, "cancel")

        # 3. THE DURABLE VERDICT, from everything that is durable: the approved intent on the
        #    dispatch payload, the row's own attempt counter, AND this generation's
        #    checkpoint. Rendered by the same vocabulary the worker's terminal record uses,
        #    so the status page and the socket read one truth.
        #
        #    THE CHECKPOINT IS READ BECAUSE THE RUN MAY ALREADY HAVE ESTABLISHED SOMETHING.
        #    Job 6ddb58aa's mesh was refused by `quality_floor` and the owner then cancelled;
        #    the checkpoint held the gate and the record said "". A cancel is not a failure
        #    and this does not make it one (build_cancelled_result keeps `failure_category`
        #    None and the closing line never reads the gate) - it stops the record claiming
        #    the run knew nothing when it knew exactly why it had stopped.
        #
        #    AND IT NEVER DECIDES THE CANCEL. The owner asked; a store we cannot reach costs
        #    the gate's name and nothing else, so every failure here is logged and carried as
        #    an absent fact. `generation` comes off the locked row, so the thread read is this
        #    execution's and never a previous one's (contracts/pipeline_state.checkpoint_thread).
        ckpt: dict = {}
        try:
            ckpt = await asyncio.wait_for(
                read_checkpoint_state(str(row_id),
                                      generation=int(row.execution_generation or 0)),
                timeout=CHECKPOINT_READ_TIMEOUT_S)
        except Exception as exc:            # noqa: BLE001 - a cancel never depends on a read
            log.warning("cancel of job %s: this generation's checkpoint was unreadable (%s) - "
                        "recording the approved intent without the gate", row_id, exc)
        facts = _fr.merge_durable_facts(approved=row.dispatch_payload or {},
                                        checkpoint_state=ckpt,
                                        db_attempt=row.current_attempt)
        result = _fr.build_cancelled_result(
            job_id=str(row_id), owner_id=str(row.owner_id), engine=facts["engine"],
            purpose=facts["purpose"], dimensionality=facts["dimensionality"],
            approved_snapshot_id=facts["approved_snapshot_id"], attempts=facts["attempts"],
            failed_gate=facts["failed_gate"], failure_cause=facts["failure_cause"])
        result_dict = result.to_dict()
        await job_repo.set_final_result(db, row_id, result_dict)

        # 4. THE ANNOUNCEMENT, in the same transaction as the record it announces - the
        #    transactional outbox every terminal event goes through, so the closing line is the
        #    rendered verdict and nothing else. Delivered below, and by the recovery sweep if
        #    this process dies first.
        await outbox_repo.enqueue(
            db, job_id=row_id, execution_generation=int(row.execution_generation or 0),
            terminal_status=JobStatus.cancelled.value,
            final_result_schema_version=int(result_dict.get("schema_version", 1)),
            event_payload={"closing_message": _fr.render_message(result),
                           "final_result": result_dict})
        await db.commit()

    log.info("job %s cancelled by its owner (worker evicted=%s, reason given=%s)",
             row_id, evicted, bool(reason))

    # AFTER THE COMMIT, and each best-effort: the cancel is already durable and the fence already
    # holds. A queued launch this fails to revoke is refused at its claim (already terminal); a
    # closing line this fails to deliver is delivered by the outbox sweep.
    await _revoke_launch(row_id)
    from meshpipeline.application import outbox_publisher
    await outbox_publisher.deliver_own_terminal_event(session_factory, row_id)
    return CancelOutcome(CancelResult.cancelled, JobStatus.cancelled, reason or None)


async def _revoke_launch(row_id: uuid.UUID) -> None:
    from meshpipeline.contracts.pipeline_execution import PipelineLaunchError, get_pipeline_launcher
    try:
        await get_pipeline_launcher().revoke(str(row_id))
    except PipelineLaunchError:
        # No launcher composed in this process (one that never dispatches): it queued nothing.
        log.info("cancel of job %s: no pipeline launcher composed here; nothing to revoke", row_id)
    except Exception as exc:  # noqa: BLE001 - the broker is a dependency; the claim still refuses
        log.warning("cancel of job %s: could not revoke the queued launch (%s) - a worker that "
                    "still receives it refuses it as already terminal", row_id, type(exc).__name__)


__all__ = ["CancelOutcome", "CancelResult", "REASON_MAX", "cancel_job"]
