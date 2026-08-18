# Responsibility: Write a run's terminal result and its announcement in one transaction.
# Owns: the atomic finalize, the terminal result, crash classification, and the durable facts a crash leaves.
# Boundaries: one commit carries both the result and the outbox row, so the record and the announcement cannot disagree.
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.application.final_result import FinalResult
from meshpipeline.errors import FailureClass
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.lease import ExecutionOwnership, LeaseRepository
from meshpipeline.persistence.models import FailedReason, JobStatus
from meshpipeline.persistence.repositories.job_repository import JobRepository
from meshpipeline.persistence.repositories.terminal_outbox_repository import TerminalOutboxRepository


@dataclass(frozen=True)
class FinalizeOutcome:
    fenced: bool                       # True = worker superseded → NOTHING written, do NOT publish
    transition: TransitionResult | None
    durable_status: JobStatus | None
    enqueued: bool                     # True = THIS call inserted the outbox row (else already present)


async def finalize_terminal_atomic(
    db: AsyncSession, *, ownership: ExecutionOwnership | None, intended_status: JobStatus,
    failed_reason: FailedReason | None, final_result_dict: dict, closing_message: str,
    lease_repo: LeaseRepository | None = None, job_repo: JobRepository | None = None,
    outbox_repo: TerminalOutboxRepository | None = None,
) -> FinalizeOutcome:
    lease_repo = lease_repo or LeaseRepository()
    job_repo = job_repo or JobRepository()
    outbox_repo = outbox_repo or TerminalOutboxRepository()

    # 1. FENCE - atomically row-lock + verify ownership; held until the caller commits.
    generation = 0
    if ownership is not None:
        locked = await lease_repo.lock_current_owner(db, ownership)
        if locked is None:
            return FinalizeOutcome(fenced=True, transition=None, durable_status=None, enqueued=False)
        generation = ownership.execution_generation
        job_id = ownership.job_id
    else:
        job_id = uuid.UUID(str(final_result_dict["job_id"]))

    # 2. terminal status CAS. We hold the row lock, so no concurrent writer can transition it out from
    #    under us while we finalize. The CAS never overwrites an already-terminal result.
    tr = await job_repo.transition(db, job_id, intended_status)
    row = await job_repo.get_internal(db, job_id)
    durable_status = row.status if row is not None else intended_status

    # 3+4. Persist the final_result and enqueue the terminal-event intent ONLY when WE actually
    # performed the terminal transition. If the job was ALREADY terminal (a prior finalize / a
    # redelivery / the reaper), we must NOT overwrite that winner's final_result or double-announce -
    # the outbox dedup key would reject a second row anyway, but we skip cleanly so an idempotent
    # re-finalize leaves the first verdict authoritative.
    if tr == TransitionResult.applied:
        if failed_reason is not None and row is not None:
            row.failed_reason = failed_reason
        await job_repo.set_final_result(db, job_id, final_result_dict)
        enqueued = await outbox_repo.enqueue(
            db, job_id=job_id, execution_generation=generation,
            terminal_status=durable_status.value,
            final_result_schema_version=int(final_result_dict.get("schema_version", 1)),
            event_payload={"closing_message": closing_message, "final_result": final_result_dict})
    else:
        enqueued = False

    return FinalizeOutcome(fenced=False, transition=tr, durable_status=durable_status,
                           enqueued=enqueued)


__all__ = ["finalize_terminal_atomic", "FinalizeOutcome"]


# #
# TERMINAL ASSEMBLY - turn a finished run into a durable terminal outcome.
# Extracted from application/pipeline_run._run_async. `finalize_terminal_atomic` above is the
# effectful half (one row-locked commit); everything here is the ASSEMBLY that feeds it: reading
# the durable artifact rows, applying the readiness policy, building the application-owned verdict,
# choosing the closing message, then committing and publishing in the established order.
# Assembly and persistence stay distinguishable - `build_terminal_result` is pure and returns what
# `assemble_and_finalize` then commits - so the verdict can be asserted without a database.
# ORDER, unchanged and load-bearing:
#   read durable artifact rows -> render verdict -> ONE atomic fenced commit -> publish the outbox
# A fenced worker stops at the commit and publishes NOTHING; the current owner finalizes.
# #


@dataclass(frozen=True)
class TerminalAssembly:

    job_id: str
    owner_id: str
    status: JobStatus
    failed_reason: FailedReason | None
    engine: str = ""
    purpose: str = ""
    dimensionality: str = ""
    requested_mesh_fidelity: str | None = None
    effective_mesh_fidelity: str = ""
    mesh_fidelity_source: str = ""
    fidelity_policy_version: str = ""
    approved_snapshot_id: str = ""
    executor_success: bool = False
    reviewer_verdict: str = ""
    failed_gate: str = ""
    api_failure: str = ""
    attempts: int = 0
    attempts_max: int = 0
    pipeline_timed_out: bool = False
    #: node_failure_handler's blameless SYSTEM-failure note, honoured only when api_failure is set
    pre_composed_message: str = ""


@dataclass(frozen=True)
class TerminalResult:

    result: FinalResult
    closing_message: str
    delivered_types: list
    required_ready: bool


def build_terminal_result(assembly: TerminalAssembly, *, delivered_types: list) -> TerminalResult:
    from meshpipeline.application import final_result as _fr
    from meshpipeline.application.artifact_policy import optional_warnings, required_ready

    ready = required_ready(assembly.engine, delivered_types)
    warnings = optional_warnings(delivered_types) if ready else []
    result = _fr.build_final_result(
        job_id=assembly.job_id, owner_id=assembly.owner_id,
        status=(_fr.TerminalStatus.succeeded if assembly.status == JobStatus.succeeded
                else _fr.TerminalStatus.failed),
        engine=assembly.engine, purpose=assembly.purpose,
        dimensionality=assembly.dimensionality,
        requested_mesh_fidelity=assembly.requested_mesh_fidelity,
        effective_mesh_fidelity=assembly.effective_mesh_fidelity,
        mesh_fidelity_source=assembly.mesh_fidelity_source,
        fidelity_policy_version=assembly.fidelity_policy_version,
        approved_snapshot_id=assembly.approved_snapshot_id,
        executor_success=assembly.executor_success,
        reviewer_verdict=assembly.reviewer_verdict,
        failed_gate=assembly.failed_gate,
        api_failure=assembly.api_failure,
        attempts=assembly.attempts, attempts_max=assembly.attempts_max,
        required_ready=ready, delivered_types=delivered_types, optional_warnings=warnings,
        # a run that exhausted its top-level budget mid-graph is reported truthfully as
        # timed_out rather than as the downstream symptom it produced.
        pipeline_timed_out=assembly.pipeline_timed_out)
    # The ONLY pre-composed message honoured is the blameless SYSTEM-failure note; any other stale
    # draft (e.g. a pre-delivery outcome_message) is discarded so one renderer owns the closing.
    closing = (assembly.pre_composed_message
               if (assembly.api_failure and assembly.pre_composed_message)
               else _fr.render_message(result))
    return TerminalResult(result, closing, delivered_types, ready)


async def read_delivered_types(session_factory, job_id: str, *, jlog) -> list:
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    async with session_factory() as db:
        try:
            rows = await ArtifactRepository().get_by_job(db, uuid.UUID(job_id))
            return sorted({r.artifact_type.value for r in rows})
        except Exception as exc:                   # noqa: BLE001 - conservative, never fatal
            jlog.warning("final_result: could not read ready artifacts (%s) - rendering "
                         "conservatively", exc)
            return []


@dataclass(frozen=True)
class TerminalPublication:

    fenced: bool
    result: FinalResult | None
    closing_message: str
    created_at: str | None
    ended_at: str | None


