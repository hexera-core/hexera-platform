# Responsibility: Verify a review RERUN is a new question to the decision cache, and that nothing
# else about the cache moves.
# Boundaries: the key and the router seam; what a rerun is for belongs to the pipeline graph.
#
# THE SENTENCE THIS FILE EXISTS FOR. A review rerun is the one place this product asks an
# identical question in order to get a DIFFERENT answer. Measured on job 74bc3e2e before this
# change: the first review asked the model 20 times, the two reruns asked it ZERO times and were
# served 40 rounds from the record; all three conversation.jsonl files shared one md5
# (ff71a9ce0669654bf34a374c42d1810e); both reruns re-saved the same null verdict, in 9 and 5
# seconds. The rerun existed and did nothing.
from __future__ import annotations

import asyncio
import inspect

from meshpipeline.contracts import decision_cache as dc
from meshpipeline.contracts import model_inference as port
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest


class _Reviewer:
    """A reviewer model that answers differently every time it is actually asked, and remembers
    exactly what it was handed."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[dict] = []

    async def call_reviewer_with_tools(self, messages, tools, job_id="", user_id="",
                                       parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        self.seen.append({"messages": messages, "tools": tools, "job_id": job_id,
                          "user_id": user_id})
        return ModelRoundResult(tool_calls=(ToolCallRequest(
            id=f"call_{self.calls:08d}", name="inspect_region",
            arguments=f'{{"region_name": "model_midplane", "n": {self.calls}}}'),))


def _conv():
    return [{"role": "system", "content": "you are a mesh-quality reviewer"},
            {"role": "user", "content": "the mesh is 87,194,075 cells in 2 pieces"}]


def _router(mode="reuse"):
    inner = _Reviewer()
    return inner, dc.CachingModelRouter(inner, model_of=lambda step: "openai:gpt-terra",
                                        store=dc.MemoryDecisions(), mode=mode)


# ---- the defect ---------------------------------------------------------------------------

def test_a_rerun_is_asked_of_the_model_again():
    """The first review and its rerun are the same question and must get two answers."""
    inner, r = _router()
    first = asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="jask", user_id="u"))
    again = asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="jask", user_id="u",
                                                   scope="rerun1"))
    assert inner.calls == 2, "a rerun that replays the review it is rerunning is not a rerun"
    assert first.tool_calls != again.tool_calls


def test_the_three_askings_of_job_74bc3e2e_are_three_questions():
    """The measured shape: a review and its two reruns. All three must reach the model."""
    inner, r = _router()
    # its own job id: dc.decisions_for reads a module-level log, so a shared id would also show
    # the rows the other tests in this file wrote
    for scope in ("", "rerun1", "rerun2"):
        asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="j74b", user_id="u",
                                               scope=scope))
    assert inner.calls == 3
    sources = [d["source"] for d in dc.decisions_for("j74b")]
    assert sources == ["model", "model", "model"], sources


def test_one_rerun_is_still_repeatable_within_itself():
    """The cache is not being switched off: the SAME asking still replays, which is what makes a
    rerun's own rounds reproducible."""
    inner, r = _router()
    a = asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="j1", user_id="u",
                                               scope="rerun1"))
    b = asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="j2", user_id="u",
                                               scope="rerun1"))
    assert inner.calls == 1
    assert a.tool_calls == b.tool_calls
    assert dc.decisions_for("j2")[-1]["source"] == "record"


# ---- the compatibility the change must not break -------------------------------------------

def test_a_first_review_keeps_the_key_it_always_had():
    """Every record written before this change was written under tool_choice "auto". An empty
    scope has to hash to exactly that, or the change silently invalidates the whole store."""
    conv, tools = _conv(), []
    was = dc.decision_key(step="reviewer", model="openai:gpt-terra", messages=conv, tools=tools,
                          tool_choice="auto", owner="u")[0]
    now = dc.decision_key(step="reviewer", model="openai:gpt-terra", messages=conv, tools=tools,
                          tool_choice="auto" if not "" else "auto|", owner="u")[0]
    assert now == was


def test_a_rerun_key_differs_from_the_first_and_from_the_other_rerun():
    conv, tools = _conv(), []
    keys = {s: dc.decision_key(step="reviewer", model="openai:gpt-terra", messages=conv,
                               tools=tools,
                               tool_choice="auto" if not s else f"auto|{s}", owner="u")[0]
            for s in ("", "rerun1", "rerun2")}
    assert len(set(keys.values())) == 3, keys


def test_the_scope_never_reaches_the_model():
    """It is a cache term. The question put to the provider must be identical, or a rerun is not
    a second opinion on the same mesh but a different review."""
    inner, r = _router()
    asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="jscope", user_id="u",
                                           scope="rerun2"))
    assert inner.calls == 1
    assert "scope" not in inner.seen[0]
    assert inner.seen[0]["messages"] == _conv()


def test_the_step_is_still_plain_reviewer():
    """The scope must not be folded into `step`: _route_model answers "" for any step outside the
    four it knows, which would blank the model out of the key and the run record both."""
    inner, r = _router()
    asyncio.run(r.call_reviewer_with_tools(_conv(), [], job_id="jstep", user_id="u",
                                           scope="rerun1"))
    assert dc.decisions_for("jstep")[-1]["step"] == "reviewer"


# ---- the seam -------------------------------------------------------------------------------

