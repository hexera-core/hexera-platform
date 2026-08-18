# Responsibility: Verify each Redis-backed capability keeps its key, TTL and cap, and an outage never fails a job.
from __future__ import annotations

import json

import pytest

from meshpipeline.adapters.dead_letter import redis as dlq_mod
from meshpipeline.adapters.delivery_guard import redis as guard_mod
from meshpipeline.adapters.event_stream import redis as stream_mod
from meshpipeline.adapters.mesh_timing import redis as timing_mod
from meshpipeline.adapters.rate_limit import redis as rl_mod


class FakeSyncRedis:

    def __init__(self):
        self.kv: dict = {}
        self.lists: dict = {}
        self.expires: dict = {}
        self.closed = False

    def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    def expire(self, k, ttl):
        self.expires[k] = ttl
        return True

    def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)
        return len(self.lists[k])

    def ltrim(self, k, a, b):
        self.lists[k] = self.lists.get(k, [])[a:b + 1]
        return True

    def lrange(self, k, a, b):
        items = self.lists.get(k, [])
        return items[a:] if b == -1 else items[a:b + 1]

    def close(self):
        self.closed = True

    def pipeline(self):
        outer = self

        class P:
            def __init__(self): self.ops = []
            def lpush(self, k, v): self.ops.append(("lpush", k, v)); return self
            def ltrim(self, k, a, b): self.ops.append(("ltrim", k, a, b)); return self
            def execute(self):
                for op in self.ops:
                    getattr(outer, op[0])(*op[1:])
        return P()


class FakeAsyncRedis:
    def __init__(self):
        self.kv: dict = {}
        self.expires: dict = {}

    async def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    async def expire(self, k, ttl):
        self.expires[k] = ttl
        return True


# delivery guard: the redelivery cap's counter
def test_the_delivery_guard_counts_atomically_under_the_preserved_key_and_ttl(monkeypatch):
    fake = FakeSyncRedis()
    monkeypatch.setattr(guard_mod, "sync_client", lambda **k: fake)
    g = guard_mod.RedisDeliveryGuard()

    assert g.record_attempt("job-x") == 1
    assert g.record_attempt("job-x") == 2
    assert "job:job-x:deliveries" in fake.kv, f"the delivery key changed: {list(fake.kv)}"
    # the TTL is set once, when the window opens - not re-armed on every delivery
    assert fake.expires["job:job-x:deliveries"] == 86400
    assert g.record_attempt("job-y") == 1, "counts must be per job"


# rate limit: the fixed-window counter
@pytest.mark.asyncio
async def test_the_rate_limit_store_counts_per_identity_window_under_the_preserved_key(monkeypatch):
    fake = FakeAsyncRedis()
    monkeypatch.setattr(rl_mod, "async_client", lambda **k: fake)
    s = rl_mod.RedisRateLimitStore()

    assert await s.incr_window("u1", 42, 90) == 1
    assert await s.incr_window("u1", 42, 90) == 2
    assert "rl:u1:42" in fake.kv, f"the rate-limit key changed: {list(fake.kv)}"
    assert fake.expires["rl:u1:42"] == 90
    # a different identity, and a different window, are different buckets
    assert await s.incr_window("u2", 42, 90) == 1
    assert await s.incr_window("u1", 43, 90) == 1


# dead letter: the capped, inspectable queue
def test_the_dead_letter_sink_appends_newest_first_and_stays_capped(monkeypatch):
    fake = FakeSyncRedis()
    monkeypatch.setattr(dlq_mod, "sync_client", lambda **k: fake)
    sink = dlq_mod.RedisDeadLetterSink()

    sink.append({"job_id": "a", "detail": "first"})
    sink.append({"job_id": "b", "detail": "second"})
    stored = [json.loads(x) for x in fake.lists["simulation:dlq"]]
    assert [r["job_id"] for r in stored] == ["b", "a"], "the DLQ is not newest-first"

    for i in range(1200):
        sink.append({"job_id": str(i)})
    assert len(fake.lists["simulation:dlq"]) == 1000, "the DLQ cap (1000) changed"


def test_the_dead_letter_sink_is_inspectable_and_survives_a_corrupt_record(monkeypatch):
    fake = FakeSyncRedis()
    monkeypatch.setattr(dlq_mod, "sync_client", lambda **k: fake)
    sink = dlq_mod.RedisDeadLetterSink()

    sink.append({"job_id": "a"})
    fake.lists["simulation:dlq"].insert(0, "{not json")   # a malformed neighbour
    sink.append({"job_id": "b"})

    got = sink.recent(10)
    assert [r["job_id"] for r in got] == ["b", "a"], (
        "one unreadable record hid the readable ones")


