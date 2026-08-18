# Responsibility: Verify a real provider round and tool invocation emit their public events in order, once each.
from __future__ import annotations

import pytest
from tests.product_modes import set_modes

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.agents.loop.tracing import TraceContext
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    ProgressObservation,
)
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest

SECRET = "sk-live-SENTINELSENTINEL01"
COT = "As GLM 5.2 I will write /srv/workspaces/j/system/meshDict. Bearer " + SECRET


class Pub:

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def reasoning(self, rid, phase, *, duration_ms=None, token_count=None,
                  content=None, status="active"):
        import meshpipeline.events as E
        self.events.append(("reasoning", E.reasoning(
            "builder", rid, "builder", phase, duration_ms=duration_ms,
            token_count=token_count, content=content, status=status).wire()))

    def tool_call(self, cid, tool_name, arguments=None, status="started"):
        import meshpipeline.events as E
        self.events.append(("tool_call", E.tool_call(
            "builder", cid, "builder", tool_name, arguments, status).wire()))

    def tool_result(self, rid, call_id, tool_name, result=None,
                    status="success", duration_ms=None):
        import meshpipeline.events as E
        self.events.append(("tool_result", E.tool_result(
            "builder", rid, call_id, "builder", tool_name, result,
            status, duration_ms).wire()))

    def rationale(self, conclusion, because=""):
        import meshpipeline.events as E
        self.events.append(("rationale", E.rationale(
            "builder", "Builder", conclusion, because).wire()))

    def screenshot(self, image_b64, op_id=""):
        import meshpipeline.events as E
        self.events.append(("screenshot", E.screenshot("reviewer", image_b64).wire()))

    def of(self, kind):
        return [w for k, w in self.events if k == kind]

    def kinds(self):
        return [k for k, _ in self.events]


class Driver:

    role = AgentRole.builder

    def __init__(self, *, raise_on: str = "", supersede: bool = False):
        self.raise_on, self.supersede = raise_on, supersede
        self.executed: list[str] = []

    def limits(self): return LoopLimits(max_rounds=3)
    def correction(self, stage, tally, observation): return None
    def observe(self, tally): return ProgressObservation(made_progress=True)
    def before_round(self, tally, messages): return None
    def forced_tool(self, tally): return None
    def category_of(self, name): return "work"
    def extension(self): return {}
    def is_supersession(self, exc): return self.supersede
    def on_plaintext(self, tally, text): return RoundDecision.stop(LoopExit.turn_complete)
    async def close_out(self, tally): return None

    async def execute(self, inv: ToolInvocation) -> ToolOutcome:
        self.executed.append(inv.tool)
        if self.raise_on and inv.tool == self.raise_on:
            raise RuntimeError("executor exploded: " + SECRET)
        if inv.tool == "submit_mesh":
            return ToolOutcome(content="ok", accepted=True, terminal=True, payload="done")
        return ToolOutcome(content={"written": "system/meshDict", "bytes": 2418},
                           accepted=True)


def _provider(rounds):
    it = iter(rounds)

    async def _call(**kw):
        try:
            return next(it)
        except StopIteration:
            return ModelRoundResult(assistant_text="done")
    return _call


def _round(*calls, **kw):
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=f"p{i}", name=n, arguments=a)
                         for i, (n, a) in enumerate(calls)), **kw)


async def _run(driver, rounds, pub, **kw):
    return await run_agent_loop(
        driver=driver, provider_call=_provider(rounds), messages=[],
        tools=[], job_id="j7", user_id="u",
        append_tool_result=lambda m, cid, c: m.append({"role": "tool", "content": str(c)}),
        trace=TraceContext(publisher=pub, job_id="j7", role="builder", attempt=1),
        **kw)


@pytest.fixture
def raw(monkeypatch):
    set_modes(monkeypatch, disclosure="raw")


@pytest.fixture
def safe(monkeypatch):
    set_modes(monkeypatch, disclosure="safe")


# provider reasoning


async def test_a_real_provider_round_emits_started_then_completed(safe):
    pub = Pub()
    await _run(Driver(), [_round(("submit_mesh", "{}"),
                                 reasoning_text=COT, reasoning_tokens=1276)], pub)
    phases = [w["phase"] for w in pub.of("reasoning")]
    assert phases[:2] == ["started", "completed"]
    assert pub.of("reasoning")[1]["duration_ms"] is not None, "duration was not measured"


async def test_a_real_provider_failure_emits_started_then_failed(safe):
    pub = Pub()
    r = await _run(Driver(), [ModelRoundResult(failure_marker="[API_FAILURE] down")], pub)
    assert r.exit is LoopExit.provider_failed
    assert [w["phase"] for w in pub.of("reasoning")] == ["started", "failed"]
    assert pub.of("reasoning")[1]["status"] == "failure"


