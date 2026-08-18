# Responsibility: Verify every written source object stays reclaimable by an automatic authority, never only by a log.
# Boundaries: the real upload route, production get_db(), real PostgreSQL and a real object store.
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("real PostgreSQL and MinIO endpoints are required", allow_module_level=True)

_STEP = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((''),'2;1');\nENDSEC;\n"
         b"DATA;\nENDSEC;\nEND-ISO-10303-21;\n")


def _tenant() -> str:
    return f"srcrec-{uuid.uuid4().hex[:12]}"


class FailingAt:
    # A DELEGATING wrapper over the real object store. Every call reaches the real provider except
    # the one operation the test has declared must fail, so object-store state stays real - this is
    # what lets the double-failure path be exercised without a restricted MinIO identity, and
    # without the suite skipping.
    def __init__(self, inner, *, fail_upload=False, fail_delete=False):
        self._inner = inner
        self.fail_upload = fail_upload
        self.fail_delete = fail_delete
        self.deleted: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def upload_file(self, *, local_path, object_key):
        if self.fail_upload:
            raise OSError("test: object store refused the write")
        return self._inner.upload_file(local_path=local_path, object_key=object_key)

    def delete_object(self, *, object_key):
        if self.fail_delete:
            raise OSError("test: object store refused the delete")
        self.deleted.append(object_key)
        return self._inner.delete_object(object_key=object_key)


@pytest.fixture()
async def client():
    import httpx

    from meshpipeline.api import app as app_mod

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app),
                                 base_url="http://srcrec.test") as c:
        yield c


@pytest.fixture()
def real_store():
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.contracts import object_storage
    st = build_object_store()
    object_storage.set_object_store(st)
    yield st
    object_storage.set_object_store(None)


@pytest.fixture(autouse=True)
def _not_gated_by_another_suites_jobs(monkeypatch):
    # MAX_CONCURRENT_JOBS is a WHOLE-SYSTEM quota and this tier shares one database, so jobs left
    # active by other suites in the same run would refuse these uploads for capacity. That is an
    # ordering artefact, not a finding about this boundary - and this module is about the object
    # and its cleanup, never about quotas, which are proven in their own suite.
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "MAX_CONCURRENT_JOBS", 1_000_000, raising=False)
    monkeypatch.setattr(polcfg, "MAX_JOBS_PER_OWNER", 1_000_000, raising=False)


@pytest.fixture(autouse=True)
async def _own_rows_only():
    # Every row this module makes belongs to a `srcrec-` tenant, and the objects it makes are named
    # by its own intents - so cleanup removes exactly its own leavings and nothing shared.
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db

    async def purge():
        keys: list[str] = []
        async with get_db() as db:
            rows = await db.execute(text(
                "select object_key from source_object_cleanups where owner_id like 'srcrec-%'"))
            keys = [r[0] for r in rows.all()]
            rows = await db.execute(text(
                "select object_key from geometry_sources where owner_id like 'srcrec-%'"))
            keys += [r[0] for r in rows.all()]
            for table in ("chat_sessions", "geometry_interpretations", "geometry_sources",
                          "source_object_cleanups"):
                await db.execute(text(f"delete from {table} where owner_id like 'srcrec-%'"))  # noqa: S608 - fixed list
            await db.commit()
        if keys:
            from meshpipeline.adapters.object_storage.factory import build_object_store
            st = build_object_store()
            for k in set(keys):
                try:
                    st.delete_object(object_key=k)
                except Exception:  # noqa: BLE001 - already gone is the desired end state
                    pass

    await purge()
    yield
    await purge()


async def _upload(client, owner, name="part.step"):
    return await client.post("/api/v1/upload/step-file", headers={"X-User-Id": owner},
                             files={"file": (name, _STEP, "application/octet-stream")})


async def _cleanups(owner):
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        rows = await db.execute(text(
            "select object_key, state::text as state, retry_count, last_error "
            "from source_object_cleanups where owner_id = :o order by created_at"), {"o": owner})
        return [dict(r) for r in rows.mappings().all()]


async def _sources(owner):
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        rows = await db.execute(text(
            "select id, object_key from geometry_sources where owner_id = :o"), {"o": owner})
        return [dict(r) for r in rows.mappings().all()]


