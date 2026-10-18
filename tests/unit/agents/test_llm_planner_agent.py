# Responsibility: Verify llm_plan validates each proposal alone, stamps directives and never raises.
# Boundaries: the model seam is stubbed through set_model_router; nothing is executed.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.agents.repair_review.planner import PlanResult, llm_plan
from meshpipeline.contracts import model_inference


class _Router:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if reply == "timeout":
            await asyncio.sleep(0.5)
        return SimpleNamespace(
            ok=True, assistant_text=reply if isinstance(reply, str) else json.dumps(reply))


def _report() -> dict:
    return {"entities": [
        {"code": "small_edge", "fingerprint": "fa", "entity_type": "edge", "entity_index": 1,
         "severity": "error", "message": "m", "measurements": {}},
        {"code": "small_edge", "fingerprint": "fb", "entity_type": "edge", "entity_index": 2,
         "severity": "error", "message": "m", "measurements": {}},
        {"code": "open_wire", "fingerprint": "fc", "entity_type": "wire", "entity_index": 0,
         "severity": "error", "message": "m", "measurements": {}}]}


def _p(op="fix_small_edges", fp="fa", targets=None, conf=0.9):
    out = {"op": op, "defect_fingerprint": fp, "targets": targets or [fp], "rationale": "r"}
    if conf is not None:
        out["confidence"] = conf
    return out


@pytest.fixture
def declines(monkeypatch):
    rows = []
    import meshpipeline.capture.logger as cl
    monkeypatch.setattr(cl.TrainingLogger, "log",
                        lambda self, ev, payload, **kw: rows.append((ev, payload, kw)))
    return rows


@pytest.fixture(autouse=True)
def _reset():
    yield
    model_inference.set_model_router(None)


@pytest.mark.asyncio
async def test_a_mixed_reply_plays_the_valid_and_declines_each_invalid_alone(declines):
    model_inference.set_model_router(_Router({"proposals": [
        _p(), _p(op="delete_everything"), _p(fp="nope"),
        _p(fp="fa", targets=["fa", "fc"]),
        _p(op="fill_planar_hole", fp="fa", targets=["fa", "fb"]),
        _p(op="sew_faces", fp="fc")]}))
    out = await llm_plan(_report(), story={}, job_id="j")
    assert [d["op"] for d in out.directives] == ["fix_small_edges", "sew_faces"]
    assert out.directives[0]["defect_code"] == "small_edge"
    assert len(out.declines) == 4 and len(declines) == 4
    assert declines[0][0] == "repair_review" and declines[0][2]["op_id"] == "review:plan"


@pytest.mark.asyncio
async def test_confidence_at_the_threshold_plays_and_below_declines(declines):
    t = rcfg.CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE
    model_inference.set_model_router(_Router({"proposals": [
        _p(conf=t), _p(conf=t - 0.01), _p(conf=None), _p(conf="high")]}))
    out = await llm_plan(_report(), story={}, job_id="j")
    assert len(out.directives) == 1 and out.directives[0]["planner_confidence"] == t
    assert len(out.declines) == 3


@pytest.mark.asyncio
async def test_no_router_returns_empty_without_raising(declines):
    model_inference.set_model_router(None)
    out = await llm_plan(_report(), story={}, job_id="j")
    assert out == PlanResult(directives=[], declines=[], called=0, model_reply_cached=False)


@pytest.mark.asyncio
async def test_one_retry_then_fail_open(monkeypatch, declines):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER_CALLS_MAX", 2)
    router = _Router("not json", {"proposals": [_p()]})
    model_inference.set_model_router(router)
    out = await llm_plan(_report(), story={}, job_id="j")
    assert router.calls == out.called == 2 and len(out.directives) == 1
    assert len(out.call_failures) == 1 and out.declines == []
    router = _Router("not json", "still not json", {"proposals": [_p()]})
    model_inference.set_model_router(router)
    out = await llm_plan(_report(), story={}, job_id="j")
    assert router.calls == 2 and out.directives == [] and len(out.call_failures) == 2


@pytest.mark.asyncio
async def test_a_timeout_is_retried_too(monkeypatch, declines):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_PLANNER_TIMEOUT_S", 0.01)
    router = _Router("timeout", "timeout")
    model_inference.set_model_router(router)
    out = await llm_plan(_report(), story={}, job_id="j")
    assert router.calls == 2 and out.directives == []


