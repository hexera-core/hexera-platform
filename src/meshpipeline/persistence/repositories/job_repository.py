# Responsibility: Read and write job rows, including status transitions and dispatch state.
# Boundaries: queries are owner-scoped; a transition is applied only where the state table permits it.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from meshpipeline.persistence.job_state import TransitionResult, legal_sources
from meshpipeline.persistence.models import JobStatus, SimulationJob
from meshpipeline.persistence.repositories import tenant_scope


class JobRepository:
    async def create(self, db: AsyncSession, owner_id: str, *,
                     organization_id: str = "") -> SimulationJob:
        job = SimulationJob(**tenant_scope.stamp(owner_id=owner_id,
                                                 organization_id=organization_id))
        db.add(job)
        await db.flush()
        return job

    async def get_for_owner(self, db: AsyncSession, job_id: uuid.UUID,
                            owner_id: str, *, organization_id: str = "") -> SimulationJob | None:
        res = await db.execute(
            select(SimulationJob)
            # the same eager load the internal read uses: the status response renders artifacts,
            # and a lazy load after the session closes is a MissingGreenlet, not a 404
            .options(selectinload(SimulationJob.artifacts))
            .where(SimulationJob.id == job_id,
                   tenant_scope.scope(SimulationJob, owner_id=owner_id,
                                      organization_id=organization_id)))
        return res.scalar_one_or_none()

    async def get_internal(self, db: AsyncSession, job_id: uuid.UUID) -> SimulationJob | None:
        result = await db.execute(
            select(SimulationJob)
            .options(selectinload(SimulationJob.artifacts))
            .where(SimulationJob.id == job_id)
        )
        return result.scalar_one_or_none()

    async def set_dispatch_payload(self, db: AsyncSession, job_id: uuid.UUID, payload: dict) -> None:
        await db.execute(
            update(SimulationJob).where(SimulationJob.id == job_id)
            .values(dispatch_payload=payload))

    async def get_dispatch_payload(self, db: AsyncSession, job_id: uuid.UUID) -> dict | None:
        result = await db.execute(
            select(SimulationJob.dispatch_payload).where(SimulationJob.id == job_id))
        return result.scalar_one_or_none()

    async def set_final_result(self, db: AsyncSession, job_id: uuid.UUID, final_result: dict) -> None:
        await db.execute(
            update(SimulationJob).where(SimulationJob.id == job_id)
            .values(final_result=final_result))

    async def get_final_result(self, db: AsyncSession, job_id: uuid.UUID) -> dict | None:
        result = await db.execute(
            select(SimulationJob.final_result).where(SimulationJob.id == job_id))
        return result.scalar_one_or_none()

    async def mark_launched(self, db: AsyncSession, job_id: uuid.UUID, backend: str) -> None:
        # THE ACCEPTANCE RECORD. `pipeline_dispatch_state` has always documented a `submitted`
        # state and `pipeline_backend`/`pipeline_submitted_at` have always documented what accepted
        # the job and when; only the failure half was ever written, so a successfully launched job
        # carried no dispatch bookkeeping at all.
        #
        # `pipeline_submitted_at IS NULL` is the idempotency guard: a redelivery of the same
        # accepted submission must not move the moment the backend first took it. The backend name
        # is refreshed regardless, because it is a fact about the accepting backend rather than a
        # timestamp - and on the same-value replay that rewrite is a no-op.
        now = datetime.now(UTC)
        await db.execute(
            update(SimulationJob)
            .where(SimulationJob.id == job_id, SimulationJob.pipeline_submitted_at.is_(None))
            .values(pipeline_dispatch_state="submitted", pipeline_backend=backend[:32],
                    pipeline_submitted_at=now, updated_at=now))

    async def mark_launch_failed(self, db: AsyncSession, job_id: uuid.UUID, error: str) -> None:
        now = datetime.now(UTC)
        res = await db.execute(
            update(SimulationJob)
            .where(SimulationJob.id == job_id,
                   SimulationJob.status.in_(legal_sources(JobStatus.failed)))
            .values(pipeline_dispatch_state="launch_failed", pipeline_launch_error=error[:2000],
                    status=JobStatus.failed, ended_at=now, updated_at=now))
        if res.rowcount == 0:
            # already terminal (or gone): never touch status, but still record the launch metadata
            await db.execute(
                update(SimulationJob).where(SimulationJob.id == job_id)
                .values(pipeline_dispatch_state="launch_failed", pipeline_launch_error=error[:2000],
                        updated_at=now))
        await db.commit()

    async def _timestamps_for(self, target: JobStatus) -> dict:
        now = datetime.now(UTC)
        values: dict = {"status": target, "updated_at": now}
        if target == JobStatus.running:
            values["started_at"] = now
        elif target in (JobStatus.succeeded, JobStatus.failed, JobStatus.pending_review):
            values["ended_at"] = now
        return values

    async def transition(self, db: AsyncSession, job_id: uuid.UUID, target: JobStatus,
                         *, allow: frozenset[JobStatus] | set[JobStatus] | None = None) -> TransitionResult:
        sources = frozenset(allow) if allow is not None else legal_sources(target)
        if not sources:
            raise ValueError(f"no legal source states for target {target!r} - declare them in job_state")
        res = await db.execute(
            update(SimulationJob)
            .where(SimulationJob.id == job_id, SimulationJob.status.in_(sources))
            .values(**(await self._timestamps_for(target))))
        if res.rowcount == 1:
            return TransitionResult.applied
        current = (await db.execute(
            select(SimulationJob.status).where(SimulationJob.id == job_id))).scalar_one_or_none()
        if current is None:
            return TransitionResult.not_found
        if current == target:
            return TransitionResult.already_at_target
        return TransitionResult.rejected_current_state
    # NOTE: there is deliberately NO admin_force_status / set_status bypass on this repository.
    # An unconditional status UPDATE reachable from application code is exactly the escape hatch
    # that lets a stale worker overwrite a terminal result. Out-of-band operator recovery (un-stick
    # a wrongly-terminal job, repair migrated state) is an explicit DBA action - a migration or a
    # psql session - not a method callable from a request, the pipeline, or a reconciler. The
    # architecture fitness suite fails if such a bypass is reintroduced.

    # Active = still-in-flight statuses that count against quotas. (queued and
    # pending_review are defined in the enum but are not assigned by the current
    # pipeline; they are intentionally excluded so a stray non-running status can
    # never pin an owner's quota forever.)
    _ACTIVE_STATUSES = [JobStatus.pending, JobStatus.running, JobStatus.queued]

    async def count_active_for_owner(self, db: AsyncSession, owner_id: str) -> int:
        # DELIBERATELY owner_id-only, not tenant_scope.scope. This is a QUOTA check
        # (settings/plans.max_jobs_per_owner), not a data-visibility read - widening it to the
        # organisation would pool concurrency across every member of a tenant, which is a product
        # decision about what a plan limits, not a consequence of "reads scope on the
        # organisation." That decision is out of this task's scope.
        result = await db.execute(
            select(func.count()).select_from(SimulationJob).where(
                SimulationJob.owner_id == owner_id,
                SimulationJob.status.in_(self._ACTIVE_STATUSES),
            )
        )
        return result.scalar() or 0

    async def count_total_active(self, db: AsyncSession) -> int:
        result = await db.execute(
            select(func.count()).select_from(SimulationJob).where(
                SimulationJob.status.in_(self._ACTIVE_STATUSES),
            )
        )
        return result.scalar() or 0

    async def update_current_attempt(self, db: AsyncSession, job_id: uuid.UUID, attempt_count: int) -> None:
        await db.execute(
            update(SimulationJob)
            .where(SimulationJob.id == job_id)
            .values(current_attempt=attempt_count, updated_at=datetime.now(UTC))
        )
