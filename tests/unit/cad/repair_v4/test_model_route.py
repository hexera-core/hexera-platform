# Responsibility: Prove the v4 AI proposer's DEFAULT path (no injected `call`) reaches the REAL router: AIProposer ->
#                 call_repair_v4_propose -> CachingModelRouter -> adapters.model_inference.router -> pricing, retry,
#                 normalisation, and the capacity admission - and that the wave ledger gates it.
# Boundaries: hermetic. The only fake is the PROVIDER CLIENT at `providers.client_for`, i.e. the wire. test_proposer
#             injects a `Fake` call (bypassing the router) and test_fail_closed_pricing calls the router without
#             AIProposer; this is the test that joins the two.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_inference import providers
from meshpipeline.cad.repair_v4 import model_route
from meshpipeline.cad.repair_v4 import proposer as proposer_mod
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
from meshpipeline.cad.repair_v4.proposer import AIProposer, NoUsableRoute
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts import model_inference as mi

ASTRA = "openai:gpt-6-astra"
TERRA = "openai:gpt-5.6-terra"
CANDIDATE = {"candidates": [{"op": "split_angle", "targets": ["F1"], "params": {"max_deg": 90}, "rationale": "r"}]}


class Wire:
    """The provider client: records every request and answers like the Responses API."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_models: set[str] = set()
        self.input_tokens, self.output_tokens = 1000, 100
        self.cache_write = None          # (total, ttl_30m) the provider reports writing to its cache
        self.candidate = CANDIDATE

    def _details(self) -> dict:
        d: dict = {"cached_tokens": 0}
        if self.cache_write is not None:
            total, w30 = self.cache_write
            d.update(cache_write_tokens=total, cache_write_tokens_details={
                "ttl_30m_tokens": w30, "ttl_12h_tokens": total - w30})
        return d

    def client(self, target):  # noqa: ANN001
        async def create(**kw):
            self.requests.append(kw)
            if kw["model"] in self.fail_models:
                raise ValueError("boom")          # not retryable: the router turns it into a failure marker
            names = [t["name"] for t in kw.get("tools", [])]
            if "report_colour" in names:
                call = {"name": "report_colour", "arguments": '{"colour": "red"}'}
            else:
                call = {"name": "propose", "arguments": json.dumps(self.candidate)}
            return SimpleNamespace(
                output=[{"type": "function_call", "call_id": "c1", **call}],
                usage={"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                       "input_tokens_details": self._details(), "output_tokens_details": {"reasoning_tokens": 0}},
                status="completed")
        return SimpleNamespace(responses=SimpleNamespace(create=create))


@pytest.fixture
def wire(monkeypatch):
    reset_breakers()
    before = (mi._router, model_capacity._controller)
    w = Wire()
    monkeypatch.setattr(providers, "client_for", w.client)
    inference_telemetry.set_inference_telemetry(None)
    yield w
    mi._router, model_capacity._controller = before
    reset_breakers()


def _state():
    from tests.unit.cad.repair_v4.test_proposer import make_ws, site, state
    return state([site("S1", "proximity")], ws=make_ws())


def _proposer(ledger, **kw):
    from tests.unit.cad.repair_v4.test_proposer import fake_observe
    # NO `call=`: the proposer must take its default path to the installed router
    return AIProposer(ASTRA, max_turns=4, usd_ceiling=1.0, ledger=ledger, observe=fake_observe,
                      file_id="f1", job_id="job-1", **kw)


def test_the_default_path_reaches_the_real_router_and_is_priced_into_the_ledger(wire):
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=100.0)
    out = _proposer(ledger).propose(_state(), k=3)
    assert [(c.op, c.targets) for c in out] == [("split_angle", ["F1"])]
    [req] = wire.requests
    assert req["model"] == "gpt-6-astra"
    assert req["max_output_tokens"] == mi.REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS == 4096
    assert {t["name"] for t in req["tools"]} >= {"propose", "declare_unfixable"}
    # the ROUTER priced it (1000 in x $10 + 100 out x $50 per Mtok) and the proposer settled that into the ledger
    assert ledger.spent_usd() == pytest.approx((1000 * 10 + 100 * 50) / 1e6)
    [row] = ledger.entries()
    assert (row["provider"], row["model"]) == ("openai", "gpt-6-astra") and row["job_id"] == "job-1"


def _settled_with(wire, cache_write):
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=100.0)
    wire.input_tokens, wire.output_tokens, wire.cache_write = 4060, 106, cache_write
    _proposer(ledger).propose(_state(), k=3)
    return ledger


def test_cache_write_tokens_are_settled_above_the_same_usage_without_them(wire):
    # THE guard against re-introducing the under-recording: the same wire usage, once with and once
    # without cache_write_tokens, must settle strictly higher with them (astra 30m write $12.50/M > $10/M).
    without = _settled_with(wire, None)
    with_writes = _settled_with(wire, (4057, 4057))
    assert with_writes.spent_usd() > without.spent_usd()
    # and the settled row is exactly usage x rates, writes included, to the micro-dollar
    assert with_writes.spent_usd() == pytest.approx((3 * 10.0 + 4057 * 12.50 + 106 * 50.0) / 1e6, abs=1e-6)
    [row] = with_writes.entries()
    assert (row["cache_write_tokens"], row["cache_write_30m_tokens"]) == (4057, 4057)


def test_an_unattributed_cache_write_settles_at_the_dearer_12h_rate(wire):
    ledger = _settled_with(wire, (4057, 0))
    assert ledger.spent_usd() == pytest.approx((3 * 10.0 + 4057 * 25.00 + 106 * 50.0) / 1e6, abs=1e-6)


def test_lane_b_live_usage_settles_to_the_corrected_figure(wire):
    # The seven real gpt-6-astra payloads of Lane B (probe, search-run1, search-run2). The ledger then
    # recorded $0.1345 (writes at $10/M); the pricing page bills them at $12.50/M: $0.1591.
    from meshpipeline.adapters.inference_telemetry.pricing import estimate_cost
    calls = [(86, 0, 0, 18), (2370, 0, 2244, 27), (2370, 2215, 29, 27), (3469, 2338, 1128, 89),
             (4653, 3466, 1184, 68), (4060, 0, 4057, 106), (5279, 4057, 1219, 71)]
    new = sum(estimate_cost("openai", "gpt-6-astra", input_tokens=i, cached_input_tokens=c,
                            cache_write_tokens=w, cache_write_30m_tokens=w, output_tokens=o)
              for i, c, w, o in calls)
    old = sum(estimate_cost("openai", "gpt-6-astra", input_tokens=i, cached_input_tokens=c, output_tokens=o)
              for i, c, w, o in calls)
    assert round(old, 4) == 0.1345 and round(new, 4) == 0.1591


def test_the_reservation_prices_input_at_the_dearest_rate_a_write_can_bill_at(wire):
    from meshpipeline.adapters.model_inference.router import repair_v4_prices
    assert repair_v4_prices(ASTRA) == (12.50, 50.0, 1.0)        # fresh input 10, but a 30m write is 12.50
    assert repair_v4_prices(TERRA) == (2.0, 12.0, 0.2)          # no confirmed write rate: unchanged


def test_without_an_installed_router_the_default_path_cannot_reach_a_model(wire):
    mi._router = None
    with pytest.raises(mi.ModelInferenceError, match="no model router configured"):
        _proposer(WaveLedger.for_test(cap_usd=100.0)).propose(_state(), k=3)
    assert wire.requests == []


def test_a_ledger_with_no_room_stops_the_call_before_the_wire(wire):
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=0.001)         # less than the reservation of one call
    p = _proposer(ledger)
    assert p.propose(_state(), k=3) == []
    assert wire.requests == [], "the ledger said no, yet a request reached the provider"
    assert p.stop_reason == "no_affordable_turn" and p.ai_capped
    assert ledger.spent_usd() == 0.0


def test_the_per_file_ceiling_holds_through_the_router(wire):
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=100.0)
    wire.input_tokens, wire.output_tokens = 4000, 400            # about $0.06 a round
    p = AIProposer(ASTRA, max_turns=50, usd_ceiling=0.10, ledger=ledger, file_id="f1", job_id="j",
                   observe=lambda st, sites: {"sites": []})
    for _ in range(10):
        p.propose(_state(), k=3)
    assert ledger.file_spent_usd("f1") <= 0.10 + 1e-9
    assert p.ai_capped and p.stop_reason == "no_affordable_turn"


def test_the_route_probe_goes_through_the_real_router_and_falls_back(wire):
    model_route.install_router()
    wire.fail_models = {"gpt-6-astra"}                 # astra's transport fails: the probe must pass over it
    ledger = WaveLedger.for_test(cap_usd=5.0)
    assert model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES, ledger=ledger) == TERRA
    assert [r["model"] for r in wire.requests] == ["gpt-6-astra", "gpt-5.6-terra"]


def test_no_route_passing_the_probe_is_a_refusal_not_a_guess(wire):
    model_route.install_router()
    wire.fail_models = {"gpt-6-astra", "gpt-5.6-terra"}
    with pytest.raises(NoUsableRoute):
        model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES, ledger=WaveLedger.for_test(cap_usd=5.0))


def test_every_route_probe_call_is_metered_into_the_shard_ledger(wire):
    """The sixth ledger gap (Task 49): the tool-and-image probe each shard makes at startup was a paid call that
    never reached the ledger. Every probe that was answered is now settled there, under its own file id and kind;
    one that failed unbilled is released."""
    model_route.install_router()
    wire.fail_models = {"gpt-6-astra"}                 # astra fails unbilled (the router's marker), terra answers
    ledger = WaveLedger.for_test(cap_usd=5.0)
    assert model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES, ledger=ledger) == TERRA
    [entry] = ledger.entries()
    assert entry["file_id"] == model_route.ROUTE_PROBE_FILE_ID and entry["kind"] == "route_probe"
    assert entry["model"] == "gpt-5.6-terra" and entry["input_tokens"] == 1000 and entry["usd"] > 0
    assert ledger.reserved_usd() == 0.0 and ledger.spent_usd() == pytest.approx(entry["usd"])


def test_a_salmon_answer_passes_and_a_retry_is_metered_too(wire):
    """dev6's failure mode through the real router and the shard ledger: "salmon" passes on the first call; a wrong
    colour then a right one passes on the second - and BOTH calls are settled in the ledger."""
    model_route.install_router()
    real = wire.client

    def client_answering(colours):
        it = iter(colours)

        def client(target):
            inner = real(target)

            async def create(**kw):
                wire.requests.append(kw)
                if "report_colour" in [t["name"] for t in kw.get("tools", [])]:
                    return SimpleNamespace(
                        output=[{"type": "function_call", "call_id": "c1", "name": "report_colour",
                                 "arguments": json.dumps({"colour": next(it)})}],
                        usage={"input_tokens": 100, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
                               "output_tokens_details": {"reasoning_tokens": 0}}, status="completed")
                return await inner.responses.create(**kw)
            return SimpleNamespace(responses=SimpleNamespace(create=create))
        return client

    providers.client_for = client_answering(["salmon"])
    ledger = WaveLedger.for_test(cap_usd=5.0)
    assert model_route.probe_route_chain([ASTRA], ledger=ledger) == ASTRA
    assert len(ledger.entries()) == 1

    providers.client_for = client_answering(["blue", "red"])
    ledger2 = WaveLedger.for_test(cap_usd=5.0)
    assert model_route.probe_route_chain([ASTRA], ledger=ledger2) == ASTRA
    assert [e["kind"] for e in ledger2.entries()] == ["route_probe", "route_probe"]
    assert ledger2.reserved_usd() == 0.0


def test_a_probe_the_ledger_has_no_room_for_is_never_sent(wire):
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=0.001)
    with pytest.raises(NoUsableRoute, match="ledger"):
        model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES, ledger=ledger)
    assert wire.requests == [] and ledger.spent_usd() == 0.0


def test_the_probe_cannot_be_called_without_a_ledger(wire):
    with pytest.raises(TypeError):
        model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES)  # type: ignore[call-arg]


def test_a_missing_key_is_refused_by_name():
    with pytest.raises(model_route.NoModelKey, match="OPENAI_API_KEY"):
        model_route.require_model_key("")
    with pytest.raises(model_route.NoModelKey):
        model_route.require_model_key("   ")
    assert model_route.require_model_key("sk-x") == "sk-x"


def test_the_installed_router_does_not_record_or_replay_a_proposer_round(wire):
    # a replayed answer would cost $0 against the ledger; CachingModelRouter must forward, never serve a record
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=100.0)
    _proposer(ledger).propose(_state(), k=3)
    _proposer(ledger).propose(_state(), k=3)
    assert len(wire.requests) == 2


# --- D12: a file always gets a turn when its slice can afford one; otherwise it says so ---------------------------


def test_a_slice_too_small_for_the_full_reservation_still_buys_the_first_turn(wire):
    """Lane D9's live sample: 0060_00606843 shared shard 6 with a file that had settled $0.293 of the $0.50 slice; its
    first reservation (~$0.295, mostly the 4,096-token output allowance) did not fit the $0.207 left, so it got
    `wave_cap` and ZERO turns. Real outputs are ~100-260 tokens. Now the turn is reserved - and requested - with the
    output allowance the remaining room can pay for, so the file gets its turn."""
    model_route.install_router()
    full = 4096 * 50 / 1e6                         # the output allowance alone, at this route's $50/M
    ledger = WaveLedger.for_test(cap_usd=full * 0.6)  # the slice cannot hold a full-allowance reservation
    p = _proposer(ledger)
    out = p.propose(_state(), k=3)
    assert out and p.turns_used == 1 and p.stop_reason is None
    [req] = wire.requests
    assert proposer_mod.MIN_OUTPUT_TOKENS <= req["max_output_tokens"] < mi.REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS
    [res] = [r for r in ledger._transact(lambda rows, _a: list(rows)) if r["t"] == "reserve"]
    assert res["u"] / 1e6 <= full * 0.6                 # the reservation fits the slice it was taken from


def test_a_slice_that_cannot_afford_any_turn_records_a_distinct_stop_reason(wire):
    model_route.install_router()
    ledger = WaveLedger.for_test(cap_usd=proposer_mod.MIN_OUTPUT_TOKENS * 50 / 1e6 * 0.5)
    p = _proposer(ledger)
    assert p.propose(_state(), k=3) == []
    assert p.turns_used == 0 and wire.requests == []
    assert p.stop_reason == "no_affordable_turn" and p.ai_capped  # not a silent zero, and the file ran det-only


def test_a_slice_with_room_reserves_and_requests_the_full_allowance_as_before(wire):
    model_route.install_router()
    p = _proposer(WaveLedger.for_test(cap_usd=100.0))
    p.propose(_state(), k=3)
    assert wire.requests[0]["max_output_tokens"] == mi.REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS
