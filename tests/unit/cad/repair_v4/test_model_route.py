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
        self.candidate = CANDIDATE

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
                       "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
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
    assert p.stop_reason in ("wave_cap", "file_ceiling") and p.ai_capped
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
    assert p.ai_capped and p.stop_reason == "file_ceiling"


def test_the_route_probe_goes_through_the_real_router_and_falls_back(wire):
    model_route.install_router()
    wire.fail_models = {"gpt-6-astra"}                 # astra's transport fails: the probe must pass over it
    assert model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES) == TERRA
    assert [r["model"] for r in wire.requests] == ["gpt-6-astra", "gpt-5.6-terra"]


def test_no_route_passing_the_probe_is_a_refusal_not_a_guess(wire):
    model_route.install_router()
    wire.fail_models = {"gpt-6-astra", "gpt-5.6-terra"}
    with pytest.raises(NoUsableRoute):
        model_route.probe_route_chain(mi.REPAIR_V4_PROPOSE_ROUTES)


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
