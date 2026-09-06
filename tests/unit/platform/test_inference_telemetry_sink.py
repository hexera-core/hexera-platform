# Responsibility: Verify every costed model call is stored, readable per job, and never disturbs the call it measures.
# Boundaries: the telemetry sink and its composition; it asserts no routing policy and no price value.
from __future__ import annotations

import json

import pytest

from meshpipeline.adapters.inference_telemetry import redis as sink_mod
from meshpipeline.contracts.inference_telemetry import InferenceCall


class FakeSyncRedis:

    def __init__(self):
        self.lists: dict = {}
        self.expires: dict = {}
        self.closed = False

    def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)
        return len(self.lists[k])

    def ltrim(self, k, a, b):
        self.lists[k] = self.lists.get(k, [])[a:b + 1]
        return True

    def lrange(self, k, a, b):
        items = self.lists.get(k, [])
        return items[a:] if b == -1 else items[a:b + 1]

    def expire(self, k, ttl):
        self.expires[k] = ttl
        return True

    def close(self):
        self.closed = True

    def pipeline(self):
        outer = self

        class P:
            def __init__(self): self.ops = []
            def lpush(self, k, v): self.ops.append(("lpush", k, v)); return self
            def ltrim(self, k, a, b): self.ops.append(("ltrim", k, a, b)); return self
            def expire(self, k, ttl): self.ops.append(("expire", k, ttl)); return self
            def execute(self):
                for op in self.ops:
                    getattr(outer, op[0])(*op[1:])
        return P()


def _call(**over) -> InferenceCall:
    base = {"job_id": "job-1", "role": "visual_reviewer", "route_id": "visual_reviewer:primary",
            "provider": "deepinfra", "model": "Qwen/Qwen3-VL-235B-A22B-Thinking",
            "target": "primary", "selection_reason": "primary_healthy",
            "input_tokens": 1200, "output_tokens": 340, "estimated_cost_usd": 0.0042}
    base.update(over)
    return InferenceCall(**base)


@pytest.fixture
def fake(monkeypatch):
    f = FakeSyncRedis()
    monkeypatch.setattr(sink_mod, "sync_client", lambda **k: f)
    return f


# the store: every call, newest first, bounded

def test_the_sink_stores_the_whole_costed_record(fake):
    sink = sink_mod.RedisInferenceTelemetrySink()
    sink.record(_call())

    stored = json.loads(fake.lists["inference:calls"][0])
    # The billing input is the record, not a summary of it: tokens AND the cost derived from them
    # have to survive the write, or a bill cannot be reconstructed from what was stored.
    assert stored["job_id"] == "job-1" and stored["role"] == "visual_reviewer"
    assert stored["input_tokens"] == 1200 and stored["output_tokens"] == 340
    assert stored["estimated_cost_usd"] == 0.0042
    assert stored["model"] == "Qwen/Qwen3-VL-235B-A22B-Thinking"


def test_the_feed_is_newest_first_and_stays_capped(fake):
    sink = sink_mod.RedisInferenceTelemetrySink()
    sink.record(_call(job_id="a"))
    sink.record(_call(job_id="b"))
    stored = [json.loads(x) for x in fake.lists["inference:calls"]]
    assert [r["job_id"] for r in stored] == ["b", "a"], "the feed is not newest-first"

    for i in range(sink_mod._FEED_KEEP + 200):
        sink.record(_call(job_id=str(i)))
    assert len(fake.lists["inference:calls"]) == sink_mod._FEED_KEEP, "the feed cap changed"


def test_a_jobs_calls_are_readable_on_their_own_key_and_expire(fake):
    sink = sink_mod.RedisInferenceTelemetrySink()
    sink.record(_call(job_id="job-1", role="intake"))
    sink.record(_call(job_id="job-2", role="builder"))
    sink.record(_call(job_id="job-1", role="visual_reviewer"))

    # Cost is billed per job, so a job's calls have to be readable without scanning the feed.
    assert [r["role"] for r in sink.for_job("job-1")] == ["visual_reviewer", "intake"]
    assert [r["role"] for r in sink.for_job("job-2")] == ["builder"]
    assert sink.for_job("nobody") == []
    assert fake.expires["job:job-1:inference"] == sink_mod._JOB_TTL_S, "the per-job TTL changed"


def test_the_sink_is_inspectable_and_survives_a_corrupt_record(fake):
    sink = sink_mod.RedisInferenceTelemetrySink()
    sink.record(_call(job_id="a"))
    fake.lists["inference:calls"].insert(0, "{not json")   # a malformed neighbour
    sink.record(_call(job_id="b"))

    assert [r["job_id"] for r in sink.recent(10)] == ["b", "a"], (
        "one unreadable record hid the readable ones")


def test_an_unattributed_call_is_still_stored_under_a_stable_key(fake):
    # A call made outside a job (a warm-up, a probe) still costs money; it must not vanish and
    # must not create an empty-named key.
    sink_mod.RedisInferenceTelemetrySink().record(_call(job_id=""))
    assert len(sink_mod.RedisInferenceTelemetrySink().recent(10)) == 1
    assert "job::inference" not in fake.lists, f"an empty job id made a key: {list(fake.lists)}"