async def _reject_source_inserts(owner: str, install: bool):
    # A REAL database failure at the real point: the source insert itself is refused by PostgreSQL.
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        if install:
            await db.execute(text(
                "create or replace function srcrec_reject() returns trigger as $$ "
                "begin raise exception 'test: durable source insert refused'; end; $$ language plpgsql"))
            await db.execute(text(
                "create trigger srcrec_reject before insert on geometry_sources for each row "
                f"when (new.owner_id = '{owner}') execute function srcrec_reject()"))  # noqa: S608 - owner is test-generated
        else:
            await db.execute(text("drop trigger if exists srcrec_reject on geometry_sources"))
        await db.commit()


# 1 - the normal path leaves nothing pending


async def test_a_successful_upload_leaves_a_source_an_object_and_no_pending_cleanup(client, real_store):
    owner = _tenant()
    resp = await _upload(client, owner)
    assert resp.status_code == 200, resp.text

    sources = await _sources(owner)
    assert len(sources) == 1
    assert real_store.exists(object_key=sources[0]["object_key"])

    pending = [c for c in await _cleanups(owner) if c["state"] == "pending"]
    assert pending == [], f"a successful upload left a pending cleanup: {pending}"


# 2 - the object write fails


async def test_an_object_write_failure_leaves_no_source_no_object_and_nothing_stranded(client, real_store):
    from meshpipeline.contracts import object_storage
    owner = _tenant()
    wrapper = FailingAt(real_store, fail_upload=True)
    object_storage.set_object_store(wrapper)

    resp = await _upload(client, owner)
    assert resp.status_code == 503, resp.text

    assert await _sources(owner) == []
    records = await _cleanups(owner)
    assert len(records) == 1, records
    assert records[0]["state"] != "pending", "nothing was written, so the intent must be closed"
    assert not real_store.exists(object_key=records[0]["object_key"])


# 3 - the source transaction fails and compensation works


async def test_a_source_transaction_failure_deletes_exactly_that_object(client, real_store):
    from meshpipeline.contracts import object_storage
    owner = _tenant()
    wrapper = FailingAt(real_store)
    object_storage.set_object_store(wrapper)
    await _reject_source_inserts(owner, True)
    try:
        resp = await _upload(client, owner)
    finally:
        await _reject_source_inserts(owner, False)

    assert resp.status_code == 500, resp.text
    assert await _sources(owner) == []
    records = await _cleanups(owner)
    assert len(records) == 1
    key = records[0]["object_key"]
    assert wrapper.deleted == [key], f"compensation deleted {wrapper.deleted}, expected [{key}]"
    assert not real_store.exists(object_key=key)
    assert records[0]["state"] != "pending"


# 4 - the double failure this repair exists for


async def test_a_failed_compensation_leaves_the_object_but_a_durable_pending_cleanup(client, real_store):
    from meshpipeline.contracts import object_storage
    owner = _tenant()
    wrapper = FailingAt(real_store, fail_delete=True)
    object_storage.set_object_store(wrapper)
    await _reject_source_inserts(owner, True)
    try:
        resp = await _upload(client, owner)
    finally:
        await _reject_source_inserts(owner, False)

    assert resp.status_code == 500, resp.text
    assert await _sources(owner) == []

    records = await _cleanups(owner)
    assert len(records) == 1, records
    assert records[0]["state"] == "pending", "the orphan is recoverable only from a log"
    assert real_store.exists(object_key=records[0]["object_key"]), "the object should still be there"


# 5 - maintenance reclaims it


async def test_maintenance_deletes_the_exact_orphan_and_resolves_the_record(client, real_store):
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.contracts import object_storage
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    object_storage.set_object_store(FailingAt(real_store, fail_delete=True))
    await _reject_source_inserts(owner, True)
    try:
        await _upload(client, owner)
    finally:
        await _reject_source_inserts(owner, False)

    key = (await _cleanups(owner))[0]["object_key"]
    survivor = _tenant()
    survivor_resp = await _upload(client, survivor)          # an unrelated, healthy upload
    assert survivor_resp.status_code == 200
    survivor_key = (await _sources(survivor))[0]["object_key"]

    object_storage.set_object_store(real_store)
    async with get_db() as db:
        counts = await reclaim_orphan_source_objects(db, real_store)
        await db.commit()

    assert counts["deleted"] >= 1, counts
    assert not real_store.exists(object_key=key), "the orphan survived maintenance"
    assert real_store.exists(object_key=survivor_key), "maintenance removed an unrelated object"
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_deleted"]


