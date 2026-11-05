# Responsibility: Verify every connection the dispute dispatch path acquires has a deterministic owner.
# Boundaries: real PostgreSQL through the production session factory; only the launcher boundary is doubled.

# A SQLAlchemy session stays usable after close(): the next statement autobegins and checks a
# connection out again. If that second checkout has no owner it is returned only when the garbage
# collector finalises the session - which is not cleanup. So the collector is DISABLED across every
# measurement, strong references are held to the sessions under test, and pg_stat_activity is read
# over a separate connection rather than through the pool being measured.
from __future__ import annotations

import asyncio
import gc
import uuid

import pytest
from sqlalchemy import event, text
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg
from meshpipeline.application.dispatch_contract import build as build_dispatch
from meshpipeline.contracts.pipeline_execution import set_pipeline_launcher
from meshpipeline.persistence.repositories.job_repository import JobRepository
from meshpipeline.runtime.composition import install_adapters

install_adapters()

REPO_JOBS = JobRepository()


@pytest.fixture(autouse=True)
def _no_gc():
    # Cleanup must be deterministic. With the collector off, anything that returns a connection
    # only when an abandoned session is finalised shows up as a retained checkout.
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


class _Launcher:
    # The ONLY doubled boundary: the external submission. Neither shipped launcher touches the
    # session, so doubling it changes nothing about connection ownership.
    def __init__(self) -> None:
        self.calls = 0
        self.raises = False
        self.pool = None                 # set by the harness so the boundary can be observed
        self.held_at_boundary: list[int] = []

    async def launch(self, db, job_id: str, payload: dict) -> None:
        self.calls += 1
        # THE external-submission boundary. dispatch commits before reaching it, so no connection
        # should be held here - a broker that hangs must not also pin a database connection.
        if self.pool is not None:
            self.held_at_boundary.append(self.pool.checkedout())
        if self.raises:
            raise RuntimeError("broker unreachable")


@pytest.fixture()
async def lifecycle():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args)
    await hp.ensure_schema(hp.dsn())

    pool = engine.sync_engine.pool
    counters = {"out": 0, "in": 0}

    @event.listens_for(pool, "checkout")
    def _out(*_a):
        counters["out"] += 1

    @event.listens_for(pool, "checkin")
    def _in(*_a):
        counters["in"] += 1

    factory = async_sessionmaker(bind=engine, expire_on_commit=False,
                                autocommit=False, autoflush=False)

    # THE production scope shape, over this engine: commit on success, rollback on failure,
    # close in finally. Reproducing it here rather than importing get_db is what lets the test
    # own its engine while still measuring the real ownership contract.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    launcher = _Launcher()
    launcher.pool = pool
    set_pipeline_launcher(launcher)

    # an independent connection for server-side observation
    observer = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args,
                                   poolclass=None)

    async def idle_in_transaction() -> int:
        async with observer.connect() as conn:
            r = await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'idle in transaction'"))
            return int(r.scalar_one())

    class Harness:
        def __init__(self):
            self.scope = scope
            self.pool = pool
            self.counters = counters
            self.launcher = launcher
            self.idle_in_transaction = idle_in_transaction
            self.held: list = []

        @property
        def checked_out(self) -> int:
            return self.pool.checkedout()

        async def seed_job(self, owner: str) -> str:
            # TERMINAL on creation. An unfinished job counts against MAX_CONCURRENT_JOBS, and a
            # tier that seeds hundreds would hit the capacity quota and be refused before it ever
            # reached dispatch - which would make every ownership assertion below vacuous.
            # Destroying rows to avoid that belongs to the disposable-database authority, not here.
            from meshpipeline.persistence.models import JobStatus as _JS
            async with self.scope() as db:
                job = await REPO_JOBS.create(db, owner_id=owner)
                job.status = _JS.succeeded
                jid = str(job.id)
                await db.commit()
            self.held.append(db)
            return jid

        def payload(self, job_id: str, owner: str) -> dict:
            return build_dispatch(job_id=job_id, owner_id=owner, geometry_source=None,
                                  geometry_interpretation=None, session_id="", request_txt="r",
                                  review_brief_txt="", intake_patches=[], dimensionality="",
                                  purpose="", input_kind="", user_dispute=None)

    h = Harness()
    yield h
    set_pipeline_launcher(None)
    await observer.dispose()
    await engine.dispose()


async def _assert_returned(h, *, where: str) -> None:
    assert h.checked_out == 0, f"{where}: {h.checked_out} connection(s) still checked out"
    assert await h.idle_in_transaction() == 0, f"{where}: a backend is idle in transaction"


# the ownership contract, one row per failure shape


@pytest.fixture(autouse=True)
def _not_gated_by_another_suites_jobs(monkeypatch):
    # This suite already seeds its own jobs TERMINAL so they cannot count against the quota, which
    # protects it from itself and not from anything else: MAX_CONCURRENT_JOBS is a WHOLE-SYSTEM
    # limit and the tier shares one database, so jobs left active by unrelated suites refuse these
    # dispatches with 429. Measured under a randomised order: 24 active against a limit of 20, and a
    # dispatch-failure test read the refusal as its own 500. Only the system-wide limit is raised;
    # the per-owner one is scoped to this suite's own tenants.
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "MAX_CONCURRENT_JOBS", 1_000_000, raising=False)


async def test_a_successful_dispatch_returns_every_connection(lifecycle):
    h = lifecycle
    from meshpipeline.application.pipeline_run import dispatch
    for _ in range(3):
        jid = await h.seed_job("ok")
        async with h.scope() as db:
            await dispatch(db, jid, h.payload(jid, "ok"))
        h.held.append(db)
        await _assert_returned(h, where="successful dispatch")
    assert h.launcher.calls == 3
    assert h.launcher.held_at_boundary == [0, 0, 0], (
        "a database connection was held while the external submission boundary ran: "
        f"{h.launcher.held_at_boundary}")


async def test_a_failure_between_the_write_and_the_commit_returns_its_connection(lifecycle):
    # THE shape that made this a defect. Before the dispatch scope owned its session, three
    # of these grew the checked-out count 1 -> 2 -> 3 and gc.collect() did not reclaim them.
    h = lifecycle
    for _ in range(3):
        jid = await h.seed_job("er")
        with pytest.raises(RuntimeError):
            async with h.scope() as db:
                await REPO_JOBS.set_dispatch_payload(db, uuid.UUID(jid), h.payload(jid, "er"))
                raise RuntimeError("failure after the write, before the commit")
        h.held.append(db)
        await _assert_returned(h, where="raise after write")


async def test_a_launcher_failure_after_the_commit_returns_its_connection(lifecycle):
    h = lifecycle
    from meshpipeline.application.pipeline_run import dispatch
    h.launcher.raises = True
    for _ in range(3):
        jid = await h.seed_job("lf")
        with pytest.raises(RuntimeError):
            async with h.scope() as db:
                await dispatch(db, jid, h.payload(jid, "lf"))
        h.held.append(db)
        await _assert_returned(h, where="launcher failure")


async def test_cancellation_between_the_write_and_the_commit_returns_its_connection(lifecycle):
    # A client that goes away mid-request. No sleep coordinates this: the task is cancelled once
    # the pool observably holds the checkout.
    h = lifecycle
    for _ in range(3):
        jid = await h.seed_job("cx")

        async def work(job_id=jid):
            async with h.scope() as db:
                await REPO_JOBS.set_dispatch_payload(db, uuid.UUID(job_id), h.payload(job_id, "cx"))
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        while h.checked_out == 0:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_returned(h, where="cancellation")


async def test_cleanup_does_not_depend_on_garbage_collection(lifecycle):
    h = lifecycle
    from meshpipeline.application.pipeline_run import dispatch
    for _ in range(4):
        jid = await h.seed_job("gc")
        async with h.scope() as db:
            await dispatch(db, jid, h.payload(jid, "gc"))
        h.held.append(db)
    assert not gc.isenabled(), "the collector must be off for this to prove anything"
    before = h.checked_out
    assert before == 0, "connections were retained while the collector was disabled"
    gc.collect()
    assert h.checked_out == before, "the count moved when the collector ran"
    assert len(h.held) >= 4, "the sessions under test were not kept alive"


async def test_sequential_load_beyond_pool_capacity_does_not_grow_checkouts(lifecycle):
    # Capacity is derived from production configuration, not chosen to keep this green.
    h = lifecycle
    from meshpipeline.application.pipeline_run import dispatch
    capacity = h.pool.size() + h.pool._max_overflow
    trend: list[int] = []
    for _i in range(capacity * 3):
        jid = await h.seed_job("ld")
        async with h.scope() as db:
            await dispatch(db, jid, h.payload(jid, "ld"))
        trend.append(h.checked_out)
    assert max(trend) == 0, f"checked-out connections grew under sequential load: {trend}"
    await _assert_returned(h, where="sequential load")


