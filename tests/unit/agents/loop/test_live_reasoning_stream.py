# Responsibility: Verify a round's reasoning reaches the user WHILE it is thinking, on both routes.
# Boundaries: the sink seam - what the model reasons is the provider's business, not this test's.
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from tests.execution_publisher_double import RecordingExecutionPublisher

from meshpipeline.agents.loop import tracing as T
from meshpipeline.agents.loop.accounting import AgentRunAccountant
from meshpipeline.agents.loop.provider_round import ExecutionProviderRound, ProviderRound
from meshpipeline.contracts.agent_loop import AgentRole, LoopLimits

_DELTAS = ("Checking ", "Checking the cell ", "Checking the cell budget.")


def _result():
    from meshpipeline.contracts.model_inference import ModelRoundResult
    return ModelRoundResult(tool_calls=(), assistant_text="done", finish_reason="stop")


def _streaming_provider(seen: dict):
    # Stands in for a provider that streams: it feeds the sink the reasoning so far, exactly as
    # consume_chat_stream does, and awaits whatever comes back.
    async def _call(*, messages, tools, job_id, user_id, tool_choice="auto", on_reasoning=None):
        seen["offered"] = on_reasoning is not None
        for delta in _DELTAS:
            emitted = on_reasoning(delta) if on_reasoning else None
            if inspect.isawaitable(emitted):
                await emitted
        return _result()
    return _call


def _silent_provider(seen: dict):
    # A provider that never heard of the sink. Nothing may be offered to it.
    async def _call(*, messages, tools, job_id, user_id, tool_choice="auto"):
        seen["called"] = True
        return _result()
    return _call


def _invoke(rounds):
    return asyncio.run(rounds.invoke([{"role": "user", "content": "go"}],
                                     acct=AgentRunAccountant(
                                         role=AgentRole.builder, job_id="j",
                                         limits=LoopLimits(max_rounds=4),
                                         pipeline_attempt=1, agent_attempt=1),
                                     forced_tool=None,
                                     remaining=None, round_no=3))


class _SyncRecorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def reasoning(self, rid, phase, **k):
        self.calls.append((rid, phase, k.get("content")))


# the builder route - the one the UI shows during a mesh

def test_the_builder_publishes_its_reasoning_as_it_arrives():
    # The defect this covers: ExecutionProviderRound overrode where rounds are traced but not
    # where their reasoning goes, so the sink was built from the unset synchronous context and
    # came back None. Every round still opened and closed correctly, which is why nothing caught
    # it - the card just stayed empty for the whole round.
    inner = RecordingExecutionPublisher("job-1")
    ctx = T.ExecutionTraceContext(publisher=inner, job_id="j", role="builder", attempt=1)
    seen: dict = {}
    _invoke(ExecutionProviderRound(provider_call=_streaming_provider(seen), tools=[], job_id="j",
                                   user_id="u", execution_trace=ctx))

    assert seen["offered"], "the builder route never offered the sink to its provider"
    partials = [c for c in inner.calls
                if c["method"] == "areasoning" and c["content"]]
    assert [c["content"] for c in partials] == list(_DELTAS), inner.calls
    # One id for the whole round, so the card the browser already shows fills in rather than a
    # new card appearing per token.
    rids = {c["rid"] for c in inner.calls if c["method"] == "areasoning"}
    assert len(rids) == 1, rids


def test_a_provider_that_cannot_stream_is_offered_nothing():
    # Streaming reasoning is optional. Offering it to a route that never declared it would cost
    # the entire round with a TypeError, for a decoration.
    ctx = T.ExecutionTraceContext(publisher=RecordingExecutionPublisher("job-1"), job_id="j",
                                  role="builder", attempt=1)
    seen: dict = {}
    out = _invoke(ExecutionProviderRound(provider_call=_silent_provider(seen), tools=[],
                                         job_id="j", user_id="u", execution_trace=ctx))
    assert seen.get("called") and getattr(out, "result", None) is not None


