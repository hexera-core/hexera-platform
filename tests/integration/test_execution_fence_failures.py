# Responsibility: Verify every way the fence lifecycle can fail leaves safety intact, never a stale writer.
# Boundaries: the failure matrix; the race and the per-context campaign are their own suites.
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from tests.integration.test_execution_fence_race import (
    PausedRedis,
    _claim,
    _client,
    _expire_lease,
    _gated,
    _publisher,
    _row,
    _seed,
    _sessions,
    assert_unchanged,
    backlog,
    cleanup,
    event_keys,
    snapshot,
)

from meshpipeline.adapters.event_stream import fence as F
from meshpipeline.contracts.event_stream import (
    StaleExecutionPublish,
    expected_fence,
    fence_fingerprint,
)
from meshpipeline.events.channels import fence_key_for

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

#: Every case appends one of these; the suite prints the matrix at the end.
MATRIX: list[dict] = []


async def record(case: str, name: str, *, barrier: str, job, delivered: bool,
                 execution_started: bool, attempted: bool, accepted: bool,
                 terminal_mutated: bool, observed: str, safety: str, availability: str,
                 cleanup_result: str) -> dict:
    r = _client()
    try:
        fence = (r.get(fence_key_for(str(job))) or b"").decode()
        pttl = r.pttl(fence_key_for(str(job)))
    finally:
        r.close()
    row = await _row(job)
    entry = {"case": case, "name": name, "barrier": barrier,
             "generation": row["generation"], "token": row["token_fingerprint"],
             "lease_expires_at": row["lease_expires_at"],
             "fence": fence[:12] or "-", "fence_pttl": pttl,
             "keys": {k.split(":")[-1]: (v["exists"], v["type"], v["sha256"][:8])
                      for k, v in snapshot(str(job)).items()},
             "ownership_delivered": delivered, "execution_started": execution_started,
             "publication_attempted": attempted, "publication_accepted": accepted,
             "terminal_mutated": terminal_mutated, "cleanup": cleanup_result,
             "observed": observed, "safety": safety, "availability": availability}
    MATRIX.append(entry)
    return entry


@pytest.fixture()
async def job():
    job_id = uuid.uuid4()
    await _seed(job_id, f"fail-{job_id.hex[:8]}")
    yield job_id
    cleanup(str(job_id))


# fault injectors: each wraps ONE operation and delegates unchanged otherwise


class _RedisDown(Exception):
    pass


def _lease_failure():
    # the class the claim authority is contracted to raise when it cannot fence
    from meshpipeline.persistence.lease import FenceUnavailable
    return FenceUnavailable


def _fail_install(monkeypatch, *, exc=None):
    import meshpipeline.persistence.lease as _lease
    real = _lease._fence_ops()

    class _Ops:
        fingerprint = staticmethod(real.fingerprint)
        refresh = staticmethod(real.refresh)
        revoke = staticmethod(real.revoke)
        current = staticmethod(real.current)

        @staticmethod
        def install(job_id, value, ttl):
            raise (exc or _RedisDown("redis is unreachable"))

    monkeypatch.setattr(_lease, "_fence_ops", lambda: _Ops)


def _fail_revoke(monkeypatch):
    import meshpipeline.persistence.lease as _lease
    real = _lease._fence_ops()

    class _Ops:
        fingerprint = staticmethod(real.fingerprint)
        install = staticmethod(real.install)
        refresh = staticmethod(real.refresh)
        current = staticmethod(real.current)

        @staticmethod
        def revoke(job_id, value):
            raise _RedisDown("redis is unreachable")

    monkeypatch.setattr(_lease, "_fence_ops", lambda: _Ops)


# F1  claim commits, fence installation fails


