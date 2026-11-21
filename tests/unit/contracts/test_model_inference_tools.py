# Responsibility: Verify the repair-review route calls tools natively: tools threaded, a typed refusal from a router that cannot, and a decision cache keyed over schemas and the tool-call trace.
from __future__ import annotations

import asyncio
import inspect

import pytest

from meshpipeline.contracts import decision_cache as dc
from meshpipeline.contracts import model_inference as mi
from meshpipeline.contracts.model_inference import (
    ModelReply,
    ModelRoundResult,
    ToolCallRequest,
    ToolSchema,
    ToolsUnsupported,
)

LOOK = ToolSchema(name="look", description="Render the region.",
                  parameters={"type": "object", "properties": {"region": {"type": "integer"}},
                              "required": ["region"]})
MEASURE = ToolSchema(name="measure", description="Measure a face.",
                     parameters={"type": "object", "properties": {"face": {"type": "integer"}}})


class _ToolModel:
    """A tool-capable repair-review router: answers each round from a script, records what it was asked."""

    def __init__(self, script=()) -> None:
        self.script = list(script)
        self.seen: list[dict] = []

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview", *,
                                       tools=None, user_id=""):
        self.seen.append({"messages": [dict(m) for m in messages], "job_id": job_id, "name": name,
                          "tools": tools, "user_id": user_id})
        if self.script:
            return self.script.pop(0)
        n = len(self.seen)
        return ModelRoundResult(tool_calls=(ToolCallRequest(id=f"call_{n:08d}", name="look",
                                                            arguments=f'{{"region": {n}}}'),))


class _LegacyModel:
    """Today's router: the repair-review call takes no tools at all."""

    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    async def call_repair_review_model(self, messages, job_id="", name="RepairReview"):
        self.kwargs.append({"messages": messages, "job_id": job_id, "name": name})
        return ModelRoundResult(assistant_text='{"ok": true}')


class _NoCalls:
    async def call_repair_review_model(self, messages, job_id="", name="RepairReview", *,
                                       tools=None, user_id=""):
        raise AssertionError("a replayed round asked the model")


@pytest.fixture
def installed():
    before = mi._router

    def _install(router):
        mi.set_model_router(router)
        return router
    yield _install
    mi._router = before


def _conv():
    return [{"role": "system", "content": "investigate the region"},
            {"role": "user", "content": "region 3: a sliver face"}]


def _caching(inner, mode="reuse"):
    return dc.CachingModelRouter(inner, model_of=lambda step: "openai:gpt-x",
                                 store=dc.MemoryDecisions(), mode=mode)


# ------------------------------------------------------------------ the contract -----------
def test_tools_are_accepted_and_threaded(installed):
    model = installed(_ToolModel())
    reply: ModelReply = asyncio.run(mi.call_repair_review_model(
        _conv(), job_id="j-tools", tools=[LOOK, MEASURE]))
    assert reply.tool_calls and reply.tool_calls[0].name == "look"
    # the router sees the neutral function-tool declarations every other route already speaks
    assert model.seen[0]["tools"] == [
        {"type": "function", "function": {"name": "look", "description": "Render the region.",
                                          "parameters": LOOK.parameters}},
        {"type": "function", "function": {"name": "measure", "description": "Measure a face.",
                                          "parameters": MEASURE.parameters}}]
    # the result goes back as a `tool` turn answering the call, after the assistant turn that made it
    call = reply.tool_calls[0]
    conv = [*_conv(), mi.assistant_turn(reply), mi.tool_result_turn(call, '{"faces": 2}')]
    asyncio.run(mi.call_repair_review_model(conv, job_id="j-tools", tools=[LOOK, MEASURE]))
    sent = model.seen[1]["messages"]
    assert sent[2] == {"role": "assistant", "content": None,
                       "tool_calls": [{"id": call.id, "type": "function",
                                       "function": {"name": "look", "arguments": call.arguments}}]}
    assert sent[3] == {"role": "tool", "tool_call_id": call.id, "name": "look",
                       "content": '{"faces": 2}'}


