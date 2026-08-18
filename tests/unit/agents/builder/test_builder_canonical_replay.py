# Responsibility: Verify a builder attempt progresses, warns and terminates on repetition, one record per exit.
# Boundaries: a lost fence stops every later call, and supersession is never converted into a failure.
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import meshpipeline.agents.builder.executor as bx
import meshpipeline.agents.builder.loop as bl
import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.application.execution_fence import StaleWorkerFenced
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


def _tc(name, **args):
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=json.dumps(args))


def _round(*calls, text="", finish=None):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason=finish or ("tool_calls" if calls else "stop"),
                            provider=ProviderAttemptInfo(1, "p", "m"))


class _Harness:
    def __init__(self):
        self.events: list[str] = []
        self.results: dict = {}
        self.fence_fails_at: set[str] = set()
        self.submits = 0
        self.records: list = []
        self.tool_choices: list = []

    async def fence(self, where, **_kw):
        self.events.append(f"fence:{where}")
        for pat in self.fence_fails_at:
            if pat in where:
                raise StaleWorkerFenced(where, types.SimpleNamespace(
                    job_id="j", execution_generation=1, token_hash=lambda: "t"))

    def dispatch(self, ctx, fn, args):
        self.events.append(f"dispatch:{fn}")
        if fn == "submit_mesh":
            self.submits += 1
        return json.dumps(self.results.get(fn, {"success": True}))

    def auto_submit(self, ws, engine):
        self.events.append("dispatch:auto_submit_mesh")
        self.submits += 1
        return self.results.get("__auto__", {"success": True})

    @property
    def dispatched(self):
        return [e.split(":", 1)[1] for e in self.events if e.startswith("dispatch:")]

    @property
    def fences(self):
        return [e.split(":", 1)[1] for e in self.events if e.startswith("fence:")]


@pytest.fixture
def h(monkeypatch):
    t = _Harness()
    monkeypatch.setattr(bx._fence, "assert_current_owner", t.fence)
    monkeypatch.setattr(bx, "_dispatch_tool", t.dispatch)
    # `run_mesh` is driven in two halves so its announcement can be authorized on the event
    # loop. A harness that stubs the dispatch seam must stub both halves through it.
    import meshpipeline.agents.builder.tools.meshing as _meshing_mod
    monkeypatch.setattr(bx, "prepare_mesh_run",
                        lambda ctx: _meshing_mod.PreparedMeshRun(engine=ctx.engine, cap=60))
    monkeypatch.setattr(bx, "dispatch_prepared_mesh",
                        lambda ctx, prepared: bx._dispatch_tool(ctx, "run_mesh", {}))
    monkeypatch.setattr(bx, "_compress_tool_output", lambda fn, a, r, **k: r)
    monkeypatch.setattr(bx, "get_spec_run_files", lambda _e: ("system/meshDict",))
    monkeypatch.setattr(bl, "_active_tools", lambda _e: [])
    monkeypatch.setattr("meshpipeline.agents.builder.context_prep._count_message_tokens",
                        lambda _m: 0)
    monkeypatch.setattr("meshpipeline.agents.builder.tools.meshing.submit_mesh", t.auto_submit)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: t.records.append(rec) or {})
    return t


def _drive(h, script, tmp_path, monkeypatch, *, max_rounds=8, mode="initial"):
    it = iter(script)

    async def _provider(*, messages, tools, job_id, user_id, tool_choice="auto"):
        h.tool_choices.append(tool_choice)
        try:
            return next(it)
        except StopIteration:
            return _round(text="thinking")

    monkeypatch.setattr("meshpipeline.contracts.model_inference.call_builder_model", _provider)
    return asyncio.run(bl._run_tool_loop(
        [{"role": "system", "content": "s"}], Path(tmp_path), job_id="j", user_id="u",
        max_rounds=max_rounds, mode=mode, engine="cfmesh", loop_timeout=60))


def _forced(choice):
    return choice["function"]["name"] if isinstance(choice, dict) else None