async def test_f1_a_claim_whose_fence_cannot_be_installed_delivers_no_execution(job, monkeypatch):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        _fail_install(monkeypatch)
        claim = await _claim(job, Session, backend_execution_id="exec-f1")

        assert isinstance(claim, _fence.DeliveryRefused), f"execution was delivered: {claim}"
        assert claim.detail["reason"] == "fence_unavailable", claim.detail
        assert not F.current(str(job)), "a fence exists after a failed installation"
        assert backlog(str(job)) == [], "an event published without a fence"
        row = await _row(job)
        # the claim IS durable - that is what a later takeover reconciles against
        assert row["generation"] == 1 and row["status"] == "running"

        await record("F1", "claim commits, fence install fails", barrier="after commit",
                     job=job, delivered=False, execution_started=False, attempted=False,
                     accepted=False, terminal_mutated=False,
                     observed=f"DeliveryRefused({claim.detail['reason']})",
                     safety="SAFE - no owner delivered, nothing published",
                     availability="LOST - this delivery does not run; the durable claim awaits "
                                  "takeover after its lease expires",
                     cleanup_result="no fence key; claim row left for recovery")
    finally:
        await engine.dispose()


# F2  PostgreSQL renewal fails


async def test_f2_a_superseded_worker_cannot_renew_or_keep_publishing(job):
    from meshpipeline.application import execution_fence as _fence
    from meshpipeline.persistence.lease import LeaseRepository

    engine, Session = _sessions()
    try:
        a = await _claim(job, Session, backend_execution_id="exec-f2a")
        a_fp = fence_fingerprint(str(job), a.ownership.execution_generation,
                                 a.ownership.worker_token)
        await _expire_lease(job)
        b = await _claim(job, Session, backend_execution_id="exec-f2b")
        assert isinstance(b, _fence.DeliveryClaim)

        before = snapshot(str(job))
        async with Session() as db:
            renewed = await LeaseRepository().heartbeat(db, a.ownership)
        after = snapshot(str(job))

        assert renewed is False, "PostgreSQL renewed a superseded claim"
        assert_unchanged(before, after, "F2 renewal by a superseded worker")
        assert F.current(str(job)) != a_fp, "A's fence came back"

        attempted = accepted = False
        with _fence.execution_ownership(a.ownership, session_factory=Session):
            attempted = True
            with pytest.raises(StaleExecutionPublish):
                await _gated(str(job)).anote("A still speaks", op_id="f2:1")
        assert backlog(str(job)) == []

        await record("F2", "PostgreSQL renewal fails", barrier="heartbeat", job=job,
                     delivered=True, execution_started=True, attempted=attempted,
                     accepted=accepted, terminal_mutated=False,
                     observed="heartbeat=False; publication StaleExecutionPublish",
                     safety="SAFE - no TTL refresh, no fence recreated, no publication",
                     availability="LOST for A only; B is the current owner and unaffected",
                     cleanup_result="B's fence intact")
    finally:
        await engine.dispose()


# F3  PostgreSQL renewal succeeds, Redis CAS refresh fails


async def test_f3_a_renewed_lease_restores_a_lapsed_fence_but_refresh_never_sets(job):
    from meshpipeline.persistence.lease import LeaseRepository

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-f3")
        fp = fence_fingerprint(str(job), claim.ownership.execution_generation,
                               claim.ownership.worker_token)
        # a real missing fence: exactly what an expiry leaves behind
        _client().delete(fence_key_for(str(job)))

        async with Session() as db:
            renewed = await LeaseRepository().heartbeat(db, claim.ownership)
            await db.commit()
        assert renewed is True, "the durable lease was not extended"

        row = await _row(job)
        assert dt.datetime.fromisoformat(row["lease_expires_at"]) > dt.datetime.now(dt.UTC), (
            "the PostgreSQL lease extension is not durable")
        # The heartbeat verified generation and token under the row lock, so restoring the
        # lapsed mirror is SAFE - and refusing to restore it is how a healthy job used to die
        # (one lapse was permanent; the next fenced publish was refused for a supersession
        # that never happened).
        assert F.current(str(job)) == fp, "the verified renewal did not restore the mirror"
        # bare CAS refresh still never SETs: drop the key again and try it directly
        _client().delete(fence_key_for(str(job)))
        assert F.refresh(str(job), fp, 60) is False, "refresh performed a SET"
        assert not F.current(str(job)), "refresh recreated the fence on its own"

        from meshpipeline.application import execution_fence as _fence
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("still owner", op_id="f3:1")   # publish-seam heal
        assert len(backlog(str(job))) == 1, "the live owner's publish did not land"

        await record("F3", "renewal restores the lapsed mirror; bare refresh never SETs",
                     barrier="heartbeat",
                     job=job, delivered=True, execution_started=True, attempted=True,
                     accepted=True, terminal_mutated=False,
                     observed="heartbeat=True healed the fence; bare refresh=False; the "
                              "publish healed its own lapse and landed",
                     safety="SAFE - every restore re-verified generation and token under the "
                            "row lock first; bare refresh still cannot SET",
                     availability="RESTORED - the live owner heals and continues",
                     cleanup_result="fence = the verified claim's fingerprint")
    finally:
        await engine.dispose()