async def assemble_and_finalize(session_factory, assembly: TerminalAssembly, *,
                                ownership, lease_repo, job_repo, jlog) -> TerminalPublication:
    delivered_types = await read_delivered_types(session_factory, assembly.job_id, jlog=jlog)
    rendered = build_terminal_result(assembly, delivered_types=delivered_types)

    async with session_factory() as db:
        outcome = await finalize_terminal_atomic(
            db, ownership=ownership, intended_status=assembly.status,
            failed_reason=assembly.failed_reason,
            final_result_dict=rendered.result.to_dict(),
            closing_message=rendered.closing_message,
            lease_repo=lease_repo, job_repo=job_repo)
        await db.commit()
    if outcome.fenced:
        jlog.warning("Worker FENCED at the atomic terminal transaction (generation/token "
                     "superseded) - no terminal side effects; the current owner finalizes. "
                     "job_id=%s", assembly.job_id)
        return TerminalPublication(True, None, rendered.closing_message, None, None)

    # Durable timestamps read AFTER finalize - ended_at is stamped by the terminal transition.
    async with session_factory() as db:
        row = await job_repo.get_internal(db, uuid.UUID(assembly.job_id))
        created = row.created_at.isoformat() if (row and row.created_at) else None
        ended = row.ended_at.isoformat() if (row and row.ended_at) else None

    # FAST-PATH delivery of THIS job's terminal event. Best-effort: if it fails (or this worker dies
    # right after), the durable outbox row is delivered by the recovery sweep. The outbox is the
    # SOLE publisher of the terminal event, so the user's message survives this worker's death.
    from meshpipeline.application import outbox_publisher as _obp
    await _obp.deliver_own_terminal_event(session_factory, assembly.job_id)
    return TerminalPublication(False, rendered.result, rendered.closing_message, created, ended)


# #
# CRASH FINALIZATION - a run that died still owes the user a truthful terminal record.
# Extracted from application/pipeline_run._run_async's 116-line `except Exception` handler. Five
# separable concerns lived in one block: classifying the crash, building the diagnostic verdict from
# whatever is DURABLE, deciding terminal eligibility (a superseded worker must not fail a job a
# newer generation owns), persisting it, and falling back to a direct closing when the durable path
# itself fails. They are separated here.
# WHAT IS DELIBERATELY NOT CAUGHT: only `Exception`. asyncio.CancelledError and SystemExit are
# BaseException, so a cancelled or shut-down run propagates untouched and NEVER produces a terminal
# record - there is no cancelled status to produce (see final_result.TerminalStatus), and inventing
# a failure for one would be a false terminal outcome.
# The caller re-raises after this returns: the crash must still reach Celery and monitoring.
# #


@dataclass(frozen=True)
class CrashClassification:

    failure_class: FailureClass
    dependency: str
    operator_detail: str
    failed_reason: FailedReason
    user_message: str


def classify_crash(exc: BaseException) -> CrashClassification:
    from meshpipeline.errors import (
        SystemFailure,
        classify_exception,
        failed_reason_for,
        user_message_for,
    )

    if isinstance(exc, SystemFailure):
        fc, dep, detail = exc.failure_class, exc.dependency, exc.operator_detail
    else:
        sf = classify_exception(exc, "pipeline")
        fc, dep, detail = sf.failure_class, sf.dependency, sf.operator_detail
    try:
        reason = FailedReason(failed_reason_for(fc))
    except ValueError:
        reason = FailedReason.unhandled
    return CrashClassification(fc, dep, detail, reason, user_message_for(fc))


async def durable_facts_after_crash(session_factory, *, job_id: str, approved: dict,
                                    graph, graph_config, job_repo, jlog) -> dict:
    from meshpipeline.application.final_result import merge_durable_facts

    checkpoint_state: dict = {}
    try:
        if graph is not None and graph_config is not None:
            snap = await graph.aget_state(graph_config)
            checkpoint_state = dict(getattr(snap, "values", None) or {})
    except Exception as exc:                       # noqa: BLE001 - never depends on a read
        jlog.warning("crash finalize: durable checkpoint unreadable (%s) - reporting only the "
                     "approved intent", exc)
    db_attempt = None
    try:
        async with session_factory() as db:
            row = await job_repo.get_internal(db, uuid.UUID(job_id))
            db_attempt = getattr(row, "current_attempt", None) if row else None
    except Exception as exc:                       # noqa: BLE001
        jlog.warning("crash finalize: attempt count unreadable (%s)", exc)
    return merge_durable_facts(approved=approved, checkpoint_state=checkpoint_state,
                               db_attempt=db_attempt)


@dataclass(frozen=True)
class CrashOutcome:

    classification: CrashClassification
    fenced: bool
    finalized: bool

    @property
    def needs_direct_closing(self) -> bool:
        return not self.finalized and not self.fenced