# repeated configuration
def test_equivalent_repeated_configuration_redirects_warns_then_terminates(
        h, tmp_path, monkeypatch):
    h.results = {"configure_mesh": {"success": False}}
    same = [_round(_tc("configure_mesh", max_cells=1, quality="strict")),
            _round(_tc("configure_mesh", quality="strict", max_cells=1)),   # reordered
            _round(_tc("configure_mesh", max_cells=1, quality="strict")),
            _round(_tc("configure_mesh", quality="strict", max_cells=1)),
            _round(_tc("configure_mesh", max_cells=1, quality="strict"))]
    out, msgs = _drive(h, same, tmp_path, monkeypatch, max_rounds=12)

    assert h.dispatched.count("run_mesh") == 0, "no native execution"
    assert h.submits == 0, "no submission"
    rec = h.records[-1]
    assert rec.exit.value == "no_progress"
    assert rec.tally.consecutive_no_progress == 4, "terminates on the FOURTH repetition"
    assert rec.tally.rounds == 4, "it stops there - the budget is not burned"

    text = json.dumps([m.get("content") for m in msgs if isinstance(m.get("content"), str)])
    assert "Call run_mesh NOW" in text, "the configure redirect fired"
    # PRESERVED: for configure_mesh the redirect SHORT-CIRCUITS the generic strategy warning,
    # exactly as StuckLoopDetector did - its configure branch returned before the LIMIT-1 one.
    assert "CHANGE your strategy NOW" not in text


def test_a_repeated_non_configure_action_gets_the_strategy_warning_at_three(
        h, tmp_path, monkeypatch):
    h.results = {"run_mesh": {"mesh_ok": False}}
    _, msgs = _drive(h, [_round(_tc("run_mesh", cells=1))] * 5, tmp_path, monkeypatch,
                     max_rounds=12)
    text = json.dumps([m.get("content") for m in msgs if isinstance(m.get("content"), str)])
    assert "CHANGE your strategy NOW" in text
    assert "One more identical call aborts the attempt." in text
    assert h.records[-1].tally.consecutive_no_progress == 4


# explicit progression
def test_valid_progression_configures_runs_and_submits(h, tmp_path, monkeypatch):
    h.results = {"configure_mesh": {"success": True}, "run_mesh": {"mesh_ok": True},
                 "submit_mesh": {"success": True}}
    out, _msgs = _drive(h, [_round(_tc("configure_mesh", max_cells=1)),
                            _round(_tc("run_mesh")),
                            _round(_tc("submit_mesh"))], tmp_path, monkeypatch)
    assert out == "submit_mesh:success"
    assert h.dispatched == ["configure_mesh", "run_mesh", "submit_mesh"]
    # forced progression: configure -> run_mesh, then run -> submit_mesh
    assert [_forced(c) for c in h.tool_choices] == [None, "run_mesh", "submit_mesh"]
    assert len(h.records) == 1 and h.records[-1].exit.value == "terminal_action"
    ext = h.records[-1].extension
    assert ext.submitted is True and ext.auto_submitted is False
    assert ext.forced_tools == ("run_mesh", "submit_mesh")


# application close-out
def test_the_application_submits_once_when_the_model_will_not(h, tmp_path, monkeypatch):
    assert bcfg.BUILDER_AUTO_SUBMIT_AFTER == 2
    h.results = {"run_mesh": {"mesh_ok": True}}
    out, _msgs = _drive(h, [_round(_tc("run_mesh")), _round(_tc("run_mesh")),
                            _round(_tc("run_mesh"))], tmp_path, monkeypatch)
    assert out == "submit_mesh:success"
    assert h.submits == 1, "exactly once"
    # routed through the SAME fenced executor operation as an explicit submission
    assert h.dispatched.count("submit_mesh") == 1
    assert "auto_submit_mesh" not in h.dispatched, "no separate unfenced auto-submit helper"
    ext = h.records[-1].extension
    assert ext.auto_submitted is True and ext.submitted is True


def test_a_failed_native_result_is_never_auto_submitted(h, tmp_path, monkeypatch):
    h.results = {"run_mesh": {"mesh_ok": False, "success": False}}
    _drive(h, [_round(_tc("run_mesh"))] * 5, tmp_path, monkeypatch, max_rounds=6)
    assert h.submits == 0, "an invalid mesh must never be submitted"
    assert h.records[-1].extension.auto_submitted is False