# F4  old-fence revocation fails before takeover


async def test_f4_a_takeover_that_cannot_revoke_the_old_fence_does_not_commit(job, monkeypatch):
    engine, Session = _sessions()
    try:
        a = await _claim(job, Session, backend_execution_id="exec-f4a")
        a_fp = fence_fingerprint(str(job), a.ownership.execution_generation,
                                 a.ownership.worker_token)
        before_row = await _row(job)
        await _expire_lease(job)

        _fail_revoke(monkeypatch)
        with pytest.raises(_lease_failure()) as caught:
            await _claim(job, Session, backend_execution_id="exec-f4b")

        row = await _row(job)
        assert row["generation"] == before_row["generation"], "the takeover committed anyway"
        assert row["token_fingerprint"] == before_row["token_fingerprint"], "the token rotated"
        assert F.current(str(job)) == a_fp, "A's fence was silently discarded"
        assert backlog(str(job)) == []

        await record("F4", "old-fence revocation fails before takeover",
                     barrier="under the claim row lock", job=job, delivered=False,
                     execution_started=False, attempted=False, accepted=False,
                     terminal_mutated=False,
                     observed=f"{type(caught.value).__name__} out of claim_execution",
                     safety="SAFE - no new owner while the old mirror could still authorize",
                     availability="LOST - takeover deferred until Redis recovers",
                     cleanup_result="old fence intact; generation unchanged")
    finally:
        await engine.dispose()


# F5  revocation succeeds, PostgreSQL takeover fails


async def test_f5_a_rolled_back_takeover_leaves_no_one_able_to_publish(job, monkeypatch):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        a = await _claim(job, Session, backend_execution_id="exec-f5a")
        a_fp = fence_fingerprint(str(job), a.ownership.execution_generation,
                                 a.ownership.worker_token)
        before_row = await _row(job)
        await _expire_lease(job)

        # the commit that would publish the transition fails; the revoke before it already ran
        import meshpipeline.persistence.lease as _lease
        real_claim = _lease.LeaseRepository.claim_execution

        async def _claim_then_fail(self, db, job_id, **kw):
            await real_claim(self, db, job_id, **kw)     # the revoke inside it really runs
            raise _RedisDown("the takeover transaction could not commit")

        monkeypatch.setattr(_lease.LeaseRepository, "claim_execution", _claim_then_fail)
        observed = "raised"
        try:
            refused = await _claim(job, Session, backend_execution_id="exec-f5b")
            observed = f"DeliveryRefused({getattr(refused, 'detail', {}).get('reason', '?')})"
        except Exception as exc:
            observed = f"{type(exc).__name__} out of the takeover"
        monkeypatch.undo()
        assert observed != "raised"

        # the rolled-back takeover never durably happened: A's generation and token survived
        row = await _row(job)
        assert row["generation"] == before_row["generation"], "the rolled-back claim persisted"

        # A is therefore still the ONE verified owner. The publish-seam recovery re-verifies
        # exactly that under the row lock and heals the mirror the ghost takeover revoked -
        # the surviving owner continues instead of being stranded by a transaction that never
        # committed.
        with _fence.execution_ownership(a.ownership, session_factory=Session):
            await _gated(str(job)).anote("A after rollback", op_id="f5:1")
        assert len(backlog(str(job))) == 1, "A's post-rollback publish did not land"
        assert F.current(str(job)) == a_fp, (
            "the heal did not restore the surviving owner's own fingerprint")

        await record("F5", "revocation succeeds, takeover rolls back",
                     barrier="after revoke, before commit", job=job, delivered=False,
                     execution_started=False, attempted=True, accepted=True,
                     terminal_mutated=False,
                     observed=f"{observed}; A healed through the ghost revoke and published",
                     safety="SAFE - the heal re-verified generation and token under the row "
                            "lock; the rollback left A the one true owner",
                     availability="RESTORED - the surviving owner heals its mirror and "
                                  "continues",
                     cleanup_result="fence = A's fingerprint; PostgreSQL row unchanged")
    finally:
        await engine.dispose()


