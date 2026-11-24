# Responsibility: Prove the AI proposer survives what a 96-shard wave does to its model route: 429s, 5xx and timeouts
#                 are retried a bounded number of times with jittered backoff that honours Retry-After; the ledger
#                 records billed usage only; a call that ultimately fails ends the AI for that file as
#                 `model_unavailable` (never an exception); and the shard's throttle bounds calls in flight.
# Boundaries: hermetic. The ONLY fake is the HTTP transport (`httpx.MockTransport`) under the REAL OpenAI SDK client,
#             under the REAL router (retries, classification, pricing) and the real AIProposer and WaveLedger. Sleeps
#             are recorded instead of slept.
from __future__ import annotations

import json
import threading
import time
from typing import Any

import anyio
import httpx
import openai
import pytest
from tests.unit.cad.repair_v4.test_proposer import ROUTE, fake_observe, make_ws, site, state

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_inference import providers, routing
from meshpipeline.cad.repair_v4 import model_route
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.model_throttle import (
    COOLDOWN_S,
    WAVE_SHARE,
    Slice,
    ThrottleConfigError,
    WaveBackoffGuard,
    WaveThrottle,
    wave_max_in_flight,
)
from meshpipeline.cad.repair_v4.proposer import MODEL_UNAVAILABLE, AIProposer
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts import model_inference as mi

USAGE = {"input_tokens": 4000, "output_tokens": 80,
         "input_tokens_details": {"cached_tokens": 3000, "cache_write_tokens": 0},
         "output_tokens_details": {"reasoning_tokens": 0}}


def ok_body(call_id: str = "c1") -> dict:
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-6-astra", "status": "completed",
            "output": [{"type": "function_call", "id": "fc_1", "call_id": call_id, "name": "propose",
                        "status": "completed",
                        "arguments": json.dumps({"candidates": [{"op": "split_angle", "targets": ["F1"],
                                                                 "params": {"max_deg": 90}, "rationale": "r"}]})}],
            "usage": USAGE, "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}


def rate_limited(retry_after_ms: int | None = 1500, code: str = "rate_limit_exceeded") -> httpx.Response:
    headers = {"retry-after-ms": str(retry_after_ms)} if retry_after_ms is not None else {}
    return httpx.Response(429, headers=headers,
                          json={"error": {"message": "Rate limit reached", "type": "requests", "code": code}})


class Transport:
    """Answers each request with the next scripted item (an httpx.Response, an exception to raise, or "ok");
    the last item repeats. Counts requests and the most in flight at once."""

    def __init__(self, script: list, hold_s: float = 0.0) -> None:
        self.script, self.hold_s = script, hold_s
        self.requests = 0
        self._lock = threading.Lock()
        self._live = 0
        self.peak = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            item = self.script[min(self.requests, len(self.script) - 1)]
            self.requests += 1
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            if self.hold_s:
                time.sleep(self.hold_s)          # blocks only this proposer's own thread and loop
            if isinstance(item, BaseException):
                raise item
            if item == "ok":
                return httpx.Response(200, json=ok_body())
            return item
        finally:
            with self._lock:
                self._live -= 1


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch):
    """The real router over the real SDK client over a scripted transport; sleeps recorded, not slept."""
    reset_breakers()
    before = (mi._router, model_capacity._controller)
    holder: dict[str, Any] = {}
    sleeps: dict[str, list[float]] = {"sdk": [], "router": []}

    def client_for(target: Any) -> Any:
        # exactly what production builds (`openai_api.get_openai_client`): the SDK's DEFAULT retries and timeout
        return openai.AsyncOpenAI(api_key="sk-test", base_url="https://api.openai.test/v1",
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(holder["t"].handle)))

    async def sdk_sleep(seconds: float) -> None:
        sleeps["sdk"].append(seconds)

    real_sleep = routing.asyncio.sleep

    async def router_sleep(seconds: float, *a: Any, **k: Any) -> None:
        sleeps["router"].append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(providers, "client_for", client_for)
    monkeypatch.setattr(anyio, "sleep", sdk_sleep)
    monkeypatch.setattr(routing, "asyncio", _AsyncioWithSleep(router_sleep))
    model_route.install_router()
    inference_telemetry.set_inference_telemetry(None)

    def use(t: Transport) -> Transport:
        holder["t"] = t
        return t

    yield use, sleeps
    mi._router, model_capacity._controller = before
    reset_breakers()


