# Responsibility: Read and write repair jobs and their attempts, and decide what a status change means.
# Owns: the tenant-scoped reads, the transition compare-and-set, and attempt numbering.
# Boundaries: WHICH transitions are legal is persistence/repair_job_state.py's; this applies them.
from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# The same four outcomes a simulation-job transition reports, for the same reason: a caller has to
# tell "already done" from "too late". One vocabulary, declared in persistence/job_state.py.
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import (
    CadRepairAttempt,
    CadRepairDecision,
    CadRepairJob,
    RepairJobStatus,
)
from meshpipeline.persistence.repair_job_state import legal_sources
from meshpipeline.persistence.repositories import tenant_scope

_SHA256_LEN = 64


def _digest(value: str, *, field: str, required: bool = True) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if len(text) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in text):
        raise ValueError(f"{field} must be 64 lowercase hex characters")
    return text


class RepairJobRepository:

    async def create(self, db: AsyncSession, *, owner_id: str, geometry_source_id: uuid.UUID,
                     geometry_interpretation_id: uuid.UUID | None = None,
                     target_engine: str = "", customer_intent: str = "",
                     service_priority: int = 100,
                     organization_id: str = "") -> CadRepairJob:
        if not str(owner_id or "").strip():
            raise ValueError("owner_id is required - a repair job is always owned")
        row = CadRepairJob(
            **tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id),
            geometry_source_id=geometry_source_id,
            geometry_interpretation_id=geometry_interpretation_id,
            status=RepairJobStatus.received,
            target_engine=str(target_engine or "")[:64],
            customer_intent=(customer_intent or None),
            service_priority=int(service_priority),
        )
        db.add(row)
        await db.flush()
        return row

    async def get_internal(self, db: AsyncSession, job_id: uuid.UUID) -> CadRepairJob | None:
        """UNSCOPED, for the pipeline and the worker - which act for the job, not for a reader."""
        res = await db.execute(select(CadRepairJob).where(CadRepairJob.id == job_id))
        return res.scalar_one_or_none()

    async def get_for_owner(self, db: AsyncSession, job_id: uuid.UUID, owner_id: str, *,
                            organization_id: str = "") -> CadRepairJob | None:
        res = await db.execute(
            select(CadRepairJob).where(
                CadRepairJob.id == job_id,
                tenant_scope.scope(CadRepairJob, owner_id=owner_id,
                                   organization_id=organization_id)))
        return res.scalar_one_or_none()

    async def queue_for_owner(self, db: AsyncSession, owner_id: str, *,
                              organization_id: str = "",
                              statuses: tuple[RepairJobStatus, ...] = (),
                              limit: int = 50) -> list[CadRepairJob]:
        """The operator queue, most urgent first: priority, then oldest - never insertion order."""
        stmt = select(CadRepairJob).where(
            tenant_scope.scope(CadRepairJob, owner_id=owner_id,
                               organization_id=organization_id))
        if statuses:
            stmt = stmt.where(CadRepairJob.status.in_(statuses))
        stmt = stmt.order_by(CadRepairJob.service_priority.asc(),
                             CadRepairJob.created_at.asc()).limit(int(limit))
        return list((await db.execute(stmt)).scalars().all())

    async def transition(self, db: AsyncSession, job_id: uuid.UUID, target: RepairJobStatus, *,
                         blocked_reason: str | None = None,
                         current_strategy: str | None = None,
                         repair_status: str | None = None,
                         simulation_job_id: uuid.UUID | None = None) -> TransitionResult:
        """Move the job to `target` if its CURRENT state allows it. One statement, no read first.

        THE DATABASE DECIDES, not a prior SELECT. Two operators clicking the same queue item, and
        a worker finishing while one of them does, are the ordinary case here - a check-then-act
        would let the later writer overwrite a decision it never saw. The legal sources go into
        the WHERE clause, so exactly one caller can perform a given transition and the others are
        told which way they lost.
        """
        sources = legal_sources(target)
        values: dict = {"status": target}
        # Only the fields a caller actually passed: a transition that says nothing about the
        # strategy must not blank the strategy somebody else chose.
        if blocked_reason is not None:
            values["blocked_reason"] = blocked_reason or None
        if current_strategy is not None:
            values["current_strategy"] = str(current_strategy)[:32]
        if repair_status is not None:
            values["repair_status"] = str(repair_status)[:32]
        if simulation_job_id is not None:
            values["simulation_job_id"] = simulation_job_id

        if sources:
            res = await db.execute(
                update(CadRepairJob)
                .where(CadRepairJob.id == job_id, CadRepairJob.status.in_(sources))
                .values(**values)
                .returning(CadRepairJob.id))
            if res.first() is not None:
                return TransitionResult.applied

        # Nothing moved. Which of the three reasons it was is worth distinguishing: a caller that
        # cannot tell "already done" from "too late" either retries forever or gives up too early.
        current = await self.get_internal(db, job_id)
        if current is None:
            return TransitionResult.not_found
        if current.status == target:
            return TransitionResult.already_at_target
        return TransitionResult.rejected_current_state

    async def record_attempt(self, db: AsyncSession, *, repair_job_id: uuid.UUID, mode: str,
                             input_sha256: str, profile: str = "", tool_version: str = "",
                             output_sha256: str = "", status: str = "",
                             report: dict | None = None, caps: dict | None = None,
                             measurements: dict | None = None) -> CadRepairAttempt:
        """Append one attempt. The number is derived here, under the job's own uniqueness rule.

        `attempt_no` is MAX+1 rather than a counter on the job: a counter read, incremented and
        written by two workers yields two attempt 3s, and the unique constraint would then reject
        the loser's whole transaction rather than just its numbering. Deriving it inside the same
        transaction as the insert keeps the constraint as the authority.
        """
        if not str(mode or "").strip():
            raise ValueError("mode is required - an attempt always says what it did")
        highest = (await db.execute(
            select(func.max(CadRepairAttempt.attempt_no))
            .where(CadRepairAttempt.repair_job_id == repair_job_id))).scalar()
        row = CadRepairAttempt(
            repair_job_id=repair_job_id,
            attempt_no=int(highest or 0) + 1,
            mode=str(mode)[:32],
            profile=str(profile or "")[:32],
            tool_version=str(tool_version or "")[:128],
            input_sha256=_digest(input_sha256, field="input_sha256"),
            output_sha256=_digest(output_sha256, field="output_sha256", required=False),
            status=str(status or "")[:32],
            report=report,
            caps=caps,
            measurements=measurements,
        )
        db.add(row)
        await db.flush()
        return row

    async def attempts_for_job(self, db: AsyncSession,
                               repair_job_id: uuid.UUID) -> list[CadRepairAttempt]:
        res = await db.execute(
            select(CadRepairAttempt)
            .where(CadRepairAttempt.repair_job_id == repair_job_id)
            .order_by(CadRepairAttempt.attempt_no.asc()))
        return list(res.scalars().all())

    # THE OPERATOR SURFACE. These read ACROSS tenants, because the people using them are this
    # service's own staff working a shared queue - the customer-scoped reads above are a different
    # question with a different answer. Nothing here takes an owner_id, so no caller can mistake
    # one of these for a tenant-scoped read by leaving an argument off.

    async def operator_job(self, db: AsyncSession, job_id: uuid.UUID) -> CadRepairJob | None:
        """ONE repair job, read ACROSS tenants for the operator surface.

        Named rather than reached through `get_internal`, which is the worker's seam: the
        architecture suite forbids request-facing code from calling that, and rightly - a request
        handler reading unscoped by accident is how one tenant's data reaches another. This method
        says cross-tenant in its own name, so a reviewer sees the intent at the call site, and the
        route that uses it is gated by the admin credential rather than an owner identity.
        """
        res = await db.execute(select(CadRepairJob).where(CadRepairJob.id == job_id))
        return res.scalar_one_or_none()

    async def operator_queue(self, db: AsyncSession, *,
                             statuses: tuple[RepairJobStatus, ...] = (),
                             assigned_operator: str | None = None,
                             unassigned_only: bool = False,
                             limit: int = 50) -> list[CadRepairJob]:
        """The cross-tenant work queue, most urgent first: priority, then oldest."""
        stmt = select(CadRepairJob)
        if statuses:
            stmt = stmt.where(CadRepairJob.status.in_(statuses))
        if unassigned_only:
            stmt = stmt.where(CadRepairJob.assigned_operator.is_(None))
        elif assigned_operator:
            stmt = stmt.where(CadRepairJob.assigned_operator == assigned_operator)
        stmt = stmt.order_by(CadRepairJob.service_priority.asc(),
                             CadRepairJob.created_at.asc()).limit(int(limit))
        return list((await db.execute(stmt)).scalars().all())

    async def assign(self, db: AsyncSession, job_id: uuid.UUID, *, operator: str,
                     claim_only_if_unassigned: bool = False) -> bool:
        """Put `operator`'s name on the job, or take it off when `operator` is empty.

        `claim_only_if_unassigned` makes it a CLAIM: the update applies only while nobody holds
        the item, so two operators opening the same queue row cannot both believe they took it.
        Reassignment - a lead moving work - is the default and deliberately overwrites.
        """
        who = str(operator or "").strip()[:256]
        stmt = update(CadRepairJob).where(CadRepairJob.id == job_id)
        if claim_only_if_unassigned:
            stmt = stmt.where(CadRepairJob.assigned_operator.is_(None))
        stmt = stmt.values(assigned_operator=who or None,
                           assigned_at=func.now() if who else None)
        res = await db.execute(stmt.returning(CadRepairJob.id))
        return res.first() is not None

    async def record_decision(self, db: AsyncSession, *, repair_job_id: uuid.UUID,
                              decision: str, actor: str, from_status: str = "",
                              reason: str = "", notes: str = "") -> CadRepairDecision:
        """Append what a person decided. Never updates: a changed mind is a new row."""
        if not str(actor or "").strip():
            raise ValueError("actor is required - an unattributed decision is not an audit")
        if not str(decision or "").strip():
            raise ValueError("decision is required")
        row = CadRepairDecision(
            repair_job_id=repair_job_id,
            decision=str(decision)[:32],
            from_status=str(from_status or "")[:32],
            actor=str(actor).strip()[:256],
            reason=(reason or None),
            notes=(notes or None),
        )
        db.add(row)
        await db.flush()
        return row

    async def decisions_for_job(self, db: AsyncSession,
                                repair_job_id: uuid.UUID) -> list[CadRepairDecision]:
        res = await db.execute(
            select(CadRepairDecision)
            .where(CadRepairDecision.repair_job_id == repair_job_id)
            .order_by(CadRepairDecision.created_at.asc()))
        return list(res.scalars().all())

    async def service_throughput(self, db: AsyncSession) -> dict:
        """What the repair service is actually delivering, counted from rows. CROSS-TENANT.

        THE LAUNCH METRIC THIS SERVICE IS JUDGED ON is the share of customer CAD that reaches a
        downloadable mesh without ad hoc shell work, and the operator time each delivery costs.
        Both are counted here from the states and timestamps the queue already writes, so the
        dashboard cannot disagree with the queue it is reporting on.

        Counted, never estimated. Every number below is a COUNT or a timestamp difference over
        rows; nothing is sampled, inferred or averaged across a window this method chose.
        """
        from sqlalchemy import case

        # Iterated rather than dict()-ed over the result: a Row is not a 2-tuple to a type
        # checker, and the state's value is the key every caller reads anyway.
        state_rows = (await db.execute(
            select(CadRepairJob.status, func.count())
            .group_by(CadRepairJob.status))).all()
        by_state: dict[str, int] = {
            getattr(row[0], "value", str(row[0])): int(row[1] or 0) for row in state_rows}

        delivered = by_state.get(RepairJobStatus.delivered.value, 0)
        repair_only = by_state.get(RepairJobStatus.repair_delivered.value, 0)
        total = sum(by_state.values())

        # WAITING ON US versus WAITING ON SOMEBODY ELSE, which an SLA has to tell apart: a job
        # sitting in waiting_customer is not late because we are slow.
        from meshpipeline.persistence.repair_job_state import (
            AWAITING_HUMAN,
            BLOCKED,
            IN_FLIGHT,
        )
        bucket = lambda group: sum(by_state.get(s.value, 0) for s in group)  # noqa: E731

        # HOW LONG A DELIVERED JOB TOOK, from the row's own timestamps. Median would be the better
        # statistic and SQL makes it awkward across backends; the average is reported as what it
        # is rather than being called a median.
        settled = (RepairJobStatus.delivered, RepairJobStatus.repair_delivered)
        elapsed = (await db.execute(
            select(
                func.count(),
                func.avg(
                    func.extract("epoch", CadRepairJob.updated_at - CadRepairJob.created_at)),
            ).where(CadRepairJob.status.in_(settled)))).first()

        attempts = (await db.execute(
            select(func.count(), func.count(func.distinct(CadRepairAttempt.repair_job_id)))
        )).first()

        decisions = (await db.execute(
            select(
                func.count(),
                func.count(func.distinct(CadRepairDecision.repair_job_id)),
                func.coalesce(func.sum(case((CadRepairDecision.decision == "retry", 1), else_=0)), 0),
            ))).first()

        return {
            "jobs_total": total,
            "by_state": by_state,
            "delivered_with_mesh": delivered,
            "delivered_repair_only": repair_only,
            # THE HEADLINE: the share of jobs that reached something the customer can download.
            # None rather than 0 when there are no jobs - "we delivered 0% of nothing" is a
            # sentence that has misled every dashboard that has ever printed it.
            "delivery_rate": (round((delivered + repair_only) / total, 4) if total else None),
            "blocked_rate": (round(bucket(BLOCKED) / total, 4) if total else None),
            "awaiting_human": bucket(AWAITING_HUMAN),
            "in_flight": bucket(IN_FLIGHT),
            "blocked": bucket(BLOCKED),
            "mean_seconds_to_delivery": (
                round(float(elapsed[1]), 1) if elapsed and elapsed[1] is not None else None),
            "attempts_total": int(attempts[0] or 0) if attempts else 0,
            "jobs_with_attempts": int(attempts[1] or 0) if attempts else 0,
            # OPERATOR EFFORT, as the only honest proxy the rows support: how many recorded human
            # decisions a job takes. It is NOT minutes - nothing here times an operator - and it
            # is named for what it counts so no dashboard can relabel it as effort it never saw.
            "operator_decisions_total": int(decisions[0] or 0) if decisions else 0,
            "jobs_touched_by_an_operator": int(decisions[1] or 0) if decisions else 0,
            "retry_decisions": int(decisions[2] or 0) if decisions else 0,
        }