def test_the_dead_letter_record_carries_the_failure_facts(monkeypatch):
    from meshpipeline.contracts import dead_letter
    from meshpipeline.errors import FailureClass, record_dead_letter

    seen: list = []

    class _Sink:
        def append(self, rec): seen.append(rec)
        def recent(self, limit=100): return list(seen)

    try:
        dead_letter.set_dead_letter_sink(_Sink())
        record_dead_letter("job-1", FailureClass.RESOURCE, "worker", "OOM killed", extra={"k": "v"})
    finally:
        dead_letter.set_dead_letter_sink(None)

    assert len(seen) == 1
    rec = seen[0]
    assert rec["job_id"] == "job-1"
    assert rec["failure_class"] == FailureClass.RESOURCE.value
    assert rec["dependency"] == "worker"
    assert "OOM killed" in rec["detail"]
    assert rec["k"] == "v" and rec["ts"]


def test_a_dead_letter_sink_outage_never_raises_into_a_failing_job():
    from meshpipeline.contracts import dead_letter
    from meshpipeline.errors import FailureClass, record_dead_letter

    class _Broken:
        def append(self, rec): raise ConnectionError("dlq down")
        def recent(self, limit=100): raise ConnectionError("dlq down")

    try:
        dead_letter.set_dead_letter_sink(_Broken())
        record_dead_letter("job-1", FailureClass.INTERNAL, "x", "y")   # must not raise
    finally:
        dead_letter.set_dead_letter_sink(None)
    record_dead_letter("job-2", FailureClass.INTERNAL, "x", "y")       # unset: also fine


# mesh timing: the capped sample history
def test_the_mesh_timing_store_keeps_a_capped_history_under_the_preserved_key(monkeypatch):
    fake = FakeSyncRedis()
    monkeypatch.setattr(timing_mod, "sync_client", lambda **k: fake)
    st = timing_mod.RedisMeshTimingStore()

    st.append_sample("snappy", "external_cfd", 212.34, cap=200)
    assert "meshtime:snappy:external_cfd" in fake.lists, f"the key changed: {list(fake.lists)}"
    assert fake.lists["meshtime:snappy:external_cfd"] == ["212.3"], "the .1f encoding changed"

    for i in range(250):
        st.append_sample("snappy", "external_cfd", float(i + 1), cap=200)
    assert len(fake.lists["meshtime:snappy:external_cfd"]) == 200, "the sample cap changed"

    assert st.read_samples("snappy", "external_cfd")[0] == 250.0
    assert st.read_samples("nobody", "nothing") == []


def test_the_mesh_timing_store_drops_unparseable_samples(monkeypatch):
    fake = FakeSyncRedis()
    monkeypatch.setattr(timing_mod, "sync_client", lambda **k: fake)
    st = timing_mod.RedisMeshTimingStore()
    fake.lists["meshtime:vmtk:internal_cfd"] = ["12.0", "junk", "3.5"]
    assert st.read_samples("vmtk", "internal_cfd") == [12.0, 3.5]


def test_an_unkeyed_engine_or_purpose_still_has_a_stable_key(monkeypatch):
    fake = FakeSyncRedis()
    monkeypatch.setattr(timing_mod, "sync_client", lambda **k: fake)
    timing_mod.RedisMeshTimingStore().append_sample("", "", 5.0, cap=10)
    assert "meshtime:unknown:unknown" in fake.lists


# event stream: the consumer half
class _FakePubSub:
    def __init__(self, messages):
        self.messages = list(messages)
        self.subscribed: list = []
        self.unsubscribed: list = []
        self.closed = False

    async def subscribe(self, ch): self.subscribed.append(ch)
    async def unsubscribe(self, ch): self.unsubscribed.append(ch)
    async def aclose(self): self.closed = True
    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        return self.messages.pop(0) if self.messages else None


class _FakeAsyncStreamRedis:
    def __init__(self, backlog=(), messages=()):
        self._backlog = list(backlog)
        self._pubsub = _FakePubSub(messages)
        self.lranged: list = []

    def pubsub(self): return self._pubsub
    async def lrange(self, key, a, b):
        self.lranged.append((key, a, b))
        return list(self._backlog)


@pytest.mark.asyncio
async def test_the_subscription_subscribes_to_the_jobs_channel_and_replays_its_log(monkeypatch):
    from meshpipeline.events.channels import channel_for, log_key_for

    fake = _FakeAsyncStreamRedis(backlog=['{"type":"note","seq":1}'])
    monkeypatch.setattr(stream_mod, "_get_async_redis", lambda: fake)

    sub = stream_mod.RedisEventSubscription("job-1")
    await sub.open()
    assert fake._pubsub.subscribed == [channel_for("job-1")], "subscribed to the wrong channel"

    got = await sub.backlog()
    assert got == ['{"type":"note","seq":1}']
    assert fake.lranged == [(log_key_for("job-1"), 0, -1)], "replayed the wrong key/range"

    await sub.close()
    assert fake._pubsub.unsubscribed == [channel_for("job-1")] and fake._pubsub.closed


