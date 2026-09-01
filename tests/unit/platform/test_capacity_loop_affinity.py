# The loop-crossing bug, capacity edition: a redis.asyncio pool binds connections to the loop
# that opened them, and the capacity controller is a process-wide singleton serving many loops
# (the uvicorn serving loop, each web_search asyncio.run() side-thread loop, each worker job
# loop). The old instance-cached client, created on whichever loop admitted first, made every
# other loop's admission die with "attached to a different loop" - reported by the fail-closed
# guard as "admission store unreachable ... refusing admission", so healthy model calls were
# refused until the process restarted. The client is now cached per loop, exactly like
# persistence/session.py's per-loop engines; these tests pin that contract.
from __future__ import annotations

import asyncio
import logging

import pytest

from meshpipeline.adapters.model_capacity import redis as capmod
from meshpipeline.contracts.model_capacity import Lease

_DOMAIN = "deepseek:default:deepseek-v4-flash"


class LoopBoundFakeRedis:
    """A fake redis.asyncio client with the REAL client's loop affinity: the first loop to
    await it owns it, and any other loop's await dies with the library's failure verbatim."""

    def __init__(self):
        self.bound_loop = None
        self.eval_calls = 0
        self.zrem_calls: list = []

    def _bind(self):
        loop = asyncio.get_running_loop()
        if self.bound_loop is None:
            self.bound_loop = loop
        if loop is not self.bound_loop:
            raise RuntimeError(
                f"Task {asyncio.current_task()!r} got Future <Future pending> "
                "attached to a different loop")

    async def eval(self, script, numkeys, key, *args):
        self._bind()
        self.eval_calls += 1
        return [1, 0]     # admitted, active_before

    async def zrem(self, key, token):
        self._bind()
        self.zrem_calls.append((key, token))
        return 1


@pytest.fixture()
def clients(monkeypatch):
    made: list[LoopBoundFakeRedis] = []

    def factory(**kwargs):
        made.append(LoopBoundFakeRedis())
        return made[-1]

    monkeypatch.setattr(capmod, "async_client", factory)
    return made


def test_admission_on_a_second_loop_gets_its_own_client_and_is_admitted(clients):
    # Today's trigger, healed: loop A (the serving loop) admits first; loop B (a web_search
    # asyncio.run) must be ADMITTED with a client of its own, not refused via loop A's.
    ctrl = capmod.RedisCapacityController()

    async def admit_and_release():
        lease = await ctrl.acquire(_DOMAIN, 4, 1.0)
        assert lease is not None
        await ctrl.release(lease)
        return lease

    lease_a = asyncio.run(admit_and_release())
    lease_b = asyncio.run(admit_and_release())
    assert isinstance(lease_a, Lease) and isinstance(lease_b, Lease)
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert clients[0].zrem_calls and clients[1].zrem_calls     # release used the same client


def test_a_client_shared_across_loops_is_exactly_todays_refusal(clients, caplog):
    # The OLD cache, reproduced: seed loop B's slot with a client bound on (closed) loop A.
    # The cross-loop await surfaces as today's log line - fail-closed refusal of an admission
    # a healthy Redis was ready to grant. This pins what the per-loop cache is protecting
    # against, and that the affinity fake genuinely enforces affinity.
    ctrl = capmod.RedisCapacityController()
    stale = LoopBoundFakeRedis()
    asyncio.run(stale.eval("", 1, "k"))     # binds it to loop A, which asyncio.run then closes

    async def admit_through_stale():
        ctrl._clients[asyncio.get_running_loop()] = stale
        return await ctrl.acquire(_DOMAIN, 4, 1.0)

    with caplog.at_level(logging.ERROR, logger="meshpipeline.adapters.model_capacity.redis"):
        assert asyncio.run(admit_through_stale()) is None
    assert "refusing admission" in caplog.text and "different loop" in caplog.text


def test_same_loop_reuses_one_client(clients):
    ctrl = capmod.RedisCapacityController()

    async def admit_twice():
        return await ctrl.acquire(_DOMAIN, 4, 1.0), await ctrl.acquire(_DOMAIN, 4, 1.0)

    a, b = asyncio.run(admit_twice())
    assert a is not None and b is not None
    assert len(clients) == 1 and clients[0].eval_calls == 2


def test_closed_loop_entry_is_never_served_and_is_swept(clients):
    # Keep the loop OBJECT alive after closing it, so a WeakKeyDictionary entry for it
    # survives; a fresh loop must still get its own client and the dead entry is swept.
    ctrl = capmod.RedisCapacityController()
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(ctrl.acquire(_DOMAIN, 4, 1.0)) is not None
    finally:
        loop.close()
    assert loop.is_closed()

    assert asyncio.run(ctrl.acquire(_DOMAIN, 4, 1.0)) is not None
    assert len(clients) == 2
    with ctrl._clients_lock:
        assert loop not in ctrl._clients     # the dead entry was swept


def test_a_truly_down_store_still_refuses_admission(monkeypatch, caplog):
    # The fail-closed contract on GENUINE unavailability is untouched by the per-loop cache.
    class DownRedis:
        async def eval(self, *a):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(capmod, "async_client", lambda **k: DownRedis())
    ctrl = capmod.RedisCapacityController()
    with caplog.at_level(logging.ERROR, logger="meshpipeline.adapters.model_capacity.redis"):
        assert asyncio.run(ctrl.acquire(_DOMAIN, 4, 1.0)) is None
    assert "failing closed" in caplog.text


def test_no_running_loop_is_a_loud_error(clients):
    # A sync caller with no running loop gets the RuntimeError, never a fallback client - a
    # cross-loop client handed out silently is exactly the bug this cache now prevents.
    with pytest.raises(RuntimeError):
        capmod.RedisCapacityController()._client()