def test_every_reviewer_seam_declares_the_scope():
    from meshpipeline.adapters.model_inference import router as concrete
    for fn in (port.call_reviewer_with_tools, concrete.call_reviewer_with_tools,
               dc.CachingModelRouter.call_reviewer_with_tools):
        params = inspect.signature(fn).parameters
        assert "scope" in params, f"{fn.__qualname__} drops the scope"
        assert params["scope"].default == "", f"{fn.__qualname__} must default to a first asking"


def test_the_bound_provider_still_offers_the_reasoning_sink():
    """provider_round asks the callable's own signature whether it takes the sink
    (loop/tracing.accepts_reasoning). A wrapper that hides it costs streamed reasoning silently."""
    from meshpipeline.agents.loop.tracing import accepts_reasoning
    from meshpipeline.agents.reviewer.visual import _reviewer_provider
    assert accepts_reasoning(_reviewer_provider(0))
    assert accepts_reasoning(_reviewer_provider(2))


def test_the_bound_provider_takes_what_the_loop_hands_it():
    """The exact kwargs provider_round.request builds for a reviewer round."""
    from meshpipeline.agents.reviewer.visual import _reviewer_provider
    params = inspect.signature(_reviewer_provider(1)).parameters
    for name in ("messages", "tools", "job_id", "user_id", "on_reasoning"):
        assert name in params, f"the bound provider cannot take {name}"


def test_a_first_review_is_the_port_itself():
    """The strongest form of the compatibility this change needs: for rerun 0 the seam is not a
    wrapper around the port, it IS the port - the same object the loop was given before any of
    this existed, so nothing written in the factory can reach the common case."""
    from meshpipeline.agents.reviewer import visual
    assert visual._reviewer_provider(0) is visual.llm_router.call_reviewer_with_tools


def test_the_bound_provider_answers_the_sink_question_as_the_layer_below_does():
    """THE REGRESSION, IN ONE LINE. provider_round.request asks the callable it was handed
    whether it takes the reasoning sink, and offers it only if so - accepts_reasoning's own
    comment: "asked at each seam that forwards, because the callable one layer down is not the
    one this layer declares". A seam that declares MORE than its target makes the loop offer
    what the target cannot take. An explicit `on_reasoning` parameter on this wrapper cost 31
    reviewer tests, every one a TypeError raised inside a stand-in that predates the sink."""
    from meshpipeline.agents.loop.tracing import accepts_reasoning
    from meshpipeline.agents.reviewer import visual

    async def _cache_without_a_sink(messages, tools, job_id="", user_id="", scope=""):
        return ModelRoundResult()

    original = visual.llm_router.call_reviewer_with_tools
    visual.llm_router.call_reviewer_with_tools = _cache_without_a_sink
    try:
        bound = visual._reviewer_provider(1)
        assert accepts_reasoning(bound) is False, (
            "the seam offered a sink its target cannot take")
    finally:
        visual.llm_router.call_reviewer_with_tools = original


def test_a_provider_that_cannot_be_told_apart_is_called_unwrapped():
    """A target with no `scope` of its own has no cache in front of it, so it answers freshly by
    construction and there is nothing to tell it. Handing it a scope it never declared would be
    the same TypeError by another name - and this is the shape of every reviewer test's double."""
    from meshpipeline.agents.reviewer import visual

    class _StandInThatPredatesTheScope:
        async def __call__(self, messages, tools, job_id="", user_id="", on_reasoning=None):
            return ModelRoundResult()

    stand_in = _StandInThatPredatesTheScope()
    original = visual.llm_router.call_reviewer_with_tools
    visual.llm_router.call_reviewer_with_tools = stand_in
    try:
        assert visual._reviewer_provider(2) is stand_in
    finally:
        visual.llm_router.call_reviewer_with_tools = original


def test_the_sink_is_forwarded_when_the_layer_below_takes_it():
    """And when the target does declare it, the sink must still arrive - the wrapper may not
    quietly swallow streamed reasoning on a rerun."""
    from meshpipeline.agents.reviewer import visual

    got: list = []

    async def _spy(**kw):
        got.append(sorted(kw))
        return ModelRoundResult()

    def _sink(_):
        return None

    original = visual.llm_router.call_reviewer_with_tools
    visual.llm_router.call_reviewer_with_tools = _spy
    try:
        asyncio.run(visual._reviewer_provider(1)(_conv(), [], job_id="j", user_id="u",
                                                 on_reasoning=_sink))
    finally:
        visual.llm_router.call_reviewer_with_tools = original
    assert got[0] == ["job_id", "messages", "on_reasoning", "scope", "tools", "user_id"]


def test_the_first_review_binds_no_scope_and_a_rerun_binds_its_number():
    """A closure's bound scope, read back through the call it makes."""
    from meshpipeline.agents.reviewer import visual

    seen: list[str] = []

    async def _spy(**kw):
        seen.append(kw.get("scope"))
        return ModelRoundResult()

    original = visual.llm_router.call_reviewer_with_tools
    visual.llm_router.call_reviewer_with_tools = _spy          # noqa: SLF001 - module port
    try:
        # by keyword, exactly as provider_round.request builds the call - for rerun 0 the seam
        # IS the spy, and the spy takes keywords only
        for r in (0, 1, 2):
            asyncio.run(visual._reviewer_provider(r)(messages=_conv(), tools=[], job_id="j",
                                                     user_id="u"))
    finally:
        visual.llm_router.call_reviewer_with_tools = original
    # None, not "": a first review passes no scope keyword AT ALL, so a stand-in written
    # before the scope existed keeps working. That is the compatibility, and it is the assertion.
    assert seen == [None, "rerun1", "rerun2"]
