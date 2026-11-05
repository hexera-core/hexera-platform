# Responsibility: Reconcile stored objects the database has no row for.
# Boundaries: it reports and resolves orphans conservatively.
from __future__ import annotations

import logging

import meshpipeline.settings.providers as provcfg

logger = logging.getLogger(__name__)

RECONCILE_MAX_RETRIES = 5   # after this, a still-unresolved record is abandoned for manual attention


def reconcile_orphan_artifacts() -> dict:
    import asyncio
    return asyncio.run(_reconcile_async())


async def _reconcile_async() -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.models import Artifact, ReconciliationState
    from meshpipeline.persistence.repositories.reconciliation_repository import (
        ReconciliationRepository,
    )

    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    engine = create_async_engine(provcfg.POSTGRES_DSN, echo=False, pool_size=2, max_overflow=2,
                                 pool_pre_ping=True, connect_args=connect_args)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    store = get_object_store()
    recon_repo = ReconciliationRepository()
    counts = {"deleted": 0, "adopted": 0, "conflict": 0, "abandoned": 0, "skipped": 0}

    try:
        async with Session() as db:
            from sqlalchemy import select
            pending = await recon_repo.claim_pending(db, limit=100)
            for rec in pending:
                # 1: what is ACTUALLY at that key now? (never trust the key alone)
                try:
                    present = await _to_thread(store.exists, object_key=rec.object_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reconcile: exists() failed for %s: %s - leaving pending",
                                   rec.object_key, exc)
                    counts["skipped"] += 1
                    continue

                # 2: does a READY Artifact row reference this exact object? then the winner owns it.
                ready = (await db.execute(
                    select(Artifact).where(Artifact.job_id == rec.job_id,
                                           Artifact.logical_key == rec.logical_key))
                         ).scalar_one_or_none()

                if ready is not None and ready.storage_key == rec.object_key:
                    if ready.delivery_attempt >= rec.delivery_attempt:
                        # a winning (same-or-newer) attempt registered this object - adopt-complete.
                        await recon_repo.resolve(db, rec.id, state=ReconciliationState.resolved_adopted,
                                                 detail="a ready Artifact row already owns this object")
                        counts["adopted"] += 1
                        continue
                    # a ready row exists but for an OLDER attempt at the same key - do not disturb it.
                    await recon_repo.resolve(db, rec.id, state=ReconciliationState.blocked_conflict,
                                             detail="ready row references this object at an older attempt")
                    counts["conflict"] += 1
                    continue

                if not present:
                    # the object is already gone (a winning retry overwrote+deleted, or a prior
                    # reconcile removed it): nothing to clean.
                    await recon_repo.resolve(db, rec.id, state=ReconciliationState.resolved_deleted,
                                             detail="object no longer present")
                    counts["deleted"] += 1
                    continue

                # 3: the object is present and no ready row references it. Verify GENERATION: the
                # current object's checksum must still match what we orphaned - otherwise a newer
                # write replaced it and we must not delete someone else's object.
                current_ck = await _current_checksum(store, rec.object_key)
                if rec.object_checksum and current_ck and current_ck != rec.object_checksum:
                    await recon_repo.resolve(db, rec.id, state=ReconciliationState.blocked_conflict,
                                             detail=f"object checksum changed ({rec.object_checksum}"
                                                    f" → {current_ck}) - a newer write owns it")
                    counts["conflict"] += 1
                    continue

                # 4: DELETE the orphan. It is unreferenced (no ready row owns this exact object),
                # its identity + generation still match what we orphaned, and its job did not
                # succeed on it (a required-delivery failure leaves a failed job). Per the delivery-
                # truthfulness rule we CLEAN rather than adopt: adopting would resurrect a downloadable
                # artifact onto a failed job. The user retries the job to get a fresh, registered
                # deliverable. (A ready row already owning the object was handled above as adopted.)
                try:
                    await _to_thread(store.delete_object, object_key=rec.object_key)
                    await recon_repo.resolve(db, rec.id, state=ReconciliationState.resolved_deleted,
                                             detail="deleted after identity + generation verification")
                    counts["deleted"] += 1
                except Exception as exc:  # noqa: BLE001
                    if rec.retry_count + 1 >= RECONCILE_MAX_RETRIES:
                        await recon_repo.resolve(db, rec.id, state=ReconciliationState.abandoned,
                                                 detail=f"delete still failing after retries: {exc}")
                        counts["abandoned"] += 1
                    else:
                        await recon_repo.resolve(db, rec.id, state=ReconciliationState.pending,
                                                 detail=f"delete failed, will retry: {exc}")
                        counts["skipped"] += 1
            await db.commit()

        # The SAME sweep also reclaims orphaned source uploads. It lives in this entrypoint rather
        # than a daemon of its own because it is the same question about a different object kind:
        # does anything still own these bytes, and if not, remove them.
        async with Session() as db:
            # Flattened into the same counter rather than nested, so the sweep's result stays one
            # flat map of names to counts and the log line remains readable.
            for name, n in (await reclaim_orphan_source_objects(db, store)).items():
                counts[f"source_{name}"] = n
            await db.commit()
    finally:
        await engine.dispose()

    logger.info("reconcile_orphan_artifacts: %s", counts)
    return counts


