# Responsibility: Own one dispute operation's identity, its duplicate resolution and its dispatch-failure recovery.
# Owns: the operation key, the cross-process claim, and the durable launch-failure record for a dispute child.
# Boundaries: it decides nothing about ownership, quotas, disputability or payload content - the route still owns those.
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# LOAD-BEARING, and now stated rather than accidental: `dispatch_contract.build()` validates a
# payload against `run_pipeline`'s signature, and that entry only exists once `pipeline_run` has
# been imported and registered it. The route imports this module before it builds its payload, so
# importing here guarantees the ordering. It used to depend on the route importing `dispatch`
# itself a few lines above the build - which stopped being true the moment dispatch moved here.
from meshpipeline.application import pipeline_run as _run_entry_registration  # noqa: F401

logger = logging.getLogger(__name__)

#: The dispatch state `JobRepository.mark_launch_failed` writes. A child carrying it was created but
#: never queued, so a repeat of its operation must not be told the run is on its way.
LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True)
class DisputeReplay:
    job_id: uuid.UUID
    launch_failed: bool


def operation_key(owner_id: str, parent_job_id: uuid.UUID | str, user_dispute: dict) -> str:
    # THE identity of one dispute operation: who is asking, which parent they are disputing, and
    # what they actually said. CANONICALISATION IS STATED, NOT INFERRED - the only normalisation is
    # the one the route has already applied when it built `user_dispute` (mode restricted to
    # rebuild/accept, comment truncated to 2000, each flag's patch to 128 and note to 500).
    # Nothing further is folded: whitespace, letter case and flag ORDER are all meaningful, because
    # the comment is stored and shown verbatim and a reordered flag list is a different set of
    # places to look. Two requests are one operation only when the intent that would be dispatched
    # is identical. The owner is inside the digest, so no two tenants can ever collide on a key.
    intent = {
        "owner":   owner_id,
        "parent":  str(parent_job_id),
        "mode":    user_dispute.get("mode", ""),
        "comment": user_dispute.get("comment", ""),
        "flags":   user_dispute.get("flags", []),
    }
    canonical = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim(db: AsyncSession, *, owner_id: str, key: str) -> DisputeReplay | None:
    # ONE operation, ONE child, across API processes. The advisory lock is transaction-scoped and
    # keyed on the operation, so two identical requests queue here instead of both passing the
    # lookup and both inserting. It is taken BEFORE the lookup and released by the commit that
    # creates the child, which is what makes the second caller's lookup see the first caller's row
    # (the engine runs READ COMMITTED, so that statement reads the latest committed state).
    #
    # The partial unique index behind `dispute_operation_key` is the durable backstop: if this lock
    # were ever not taken, a second child would still be impossible. The lock is what turns that
    # impossibility into a clean replay rather than an integrity error.
    #
    # Ordering note: callers take this lock before the owner-scoped quota lock and never the other
    # way round, so the two cannot deadlock.
    try:
        bind = getattr(db, "bind", None)
        if bind is not None and bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    except Exception as _exc:  # noqa: BLE001 - the unique index still forbids a second child
        logger.warning("dispute claim: advisory lock unavailable (%s) - proceeding on the index", _exc)

    from meshpipeline.persistence.models import SimulationJob

    # Scoped by owner as well as by key. The owner is already inside the digest, so this cannot
    # change the answer - it is here so that a tenant boundary is enforced by the query too.
    row = (await db.execute(
        select(SimulationJob.id, SimulationJob.pipeline_dispatch_state)
        .where(SimulationJob.dispute_operation_key == key,
               SimulationJob.owner_id == owner_id))).first()
    if row is None:
        return None
    return DisputeReplay(job_id=row[0], launch_failed=row[1] == LAUNCH_FAILED)


async def dispatch_or_record_failure(child_job_id: uuid.UUID, payload: dict, *,
                                     parent_job_id: uuid.UUID | str, key: str) -> None:
    # The child row is COMMITTED before this runs, deliberately: a worker that picks the message up
    # immediately must be able to see the job it was told about. That ordering is what makes a
    # dispatch failure dangerous, because silence would leave the child pending forever while it
    # consumed the owner's concurrency budget for a run nobody queued. So the failure is recorded
    # through the same durable authority the approval path uses - compare-and-set, so a terminal
    # result is never overwritten - and the caller still fails.
    from meshpipeline.application.pipeline_run import dispatch as dispatch_pipeline
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db

    try:
        async with get_db() as db:
            await dispatch_pipeline(db, str(child_job_id), payload)
    except Exception as exc:
        # Structured, and deliberately narrow: the failure CLASS, never its message, and never the
        # dispute text, a broker address or a credential. The message is preserved durably below,
        # where it is a diagnostic rather than something anyone is shown.
        logger.error("dispute dispatch failed - parent=%s child=%s operation=%s failure_class=%s",
                     parent_job_id, child_job_id, key[:16], type(exc).__name__)
        try:
            async with get_db() as db2:
                await JobRepository().mark_launch_failed(
                    db2, child_job_id, f"{type(exc).__name__}: {exc}"[:2000])
        except Exception as rec_exc:  # noqa: BLE001 - never mask the original failure
            logger.error("dispute dispatch failed - could not record launch failure for child=%s: %s",
                         child_job_id, rec_exc)
        raise