def test_auto_submission_does_not_run_when_the_deliverable_is_absent(h, tmp_path, monkeypatch):
    h.results = {"run_mesh": {"mesh_ok": True}, "submit_mesh": {"success": False}}
    _drive(h, [_round(_tc("run_mesh"))] * 4, tmp_path, monkeypatch, max_rounds=5)
    assert h.records[-1].extension.auto_submitted is False


# fencing / supersession
def test_fence_loss_before_a_mutating_call_stops_everything(h, tmp_path, monkeypatch):
    h.fence_fails_at = {"builder tool configure_mesh"}
    with pytest.raises(StaleWorkerFenced):
        _drive(h, [_round(_tc("configure_mesh"), _tc("run_mesh"), _tc("submit_mesh"))],
               tmp_path, monkeypatch)
    assert h.dispatched == [], "no tool ran"
    assert h.submits == 0


def test_fence_loss_after_run_mesh_rejects_the_result_and_later_calls(h, tmp_path, monkeypatch):
    h.results = {"run_mesh": {"mesh_ok": True}}
    h.fence_fails_at = {"accept native run_mesh output"}
    with pytest.raises(StaleWorkerFenced):
        _drive(h, [_round(_tc("run_mesh"), _tc("submit_mesh"))], tmp_path, monkeypatch)
    assert h.dispatched == ["run_mesh"], "submit_mesh must not run"
    assert h.submits == 0, "a superseded worker submits nothing"


def test_a_superseded_builder_returns_no_state_and_does_not_terminalize(monkeypatch, tmp_path):
    import meshpipeline.agents.builder.agent as agent

    async def _boom(*_a, **_k):
        raise StaleWorkerFenced("builder tool run_mesh", types.SimpleNamespace(
            job_id="j", execution_generation=1, token_hash=lambda: "t"))

    # Since the node holds none of these: `invoke` runs the loop and resolves the engine
    # spec, `attempt_capture` writes the record. Patching the node would prove nothing.
    from tests.execution_publisher_double import install

    import meshpipeline.agents.builder.attempt_capture as attempt_capture
    import meshpipeline.agents.builder.invoke as invoke
    import meshpipeline.agents.builder.loop as builder_loop
    install(monkeypatch, agent)
    monkeypatch.setattr(builder_loop, "_run_tool_loop", _boom)
    monkeypatch.setattr(attempt_capture, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None))
    monkeypatch.setattr(invoke, "get_spec",
                        lambda *_a, **_k: types.SimpleNamespace(build_driver=None), raising=False)
    with pytest.raises(StaleWorkerFenced):
        asyncio.run(agent.node_builder({
            "job_id": "j", "engine": "cfmesh", "retry_count": 0, "builder_mode": "initial",
            "geometry": {}, "request_txt": "r", "review_brief_txt": "",
            "intake_patches": [], "openfoam_workspace": str(tmp_path)}))


def test_supersession_is_not_converted_into_a_builder_or_pipeline_failure():
    import inspect

    import meshpipeline.agents.builder.agent as agent
    import meshpipeline.agents.builder.invoke as invoke

    node = inspect.getsource(agent.node_builder)
    assert "StaleWorkerFenced" not in node, (
        "the graph node handles supersession again - it must propagate untouched")

    src = inspect.getsource(invoke)
    assert "except fence.StaleWorkerFenced:" in src
    for body in src.split("except fence.StaleWorkerFenced:")[1:]:
        body = body.split("except ")[0]
        assert "raise" in body, "it must propagate to the generation-aware boundary"
        for banned in ("api_failure", "internal_pipeline_failure", "TurnPatch"):
            assert banned not in body, f"supersession must not become {banned}"


# multiple calls
def test_one_response_is_one_round_and_every_call_is_counted_in_order(
        h, tmp_path, monkeypatch):
    h.results = {"run_mesh": {"mesh_ok": False}}
    _drive(h, [_round(_tc("read_file"), _tc("list_directory"), _tc("run_mesh"))],
           tmp_path, monkeypatch, max_rounds=2)
    assert h.dispatched == ["read_file", "list_directory", "run_mesh"], "provider order"
    tally = h.records[-1].tally
    assert tally.tool_calls == 3 and tally.rounds == 2