def test_no_tools_is_todays_call_exactly(installed):
    model = installed(_LegacyModel())
    reply = asyncio.run(mi.call_repair_review_model(_conv(), job_id="j-text"))
    assert reply.assistant_text == '{"ok": true}'
    assert model.kwargs == [{"messages": _conv(), "job_id": "j-text", "name": "RepairReview"}]


def test_a_router_without_tools_raises_tools_unsupported(installed):
    installed(_LegacyModel())
    with pytest.raises(ToolsUnsupported):
        asyncio.run(mi.call_repair_review_model(_conv(), job_id="j", tools=[LOOK]))
    # the decision cache says the same for an inner router that cannot, and never asks it
    legacy = _LegacyModel()
    installed(_caching(legacy))
    with pytest.raises(ToolsUnsupported):
        asyncio.run(mi.call_repair_review_model(_conv(), job_id="j", tools=[LOOK], user_id="u"))
    assert legacy.kwargs == []
    assert issubclass(ToolsUnsupported, mi.ModelInferenceError)


def test_the_concrete_router_threads_tools_on_the_configured_route(monkeypatch):
    from meshpipeline.adapters.model_inference import router
    seen: dict = {}

    async def _fake_run_chat(route, label, messages, job_id, user_id, tools=None,
                             parallel_tool_calls=None):
        seen.update(route=route, label=label, tools=tools, user_id=user_id)
        return ModelRoundResult(assistant_text="{}")

    monkeypatch.setattr(router, "_run_chat", _fake_run_chat)
    defs = [LOOK.definition()]
    asyncio.run(router.call_repair_review_model(_conv(), job_id="j", tools=defs, user_id="u"))
    assert seen["tools"] == defs and seen["user_id"] == "u" and seen["label"] == "RepairReview"
    # no tools: today's call - no tools, no owner
    asyncio.run(router.call_repair_review_model(_conv(), job_id="j"))
    assert seen["tools"] is None and seen["user_id"] == ""
    assert "tools" in inspect.signature(router.call_repair_review_model).parameters


def test_the_concrete_router_refuses_tools_on_a_route_without_them(monkeypatch):
    import dataclasses

    import meshpipeline.agents.intake.settings as icfg
    from meshpipeline.adapters.model_inference import router
    bare = dataclasses.replace(icfg.INTAKE_ROUTE, required_capabilities=frozenset())
    monkeypatch.setattr(icfg, "INTAKE_ROUTE", bare)
    with pytest.raises(ToolsUnsupported):
        asyncio.run(router.call_repair_review_model(_conv(), job_id="j", tools=[LOOK.definition()]))


# ---------------------------------------------------------------- the decision cache --------
def _round(router, conv, tools=(LOOK, MEASURE), user_id="u"):
    return asyncio.run(router.call_repair_review_model(
        conv, job_id="j-cache", tools=[t.definition() for t in tools], user_id=user_id))


def _traced(result_text: str, *, first="call_aaaaaaaa", second="call_bbbbbbbb", swap=False):
    calls = [{"id": first, "type": "function", "function": {"name": "measure", "arguments": '{"face": 1}'}},
             {"id": second, "type": "function", "function": {"name": "measure", "arguments": '{"face": 2}'}}]
    answers = [(second if swap else first, result_text), (first if swap else second, '{"area": 9.0}')]
    return [*_conv(), {"role": "assistant", "content": None, "tool_calls": calls},
            *({"role": "tool", "tool_call_id": cid, "content": text} for cid, text in answers)]


def test_the_cache_key_covers_the_tool_schemas():
    inner = _ToolModel()
    r = _caching(inner)
    _round(r, _conv(), tools=(LOOK,))
    _round(r, _conv(), tools=(LOOK, MEASURE))
    altered = ToolSchema(name="look", description="Render the region at 4x.", parameters=LOOK.parameters)
    _round(r, _conv(), tools=(altered,))
    assert len(inner.seen) == 3


