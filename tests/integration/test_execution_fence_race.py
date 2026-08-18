# Responsibility: Verify a worker that loses its claim mid-publication cannot write, against real PostgreSQL and Redis.
# Boundaries: the fence protocol under real turnover; the failure matrix and per-site campaign are separate.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.event_stream import fence as F
from meshpipeline.application.execution_fence import claim_delivery
from meshpipeline.contracts.event_stream import (
    StaleExecutionPublish,
    fence_fingerprint,
)
from meshpipeline.events.channels import (
    fence_key_for,
    log_key_for,
    opkey_set_for,
    seq_key_for,
)
from meshpipeline.persistence.models import SimulationJob

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

jlog = logging.getLogger(__name__)


# real PostgreSQL


def _sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=4, max_overflow=4,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _seed(job_id: uuid.UUID, owner_id: str) -> None:
    engine, Session = _sessions()
    try:
        async with Session() as db:
            db.add(SimulationJob(id=job_id, owner_id=owner_id))
            await db.commit()
    finally:
        await engine.dispose()


async def _row(job_id: uuid.UUID) -> dict:
    engine, Session = _sessions()
    try:
        async with Session() as db:
            row = (await db.execute(
                select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
            return {"generation": int(row.execution_generation or 0),
                    "token_fingerprint": hashlib.sha256(
                        str(row.active_worker_token).encode()).hexdigest()[:12],
                    "lease_expires_at": str(row.lease_expires_at),
                    "status": row.status.value}
    finally:
        await engine.dispose()


# the reusable snapshot helper


def _client():
    import redis as _redis
    return _redis.from_url(provcfg.REDIS_URL)


def event_keys(job_id: str) -> list[str]:
    return [seq_key_for(job_id), log_key_for(job_id), opkey_set_for(job_id)]


def all_keys(job_id: str) -> list[str]:
    return [*event_keys(job_id), fence_key_for(job_id)]


def snapshot(job_id: str) -> dict:
    # Distinguishes absent / present-without-expiry / present-with-expiry, content change and
    # TTL-only change: pttl is -2 when absent, -1 when there is no expiry, positive otherwise.
    r = _client()
    try:
        out = {}
        for key in all_keys(job_id):
            exists = bool(r.exists(key))
            raw = r.dump(key)
            out[key] = {"key": key, "exists": exists,
                        "type": r.type(key).decode() if exists else "none",
                        "pttl": r.pttl(key),
                        "sha256": hashlib.sha256(raw).hexdigest() if raw else ""}
        return out
    finally:
        r.close()


def assert_unchanged(before: dict, after: dict, label: str) -> None:
    for key, was in before.items():
        now = after[key]
        for field in ("exists", "type", "sha256"):
            assert now[field] == was[field], (
                f"{label}: {key}.{field} changed {was[field]!r} -> {now[field]!r}")
        # a live TTL counts down on its own; a REFRESH is what must never happen
        assert now["pttl"] <= was["pttl"], (
            f"{label}: {key} TTL was refreshed {was['pttl']} -> {now['pttl']}")


def backlog(job_id: str) -> list[dict]:
    r = _client()
    try:
        return [json.loads(x) for x in r.lrange(log_key_for(job_id), 0, -1)]
    finally:
        r.close()


def cleanup(job_id: str) -> None:
    r = _client()
    try:
        for key in all_keys(job_id):
            r.delete(key)
    finally:
        r.close()


# the barrier: transparent, and it delegates unchanged


class PausedRedis:
    # Wraps the REAL client. `eval` announces that it is about to call Lua, waits, then calls
    # straight through: nothing is synthesized, no key is touched here, and every other method
    # is the real one. Worker A runs on a thread so the pause frees the loop for worker B.
    def __init__(self, inner, gate: threading.Event, reached: threading.Event) -> None:
        self._inner, self._gate, self._reached = inner, gate, reached

    def eval(self, *a, **k):
        self._reached.set()
        assert self._gate.wait(timeout=30), "the race barrier was never released"
        return self._inner.eval(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# claim delivery, through the real authority


async def _claim(job_id: uuid.UUID, session_factory, *, backend_execution_id: str):
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    return await claim_delivery(session_factory, JobRepository(), str(job_id), jlog=jlog,
                                backend="local", backend_execution_id=backend_execution_id)


def _publisher(job_id: str):
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    return JobPublisher(str(job_id), agent="builder")


def _gated(job_id: str):
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher
    return OwnershipCheckedPublisher(_publisher(job_id))


@pytest.fixture()
async def job():
    job_id = uuid.uuid4()
    await _seed(job_id, f"race-{job_id.hex[:8]}")
    yield job_id
    cleanup(str(job_id))


async def _expire_lease(job_id: uuid.UUID) -> None:
    # Deterministic turnover: the previous lease is aged out by writing the row, never by
    # waiting. This is the state a dead worker leaves behind.
    import datetime as dt
    engine, Session = _sessions()
    try:
        async with Session() as db:
            row = (await db.execute(
                select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
            row.lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
            await db.commit()
    finally:
        await engine.dispose()


# the deterministic two-worker race


async def test_a_worker_that_loses_its_claim_mid_publication_writes_nothing(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    timeline: list[dict] = []

    async def mark(name: str) -> None:
        r = _client()
        try:
            timeline.append({
                "barrier": name, "postgres": await _row(job),
                "fence": (r.get(fence_key_for(str(job))) or b"").decode(),
                "fence_pttl": r.pttl(fence_key_for(str(job))),
                "seq": int(r.get(seq_key_for(str(job))) or 0),
                "backlog": r.llen(log_key_for(str(job))),
                "opkeys": r.scard(opkey_set_for(str(job)))})
        finally:
            r.close()

    try:
        # 1. A claims, and its fence exists before anything else happens
        a = await _claim(job, Session, backend_execution_id="exec-A")
        assert isinstance(a, _fence.DeliveryClaim), f"A was refused: {a}"
        await mark("A claim delivered")
        a_fp = fence_fingerprint(str(job), a.ownership.execution_generation,
                                 a.ownership.worker_token)
        assert timeline[-1]["fence"] == a_fp, "A's fence is not the claim it was given"

        release_a, a_at_lua = threading.Event(), threading.Event()
        a_result: dict = {}

        def worker_a():
            # A runs on its own thread with its own loop, so its pause inside the real Redis
            # call frees this loop for B. Its ownership is the real one it was granted.
            async def go():
                # its OWN engine on its own loop: asyncpg connections cannot cross loops
                a_engine, a_sessions = _sessions()
                with _fence.execution_ownership(a.ownership, session_factory=a_sessions):
                    pub = _gated(str(job))
                    pub._inner._redis = PausedRedis(pub._inner._get_redis(),
                                                    release_a, a_at_lua)
                    try:
                        # 3. pauses inside the wrapper, immediately before the real Lua call
                        await pub.anote("A speaks", op_id="a:1")
                        a_result["outcome"] = "published"
                    except StaleExecutionPublish as exc:
                        a_result["outcome"] = "refused"
                        a_result["error"] = str(exc)
                    finally:
                        await a_engine.dispose()
            asyncio.run(go())

        thread = threading.Thread(target=worker_a, daemon=True)
        await mark("A authorization complete")
        thread.start()
        assert a_at_lua.wait(timeout=30), "A never reached the Lua call"
        await mark("A paused before Lua")

        # 4-7. B takes over through the real row-locked claim path. A's lease is aged out
        # first, which is the state a lost worker leaves and what admits a new generation.
        await _expire_lease(job)
        b = await _claim(job, Session, backend_execution_id="exec-B")
        assert isinstance(b, _fence.DeliveryClaim), f"B was refused: {b}"
        await mark("B takeover committed and fence installed")
        b_fp = fence_fingerprint(str(job), b.ownership.execution_generation,
                                 b.ownership.worker_token)
        assert b.ownership.execution_generation > a.ownership.execution_generation
        assert timeline[-1]["fence"] == b_fp, "B's fence is not B's claim"
        assert timeline[-1]["fence"] != a_fp, "A's fence survived the takeover"

        # 8. release A into the real Lua call. The snapshot brackets A'S ATTEMPT ALONE - taken
        # after B's takeover, so every difference below is attributable to A and nothing else.
        before_a = snapshot(str(job))
        release_a.set()
        await asyncio.to_thread(thread.join, 30)
        assert not thread.is_alive(), "A never completed its Lua attempt"
        await mark("A Lua attempt complete")
        after_a = snapshot(str(job))

        # 9-10. A is refused and changed nothing
        assert a_result["outcome"] == "refused", (
            f"A published after losing its claim: {a_result}")
        assert_unchanged(before_a, after_a, "A after takeover")

        # 11. B publishes exactly once
        with _fence.execution_ownership(b.ownership, session_factory=Session):
            await _gated(str(job)).anote("B speaks", op_id="b:1")
        await mark("B publication complete")

        events = backlog(str(job))
        texts = [e.get("text") or e.get("message") or "" for e in events]
        assert sum("B speaks" in t for t in texts) == 1, f"B did not publish exactly once: {texts}"
        assert not any("A speaks" in t for t in texts), f"A's event reached the log: {texts}"
        assert [e.get("seq") for e in events] == sorted(e.get("seq") for e in events)

        # 12. a delayed heartbeat from A cannot bring its fence back
        async with Session() as db:
            from meshpipeline.persistence.lease import LeaseRepository
            renewed = await LeaseRepository().heartbeat(db, a.ownership)
        assert renewed is False, "PostgreSQL renewed a superseded claim"
        assert F.current(str(job)) == b_fp, "A's delayed heartbeat disturbed B's fence"

        print("\nRACE TIMELINE")
        for t in timeline:
            print(f"  {t['barrier']:<44} gen={t['postgres']['generation']} "
                  f"token={t['postgres']['token_fingerprint']} fence={t['fence'][:12] or '-':<12} "
                  f"pttl={t['fence_pttl']:<8} seq={t['seq']} backlog={t['backlog']} "
                  f"opkeys={t['opkeys']}")
    finally:
        await engine.dispose()


async def test_the_raw_worker_token_never_reaches_redis(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-token")
        assert isinstance(claim, _fence.DeliveryClaim)
        held = F.current(str(job))
        assert held and str(claim.ownership.worker_token) not in held
        r = _client()
        try:
            for key in all_keys(str(job)):
                raw = r.dump(key)
                if raw:
                    assert str(claim.ownership.worker_token).encode() not in raw, key
        finally:
            r.close()
    finally:
        await engine.dispose()


# the eight rejection cases


CASES = ["missing fence", "wrong job key", "wrong generation", "wrong worker token",
         "revoked fence", "expired fence", "stale replay", "old worker after takeover"]


@pytest.mark.parametrize("case", CASES)
async def test_a_write_without_the_matching_claim_changes_nothing(job, case):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id=f"exec-{case[:6]}")
        assert isinstance(claim, _fence.DeliveryClaim), claim
        own = claim.ownership

        # a real prior event, so refusal is measured against non-empty state
        with _fence.execution_ownership(own, session_factory=Session):
            await _gated(str(job)).anote("first", op_id="a:1")

        r, use = _client(), own
        try:
            if case == "missing fence":
                r.delete(fence_key_for(str(job)))
            elif case == "wrong job key":
                # the fence lives under another job's key, so this job's key is absent
                other = uuid.uuid4()
                F.install(str(other), F.current(str(job)) or "x" * 32, 60)
                r.delete(fence_key_for(str(job)))
                cleanup(str(other))
            elif case == "wrong generation":
                F.install(str(job), fence_fingerprint(
                    str(job), own.execution_generation + 1, own.worker_token), 60)
            elif case == "wrong worker token":
                F.install(str(job), fence_fingerprint(
                    str(job), own.execution_generation, uuid.uuid4()), 60)
            elif case == "revoked fence":
                F.revoke(str(job), F.current(str(job)))
            elif case == "expired fence":
                # deterministic expiry: remove the key exactly as expiry would, no waiting
                r.delete(fence_key_for(str(job)))
            elif case == "old worker after takeover":
                await _expire_lease(job)
                nxt = await _claim(job, Session, backend_execution_id="exec-next")
                assert isinstance(nxt, _fence.DeliveryClaim), nxt
            # "stale replay" keeps the same op_id below and revokes the fence
            if case == "stale replay":
                F.revoke(str(job), F.current(str(job)))
        finally:
            r.close()

        op = "a:1" if case == "stale replay" else "a:2"
        before = snapshot(str(job))
        with _fence.execution_ownership(use, session_factory=Session):
            with pytest.raises(StaleExecutionPublish):
                await _gated(str(job)).anote("second", op_id=op)
        after = snapshot(str(job))

        assert_unchanged(before, after, case)
        assert len(backlog(str(job))) == 1, f"{case}: the backlog grew"
        print(f"\n{case}: refused; "
              + "; ".join(f"{k.split(':')[-1]} exists={v['exists']} type={v['type']} "
                          f"pttl={v['pttl']} sha={v['sha256'][:12] or '-'}"
                          for k, v in after.items()))
    finally:
        await engine.dispose()


# the controls that must still work


async def test_an_authorized_replay_is_still_suppressed_not_refused(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-replay")
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("hello", op_id="same")
            await _gated(str(job)).anote("hello", op_id="same")   # the -1 path, not -2
        assert len(backlog(str(job))) == 1, "the authorized replay was written twice"
    finally:
        await engine.dispose()


async def test_a_matching_claim_publishes_normally(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-ok")
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("hello", op_id="ok:1")
        assert len(backlog(str(job))) == 1
    finally:
        await engine.dispose()


async def test_plain_terminal_publication_needs_no_fence(job):
    # no claim at all: the terminal authority publishes when ownership is gone
    _publisher(str(job)).publish_terminal("the run ended", "t:1")
    assert len(backlog(str(job))) == 1
    assert not F.current(str(job)), "terminal publication created a fence"


async def test_publication_never_changes_the_fence_key(job):
    from meshpipeline.application import execution_fence as _fence

    engine, Session = _sessions()
    try:
        claim = await _claim(job, Session, backend_execution_id="exec-fence")
        before = snapshot(str(job))[fence_key_for(str(job))]
        with _fence.execution_ownership(claim.ownership, session_factory=Session):
            await _gated(str(job)).anote("hello", op_id="f:1")
        after = snapshot(str(job))[fence_key_for(str(job))]
        assert before["sha256"] == after["sha256"] and before["exists"] == after["exists"]
    finally:
        await engine.dispose()


async def test_the_image_under_test_carries_the_fifth_key_fence():
    from meshpipeline.adapters.event_stream.redis import _EMIT_LUA
    guard = "\n".join(_EMIT_LUA.strip().splitlines()[:3])
    assert "KEYS[5]" in guard and "ARGV[5]" in guard and "-2" in guard, guard