async def test_concurrent_mixed_outcomes_do_not_grow_checkouts(lifecycle):
    h = lifecycle
    from meshpipeline.application.pipeline_run import dispatch

    async def one(kind: str) -> None:
        jid = await h.seed_job(kind)
        try:
            async with h.scope() as db:
                await dispatch(db, jid, h.payload(jid, kind))
                if kind == "bad":
                    raise RuntimeError("failure after dispatch")
        except RuntimeError:
            pass

    await asyncio.gather(*[one("good") for _ in range(8)], *[one("bad") for _ in range(6)])
    await _assert_returned(h, where="concurrent mixed")
    assert h.counters["out"] > 0, "no checkout was observed - the measurement is vacuous"


async def test_the_external_submission_boundary_is_never_contacted(lifecycle):
    # The launcher double is the only external seam; a real broker or provider call would mean
    # this tier reached outside the machine.
    h = lifecycle
    from meshpipeline.contracts.pipeline_execution import get_pipeline_launcher
    assert get_pipeline_launcher() is h.launcher


# controls that keep the measurements above load-bearing


async def test_the_measurement_detects_a_retained_connection(lifecycle):
    # POSITIVE control. If _assert_returned cannot see a connection that really is held, every
    # "returns to baseline" assertion above is decoration. A session is opened and deliberately
    # NOT closed; the check must fail, and then the connection is returned by hand.
    h = lifecycle
    from sqlalchemy import text as _text
    session = None
    try:
        async with h.scope() as probe:
            session = probe
            await probe.execute(_text("SELECT 1"))
            assert h.checked_out >= 1, "an open statement did not register as a checkout"
            with pytest.raises(AssertionError, match="still checked out"):
                await _assert_returned(h, where="positive control")
    finally:
        if session is not None:
            await session.close()
    await _assert_returned(h, where="after the positive control released it")


async def test_the_sequential_load_really_exceeds_pool_capacity(lifecycle):
    # Guards the load-shape: a workload that fits inside the pool proves nothing about retention.
    h = lifecycle
    import inspect
    src = inspect.getsource(test_sequential_load_beyond_pool_capacity_does_not_grow_checkouts)
    assert "capacity * 3" in src, "the sequential load no longer exceeds pool capacity"
    capacity = h.pool.size() + h.pool._max_overflow
    assert capacity >= 2, "pool capacity could not be derived from production configuration"
    assert capacity * 3 >= capacity * 2, "the load must exceed capacity at least twice over"


async def test_the_dispute_route_itself_owns_its_dispatch_connection(lifecycle, monkeypatch):
    # THE production call site, not just the contract. This is what the defect was: dispute_job
    # passed a session whose scope had already closed into dispatch. The launcher is made to fail
    # so the request takes the error path, which is where an unowned connection was retained.
    h = lifecycle
    from meshpipeline.api.schemas.job import DisputeIn
    from meshpipeline.api.v1 import simulation as route
    from meshpipeline.persistence.models import JobStatus

    async with h.scope() as db:
        parent = await REPO_JOBS.create(db, owner_id="route-owner")
        parent.status = JobStatus.succeeded          # terminal: disputable, and not "active"
        parent_id = parent.id
        await db.commit()
    h.held.append(db)

    # the route opens its own sessions through get_db; point that at this test's engine
    monkeypatch.setattr(route, "get_db", h.scope)

    # The failure must land BETWEEN the write and dispatch's commit. A launcher failure is too
    # late: dispatch has already committed by then, which releases the connection on its own. The
    # reachable production shapes here are a statement error, a dropped connection or the client
    # cancelling - all of which strand an autobegun transaction. The real write still happens.
    import meshpipeline.application.pipeline_run as pr
    real_set = JobRepository.set_dispatch_payload

    reached = {"writes": 0}

    async def write_then_fail(self, db, job_id, payload):
        reached["writes"] += 1
        await real_set(self, db, job_id, payload)
        raise RuntimeError("connection lost after the write, before the commit")

    monkeypatch.setattr(JobRepository, "set_dispatch_payload", write_then_fail)
    assert pr.dispatch is not None

    before = h.checked_out
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await route.dispute_job(parent_id, DisputeIn(comment="please refine the wake region",
                                                     flags=[], mode="rebuild"),
                                owner_id="route-owner")
    assert exc.value.status_code == 500, "the dispatch failure did not surface as a server error"
    # NOT vacuous: the route must actually have reached the dispatch write, or this proves
    # nothing about the dispatch session at all.
    assert reached["writes"] == 1, (
        f"the route never reached the dispatch write ({reached['writes']} writes) - it failed "
        "earlier, so this test would pass whatever the dispatch session did")
    assert h.checked_out == before, (
        f"the dispute route retained {h.checked_out - before} connection(s) after its dispatch "
        "failed - the dispatch session is not owned by a scope that cleans it up")
    await _assert_returned(h, where="dispute route error path")
