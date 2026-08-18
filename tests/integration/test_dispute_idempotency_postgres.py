# Responsibility: Verify one dispute operation yields one child and one dispatch, and no pending orphan.
# Boundaries: the real route, production get_db() and real PostgreSQL; only the broker is a double.
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("real PostgreSQL is required", allow_module_level=True)


def _tenant(tag: str = "") -> str:
    # A FRESH tenant per test. The suite shares one database, so an owner that outlived its test
    # would make these assertions depend on execution order - which is exactly what an idempotency
    # suite must not do.
    return f"dispute-{tag}{uuid.uuid4().hex[:12]}"


class CountingLauncher:
    # An OBSERVING command double for the broker only. It publishes nothing itself, counts what the
    # route asked for, and can refuse - which is the one way to reach the dispatch-failure path
    # without breaking the database the rest of the assertions read. Everything before it (payload
    # validation, set_dispatch_payload, the commit) is the real `pipeline_run.dispatch`, so the
    # route's call ordering is preserved exactly.
    def __init__(self) -> None:
        self.accepted: list[str] = []
        self.rejected: list[str] = []
        self.refuse = False

    async def launch(self, db, job_id: str, payload: dict) -> None:
        if self.refuse:
            self.rejected.append(job_id)
            raise RuntimeError("broker refused the message")
        self.accepted.append(job_id)

    @property
    def calls(self) -> int:
        return len(self.accepted) + len(self.rejected)


@pytest.fixture(autouse=True)
def _not_gated_by_another_suites_jobs(monkeypatch):
    # The fixture below removes this module's OWN leftovers, which is enough in declaration order
    # and not enough in any other: MAX_CONCURRENT_JOBS is a WHOLE-SYSTEM quota and the tier shares
    # one database, so jobs left active by unrelated suites refuse these disputes with 429 before
    # the boundary under test is reached. Measured: a randomised pass hit 36 active jobs against a
    # limit of 20 and failed eleven tests here for a reason that has nothing to do with idempotency.
    # Quotas are proven in their own suite; this one is about dispute identity.
    #
    # ONLY the system-wide limit. MAX_JOBS_PER_OWNER is counted per tenant and every tenant here
    # carries this module's own `dispute-` prefix, so no other suite can contaminate it - and one
    # test in this module deliberately exercises it, deriving its loop bound from the value.
    # Raising that one too made it ask for a million disputes.
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "MAX_CONCURRENT_JOBS", 1_000_000, raising=False)


@pytest.fixture(autouse=True)
async def _only_this_test_s_tenants():
    # MAX_CONCURRENT_JOBS is a whole-system quota, so a child left behind by one test would refuse
    # the next one on capacity. Every row this module creates belongs to a `dispute-` tenant, and
    # nothing else in the tier uses that prefix, so this removes exactly its own leftovers - before
    # as well as after, so a previously interrupted run cannot poison a fresh one.
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db

    async def purge():
        async with get_db() as db:
            for table in ("chat_sessions", "simulation_jobs",
                          "geometry_interpretations", "geometry_sources"):
                await db.execute(text(f"delete from {table} where owner_id like 'dispute-%'"))  # noqa: S608 - fixed table list
            await db.commit()

    await purge()
    yield
    await purge()


@pytest.fixture()
def launcher():
    from meshpipeline.contracts import pipeline_execution as pe

    previous = pe._launcher
    double = CountingLauncher()
    pe.set_pipeline_launcher(double)
    yield double
    pe._launcher = previous


@pytest.fixture()
async def client():
    import httpx

    from meshpipeline.api import app as app_mod

    # httpx over the ASGI app, on ONE event loop: the process-cached engine binds pooled
    # connections to the loop that opened them, and TestClient's per-request portal loop would
    # break the second request. uvicorn runs one loop per process, so this matches production.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app),
                                 base_url="http://dispute.test") as c:
        yield c


def _headers(owner: str) -> dict:
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.api.security import expected_user_sig

    head = {"X-User-Id": owner}
    if polcfg.MESH_API_KEY:
        head["X-API-Key"] = polcfg.MESH_API_KEY
    if polcfg.USER_TOKEN_SECRET:
        head["X-User-Sig"] = expected_user_sig(owner)
    return head


async def _parent(owner: str) -> uuid.UUID:
    # Built through the PRODUCTION authorities. A succeeded job always carries a geometry source and
    # its interpretation together - dispatch_contract refuses the pair split - so the fixture does
    # too, or the dispute could never be dispatched at all.
    from meshpipeline.contracts.geometry_source import source_object_key
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.models import GeometrySource, JobStatus
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db

    async with get_db() as db:
        sid = uuid.uuid4()
        db.add(GeometrySource(id=sid, owner_id=owner, original_filename="parent.step",
                              suffix_hint=".step", object_key=source_object_key(sid),
                              sha256=uuid.uuid4().hex * 2, size_bytes=48))
        await db.flush()
        interp = await GeometryInterpretationRepository().record(
            db, owner_id=owner, geometry_source_id=sid, unit=LengthUnit.millimetre,
            basis=ResolutionBasis.file_declared, evidence="fixture")
        job = await JobRepository().create(db, owner_id=owner)
        job.status = JobStatus.succeeded
        job.geometry_source_id = sid
        job.geometry_interpretation_id = interp.interpretation_id
        session = await SessionRepository().create(db, owner)
        session.job_id = job.id
        session.geometry_source_id = sid
        session.request_txt = "mesh it"
        await db.flush()
        job_id = job.id
        await db.commit()
    return job_id