# the reviewer and intake route

def test_the_synchronous_route_publishes_the_same_partials():
    rec = _SyncRecorder()
    seen: dict = {}
    _invoke(ProviderRound(provider_call=_streaming_provider(seen), tools=[], job_id="j",
                          user_id="u",
                          trace=T.TraceContext(publisher=rec, job_id="j", role="reviewer",
                                               attempt=1)))
    assert [c[2] for c in rec.calls if c[2]] == list(_DELTAS), rec.calls


# an untraced run keeps exactly its old path

@pytest.mark.parametrize("rounds", [
    lambda p: ProviderRound(provider_call=p, tools=[], job_id="j", user_id="u"),
    lambda p: ExecutionProviderRound(provider_call=p, tools=[], job_id="j", user_id="u")])
def test_no_trace_offers_no_sink(rounds):
    seen: dict = {}
    out = _invoke(rounds(_streaming_provider(seen)))
    assert seen["offered"] is False
    assert getattr(out, "result", None) is not None


# a failing trace must never cost the round

def test_a_sink_that_raises_does_not_break_the_round():
    class _Boom:
        def reasoning(self, *a, **k):
            raise RuntimeError("event stream down")

    seen: dict = {}
    out = _invoke(ProviderRound(provider_call=_streaming_provider(seen), tools=[], job_id="j",
                                user_id="u",
                                trace=T.TraceContext(publisher=_Boom(), job_id="j",
                                                     role="reviewer", attempt=1)))
    assert getattr(out, "result", None) is not None, "a trace failure cost the round"


# the consumer that actually drives the sink