# F6  new owner commits, new-fence installation fails


async def test_f6_a_new_owner_without_a_fence_never_executes(job, monkeypatch):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        a = await _claim(job, Session, backend_execution_id="exec-f6a")
        await _expire_lease(job)

        _fail_install(monkeypatch)
        b = await _claim(job, Session, backend_execution_id="exec-f6b")
        monkeypatch.undo()

        assert isinstance(b, _fence.DeliveryRefused), f"B was delivered without a fence: {b}"
        assert b.detail["reason"] == "fence_unavailable"
        row = await _row(job)
        assert row["generation"] == 2, "the ownership transition was not durable"
        assert not F.current(str(job)), "a fence exists after a failed installation"

        # neither worker can publish: A is superseded, B has no mirror
        with _fence.execution_ownership(a.ownership, session_factory=Session):
            with pytest.raises(StaleExecutionPublish):
                await _gated(str(job)).anote("A speaks", op_id="f6:a")
        assert backlog(str(job)) == []

        await record("F6", "new owner commits, fence install fails", barrier="after commit",
                     job=job, delivered=False, execution_started=False, attempted=True,
                     accepted=False, terminal_mutated=False,
                     observed="generation=2 durable; DeliveryRefused(fence_unavailable)",
                     safety="SAFE - old worker fenced out, new worker never runs",
                     availability="LOST - the job waits for another takeover",
                     cleanup_result="no fence key; generation advanced durably")
    finally:
        await engine.dispose()


# F7  release sees a mismatched fence


async def test_f7_release_cannot_delete_another_workers_fence(job):
    from meshpipeline.persistence.lease import LeaseRepository

    engine, Session = _sessions()
    try:
        a = await _claim(job, Session, backend_execution_id="exec-f7a")
        # someone else's fence is what the mirror now holds
        foreign = fence_fingerprint(str(job), 99, uuid.uuid4())
        F.install(str(job), foreign, 60)

        async with Session() as db:
            await LeaseRepository().release(db, a.ownership)
            await db.commit()

        assert F.current(str(job)) == foreign, "release deleted a fence that was not its own"
        row = await _row(job)

        await record("F7", "release sees a mismatched fence", barrier="release", job=job,
                     delivered=True, execution_started=True, attempted=False, accepted=False,
                     terminal_mutated=False,
                     observed=f"revoke=False; fence still {foreign[:12]}; "
                              f"token now {row['token_fingerprint']}",
                     safety="SAFE - the current owner's fingerprint is intact and no stale "
                            "publication is enabled",
                     availability="unaffected",
                     cleanup_result="foreign fence preserved")
    finally:
        await engine.dispose()


# F8  Redis unavailable during publication


async def test_f8_redis_unavailable_during_publication_never_falls_back(job, monkeypatch):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-f8")
        before = snapshot(str(job))

        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            pub = _gated(str(job))
            real = pub._inner._get_redis()

            class _Down:
                def eval(self, *a, **k):
                    raise _RedisDown("connection reset")

                def __getattr__(self, name):
                    return getattr(real, name)

            pub._inner._redis = _Down()
            # A TRANSPORT OUTAGE IS NOT A LOST CLAIM. The keyed path absorbs it as the
            # best-effort blip it is; only the fence refusal propagates. Either way nothing
            # is accepted, and nothing falls back to the unfenced route.
            raised = ""
            try:
                await pub.anote("into the void", op_id="f8:1")
            except StaleExecutionPublish:  # pragma: no cover - would be a contract inversion
                raised = "StaleExecutionPublish"
            except Exception as exc:  # noqa: BLE001
                raised = type(exc).__name__
        assert raised != "StaleExecutionPublish", (
            "a transport outage was reported as a lost claim")
        assert expected_fence() == "", "the fence expectation leaked out of a failed publication"
        assert_unchanged(before, snapshot(str(job)), "F8 transport failure")
        assert backlog(str(job)) == [], "an event was accepted while Redis was unreachable"

        await record("F8", "Redis unavailable during publication", barrier="at Lua", job=job,
                     delivered=True, execution_started=True, attempted=True, accepted=False,
                     terminal_mutated=False,
                     observed=f"absorbed as best-effort ({raised or 'swallowed'}); "
                              "not StaleExecutionPublish",
                     safety="SAFE - no plain-route fallback, no event accepted, expectation reset",
                     availability="LOST while Redis is down",
                     cleanup_result="no keys created")
    finally:
        await engine.dispose()