# fail-open: the contract's promise

def test_a_telemetry_outage_never_raises_into_the_call_it_measures(monkeypatch):
    from meshpipeline.contracts import inference_telemetry

    class _Broken:
        def pipeline(self): raise ConnectionError("telemetry down")
        def close(self): pass

    monkeypatch.setattr(sink_mod, "sync_client", lambda **k: _Broken())
    try:
        inference_telemetry.set_inference_telemetry(sink_mod.RedisInferenceTelemetrySink())
        inference_telemetry.record(_call())          # must not raise into the model call
    finally:
        inference_telemetry.set_inference_telemetry(None)


def test_a_dropped_connection_is_not_cached_into_every_later_call(monkeypatch):
    good = FakeSyncRedis()
    clients = iter([ConnectionError("telemetry down"), good])

    class _Flaky:
        def __init__(self, outcome): self.outcome = outcome
        def pipeline(self):
            raise self.outcome

    def _client(**k):
        nxt = next(clients)
        return _Flaky(nxt) if isinstance(nxt, Exception) else nxt

    monkeypatch.setattr(sink_mod, "sync_client", _client)
    sink = sink_mod.RedisInferenceTelemetrySink()
    with pytest.raises(ConnectionError):
        sink.record(_call())                          # the contract's recorder logs this
    sink.record(_call(job_id="after"))                # a new connection, not the dead one
    assert [r["job_id"] for r in sink.recent(10)] == ["after"]


# the seam itself

def test_the_inference_telemetry_sink_is_bound_by_runtime_composition(monkeypatch):
    # The regression this exists to prevent: routing builds a fully costed InferenceCall for
    # EVERY model call, and with no sink bound contracts.record() returned immediately and threw
    # each one away. Pricing is measured resource cost, so that is lost revenue, not a lost graph.
    from meshpipeline.contracts import inference_telemetry
    from meshpipeline.runtime.composition import install_adapters

    monkeypatch.setattr(inference_telemetry, "_sink", None)
    install_adapters()
    assert inference_telemetry._sink is not None, "the inference-telemetry sink is not composed"
    assert isinstance(inference_telemetry._sink, sink_mod.RedisInferenceTelemetrySink), (
        f"production composed {type(inference_telemetry._sink).__name__}, not the durable sink")


def test_a_composed_process_actually_writes_the_record_it_builds(monkeypatch, fake):
    # Binding is not enough: the composed sink has to reach storage through the contract's
    # module-level recorder, which is the only thing routing calls.
    from meshpipeline.contracts import inference_telemetry
    from meshpipeline.runtime.composition import install_adapters

    monkeypatch.setattr(inference_telemetry, "_sink", None)
    install_adapters()
    inference_telemetry.record(_call(job_id="job-9"))
    assert [r["job_id"] for r in sink_mod.RedisInferenceTelemetrySink().recent(10)] == ["job-9"]


# the price gap, made machine-detectable

def test_the_unpriced_detector_answers_from_the_configured_routes(monkeypatch):
    from meshpipeline.adapters.inference_telemetry import pricing

    monkeypatch.delenv("MODEL_PRICE_OVERRIDES", raising=False)
    unpriced = pricing.unpriced_route_models()
    assert "deepinfra:Qwen/Qwen3-VL-235B-A22B-Thinking" in unpriced, (
        "the detector does not see the reviewer's unpriced model")

    # An operator who supplies the missing price makes it priced - the detector reports the
    # configuration, not a hardcoded list of known gaps.
    monkeypatch.setenv("MODEL_PRICE_OVERRIDES",
                       "deepinfra:Qwen/Qwen3-VL-235B-A22B-Thinking=0.20,0.88,0.11")
    assert pricing.unpriced_route_models() == []


@pytest.mark.xfail(
    reason="a configured route model has no confirmed price; the release gate blocks on this, "
           "so the suite reports it without failing every unrelated commit",
    strict=False,
)
def test_every_configured_route_model_has_a_verified_price():
    # KNOWN GAP, deliberately red. The reviewer's model is absent from the price table because
    # its price could not be confirmed, so it meters at $0.00 - and the reviewer is the
    # image-heavy role, so every cost figure understates the most expensive agent. Guessing the
    # number here would fabricate cost evidence, which is worse than the gap; a human confirms
    # the provider's price and adds ONE entry. Do not delete this test to make the suite green.
    from meshpipeline.adapters.inference_telemetry.pricing import unpriced_route_models

    unpriced = unpriced_route_models()
    assert not unpriced, (
        f"these configured route models have no verified price and meter at $0.00: {unpriced}. "
        "Pricing is measured resource cost, so every one of them silently under-bills. Confirm "
        "the provider's published price, then add it to _PRICES in "
        "adapters/inference_telemetry/pricing.py (or set MODEL_PRICE_OVERRIDES).")