async def finalize_crash(session_factory, exc: BaseException, *, job_id: str, owner_id: str,
                         assembly_defaults: TerminalAssembly, approved: dict,
                         graph, graph_config, ownership, lease_repo, job_repo,
                         jlog, publish) -> CrashOutcome:
    cls = classify_crash(exc)
    jlog.error("Job failed - system failure [%s] %s: %s", cls.failure_class.value,
               cls.dependency, exc)
    # The user's own account of a run that ended without a mesh: the application's CLASSIFICATION,
    # never the exception text and never a stack trace.
    try:
        from meshpipeline.contracts import rationale as _R
        from meshpipeline.contracts.event_stream import publisher as _pubf
        _R.terminal_failure(_pubf(str(job_id), "outcome"), classification=cls.failure_class.value)
    except Exception:                              # noqa: BLE001
        pass
    from meshpipeline.errors import record_dead_letter
    record_dead_letter(job_id, cls.failure_class, cls.dependency, cls.operator_detail)
    try:
        from meshpipeline.metrics import failure as _mfail
        _mfail(cls.failure_class.value, cls.dependency)
    except Exception:                              # noqa: BLE001
        pass

    fenced = finalized = False
    try:
        facts = await durable_facts_after_crash(
            session_factory, job_id=job_id, approved=approved, graph=graph,
            graph_config=graph_config, job_repo=job_repo, jlog=jlog)
        from dataclasses import replace
        assembly = replace(
            assembly_defaults, job_id=job_id, owner_id=owner_id, status=JobStatus.failed,
            failed_reason=cls.failed_reason,
            engine=facts["engine"], purpose=facts["purpose"],
            dimensionality=facts["dimensionality"],
            approved_snapshot_id=facts["approved_snapshot_id"],
            executor_success=facts["executor_success"],
            reviewer_verdict=facts["reviewer_verdict"], failed_gate=facts["failed_gate"],
            api_failure=f"{cls.dependency}: crash", attempts=facts["attempts"])
        # DELIVERY IS NEVER ASSUMED on a crash: packaging runs only on a succeeded terminal status,
        # so readiness stays false and the deliverable stays missing.
        rendered = build_terminal_result(assembly, delivered_types=[])
        async with session_factory() as db:
            outcome = await finalize_terminal_atomic(
                db, ownership=ownership, intended_status=JobStatus.failed,
                failed_reason=cls.failed_reason,
                final_result_dict=rendered.result.to_dict(),
                closing_message=cls.user_message,
                lease_repo=lease_repo, job_repo=job_repo)
            await db.commit()
            fenced = outcome.fenced
        finalized = not fenced
        if finalized:
            from meshpipeline.application import outbox_publisher as _obp
            await _obp.deliver_own_terminal_event(session_factory, job_id)
    except Exception as db_exc:                    # noqa: BLE001 - the crash must still be raised
        jlog.warning("crash finalize: could not finalize job via outbox: %s", db_exc)

    result = CrashOutcome(cls, fenced, finalized)
    if result.needs_direct_closing:
        # The durable path failed (not a fence) - the user must still get a blameless
        # closing. A FENCE is not a failure to record: the current owner records it.
        try:
            publish(cls.failure_class)
        except Exception:                          # noqa: BLE001
            pass
    return result


async def refuse_before_execution(session_factory, *, job_id: str, failure_class,
                                  dependency: str, detail: str, user_message: str, reason: str,
                                  job_repo, jlog, publish) -> dict:
    from meshpipeline.errors import failed_reason_for, record_dead_letter
    from meshpipeline.persistence.job_state import TransitionResult

    try:
        async with session_factory() as db:
            if await job_repo.transition(db, uuid.UUID(job_id), JobStatus.failed) == TransitionResult.applied:
                row = await job_repo.get_internal(db, uuid.UUID(job_id))
                if row:
                    try:
                        row.failed_reason = FailedReason(failed_reason_for(failure_class))
                    except (ValueError, AttributeError):
                        row.failed_reason = FailedReason.unhandled
            await db.commit()
    except Exception as exc:                       # noqa: BLE001 - the refusal must still stand
        jlog.warning("pre-execution refusal: could not mark job failed: %s", exc)
    record_dead_letter(job_id, failure_class, dependency, detail)
    try:
        publish(user_message)
    except Exception:                              # noqa: BLE001
        pass
    return {"job_id": job_id, "status": "failed", "reason": reason}