# 6 - the object is already gone


async def test_an_already_absent_object_resolves_idempotently(real_store):
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    sid = uuid.uuid4()
    key = f"sources/{sid}"
    async with get_db() as db:
        await SourceCleanupRepository().record_intent(db, owner_id=owner, source_id=sid,
                                                      object_key=key)
        await db.commit()

    async with get_db() as db:
        counts = await reclaim_orphan_source_objects(db, real_store)
        await db.commit()
    assert counts["absent"] >= 1, counts
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_deleted"]

    # running it again changes nothing
    async with get_db() as db:
        await reclaim_orphan_source_objects(db, real_store)
        await db.commit()
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_deleted"]


# 7 + 9 - a live source must never be reclaimed


async def test_maintenance_never_deletes_an_object_a_live_source_owns(client, real_store):
    # This is also the "died after the source commit" window: an intent left pending beside a
    # committed source must resolve as adopted, never as a delete.
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    assert (await _upload(client, owner)).status_code == 200
    src = (await _sources(owner))[0]

    async with get_db() as db:
        # force the intent back to pending, as a crash between commit and close would have left it
        from sqlalchemy import text
        await db.execute(text("update source_object_cleanups set state='pending' "
                              "where object_key = :k"), {"k": src["object_key"]})
        await SourceCleanupRepository().record_intent(
            db, owner_id=owner, source_id=src["id"], object_key=src["object_key"])
        await db.commit()

    async with get_db() as db:
        counts = await reclaim_orphan_source_objects(db, real_store)
        await db.commit()

    assert counts["owned"] >= 1, counts
    assert real_store.exists(object_key=src["object_key"]), "a live source lost its object"
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_adopted"]


# 8 - the process dies between the write and the source transaction


async def test_an_object_written_before_a_process_death_stays_discoverable(real_store):
    # The upload's own ordering, stopped where a killed process would stop: intent committed, object
    # written, source transaction never reached.
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.contracts.geometry_source import source_object_key
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    sid = uuid.uuid4()
    key = source_object_key(sid)
    async with get_db() as db:
        await SourceCleanupRepository().record_intent(db, owner_id=owner, source_id=sid,
                                                      object_key=key)
        await db.commit()

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "part.step"
        p.write_bytes(_STEP)
        real_store.upload_file(local_path=p, object_key=key)
    assert real_store.exists(object_key=key)

    assert [c["state"] for c in await _cleanups(owner)] == ["pending"], "the object is undiscoverable"

    async with get_db() as db:
        await reclaim_orphan_source_objects(db, real_store)
        await db.commit()
    assert not real_store.exists(object_key=key)
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_deleted"]


# 10 - two reconcilers at once


async def test_two_concurrent_reconcilers_delete_once_and_agree(client, real_store, monkeypatch):
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.contracts import object_storage
    from meshpipeline.persistence.repositories import source_cleanup_repository as repo_mod
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    object_storage.set_object_store(FailingAt(real_store, fail_delete=True))
    await _reject_source_inserts(owner, True)
    try:
        await _upload(client, owner)
    finally:
        await _reject_source_inserts(owner, False)
    key = (await _cleanups(owner))[0]["object_key"]
    object_storage.set_object_store(real_store)

    counted = FailingAt(real_store)

    # THE BARRIER GOES IMMEDIATELY BEFORE THE CLAIM, not before the sweep. Releasing two sweeps
    # together does not produce the race: a sweep is a handful of round trips, so the first one
    # finishes and commits before the second even issues its SELECT, and the test would then pass
    # with no claim authority at all. Measured: with the row lock removed, sweep #1 claimed 0 rows
    # and sweep #2 claimed 1. Holding both at the door of the claim is what makes them contend.
    at_the_door = asyncio.Barrier(2)
    real_claim = repo_mod.SourceCleanupRepository.claim_pending

    async def synchronised(self, db, *, limit=50):
        await at_the_door.wait()
        return await real_claim(self, db, limit=limit)

    monkeypatch.setattr(repo_mod.SourceCleanupRepository, "claim_pending", synchronised)

    async def sweep():
        async with get_db() as db:
            out = await reclaim_orphan_source_objects(db, counted)
            await db.commit()
            return out

    results = await asyncio.gather(sweep(), sweep())

    assert counted.deleted.count(key) == 1, f"deleted {counted.deleted}"
    assert sum(r["deleted"] for r in results) == 1, results
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_deleted"]
    assert not real_store.exists(object_key=key)


