# Responsibility: Claim a real execution through the production seam, so a test can publish under it.
# Boundaries: it claims and binds; it publishes nothing, asserts nothing and stubs no authority.
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.models import SimulationJob


def sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=1, max_overflow=1,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def seed(job_id: uuid.UUID, owner_id: str) -> None:
    engine, Session = sessions()
    try:
        async with Session() as db:
            db.add(SimulationJob(id=job_id, owner_id=owner_id))
            await db.commit()
    finally:
        await engine.dispose()


async def claim(session_factory, job_id: uuid.UUID, *, backend_execution_id: str = ""):
    # THE PRODUCTION CLAIM, not a reconstruction of it. `claim_delivery` takes the row lock, writes
    # the generation, commits, and only then installs the Redis fingerprint the adapter compares
    # against - an order a test that claims the lease directly does not reproduce, which is how a
    # correctly claimed job could still be refused at publication time with no fence to match.
    from meshpipeline.persistence.repositories.job_repository import JobRepository

    claimed = await fence.claim_delivery(
        session_factory, JobRepository(), str(job_id),
        jlog=logging.getLogger(f"tests.ownership.{job_id}"),
        backend="test",
        backend_execution_id=backend_execution_id or f"exec-{uuid.uuid4().hex[:8]}")
    if isinstance(claimed, fence.DeliveryRefused):
        raise AssertionError(f"the production claim refused job {job_id}: {claimed.detail}")
    return claimed.ownership


async def seeded_claim(owner_prefix: str = "own", *, backend_execution_id: str = ""):
    job_id = uuid.uuid4()
    await seed(job_id, f"{owner_prefix}-{job_id.hex[:8]}")
    engine, Session = sessions()
    try:
        return (job_id,
                await claim(Session, job_id, backend_execution_id=backend_execution_id),
                Session, engine)
    except BaseException:
        await engine.dispose()
        raise


def superseded(job_id: uuid.UUID, own):
    # A claim that WAS this job's and no longer is: same job, a generation and token PostgreSQL
    # has moved past. Nothing about the publication path is stubbed - the authority is simply
    # asked about an identity it can prove is stale.
    from meshpipeline.persistence.lease import ExecutionOwnership

    return ExecutionOwnership(
        job_id=job_id, execution_generation=(own.execution_generation or 0) + 7,
        worker_token=uuid.uuid4(), backend="test", pipeline_deadline_at=None,
        claim_epoch=(own.claim_epoch or 0) + 7,
        fence_ttl_seconds=getattr(own, "fence_ttl_seconds", 60))