@pytest.mark.asyncio
async def test_the_subscription_delivers_live_messages_and_reports_silence(monkeypatch):
    fake = _FakeAsyncStreamRedis(messages=[
        {"type": "message", "data": '{"type":"note"}'},
        {"type": "subscribe", "data": 1},          # pub/sub bookkeeping: not an event
    ])
    monkeypatch.setattr(stream_mod, "_get_async_redis", lambda: fake)
    sub = stream_mod.RedisEventSubscription("job-1")
    await sub.open()

    assert await sub.next_event(timeout=5.0) == '{"type":"note"}'
    assert await sub.next_event(timeout=5.0) is None, "bookkeeping leaked out as an event"
    assert await sub.next_event(timeout=5.0) is None, "silence must read as None, not hang"


@pytest.mark.asyncio
async def test_closing_an_unopened_subscription_is_harmless(monkeypatch):
    monkeypatch.setattr(stream_mod, "_get_async_redis", lambda: _FakeAsyncStreamRedis())
    sub = stream_mod.RedisEventSubscription("job-1")
    await sub.close()                       # the socket's finally: runs even if open() never did
    assert await sub.next_event(timeout=0.1) is None


# the seam itself
def test_every_redis_capability_is_bound_by_runtime_composition(monkeypatch):
    from meshpipeline.contracts import (
        dead_letter,
        delivery_guard,
        event_stream,
        mesh_timing,
        model_capacity,
        rate_limit,
    )
    from meshpipeline.runtime.composition import install_adapters

    for mod, attr in ((delivery_guard, "_guard"), (rate_limit, "_store"),
                      (dead_letter, "_sink"), (mesh_timing, "_store"),
                      (event_stream, "_subscription_factory"),
                      (model_capacity, "_controller")):
        monkeypatch.setattr(mod, attr, None)

    install_adapters()

    assert delivery_guard._guard is not None, "the delivery guard is not composed"
    assert rate_limit._store is not None, "the rate-limit store is not composed"
    assert dead_letter._sink is not None, "the dead-letter sink is not composed"
    assert mesh_timing._store is not None, "the mesh-timing store is not composed"
    assert event_stream._subscription_factory is not None, "the event-stream reader is not composed"
    # Model-call ADMISSION was the one capability composition forgot, and nothing noticed:
    # every model-backed path (Intake, Builder, Reviewer) raises CapacityError on its first
    # call when it is missing, and no tier that reaches a real model ran in CI.
    assert model_capacity._controller is not None, "the model-capacity controller is not composed"


def test_the_composed_capacity_controller_is_the_redis_one(monkeypatch):
    # Admission has to be shared across the API and the worker; an in-process controller would
    # admit each of them separately and silently double the real concurrency ceiling.
    from meshpipeline.adapters.model_capacity.redis import RedisCapacityController
    from meshpipeline.contracts import model_capacity
    from meshpipeline.runtime.composition import install_adapters

    monkeypatch.setattr(model_capacity, "_controller", None)
    install_adapters()
    assert isinstance(model_capacity._controller, RedisCapacityController), (
        f"production composed {type(model_capacity._controller).__name__}, not "
        "RedisCapacityController")


@pytest.mark.asyncio
async def test_a_routed_model_call_no_longer_fails_for_want_of_composition(monkeypatch):
    # The exact failure this wiring fixes: routing.execute admits through model_capacity before
    # it calls a provider, so an uncomposed process died with CapacityError on the first turn.
    from meshpipeline.contracts import model_capacity
    from meshpipeline.runtime.composition import install_adapters

    monkeypatch.setattr(model_capacity, "_controller", None)
    with pytest.raises(model_capacity.CapacityError, match="no capacity controller configured"):
        model_capacity.get_capacity_controller()

    install_adapters()
    assert model_capacity.get_capacity_controller() is not None

    # ...and the explicit failure is still there for a process that never composed - the fix
    # must not have degraded it into a lazy default.
    monkeypatch.setattr(model_capacity, "_controller", None)
    with pytest.raises(model_capacity.CapacityError, match="no capacity controller configured"):
        model_capacity.get_capacity_controller()


def test_each_port_names_a_capability_not_a_transport():
    import inspect

    from meshpipeline.contracts import dead_letter, delivery_guard, mesh_timing, rate_limit

    for mod in (delivery_guard, rate_limit, dead_letter, mesh_timing):
        src = inspect.getsource(mod)
        for banned in ("import redis", "aioredis", "REDIS_URL", "lpush", "ltrim", "pubsub"):
            assert banned not in src, f"{mod.__name__} leaks transport vocabulary: {banned!r}"