async def reclaim_orphan_source_objects(db, store) -> dict:
    # ONE pass over the pending source-object cleanup intents. Every intent is committed before its
    # object is written, so the set of objects this can be asked about is exactly the set that could
    # ever have been stored - no bucket listing is needed, or wanted.
    from sqlalchemy import select

    from meshpipeline.persistence.models import GeometrySource, ReconciliationState
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )

    repo = SourceCleanupRepository()
    counts = {"deleted": 0, "absent": 0, "owned": 0, "failed": 0}

    for rec in await repo.claim_pending(db, limit=100):
        # 1. DOES A LIVE SOURCE OWN THESE BYTES? Asked first, and against the row rather than
        # inferred from the intent: an upload that succeeded must keep its object. The upload
        # closes its own intent inside the source transaction, so this is the belt-and-braces
        # answer for an intent that outlived its close for any other reason.
        owner_row = (await db.execute(
            select(GeometrySource).where(GeometrySource.id == rec.source_id))).scalar_one_or_none()
        if owner_row is not None and owner_row.object_key == rec.object_key:
            await repo.resolve(db, object_key=rec.object_key,
                               state=ReconciliationState.resolved_adopted,
                               detail="a live geometry source owns this object")
            counts["owned"] += 1
            continue

        # 2. IS ANYTHING ACTUALLY THERE? An absent object is a finished cleanup, not a failure:
        # the write may never have happened, or an earlier sweep may already have removed it.
        try:
            present = await _to_thread(store.exists, object_key=rec.object_key)
        except Exception as exc:  # noqa: BLE001 - the provider's message is an operator fact
            await repo.record_failure(db, row_id=rec.id, detail=f"exists() failed: {exc}")
            counts["failed"] += 1
            continue
        if not present:
            await repo.resolve(db, object_key=rec.object_key,
                               state=ReconciliationState.resolved_deleted,
                               detail="object is already absent")
            counts["absent"] += 1
            continue

        # 3. REMOVE EXACTLY THIS KEY - never a prefix and never a listing. The key came from the
        # intent, which was generated from one uuid for one upload.
        try:
            await _to_thread(store.delete_object, object_key=rec.object_key)
            await repo.resolve(db, object_key=rec.object_key,
                               state=ReconciliationState.resolved_deleted,
                               detail="orphaned source object removed by maintenance")
            counts["deleted"] += 1
        except Exception as exc:  # noqa: BLE001
            await repo.record_failure(db, row_id=rec.id, detail=f"delete failed: {exc}")
            counts["failed"] += 1

    logger.info("reclaim_orphan_source_objects: %s", counts)
    return counts


async def _to_thread(fn, **kwargs):
    import asyncio
    return await asyncio.to_thread(lambda: fn(**kwargs))


async def _current_checksum(store, object_key: str) -> str | None:
    getter = getattr(store, "object_checksum", None)
    if callable(getter):
        try:
            return await _to_thread(getter, object_key=object_key)
        except Exception:  # noqa: BLE001
            return None
    return None