def test_a_terminal_submission_stops_later_calls_in_the_same_response(
        h, tmp_path, monkeypatch):
    h.results = {"submit_mesh": {"success": True}}
    out, _ = _drive(h, [_round(_tc("submit_mesh"), _tc("run_mesh"))], tmp_path, monkeypatch)
    assert out == "submit_mesh:success" and h.dispatched == ["submit_mesh"]


def test_a_malformed_call_executes_nothing_and_the_next_valid_call_still_runs(
        h, tmp_path, monkeypatch):
    bad = ToolCallRequest(id="c1", name="run_mesh", arguments="{not json")
    h.results = {"read_file": {"ok": True}}
    _drive(h, [_round(bad, _tc("read_file"))], tmp_path, monkeypatch, max_rounds=2)
    assert h.dispatched == ["read_file"], "the malformed call ran nothing"
    rec = h.records[-1]
    assert rec.tally.malformed_calls == 1 and rec.tally.tool_calls == 2
    assert not any(c.category == "execution" and c.count for c in ()), ""


def test_a_malformed_call_produces_no_progress_and_no_forced_tool(h, tmp_path, monkeypatch):
    bad = ToolCallRequest(id="c1", name="configure_mesh", arguments="{not json")
    _drive(h, [_round(bad)], tmp_path, monkeypatch, max_rounds=2)
    assert h.tool_choices[-1] == "auto", "a nonexistent result forces nothing"
    assert h.records[-1].tally.progress_count == 0


# attempts
def test_a_new_attempt_starts_with_fresh_loop_and_policy_state(h, tmp_path, monkeypatch):
    h.results = {"configure_mesh": {"success": True}}
    for attempt in (0, 1):
        h.events.clear(); h.records.clear(); h.tool_choices.clear()
        _drive(h, [_round(_tc("configure_mesh", max_cells=1))], tmp_path, monkeypatch,
               max_rounds=2, mode="initial" if attempt == 0 else "retry")
        rec = h.records[-1]
        assert h.tool_choices[0] == "auto", "no forced tool leaks into a new attempt"
        assert rec.tally.consecutive_no_progress == 0, "no stall state leaks"
        assert rec.tally.rounds <= 2, "the round tally is attempt-local"


# truncation
def test_a_truncated_response_with_a_valid_call_still_executes_it(h, tmp_path, monkeypatch):
    h.results = {"run_mesh": {"mesh_ok": False}}
    _drive(h, [_round(_tc("run_mesh"), finish="length")], tmp_path, monkeypatch, max_rounds=2)
    assert h.dispatched == ["run_mesh"], "a valid call is never discarded for finish_reason"
    assert h.records[-1].extension.truncated_rounds == 1


def test_a_truncated_response_with_no_call_is_counted_and_continued(h, tmp_path, monkeypatch):
    _, msgs = _drive(h, [_round(text="cut off", finish="length")], tmp_path, monkeypatch,
                     max_rounds=2)
    assert h.dispatched == []
    assert h.records[-1].extension.truncated_rounds >= 1
    text = json.dumps([m.get("content") for m in msgs if isinstance(m.get("content"), str)])
    assert "truncated" in text


# canonical diagnostics
@pytest.mark.parametrize("script,expected", [
    ([_round(_tc("submit_mesh"))], "terminal_action"),
    ([_round(_tc("read_file"))] * 9, "rounds_exhausted"),
])
def test_exactly_one_canonical_record_on_every_exit(script, expected, h, tmp_path, monkeypatch):
    h.results = {"submit_mesh": {"success": True}, "read_file": {"ok": True}}
    _drive(h, script, tmp_path, monkeypatch, max_rounds=3)
    assert len(h.records) == 1
    assert h.records[0].exit.value == expected
    assert h.records[0].role.value == "builder"


def test_the_record_carries_no_prompt_argument_or_reasoning_content(h, tmp_path, monkeypatch):
    from meshpipeline.agents.loop.diagnostics import sanitized
    h.results = {"configure_mesh": {"success": True}}
    _drive(h, [_round(_tc("configure_mesh", secret="sk-live-XYZ"), text="my reasoning")],
           tmp_path, monkeypatch, max_rounds=2)
    blob = json.dumps(sanitized(h.records[-1]))
    for banned in ("sk-live-XYZ", "my reasoning", "system", "content"):
        assert banned not in blob, banned