def test_two_conversations_differing_only_in_a_tool_result_do_not_share_an_entry():
    inner = _ToolModel()
    r = _caching(inner)
    _round(r, _traced('{"area": 1.0}'))
    _round(r, _traced('{"area": 2.0}'))
    # a fingerprint is a measurement here, not run noise: the text route's hex rule must not merge them
    _round(r, _traced('{"fingerprint": "0123456789abcdef0123"}'))
    _round(r, _traced('{"fingerprint": "fedcba9876543210fedc"}'))
    # the same two results answering the OTHER call is a different trace
    _round(r, _traced('{"area": 1.0}', swap=True))
    assert len(inner.seen) == 5
    # while the call ids themselves are run noise: another run's ids are the same question
    _round(r, _traced('{"area": 1.0}', first="call_zzzzzzzz", second="call_yyyyyyyy"))
    assert len(inner.seen) == 5


def test_replay_of_a_recorded_tool_conversation_makes_zero_model_calls():
    store = dc.MemoryDecisions()
    script = [
        ModelRoundResult(tool_calls=(ToolCallRequest(id="call_00000001", name="look",
                                                     arguments='{"region": 3}'),)),
        ModelRoundResult(tool_calls=(ToolCallRequest(id="call_00000002", name="measure",
                                                     arguments='{"face": 7}'),)),
        ModelRoundResult(assistant_text='{"conclude": "rebuild_face", "face": 7}'),
    ]
    results = ['{"view": "sliver"}', '{"area": 0.002}']

    async def investigate(router) -> list[ModelRoundResult]:
        conv, replies = _conv(), []
        for i in range(3):
            reply = await router.call_repair_review_model(
                conv, job_id="j-replay", tools=[LOOK.definition(), MEASURE.definition()], user_id="u")
            replies.append(reply)
            if not reply.tool_calls:
                break
            conv = [*conv, mi.assistant_turn(reply), mi.tool_result_turn(reply.tool_calls[0], results[i])]
        return replies

    live = dc.CachingModelRouter(_ToolModel(script), model_of=lambda s: "openai:gpt-x", store=store,
                                 mode="reuse")
    first = asyncio.run(investigate(live))
    replay = dc.CachingModelRouter(_NoCalls(), model_of=lambda s: "openai:gpt-x", store=store,
                                   mode="reuse")
    second = asyncio.run(investigate(replay))
    assert [(r.tool_calls, r.assistant_text) for r in second] == \
           [(r.tool_calls, r.assistant_text) for r in first]
    assert all(e["source"] == "record" for e in dc.decisions_for("j-replay")[-3:])


def test_an_ownerless_tool_round_is_asked_and_never_kept_like_text():
    inner = _ToolModel()
    r = _caching(inner)
    _round(r, _conv(), user_id="")
    _round(r, _conv(), user_id="")
    assert len(inner.seen) == 2


def test_the_text_route_key_is_unchanged_by_the_trace_rule():
    conv = _traced('{"area": 1.0}')
    text_key = dc.decision_key(step="builder", model="m", messages=conv, tools=None,
                               tool_choice="auto", owner="u")
    traced = dc.decision_key(step="builder", model="m", messages=conv, tools=None,
                             tool_choice="auto", owner="u", trace=True)
    assert text_key != traced
    # the default key is today's key: the same digest the v1 rule produced
    turns = [dc._canon(m) for m in conv if m.get("role") != "system"]
    ih = dc._digest({"turns": turns, "tool_choice": dc._canon("auto")})[:32]
    assert text_key[2] == ih


def test_composition_names_the_repair_review_model():
    from meshpipeline.runtime.composition import _route_model
    assert _route_model("repair_review")