async def _children(owner: str) -> list[dict]:
    # Read on an INDEPENDENT connection, by the operation's own durable identity rather than by
    # counting rows the test believes it created.
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        res = await db.execute(text(
            "select id, status::text as status, dispute_operation_key, pipeline_dispatch_state "
            "from simulation_jobs "
            "where owner_id = :o and dispute_operation_key is not null order by created_at"),
            {"o": owner})
        return [dict(r) for r in res.mappings().all()]


def _dispute(client, parent_id, owner, comment="please refine the wake", flags=None):
    return client.post(f"/api/v1/simulation/{parent_id}/dispute", headers=_headers(owner),
                       json={"comment": comment, "flags": flags or [], "mode": "rebuild"})


# one operation, one child


async def test_a_first_dispute_creates_one_child_and_one_dispatch(client, launcher):
    owner = _tenant()
    parent = await _parent(owner)

    resp = await _dispute(client, parent, owner)
    assert resp.status_code == 202, resp.text

    children = await _children(owner)
    assert len(children) == 1, children
    assert str(children[0]["id"]) == resp.json()["job_id"]
    assert launcher.calls == 1, (launcher.accepted, launcher.rejected)


async def test_an_identical_repeat_returns_the_same_child_and_does_not_dispatch_again(client, launcher):
    owner = _tenant()
    parent = await _parent(owner)

    first = await _dispute(client, parent, owner)
    assert first.status_code == 202, first.text

    repeat = await _dispute(client, parent, owner)
    assert repeat.status_code == 202, repeat.text
    assert repeat.json()["job_id"] == first.json()["job_id"], "the repeat started a second run"

    assert len(await _children(owner)) == 1
    assert launcher.calls == 1, "the idempotent replay dispatched again"


async def test_concurrent_identical_requests_create_one_child_and_one_dispatch(
        client, launcher, monkeypatch):
    from meshpipeline.application import dispute_operation

    owner = _tenant()
    parent = await _parent(owner)

    # THE BARRIER GOES AT THE CLAIM, not at the client. Releasing four clients together does NOT
    # produce the race: per-request start-up costs more than the whole transaction, so the first
    # request commits before the others even reach the claim, and the suite would then pass whether
    # or not any concurrency authority existed. Measured: claim #1 returned at t+1.2ms and claims
    # #2-#4 did not arrive until t+18ms. Holding every request at the door until all four are
    # present is what makes them contend; the real claim then runs completely unchanged.
    at_the_door = asyncio.Barrier(4)
    real_claim = dispute_operation.claim

    async def synchronised(db, *, owner_id, key):
        await at_the_door.wait()
        return await real_claim(db, owner_id=owner_id, key=key)

    monkeypatch.setattr(dispute_operation, "claim", synchronised)

    async def one():
        return await _dispute(client, parent, owner)

    results = await asyncio.gather(*(one() for _ in range(4)))

    assert all(r.status_code == 202 for r in results), [(r.status_code, r.text) for r in results]
    ids = {r.json()["job_id"] for r in results}
    assert len(ids) == 1, f"concurrent identical requests produced {len(ids)} children: {ids}"

    children = await _children(owner)
    assert len(children) == 1, children
    assert str(children[0]["id"]) == ids.pop()
    assert launcher.calls == 1, (launcher.accepted, launcher.rejected)


# what makes an operation DIFFERENT


async def test_different_feedback_creates_a_distinct_child(client, launcher):
    owner = _tenant()
    parent = await _parent(owner)

    first = await _dispute(client, parent, owner, comment="refine the wake")
    second = await _dispute(client, parent, owner, comment="refine the leading edge")
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]
    assert len(await _children(owner)) == 2
    assert launcher.calls == 2


async def test_the_same_feedback_on_another_parent_is_a_distinct_operation(client, launcher):
    owner = _tenant()
    one, two = await _parent(owner), await _parent(owner)

    first = await _dispute(client, one, owner, comment="identical text")
    second = await _dispute(client, two, owner, comment="identical text")
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]
    assert launcher.calls == 2


async def test_whitespace_is_not_folded_because_the_comment_is_stored_verbatim(client, launcher):
    # The canonicalisation is exactly what the route already applies and nothing more. This states
    # that out loud, so a later "helpful" normalisation cannot be introduced without failing here.
    owner = _tenant()
    parent = await _parent(owner)

    first = await _dispute(client, parent, owner, comment="refine the wake")
    second = await _dispute(client, parent, owner, comment="refine the wake ")
    assert first.json()["job_id"] != second.json()["job_id"]
    assert launcher.calls == 2


# the ownership boundary the audit proved, kept