async def test_safe_mode_stores_no_reasoning_text_from_a_real_round(safe):
    pub = Pub()
    await _run(Driver(), [_round(("submit_mesh", "{}"), reasoning_text=COT)], pub)
    body = repr(pub.events)
    assert SECRET not in body and "meshDict" not in body.split("tool_result")[0]
    assert all(w["content"] is None for w in pub.of("reasoning"))


async def test_raw_mode_stores_the_returned_reasoning_from_a_real_round(raw):
    pub = Pub()
    await _run(Driver(), [_round(("submit_mesh", "{}"), reasoning_text=COT)], pub)
    done = pub.of("reasoning")[1]
    assert done["content"], "raw mode published no reasoning from a round that returned it"
    assert SECRET not in done["content"], "the secret survived"
    assert "GLM" not in done["content"], "model identity survived"
    assert "/srv/workspaces" not in done["content"]


async def test_reasoning_tokens_come_only_from_provider_reasoning_tokens(safe):
    pub = Pub()
    # a round that reports OUTPUT tokens but no reasoning tokens
    await _run(Driver(), [_round(("submit_mesh", "{}"), output_tokens=900)], pub)
    assert pub.of("reasoning")[1]["token_count"] is None, \
        "output tokens were relabelled as reasoning tokens"
    pub2 = Pub()
    await _run(Driver(), [_round(("submit_mesh", "{}"), reasoning_tokens=1276,
                                 output_tokens=900)], pub2)
    assert pub2.of("reasoning")[1]["token_count"] == 1276


# real tool lifecycle


async def test_a_real_tool_invocation_emits_one_call_and_one_result(safe):
    pub = Pub()
    d = Driver()
    await _run(d, [_round(("write_file", '{"path":"a"}'), ("submit_mesh", "{}"))], pub)
    assert d.executed == ["write_file", "submit_mesh"]
    assert len(pub.of("tool_call")) == 2 and len(pub.of("tool_result")) == 2
    call, res = pub.of("tool_call")[0], pub.of("tool_result")[0]
    assert res["tool_call_id"] == call["id"], "the result does not match its call"
    assert res["duration_ms"] is not None


async def test_multiple_calls_preserve_provider_order(safe):
    pub = Pub()
    await _run(Driver(), [_round(("read_file", "{}"), ("write_file", "{}"),
                                 ("submit_mesh", "{}"))], pub)
    labels = [w["public_label"] for w in pub.of("tool_call")]
    assert labels == ["Read a configuration file", "Generated file", "Submitted the mesh"]


async def test_a_malformed_call_is_reported_blocked_and_names_nothing(safe):
    pub = Pub()
    d = Driver()
    await _run(d, [_round(("write_file", "{not json"), ("submit_mesh", "{}"))], pub)
    blocked = [w for w in pub.of("tool_call") if w["status"] == "blocked"]
    assert blocked, "a malformed call was not reported as blocked"
    assert any(w["status"] == "blocked" for w in pub.of("tool_result"))
    assert blocked[0]["tool_name"] is None, "safe mode named the tool"
    assert blocked[0]["arguments"] is None


async def test_an_executor_failure_emits_a_failed_result_without_a_stack_trace(safe):
    pub = Pub()
    with pytest.raises(RuntimeError):
        await _run(Driver(raise_on="write_file"), [_round(("write_file", "{}"))], pub)
    fails = [w for w in pub.of("tool_result") if w["status"] == "failure"]
    assert fails, "an executor failure published no result"
    body = repr(pub.events)
    assert "Traceback" not in body and SECRET not in body


async def test_a_superseded_generation_publishes_no_late_success(safe):
    pub = Pub()
    with pytest.raises(RuntimeError):
        await _run(Driver(raise_on="write_file", supersede=True),
                   [_round(("write_file", "{}"))], pub)
    assert not [w for w in pub.of("tool_result") if w["status"] == "success"], \
        "a superseded generation told the reader its work landed"


async def test_safe_tool_events_from_a_real_run_carry_no_internals(safe):
    pub = Pub()
    await _run(Driver(), [_round(("write_file", '{"path":"/srv/workspaces/j/x"}'),
                                 ("submit_mesh", "{}"))], pub)
    body = repr(pub.of("tool_call") + pub.of("tool_result"))
    assert "write_file" not in body and "/srv/workspaces" not in body
    assert "Generated file" in body


async def test_raw_tool_events_from_a_real_run_carry_sanitized_internals(raw):
    pub = Pub()
    await _run(Driver(), [_round(("write_file", '{"path":"/srv/workspaces/j/x",'
                                                '"api_key":"' + SECRET + '"}'),
                                 ("submit_mesh", "{}"))], pub)
    call = pub.of("tool_call")[0]
    assert call["tool_name"] == "write_file"
    assert call["arguments"]["path"] == "<workspace>"
    assert SECRET not in repr(pub.events)
    assert pub.of("tool_result")[0]["result"]["written"] == "system/meshDict"