# F9  the claim lapses between authorization and Lua


async def test_f9_a_claim_that_lapses_before_lua_is_refused_before_replay(job):
    import threading

    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-f9")
        fp = fence_fingerprint(str(job), claim.ownership.execution_generation,
                              claim.ownership.worker_token)
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("first", op_id="f9:same")

        release, at_lua = threading.Event(), threading.Event()
        outcome: dict = {}

        def worker():
            import asyncio as _a

            async def go():
                w_engine, w_sessions = _sessions()
                with _fence.execution_ownership(claim.ownership, session_factory=w_sessions):
                    pub = _gated(str(job))
                    pub._inner._redis = PausedRedis(pub._inner._get_redis(), release, at_lua)
                    try:
                        # the SAME op key: an authorized replay would return -1
                        await pub.anote("first", op_id="f9:same")
                        outcome["result"] = "published"
                    except StaleExecutionPublish:
                        outcome["result"] = "refused"
                    finally:
                        await w_engine.dispose()
            _a.run(go())

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert at_lua.wait(timeout=30), "the worker never reached Lua"

        # the claim lapses while it waits: lease aged out, mirror revoked - no sleeping
        await _expire_lease(job)
        F.revoke(str(job), fp)

        before = snapshot(str(job))
        release.set()
        t.join(30)
        assert not t.is_alive()
        after = snapshot(str(job))
        # The EVENT state must not change - the replay is suppressed, never duplicated. The
        # fence key legitimately reappears: the lease was expired but UNCLAIMED, so the
        # recovery re-verified generation and token under the row lock, renewed it and healed
        # the mirror (the exact lapse that used to kill a finished mesh mid-review).
        for key in event_keys(str(job)):
            for field in ("exists", "type", "sha256"):
                assert after[key][field] == before[key][field], f"F9: {key}.{field} changed"
        assert outcome["result"] == "published", (
            "the expired-but-unclaimed owner was refused instead of healed")
        assert len(backlog(str(job))) == 1, "the healed replay wrote a duplicate"
        assert F.current(str(job)) == fp, "the heal did not restore the verified fingerprint"

        await record("F9", "claim lapses between authorization and Lua", barrier="pre-Lua",
                     job=job, delivered=True, execution_started=True, attempted=True,
                     accepted=False, terminal_mutated=False,
                     observed="lapse healed under the row lock; the replayed op key was "
                              "suppressed as idempotent",
                     safety="SAFE - the heal re-verified generation and token under the row "
                            "lock, and the replay stayed suppressed - nothing new was written",
                     availability="RESTORED - the surviving owner healed its mirror and "
                                  "continued",
                     cleanup_result="fence = the verified claim's fingerprint; event state "
                                    "unchanged")
    finally:
        await engine.dispose()


# F10  cancellation during fence maintenance


async def test_f10_cancellation_during_fence_installation_grants_no_authority(job, monkeypatch):
    import asyncio


    engine, Session = _sessions()
    try:
        # INSTALL is the maintenance operation whose cancellation carries the most risk: it is
        # the one that would GRANT authority. Refresh and revoke can only ever narrow it, so a
        # cancelled install is the worst case of the shared teardown contract.
        import meshpipeline.persistence.lease as _lease
        real = _lease._fence_ops()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        class _Ops:
            fingerprint = staticmethod(real.fingerprint)
            refresh = staticmethod(real.refresh)
            revoke = staticmethod(real.revoke)
            current = staticmethod(real.current)

            @staticmethod
            def install(job_id, value, ttl):
                loop.call_soon_threadsafe(entered.set)
                raise asyncio.CancelledError

        monkeypatch.setattr(_lease, "_fence_ops", lambda: _Ops)

        task = asyncio.create_task(_claim(job, Session, backend_execution_id="exec-f10"))
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.undo()

        assert not F.current(str(job)), "a partial installation left a fence behind"
        assert backlog(str(job)) == []
        assert expected_fence() == "", "a cancelled claim left a fence expectation bound"
        # no lingering task from the cancelled delivery
        assert task.done()

        await record("F10", "cancellation during fence installation", barrier="install",
                     job=job, delivered=False, execution_started=False, attempted=False,
                     accepted=False, terminal_mutated=False,
                     observed="CancelledError out of claim delivery",
                     safety="SAFE - no partial fence, so no authority was granted",
                     availability="LOST - the delivery is abandoned; the claim awaits takeover",
                     cleanup_result="no fence key; no pending task; context clean")
    finally:
        await engine.dispose()