async def test_a_claimed_record_is_invisible_to_a_second_reconciler(real_store):
    # THE claim property, stated without any timing assumption: while one reconciler holds a
    # record inside its open transaction, a second reconciler must not be handed the same record.
    # Ordering here is explicit - A claims and stays open, B then claims - so nothing depends on
    # which task the event loop happens to schedule first. (Two sweeps that merely run one after
    # the other are not a race, and each legitimately spends one retry.)
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    sid = uuid.uuid4()
    key = f"sources/{sid}"
    async with get_db() as db:
        await SourceCleanupRepository().record_intent(db, owner_id=owner, source_id=sid,
                                                      object_key=key)
        await db.commit()

    repo = SourceCleanupRepository()
    async with get_db() as first:
        held = await repo.claim_pending(first)
        assert any(c.object_key == key for c in held), "the first reconciler did not claim it"

        # a second reconciler, on its own connection, while the first still holds the row
        async with get_db() as second:
            seen = await repo.claim_pending(second)
            assert all(c.object_key != key for c in seen), (
                "a second reconciler was handed a record another one already holds")
        await first.rollback()

    # once the holder is gone the record is claimable again - this is also the stale-claim recovery
    async with get_db() as third:
        again = await repo.claim_pending(third)
        assert any(c.object_key == key for c in again), "the record never became claimable again"
        await third.rollback()


# 11 - a claim whose holder went away


async def test_a_stale_claim_is_taken_over_by_the_next_sweep(real_store):
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    sid = uuid.uuid4()
    key = f"sources/{sid}"
    async with get_db() as db:
        await SourceCleanupRepository().record_intent(db, owner_id=owner, source_id=sid,
                                                      object_key=key)
        await db.commit()

    # a reconciler claims the row and then dies without resolving it
    async with get_db() as db:
        claimed = await SourceCleanupRepository().claim_pending(db)
        assert any(c.object_key == key for c in claimed)
        await db.rollback()

    # the lock died with that transaction, so the next sweep may take the record over
    async with get_db() as db:
        counts = await reclaim_orphan_source_objects(db, real_store)
        await db.commit()
    assert counts["absent"] + counts["deleted"] >= 1, counts
    assert [c["state"] for c in await _cleanups(owner)] == ["resolved_deleted"]


# 12 - nothing about storage reaches the caller


async def test_a_double_failure_response_leaks_no_storage_detail(client, real_store):
    from meshpipeline.contracts import object_storage
    owner = _tenant()
    object_storage.set_object_store(FailingAt(real_store, fail_delete=True))
    await _reject_source_inserts(owner, True)
    try:
        resp = await _upload(client, owner)
    finally:
        await _reject_source_inserts(owner, False)

    body = resp.text
    assert resp.status_code == 500
    key = (await _cleanups(owner))[0]["object_key"]
    for secret in (key, "sources/", "minio", "bucket", "9000", "srcrec_reject", "/srv", "sha256"):
        assert secret.lower() not in body.lower(), f"{secret!r} leaked in {body!r}"


# 13 - retries are bounded


async def test_retries_are_bounded_and_the_record_is_finally_abandoned(real_store):
    from meshpipeline.application.maintenance.reconcile import reclaim_orphan_source_objects
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        MAX_CLEANUP_RETRIES,
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    sid = uuid.uuid4()
    key = f"sources/{sid}"
    async with get_db() as db:
        await SourceCleanupRepository().record_intent(db, owner_id=owner, source_id=sid,
                                                      object_key=key)
        await db.commit()

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "part.step"
        p.write_bytes(_STEP)
        real_store.upload_file(local_path=p, object_key=key)

    stubborn = FailingAt(real_store, fail_delete=True)
    for _ in range(MAX_CLEANUP_RETRIES + 1):
        async with get_db() as db:
            await reclaim_orphan_source_objects(db, stubborn)
            await db.commit()

    rec = (await _cleanups(owner))[0]
    assert rec["state"] == "abandoned", rec
    assert rec["retry_count"] <= MAX_CLEANUP_RETRIES + 1, rec
    assert rec["last_error"] and len(rec["last_error"]) <= 512

    real_store.delete_object(object_key=key)