@pytest.mark.asyncio
async def test_the_model_order_is_preserved_and_ids_are_stamped(declines):
    model_inference.set_model_router(_Router({"proposals": [
        _p(op="sew_faces", fp="fc"), _p(fp="fb"), _p(op="fix_shape_global", fp="fa")]}))
    out = await llm_plan(_report(), story={"x": 1}, job_id="j")
    assert [d["directive_id"] for d in out.directives] == ["llm:0", "llm:1", "llm:2"]
    assert [d["defect_fingerprint"] for d in out.directives] == ["fc", "fb", "fa"]
    d = out.directives[0]
    assert d["source"] == "llm-planner" and d["item_id"] == "" and d["targets"] == ["fc"]
    assert d["defect_code"] == "open_wire" and d["planner_confidence"] == 0.9


@pytest.mark.asyncio
async def test_a_target_of_another_code_than_the_defect_fingerprint_declines(declines):
    model_inference.set_model_router(_Router({"proposals": [_p(fp="fa", targets=["fc"])]}))
    out = await llm_plan(_report(), story={}, job_id="j")
    assert out.directives == [] and len(out.declines) == 1


@pytest.mark.asyncio
async def test_nan_inf_and_malformed_targets_decline_and_duplicates_collapse(declines):
    bad = [_p(conf=float("nan")), _p(conf=float("inf")), _p(targets="fa"),
           {**_p(), "targets": []}, {**_p(), "targets": None}]
    ok = _p(targets=["fa", "fa", "fb"])
    model_inference.set_model_router(_Router('{"proposals": ' + json.dumps(bad + [ok]).replace(
        "NaN", "NaN") + "}"))
    out = await llm_plan(_report(), story={}, job_id="j")
    assert len(out.declines) == 5 and out.directives[0]["targets"] == ["fa", "fb"]


@pytest.mark.asyncio
async def test_an_empty_list_is_one_logged_decline_and_no_entities_makes_no_call(declines):
    router = _Router({"proposals": []})
    model_inference.set_model_router(router)
    out = await llm_plan(_report(), story={}, job_id="j")
    assert out.directives == [] and len(out.declines) == 1 and len(declines) == 1
    out = await llm_plan({"entities": []}, story={}, job_id="j")
    assert router.calls == 1 and out.called == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("conf", [80, 1.5, -0.1])
async def test_a_confidence_outside_zero_to_one_declines(declines, conf):
    """The planner asks for a number from 0 to 1; `80` passed the minimum check, ran live and went
    into training records as a valid confidence."""
    model_inference.set_model_router(_Router({"proposals": [_p(conf=conf)]}))
    out = await llm_plan(_report(), story={}, job_id="j")
    assert out.directives == []
    assert any("between 0 and 1" in d["reason"] for d in out.declines)


def _two_codes_on_one_edge() -> dict:
    report = _report()
    report["entities"].append(
        {"code": "degenerate_edge", "fingerprint": "fa", "entity_type": "edge",
         "entity_index": 1, "severity": "error", "message": "m", "measurements": {}})
    return report


@pytest.mark.asyncio
async def test_a_proposal_names_which_of_an_entitys_problems_it_answers(declines):
    """One entity can carry several codes under one fingerprint; the label used to be whichever
    code the scan listed last. The reply names the code and the pair is checked."""
    model_inference.set_model_router(_Router({"proposals": [
        {**_p(), "defect_code": "small_edge"},
        {**_p(), "defect_code": "degenerate_edge"},
        {**_p(), "defect_code": "open_wire"}]}))
    out = await llm_plan(_two_codes_on_one_edge(), story={}, job_id="j")
    assert [d["defect_code"] for d in out.directives] == ["small_edge", "degenerate_edge"]
    assert any("does not carry" in d["reason"] for d in out.declines)


@pytest.mark.asyncio
async def test_an_entity_with_several_problems_and_no_named_code_declines(declines):
    model_inference.set_model_router(_Router({"proposals": [_p(), _p(fp="fb")]}))
    out = await llm_plan(_two_codes_on_one_edge(), story={}, job_id="j")
    # fb carries one code, so naming it is optional; fa carries two, so the reply must say which
    assert [d["defect_fingerprint"] for d in out.directives] == ["fb"]
    assert any("name the defect_code" in d["reason"] for d in out.declines)