def _chunk(reasoning=None, content=None, finish=None):
    delta = SimpleNamespace(role="assistant", content=content, reasoning_content=reasoning,
                            tool_calls=None, model_extra={})
    return SimpleNamespace(id="c", model="m", created=1, usage=None,
                           choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


async def _stream_of(chunks):
    for c in chunks:
        yield c


def _consume(sink):
    from meshpipeline.adapters.model_inference.streaming import consume_chat_stream
    chunks = [_chunk(reasoning=d) for d in ("a", "b")] + [_chunk(content="x", finish="stop")]
    return asyncio.run(consume_chat_stream(_stream_of(chunks), on_reasoning=sink))


@pytest.mark.parametrize("colour", ["sync", "async"])
def test_the_consumer_drives_a_sink_of_either_colour(colour):
    # The builder's sink is a coroutine function, so a consumer that calls and discards would
    # leave it as an un-awaited coroutine: no publication, no error, and a warning at most.
    got: list[str] = []
    if colour == "sync":
        sink = got.append
    else:
        async def sink(text):
            got.append(text)
    out = _consume(sink)
    assert got == ["a", "ab"], got
    assert out.choices[0].message.content == "x"


def test_a_sink_that_raises_is_dropped_and_the_stream_completes():
    calls = {"n": 0}

    def sink(_text):
        calls["n"] += 1
        raise RuntimeError("event stream down")

    out = _consume(sink)
    assert calls["n"] == 1, "a failing sink was called again instead of being dropped"
    assert out.choices[0].message.content == "x"


def _stub_geometry(monkeypatch):
    # The geometry read is another module's concern; these tests are about what the round is given.
    import meshpipeline.cad.analysis as analysis
    import meshpipeline.cad.prepared_surface as prepared
    monkeypatch.setattr(prepared, "require_metre_surface", lambda *a, **k: object())
    monkeypatch.setattr(analysis, "analyze_surface",
                        lambda _s: {"diag": 1.0, "extent": (1.0, 1.0, 1.0),
                                    "surface_area": 6.0, "min_feature": 0.01})
    monkeypatch.setattr(analysis, "recommend_refinement",
                        lambda _a, max_cells=0: {"surface_level": 2, "feature_level": 3,
                                                 "afford_level": 2})


# the planner, which traces its own rounds rather than going through ProviderRound

def test_the_planner_sink_publishes_against_the_id_its_round_opened():
    from meshpipeline.engines.snappy.planner import _plan_reasoning_sink

    inner = RecordingExecutionPublisher("job-1")
    sink = _plan_reasoning_sink(inner, "job-1", 2, "r:job-1:builder:2:0")
    assert sink is not None, "the planner round was given no sink"
    asyncio.run(sink("weighing the cell budget"))
    assert [(c["rid"], c["content"]) for c in inner.calls if c["method"] == "areasoning"] == \
        [("r:job-1:builder:2:0", "weighing the cell budget")], inner.calls


@pytest.mark.parametrize("publish,rid", [(None, "r:j:builder:1:0"),
                                         (RecordingExecutionPublisher("job-1"), "")])
def test_the_planner_sink_is_absent_when_there_is_nothing_to_publish_against(publish, rid):
    # An untraced plan and a round whose trace failed to open both keep their old path exactly.
    from meshpipeline.engines.snappy.planner import _plan_reasoning_sink

    assert _plan_reasoning_sink(publish, "job-1", 1, rid) is None


def test_the_planner_round_is_actually_given_its_sink(tmp_path, monkeypatch):
    # The helper existing proves nothing: the defect this guards is a planner that builds a sink
    # and then calls the router without it, which looks correct and publishes nothing.
    from meshpipeline.contracts import execution_guard as fence
    from meshpipeline.contracts import model_inference as llm_router
    from meshpipeline.engines.snappy import planner as P

    (tmp_path / "input.stl").write_bytes(b"solid s\nendsolid s\n")
    seen: dict = {}

    async def _still_owner(_why):
        return None

    async def _call_planner(*_a, on_reasoning=None, **_k):
        seen["sink"] = on_reasoning
        return _result()

    _stub_geometry(monkeypatch)
    monkeypatch.setattr(fence, "assert_still_owner", _still_owner)
    monkeypatch.setattr(llm_router, "call_planner_model", _call_planner)

    inner = RecordingExecutionPublisher("job-1")
    asyncio.run(P.plan_with_accounting(workspace=tmp_path, job_id="job-1",
                                       request_txt="mesh it", publish=inner, attempt=2))
    assert seen.get("sink") is not None, "the planner called the router without its sink"
    asyncio.run(seen["sink"]("partial"))
    assert any(c["method"] == "areasoning" and c["content"] == "partial" for c in inner.calls)


def test_a_router_that_cannot_stream_still_gets_a_plan(tmp_path, monkeypatch):
    # The defect this guards: the planner passed the sink unconditionally, so a router stand-in
    # that never declared it raised TypeError INSIDE plan_with_accounting's broad try. That does
    # not surface as an error - it yields no plan at all, and the only visible symptom is a run
    # that made zero planner rounds.
    from meshpipeline.contracts import execution_guard as fence
    from meshpipeline.contracts import model_inference as llm_router
    from meshpipeline.engines.snappy import planner as P

    (tmp_path / "input.stl").write_bytes(b"solid s\nendsolid s\n")
    calls = {"n": 0}

    async def _still_owner(_why):
        return None

    async def _no_stream_router(messages, tools=None, tool_choice="auto", job_id="",
                                user_id="", parallel_tool_calls=None):
        calls["n"] += 1
        return _result()

    _stub_geometry(monkeypatch)
    monkeypatch.setattr(fence, "assert_still_owner", _still_owner)
    monkeypatch.setattr(llm_router, "call_planner_model", _no_stream_router)

    asyncio.run(P.plan_with_accounting(workspace=tmp_path, job_id="job-1",
                                       request_txt="mesh it",
                                       publish=RecordingExecutionPublisher("job-1"), attempt=2))
    assert calls["n"] == 1, "the planner round never happened at all"