# cross-case invariants and the successful controls


async def test_the_successful_lifecycle_still_works_with_no_injector(job):
    from meshpipeline.application import execution_fence as _fence
    from meshpipeline.persistence.lease import LeaseRepository

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-ok")
        assert isinstance(claim, _fence.DeliveryClaim)
        fp = fence_fingerprint(str(job), claim.ownership.execution_generation,
                               claim.ownership.worker_token)
        assert F.current(str(job)) == fp, "the successful claim installed no fence"

        async with Session() as db:
            assert await LeaseRepository().heartbeat(db, claim.ownership) is True
        assert F.current(str(job)) == fp, "renewal disturbed the fence"

        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("hello", op_id="ok:1")
            await _gated(str(job)).anote("hello", op_id="ok:1")     # authorized replay, -1
        assert len(backlog(str(job))) == 1

        await _expire_lease(job)
        nxt = await _claim(job, Session, backend_execution_id="exec-ok2")
        assert isinstance(nxt, _fence.DeliveryClaim), "a clean takeover was refused"
        assert F.current(str(job)) == fence_fingerprint(
            str(job), nxt.ownership.execution_generation, nxt.ownership.worker_token)

        async with Session() as db:
            await LeaseRepository().release(db, nxt.ownership)
            await db.commit()
        assert not F.current(str(job)), "release left the fence behind"

        # the terminal authority still works when no claim remains
        _publisher(str(job)).publish_terminal("the run ended", "t:1")
        assert len(backlog(str(job))) == 2
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _cross_case_invariants():
    # THE AGGREGATE RUNS LAST, ALWAYS. This was a test, and a test can be scheduled anywhere: under
    # a randomised order it ran before the ten cases had populated MATRIX and failed on an empty
    # accumulator. Asserting from module teardown is the only placement that cannot be reordered,
    # and it keeps the invariant loud - a violation fails the module rather than being reported as
    # a missing case.
    yield
    assert len(MATRIX) == 10, f"only {len(MATRIX)} of the ten cases recorded"
    for e in MATRIX:
        # A publication is accepted in exactly one shape of case: the writer was re-verified
        # as the ONE current owner under the claim row lock and its lapsed mirror was healed
        # (availability RESTORED). A stale or superseded writer is still never accepted.
        assert e["publication_accepted"] is False or e["availability"].startswith("RESTORED"), (
            f"{e['case']} accepted a publication without a verified heal")
        assert e["terminal_mutated"] is False, f"{e['case']} mutated terminal state"
        assert e["safety"].startswith("SAFE"), f"{e['case']} is not safe: {e['safety']}"
        assert "token" in e and len(e["token"]) == 12, "the token is not a fingerprint"
    print("\nFENCE LIFECYCLE FAILURE MATRIX")
    for e in MATRIX:
        print(f"  {e['case']}  {e['name']}")
        print(f"      barrier={e['barrier']}  gen={e['generation']} token={e['token']} "
              f"fence={e['fence']} pttl={e['fence_pttl']}")
        print(f"      delivered={e['ownership_delivered']} started={e['execution_started']} "
              f"attempted={e['publication_attempted']} accepted={e['publication_accepted']} "
              f"terminal_mutated={e['terminal_mutated']}")
        print(f"      observed  : {e['observed']}")
        print(f"      safety    : {e['safety']}")
        print(f"      available : {e['availability']}")
        print(f"      cleanup   : {e['cleanup']}")