class _AsyncioWithSleep:
    """`routing`'s view of asyncio with only `sleep` replaced (wait_for, CancelledError... stay real)."""

    def __init__(self, sleep: Any) -> None:
        import asyncio
        self._asyncio, self.sleep = asyncio, sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(self._asyncio, name)


async def one_round(**kw: Any) -> mi.ProposeReply:
    return await mi.call_repair_v4_propose([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                                           [], route=ROUTE, job_id="j", **kw)


def proposer(ledger: WaveLedger, **kw: Any) -> AIProposer:
    return AIProposer(ROUTE, max_turns=6, usd_ceiling=1.0, ledger=ledger, observe=fake_observe, file_id="f1",
                      job_id="job", output_tokens=1000, **kw)


# --- the router under the real SDK: bounded, jittered, Retry-After honoured ---------------------------------------


def test_a_persistent_429_is_retried_a_bounded_number_of_times_honouring_retry_after(net: Any) -> None:
    use, sleeps = net
    t = use(Transport([rate_limited(retry_after_ms=1500)]))
    reply = anyio.run(one_round)
    assert reply.reply.failure_marker == "repair_v4_propose:rate_limit" and reply.cost_usd == 0.0
    # 2 router attempts x (1 + the SDK's 2 retries) = 6 requests, and not one more
    assert t.requests == 6
    assert sleeps["sdk"] == [1.5] * 4                                # the SDK waits exactly what the server asked
    [between] = sleeps["router"]                                     # one wait between the router's two attempts
    assert 5.0 * 0.75 <= between <= 5.0 * 1.25                       # its own jittered backoff (base 5 s) > 1.5 s


def test_the_router_never_comes_back_sooner_than_a_long_retry_after(net: Any) -> None:
    use, sleeps = net
    use(Transport([rate_limited(retry_after_ms=20_000)]))
    anyio.run(one_round)
    [between] = sleeps["router"]
    assert 20.0 <= between <= 20.0 * 1.25, between                  # Retry-After is a MINIMUM, plus jitter
    assert sleeps["sdk"] == [20.0] * 4


def test_a_retry_after_beyond_a_minute_is_capped_not_obeyed_for_ever(net: Any) -> None:
    use, sleeps = net
    use(Transport([rate_limited(retry_after_ms=600_000)]))
    anyio.run(one_round)
    assert sleeps["router"][0] <= routing.RETRY_AFTER_MAX_S * 1.25


def test_jitter_spreads_a_fleet_told_the_same_retry_after(net: Any) -> None:
    use, sleeps = net
    use(Transport([rate_limited(retry_after_ms=20_000)]))
    for _ in range(12):
        reset_breakers()
        anyio.run(one_round)
    assert len({round(s, 6) for s in sleeps["router"]}) > 1        # not all twelve back in the same instant


def test_5xx_then_success_returns_the_answer_and_bills_only_the_answer(net: Any) -> None:
    use, _ = net
    t = use(Transport([httpx.Response(500, json={"error": {"message": "x"}}),
                       httpx.Response(503, json={"error": {"message": "x"}}), "ok"]))
    ledger = WaveLedger.for_test(cap_usd=10.0)
    p = proposer(ledger)
    out = p.propose(state([site("S1", "proximity")], ws=make_ws()), k=3)
    assert t.requests == 3 and len(out) == 1
    [entry] = ledger.entries()
    assert (entry["input_tokens"], entry["cached_input_tokens"], entry["output_tokens"]) == (4000, 3000, 80)
    assert entry["usd"] == pytest.approx((1000 * 10 + 3000 * 1 + 80 * 50) / 1e6)  # the 200's usage, priced
    assert ledger.reserved_usd() == 0.0


def test_timeouts_are_bounded_and_end_as_a_marker_not_an_exception(net: Any) -> None:
    use, _ = net
    t = use(Transport([httpx.ReadTimeout("slow")]))
    reply = anyio.run(one_round)
    assert reply.reply.failure_marker == "repair_v4_propose:timeout" and t.requests == 6


def test_an_out_of_quota_429_is_not_retried_by_the_router(net: Any) -> None:
    use, sleeps = net
    t = use(Transport([rate_limited(retry_after_ms=None, code="insufficient_quota")]))
    reply = anyio.run(one_round)
    assert reply.reply.failure_marker == "repair_v4_propose:insufficient_balance"
    assert t.requests == 3 and sleeps["router"] == []                # the SDK's own 3 tries; no second attempt


# --- the proposer: a model that cannot be reached ends the AI for the file, cleanly -------------------------------


def test_a_model_that_stays_unreachable_ends_the_ai_as_model_unavailable_and_bills_nothing(net: Any) -> None:
    use, _ = net
    t = use(Transport([rate_limited(retry_after_ms=10)]))
    ledger = WaveLedger.for_test(cap_usd=10.0)
    p = proposer(ledger)
    st = state([site("S1", "proximity")], ws=make_ws())
    for _ in range(5):
        assert p.propose(st, k=3) == []                              # never raises; the search goes on det-only
    assert p.stop_reason == MODEL_UNAVAILABLE and not p.ai_capped
    assert t.requests == 3 * 6                                       # three rounds, each fully retried, then nothing
    assert ledger.entries() == [] and ledger.spent_usd() == 0.0 and ledger.reserved_usd() == 0.0
    assert ledger.file_statuses()["f1"]["stop_reason"] == MODEL_UNAVAILABLE


def test_one_failed_round_does_not_end_the_ai(net: Any) -> None:
    use, _ = net
    use(Transport([rate_limited(retry_after_ms=10)] * 6 + ["ok"]))
    p = proposer(WaveLedger.for_test(cap_usd=10.0))
    st = state([site("S1", "proximity")], ws=make_ws())
    assert p.propose(st, k=3) == [] and p.stop_reason is None
    assert p.model_unavailable          # so far the model has never answered: a search ending now ran without it
    assert len(p.propose(st, k=3)) == 1 and p.stop_reason is None
    assert not p.model_unavailable and p.result()["answered_rounds"] == 1 and p.result()["failed_rounds"] == 1


# --- the shard's throttle -------------------------------------------------------------------------------------------


def test_the_throttle_bounds_calls_in_flight_against_a_transport_that_429s(net: Any) -> None:
    """Eight proposers (eight files' worth of calls) share one shard slice of two in-flight calls. The transport
    rate-limits the first requests; the SDK and router retry; every proposer ends with an answer, the provider
    never sees more than two requests at once, and the ledger holds exactly the answered calls."""
    use, _ = net
    t = use(Transport([rate_limited(retry_after_ms=10)] * 5 + ["ok"], hold_s=0.05))
    throttle = WaveThrottle(Slice(max_in_flight=2, rpm=10_000, tpm=10_000_000))
    ledger = WaveLedger.for_test(cap_usd=50.0)
    results: list[Any] = []
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            p = AIProposer(ROUTE, max_turns=6, usd_ceiling=1.0, ledger=ledger, observe=fake_observe,
                           file_id=f"f{i}", job_id="job", output_tokens=1000, throttle=throttle)
            results.append((p.propose(state([site("S1", "proximity")], ws=make_ws()), k=3), p.stop_reason))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert errors == []
    assert t.peak <= 2 and throttle.peak_in_flight == 2
    assert len(results) == 8 and all(len(out) == 1 and stop is None for out, stop in results)
    assert len(ledger.entries()) == 8 and ledger.reserved_usd() == 0.0


def test_a_rate_limited_call_pauses_the_shard_before_its_next_call() -> None:
    now = [0.0]
    throttle = WaveThrottle(Slice(4, 1000, 1e9), clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert throttle.acquire(100)
    throttle.release(rate_limited=True)
    assert throttle.acquire(100) and now[0] >= COOLDOWN_S            # the next call waited out the cool-down
    throttle.release(rate_limited=True, retry_after_s=7.0)
    t0 = now[0]
    assert throttle.acquire(100) and 7.0 <= now[0] - t0 < COOLDOWN_S


def test_the_proposer_reports_a_rate_limited_round_to_the_throttle(net: Any) -> None:
    use, _ = net
    use(Transport([rate_limited(retry_after_ms=10)]))
    throttle = WaveThrottle(Slice(1, 1000, 1e9))
    proposer(WaveLedger.for_test(cap_usd=10.0), throttle=throttle).propose(
        state([site("S1", "proximity")], ws=make_ws()), k=3)
    assert throttle.rate_limited == 1


def test_a_slot_that_never_comes_is_a_failed_round_not_a_hang() -> None:
    now = [0.0]
    throttle = WaveThrottle(Slice(1, 1000, 1e9), clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert throttle.acquire(1)                                       # someone else holds the shard's only slot
    calls: list = []

    async def never(*a: Any, **k: Any) -> Any:
        calls.append(1)
        raise AssertionError("no call may be sent without a slot")

    ledger = WaveLedger.for_test(cap_usd=10.0)
    p = proposer(ledger, throttle=throttle, call=never, prices=(10.0, 50.0, 1.0))
    st = state([site("S1", "proximity")], ws=make_ws())
    for _ in range(3):
        assert p.propose(st, k=3) == []
    assert calls == [] and p.stop_reason == MODEL_UNAVAILABLE and ledger.reserved_usd() == 0.0


def test_the_requests_and_tokens_per_minute_slices_hold() -> None:
    now = [0.0]
    throttle = WaveThrottle(Slice(100, rpm=3, tpm=10_000), clock=lambda: now[0],
                            sleep=lambda s: now.__setitem__(0, now[0] + s))
    for _ in range(3):
        assert throttle.acquire(100, wait_s=0)
        throttle.release()
    assert not throttle.acquire(100, wait_s=0)                       # the 4th request in a minute waits
    assert throttle.acquire(100) and now[0] >= 60.0
    throttle.release()
    now[0] += 120
    assert throttle.acquire(9_000, wait_s=0)
    throttle.release()
    assert not throttle.acquire(2_000, wait_s=0)                     # 11k tokens would exceed 10k in the window


def test_a_wave_is_sliced_one_nth_per_shard_and_a_floor_needs_a_recorded_override() -> None:
    m = wave_max_in_flight(ROUTE)
    assert WAVE_SHARE == 0.05
    assert m == 37                                                   # 0.05 x 15000 rpm x 3 s / 60, floored
    # 37 // 96 == 0: a 96-shard wave's slice would be below one call. Flooring it to one lets the wave have 96 calls
    # in flight against a ceiling of 37, so that is REFUSED unless the launch says so explicitly (B11)
    with pytest.raises(ThrottleConfigError, match="allow.in.flight.floor"):
        WaveThrottle.for_shard(ROUTE, n_shards=96)
    s = WaveThrottle.for_shard(ROUTE, n_shards=96, allow_floor=True).slice
    assert s.max_in_flight == 1 and s.floored and s.max_in_flight * 96 > m
    assert s.rpm == pytest.approx(0.05 * 15_000 / 96) and s.tpm == pytest.approx(0.05 * 40_000_000 / 96)
    assert s.rpm * 96 == pytest.approx(750) and s.tpm * 96 == pytest.approx(2_000_000)    # the wave's whole share
    few = WaveThrottle.for_shard(ROUTE, n_shards=4).slice
    assert few.max_in_flight == 9 and not few.floored                # 37 // 4: no floor needed, no override needed
    assert WaveThrottle.for_shard(ROUTE, n_shards=6).slice.max_in_flight == 6     # snapshot v1r's dev shards
    with pytest.raises(ThrottleConfigError):
        WaveThrottle.for_shard(ROUTE, n_shards=96, max_in_flight=50)
    assert WaveThrottle.for_shard(ROUTE, n_shards=96, max_in_flight=50, allow_floor=True).slice.max_in_flight == 1
    with pytest.raises(ThrottleConfigError):
        WaveThrottle.for_shard("openai:unmeasured", n_shards=1)
    with pytest.raises(ThrottleConfigError):
        WaveThrottle.for_shard(ROUTE, n_shards=0)


def test_acquire_never_blocks_past_its_wall_time_budget_even_under_a_wave_backoff() -> None:
    """B11 (1): while the wave guard held, `deadline += wait` meant `remaining` never shrank, so acquire() could block
    for ever (300 s plus jitter per trip, re-armed by every new trip), outside the router's own deadline. Now wait_s
    of time is the bound, whatever is holding the call."""
    now = [0.0]

    class HoldForever:
        def hold_for(self) -> float:
            return 300.0

        def note_held(self, seconds: float) -> None:
            pass

    throttle = WaveThrottle(Slice(4, 1000, 1e9), clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s),
                            guard=HoldForever())  # type: ignore[arg-type]
    assert throttle.acquire(100, wait_s=5.0) is False
    assert now[0] == pytest.approx(5.0)                               # exactly the budget, not 300 s, not for ever
    assert throttle.waits_timed_out == 1


def test_a_malformed_trip_marker_is_ignored_not_raised_into_the_proposer(tmp_path: Any) -> None:
    """B11 (2): `hold_for` read t["until"] from whatever JSON the store held. A marker without a numeric `until`, or
    one that is not JSON, is now dropped in `_refresh` and counted; the shard is not held by it and nothing raises."""
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    store, clock = FileMarkerStore(tmp_path), Clock()
    guard = guard_for(store, "1/1", clock)
    bucket = int(clock() // guard.window_s)
    store.put(f"w1/429/{bucket}/tripped", json.dumps({"bucket": bucket, "at": clock()}))          # no until
    store.put(f"w1/429/{bucket - 1}/tripped", "not json at all")
    assert guard.hold_for() == 0.0
    assert guard.describe()["malformed_markers"] == 2
    store.put(f"w1/429/{bucket}/tripped", json.dumps({"until": "soon"}))                           # not a number
    guard._checked_at = -1e18
    assert guard.hold_for() == 0.0
    store.put(f"w1/429/{bucket}/tripped", json.dumps({"until": clock() + 60.0}))                   # a real trip
    guard._checked_at = -1e18
    assert guard.hold_for() > 0.0


# --- the wave-level 429 guard ---------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def guard_for(store: Any, shard: str, clock: Clock, **kw: Any) -> WaveBackoffGuard:
    return WaveBackoffGuard(store, wave_id="w1", shard_id=shard, wall=clock, check_interval_s=0.0,
                            resume_spread_s=0.0, **kw)


def test_429s_across_shards_past_the_threshold_trip_the_whole_wave(tmp_path: Any) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    store, clock = FileMarkerStore(tmp_path), Clock()
    shards = [guard_for(store, f"{i}/6", clock, threshold=5) for i in range(1, 7)]
    assert all(g.hold_for() == 0 for g in shards)
    # six DIFFERENT shards each see one rate-limited call: no shard is anywhere near a per-shard threshold
    results = [g.report_429() for g in shards]
    assert results == [False] * 5 + [True]                           # the sixth claims the last slot and trips
    for g in shards:                                                 # every shard - including ones that never 429ed
        assert g.hold_for() > 0
    assert guard_for(store, "7/6", clock, threshold=5).report_429() is False     # only ONE shard writes the trip
    clock.t += 301
    assert all(g.hold_for() == 0 for g in shards)                    # and the back-off ends


def test_429s_at_or_below_the_threshold_do_not_trip(tmp_path: Any) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    store, clock = FileMarkerStore(tmp_path), Clock()
    shards = [guard_for(store, f"{i}/6", clock, threshold=5) for i in range(1, 6)]
    for g in shards:
        g.report_429()
    assert all(g.hold_for() == 0 for g in shards)                    # five is not MORE than five


def test_the_throttle_holds_every_new_call_while_the_wave_is_tripped(tmp_path: Any) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    store, clock = FileMarkerStore(tmp_path), Clock()
    other = guard_for(store, "2/2", clock, threshold=1)
    other.report_429()
    assert other.report_429() is True                                # a different shard trips the wave
    mine = guard_for(store, "1/2", clock, threshold=1)
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s
        clock.t += s

    throttle = WaveThrottle(Slice(4, 1000, 1e9), clock=lambda: t[0], sleep=sleep, guard=mine)
    # held for the whole of a short budget, then given up - never blocked past wait_s (B11)
    assert throttle.acquire(10, wait_s=5) is False and t[0] == 5.0
    assert mine.describe()["held_s"] >= 5 and mine.describe()["holds"] >= 1 and throttle.waits_timed_out == 1
    # a call whose budget covers the trip is held until it lifts (~300 s), then goes
    assert throttle.acquire(10, wait_s=400) and t[0] >= 299
    assert mine.describe()["trips_seen"] and mine.describe()["trips_seen"][0]["by_shard"] == "2/2"


def test_a_marker_store_failure_fails_open_and_is_recorded() -> None:
    class Broken:
        def create(self, *a: Any) -> bool:
            raise OSError("bucket gone")

        def get(self, *a: Any) -> str | None:
            raise OSError("bucket gone")

    g = guard_for(Broken(), "1/2", Clock())
    assert g.report_429() is False and g.hold_for() == 0.0
    assert g.describe()["store_errors"] == 2


def test_insufficient_quota_is_never_reported_to_the_wave_guard(net: Any, tmp_path: Any) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    use, _ = net
    t = use(Transport([rate_limited(retry_after_ms=None, code="insufficient_quota")]))
    guard = guard_for(FileMarkerStore(tmp_path), "1/1", Clock(), threshold=1)
    throttle = WaveThrottle(Slice(1, 1000, 1e9), guard=guard)
    proposer(WaveLedger.for_test(cap_usd=10.0), throttle=throttle).propose(
        state([site("S1", "proximity")], ws=make_ws()), k=3)
    assert t.requests == 3                                           # still un-retried by the router (SDK's own 3)
    assert guard.describe()["reported_429"] == 0 and throttle.rate_limited == 0


def test_a_429ing_provider_across_shards_trips_the_wave_through_the_proposer(net: Any, tmp_path: Any) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    use, _ = net
    use(Transport([rate_limited(retry_after_ms=10)]))
    store, clock = FileMarkerStore(tmp_path), Clock()
    guards = [guard_for(store, f"{i}/3", clock, threshold=2) for i in (1, 2, 3)]
    for g in guards:                                                 # one rate-limited file on each of three shards
        proposer(WaveLedger.for_test(cap_usd=10.0), throttle=WaveThrottle(Slice(1, 1000, 1e9), guard=g)).propose(
            state([site("S1", "proximity")], ws=make_ws()), k=3)
    assert guards[0].hold_for() > 0 and guards[1].hold_for() > 0     # shards 1 and 2 are held by shard 3's trip
    assert sum(g.describe()["tripped_here"] for g in guards) == 1


def test_one_round_cannot_outlast_half_a_files_wall_clock() -> None:
    """A hung provider used to cost 300 s per attempt x 2 attempts + backoff - more than the 600 s a file gets."""
    from meshpipeline.adapters.model_inference import router

    route = router._propose_route("openai", "gpt-6-astra")
    assert route.total_deadline_s is not None and route.total_deadline_s <= 600.0 / 2
    assert route.timeout_s < route.total_deadline_s


def test_a_hung_provider_ends_the_round_at_the_round_deadline(net: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt hangs; the round still returns a timeout marker, only as many attempts as fit, and
    the attempt count the ledger and the report are shown is the number that REACHED the provider."""
    import asyncio

    from meshpipeline.adapters.model_inference import router

    use, _ = net
    # Warm the call path before shrinking the timeouts. The first propose call in a process spends
    # ~0.6-1.0 s inside `invoke` on imports, pricing and client construction BEFORE the request reaches
    # the wire; against the 50 ms attempt timeout below that alone ends the round with the transport
    # never asked, which measures the shrunken timeout rather than the hang. Production's timeout is
    # 120 s against the same ~1 s, so nothing real rides on this (see task-56 report).
    use(Transport(["ok"]))
    assert anyio.run(one_round).reply.ok

    monkeypatch.setattr(router, "PROPOSE_ATTEMPT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(router, "PROPOSE_ROUND_DEADLINE_S", 0.2)

    class Hang(Transport):
        async def handle(self, request: httpx.Request) -> httpx.Response:
            self.requests += 1
            await asyncio.Event().wait()           # never answers
            raise AssertionError

    t = use(Hang([]))
    t0 = time.monotonic()
    reply = anyio.run(one_round)
    assert reply.reply.failure_marker == "repair_v4_propose:timeout"
    assert time.monotonic() - t0 < 2.0 and 1 <= t.requests <= 2
    # No fabricated attempts: every attempt the ledger will charge to this round is one a provider
    # really received. A count above t.requests would attribute a call that never left the process.
    assert reply.reply.provider.attempts == t.requests