async def test_a_cross_owner_dispute_is_indistinguishable_from_a_missing_job(client, launcher):
    owner, stranger = _tenant("a-"), _tenant("b-")
    parent = await _parent(owner)

    foreign = await _dispute(client, parent, stranger)
    absent = await _dispute(client, uuid.uuid4(), owner)

    assert foreign.status_code == absent.status_code == 404
    assert foreign.json() == absent.json() == {"detail": "Job not found"}
    assert await _children(owner) == []
    assert await _children(stranger) == []
    assert launcher.calls == 0


# dispatch failure


async def test_a_dispatch_failure_leaves_no_pending_orphan(client, launcher):
    owner = _tenant()
    parent = await _parent(owner)
    launcher.refuse = True

    resp = await _dispute(client, parent, owner)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Failed to enqueue dispute job"}

    children = await _children(owner)
    assert len(children) == 1, "the failed dispatch created more than one child"
    assert children[0]["status"] != "pending", (
        "the child is still pending although nothing was queued - it will consume the owner's "
        "concurrency budget forever")
    assert launcher.rejected and not launcher.accepted


async def test_a_failed_child_carries_the_launch_failure_classification(client, launcher):
    owner = _tenant()
    parent = await _parent(owner)
    launcher.refuse = True

    await _dispute(client, parent, owner)

    child = (await _children(owner))[0]
    assert child["status"] == "failed", child
    assert child["pipeline_dispatch_state"] == "launch_failed", child


async def test_an_identical_retry_after_a_failed_dispatch_creates_no_second_child(client, launcher):
    # THE DECLARED POLICY: one operation identity is one child, for good. A retry of the same
    # request resolves to the operation that already failed and is answered with the same sanitized
    # failure; it does not quietly start a second run. Retrying with different feedback is a
    # different operation and is allowed - the test below proves that still works.
    owner = _tenant()
    parent = await _parent(owner)
    launcher.refuse = True
    first = await _dispute(client, parent, owner)
    assert first.status_code == 500

    launcher.refuse = False
    retry = await _dispute(client, parent, owner)
    assert retry.status_code == 500, retry.text
    assert retry.json() == {"detail": "Failed to enqueue dispute job"}

    assert len(await _children(owner)) == 1
    assert not launcher.accepted, "the retry dispatched against a terminal child"


async def test_a_later_different_dispute_still_works_after_a_failure(client, launcher):
    owner = _tenant()
    parent = await _parent(owner)
    launcher.refuse = True
    assert (await _dispute(client, parent, owner, comment="first attempt")).status_code == 500

    launcher.refuse = False
    ok = await _dispute(client, parent, owner, comment="second attempt, different words")
    assert ok.status_code == 202, ok.text
    assert len(await _children(owner)) == 2
    assert launcher.accepted == [ok.json()["job_id"]]


# quotas and connections


async def test_failed_dispatch_and_duplicates_do_not_exhaust_the_quota(client, launcher):
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db

    owner = _tenant()
    parent = await _parent(owner)
    launcher.refuse = True

    # more repeats than the quota would ever allow, every one of them the same operation
    for _ in range(polcfg.MAX_JOBS_PER_OWNER + 2):
        assert (await _dispute(client, parent, owner, comment="doomed")).status_code == 500

    async with get_db() as db:
        active = await JobRepository().count_active_for_owner(db, owner)
    assert active == 0, f"{active} active job(s) left by a dispatch that never queued anything"
    assert len(await _children(owner)) == 1

    launcher.refuse = False
    fresh = await _dispute(client, parent, owner, comment="a genuinely new request")
    assert fresh.status_code == 202, fresh.text


async def test_no_idle_in_transaction_or_connection_leak_after_conflict_and_failure(client, launcher):
    # Measured from the SERVER, never from the engine's own bookkeeping: a pool that miscounts its
    # checkouts would agree with itself. `pg_stat_activity` is the independent witness.
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db

    async def backends() -> tuple[int, int]:
        async with get_db() as db:
            row = (await db.execute(text(
                "select count(*) filter (where state = 'idle in transaction'), count(*) "
                "from pg_stat_activity where datname = current_database()"))).first()
        return int(row[0]), int(row[1])

    async def one_cycle():
        owner, stranger = _tenant("a-"), _tenant("b-")
        parent = await _parent(owner)
        launcher.refuse = False
        await _dispute(client, parent, owner)                        # committed
        await _dispute(client, parent, owner)                        # idempotent replay
        await _dispute(client, parent, stranger)                     # 404
        launcher.refuse = True
        await _dispute(client, parent, owner, comment="doomed")      # 500 with compensation

    await one_cycle()
    stuck_first, total_first = await backends()
    assert stuck_first == 0, f"{stuck_first} connection(s) left idle in transaction"

    # A leak shows as growth, not as an absolute number: a pooled connection is meant to stay open,
    # so the second identical cycle must not need any more of them than the first.
    await one_cycle()
    stuck_second, total_second = await backends()
    assert stuck_second == 0, f"{stuck_second} connection(s) left idle in transaction"
    assert total_second <= total_first, (
        f"connections grew {total_first} -> {total_second} across two identical "
        "conflict-and-failure cycles")
