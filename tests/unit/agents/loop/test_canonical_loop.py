# Responsibility: Verify the runner counts rounds and calls, names each exit, and leaves every decision to the driver.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.agents.loop import diagnostics
from meshpipeline.agents.loop.driver import LoopResult, RoundDecision, ToolOutcome
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
)
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


class _Ext:
    def sanitized(self):
        return {"kind": "test"}


class _Driver:

    role = AgentRole.reviewer

    def __init__(self, *, limits=None, progress=None, terminal_on=None):
        self._limits = limits or LoopLimits()
        self._progress = list(progress or [])
        self._terminal_on = terminal_on
        self.executed: list[tuple[str, dict | None]] = []
        self.corrections: list[str] = []
        self.plaintext_nudges = 0
        self.prepared = 0
        self._force_next: str | None = None

    def limits(self):
        return self._limits

    def category_of(self, tool):
        return "submission" if tool == "submit" else "navigation"

    async def execute(self, invocation):
        self.executed.append((invocation.tool, invocation.parsed))
        if invocation.parsed is None:
            return ToolOutcome(content="unparseable", accepted=False)
        terminal = invocation.tool == self._terminal_on
        return ToolOutcome(content=f"{invocation.tool} ok", terminal=terminal,
                           payload="DONE" if terminal else None)

    def observe(self, tally: LoopTally):
        made = self._progress.pop(0) if self._progress else True
        return ProgressObservation(made_progress=made, signature="sig", detail="d")

    def correction(self, stage, tally, observation):
        if observation.made_progress:
            return None
        note = f"correct:{stage.value}:{tally.remaining_rounds(self._limits)}"
        self.corrections.append(note)
        return note

    def before_round(self, tally, messages):
        self.prepared += 1

    def forced_tool(self, tally):
        return self._force_next

    def is_supersession(self, exc):
        return False              # this driver has no ownership concept

    async def close_out(self, tally):
        return None               # no application-initiated terminal action

    def on_plaintext(self, tally):
        self.plaintext_nudges += 1
        return RoundDecision(message="call a tool", complete=False)

    def extension(self):
        return _Ext()


def _tc(name, args="{}"):
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=args)


def _round(*calls, text="", finish="tool_calls", attempts=1, marker=""):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason=finish if calls else "stop",
                            provider=ProviderAttemptInfo(attempts, "p", "m"),
                            failure_marker=marker)


def _append(messages, call_id, content):
    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


def _drive(script, driver, **kw):
    it = iter(script)

    async def provider(**_kw):
        try:
            return next(it)
        except StopIteration:
            return _round(text="nothing more")

    kw.setdefault("deadline_s", 30.0)
    return asyncio.run(run_agent_loop(
        driver=driver, provider_call=provider, messages=[], tools=[], job_id="job-loop",
        append_tool_result=_append, **kw))


@pytest.fixture(autouse=True)
def captured(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(diagnostics, "emit",
                        lambda rec: out.append(diagnostics.sanitized(rec)) or out[-1])
    return out


# accounting
def test_provider_rounds_are_counted(captured):
    d = _Driver(limits=LoopLimits(max_rounds=3))
    _drive([_round(_tc("zoom")), _round(_tc("zoom"))], d)
    assert captured[0]["tally"]["rounds"] == 3


def test_actual_tool_calls_are_counted_including_several_in_one_round(captured):
    d = _Driver(limits=LoopLimits(max_rounds=2))
    _drive([_round(_tc("a"), _tc("b"), _tc("c"))], d)
    assert captured[0]["tally"]["tool_calls"] == 3
    assert [t for t, _ in d.executed] == ["a", "b", "c"]


def test_a_plain_text_round_is_counted_and_nudged(captured):
    d = _Driver(limits=LoopLimits(max_rounds=2))
    _drive([_round(text="just talking")], d)
    assert captured[0]["tally"]["plaintext_turns"] == 2
    assert d.plaintext_nudges == 2


def test_malformed_arguments_are_recorded_and_handed_to_the_agent(captured):
    d = _Driver(limits=LoopLimits(max_rounds=1))
    _drive([_round(_tc("zoom", "{not json"))], d)
    assert captured[0]["tally"]["malformed_calls"] == 1
    assert d.executed == [("zoom", None)], "the agent is TOLD the arguments did not parse"
    assert captured[0]["tool_calls"][0]["accepted"] is False


def test_provider_attempts_are_recorded_not_performed(captured):
    d = _Driver(limits=LoopLimits(max_rounds=1))
    _drive([_round(_tc("zoom"), attempts=3)], d)
    assert captured[0]["tally"]["provider_attempts"] == 3


def test_calls_are_grouped_by_the_agents_own_categories(captured):
    d = _Driver(limits=LoopLimits(max_rounds=2))
    _drive([_round(_tc("zoom"), _tc("submit"))], d)
    assert captured[0]["calls_by_category"] == {"navigation": 1, "submission": 1}


# loop exits
def test_a_terminal_action_ends_the_loop_and_returns_the_payload(captured):
    d = _Driver(limits=LoopLimits(max_rounds=9), terminal_on="submit")
    result = _drive([_round(_tc("zoom")), _round(_tc("submit")), _round(_tc("zoom"))], d)
    assert isinstance(result, LoopResult)
    assert result.exit is LoopExit.terminal_action and result.payload == "DONE"
    assert captured[0]["tally"]["rounds"] == 2, "no round runs after the terminal action"


def test_round_exhaustion_is_named(captured):
    d = _Driver(limits=LoopLimits(max_rounds=2))
    result = _drive([_round(_tc("zoom"))] * 5, d)
    assert result.exit is LoopExit.rounds_exhausted
    assert captured[0]["tally"]["rounds"] == 2


def test_deadline_exhaustion_is_named(captured):
    d = _Driver(limits=LoopLimits(max_rounds=50, total_timeout_s=0.0))
    result = _drive([_round(_tc("zoom"))] * 3, d, deadline_s=0.0)
    assert result.exit is LoopExit.deadline_exhausted
    assert captured[0]["tally"]["rounds"] == 0, "no round starts after the budget is gone"


def test_a_provider_failure_ends_the_loop_with_the_routers_marker(captured):
    d = _Driver(limits=LoopLimits(max_rounds=5))
    result = _drive([_round(marker="<<API_FAILURE:reviewer_timeout>>")], d)
    assert result.exit is LoopExit.provider_failed
    assert result.failure_marker == "<<API_FAILURE:reviewer_timeout>>"


def test_an_unset_round_limit_is_not_enforced(captured):
    d = _Driver(limits=LoopLimits(total_timeout_s=30.0), terminal_on="submit")
    result = _drive([_round(_tc("zoom"))] * 4 + [_round(_tc("submit"))], d)
    assert result.exit is LoopExit.terminal_action and captured[0]["tally"]["rounds"] == 5


# progress and stalling
def test_no_progress_escalates_then_terminates(captured):
    d = _Driver(limits=LoopLimits(max_rounds=20, no_progress_threshold=3),
                progress=[False, False, False])
    result = _drive([_round(_tc("zoom"))] * 5, d)
    assert result.exit is LoopExit.no_progress
    assert captured[0]["tally"]["consecutive_no_progress"] == 3
    assert len(d.corrections) == 3, "every unproductive round gets a correction"


def test_progress_resets_the_streak(captured):
    d = _Driver(limits=LoopLimits(max_rounds=6, no_progress_threshold=3),
                progress=[False, False, True, False, False, False])
    result = _drive([_round(_tc("zoom"))] * 6, d)
    assert result.exit is LoopExit.no_progress
    assert captured[0]["tally"]["progress_count"] == 1


def test_an_unset_stall_threshold_never_terminates(captured):
    d = _Driver(limits=LoopLimits(max_rounds=4), progress=[False] * 4)
    result = _drive([_round(_tc("zoom"))] * 4, d)
    assert result.exit is LoopExit.rounds_exhausted
    assert captured[0]["tally"]["consecutive_no_progress"] == 4


def test_the_correction_is_told_the_remaining_budget(captured):
    d = _Driver(limits=LoopLimits(max_rounds=5, no_progress_threshold=99), progress=[False] * 5)
    _drive([_round(_tc("zoom"))] * 5, d)
    assert d.corrections[0].endswith(":4") and d.corrections[-1].endswith(":0")


def test_the_escalation_stage_is_computed_from_configured_thresholds(captured):
    d = _Driver(limits=LoopLimits(max_rounds=5, warn_at_remaining_rounds=3,
                                  closing_at_remaining_rounds=1, no_progress_threshold=99),
                progress=[False] * 5)
    _drive([_round(_tc("zoom"))] * 5, d)
    stages = [c.split(":")[1] for c in d.corrections]
    assert stages[0] == LoopStage.running.value
    assert LoopStage.warned.value in stages and LoopStage.closing.value in stages


# exactly one record
@pytest.mark.parametrize("script,driver_kw,expected", [
    ([_round(_tc("submit"))], {"terminal_on": "submit"}, "terminal_action"),
    ([_round(_tc("zoom"))] * 5, {}, "rounds_exhausted"),
    ([_round(marker="<<API_FAILURE:x>>")], {}, "provider_failed"),
])
def test_every_exit_emits_exactly_one_record(script, driver_kw, expected, captured):
    d = _Driver(limits=LoopLimits(max_rounds=2), **driver_kw)
    _drive(script, d)
    assert len(captured) == 1 and captured[0]["exit"] == expected
    assert captured[0]["role"] == "reviewer" and captured[0]["job_id"] == "job-loop"


def test_the_record_carries_the_agents_own_extension(captured):
    _drive([_round(_tc("zoom"))], _Driver(limits=LoopLimits(max_rounds=1)))
    assert captured[0]["extension"] == {"kind": "test"}


def test_a_driver_from_a_domain_the_runner_has_never_seen_runs_unchanged(captured):
    class _AlienExt:
        def sanitized(self):
            return {"kind": "spectrograph"}

    class _AlienDriver:
        role = AgentRole.builder                      # the enum is the only shared vocabulary

        def __init__(self):
            self.seen: list[str] = []
            self.categories: list[str] = []

        def limits(self):
            return LoopLimits(max_rounds=6, no_progress_threshold=2)

        def category_of(self, tool):
            cat = "calibration" if tool.startswith("tune_") else "acquisition"
            self.categories.append(cat)
            return cat

        async def execute(self, invocation):
            self.seen.append(invocation.tool)
            return ToolOutcome(content="ok", terminal=invocation.tool == "emit_spectrum",
                               payload="SPECTRUM" if invocation.tool == "emit_spectrum" else None)

        def observe(self, tally):
            return ProgressObservation(made_progress=True, signature="grating")

        def correction(self, stage, tally, observation):
            return None

        def before_round(self, tally, messages):
            return None

        def forced_tool(self, tally):
            return "tune_grating" if tally.rounds == 0 else None

        def is_supersession(self, exc):
            return False

        async def close_out(self, tally):
            return None

        def on_plaintext(self, tally):
            return RoundDecision(message="acquire something", complete=False)

        def extension(self):
            return _AlienExt()

    d = _AlienDriver()
    result = _drive([_round(_tc("tune_grating")), _round(_tc("emit_spectrum"))], d)

    assert result.exit is LoopExit.terminal_action and result.payload == "SPECTRUM"
    assert d.seen == ["tune_grating", "emit_spectrum"]
    rec = captured[0]
    assert set(rec["calls_by_category"]) == {"calibration", "acquisition"}
    assert rec["tally"]["tool_calls"] == 2
    assert rec["extension"] == {"kind": "spectrograph"}, \
        "the runner did not carry the driver's own diagnostics through"


# the agent's own hooks are consulted
def test_the_driver_prepares_its_own_context_before_every_round(captured):
    d = _Driver(limits=LoopLimits(max_rounds=3))
    _drive([_round(_tc("zoom"))], d)
    assert d.prepared == 3, "the agent gets a before_round hook on every round"


def test_a_forced_tool_is_carried_to_the_provider_but_chosen_by_the_agent(captured):
    seen: list[dict | None] = []

    async def provider(**kw):
        seen.append(kw.get("tool_choice"))
        return _round(_tc("submit"))

    d = _Driver(limits=LoopLimits(max_rounds=2), terminal_on="submit")
    d._force_next = "submit"
    asyncio.run(run_agent_loop(
        driver=d, provider_call=provider, messages=[], tools=[], job_id="j",
        append_tool_result=_append, deadline_s=30.0))
    assert seen[0] == {"type": "function", "function": {"name": "submit"}}


def test_no_tool_choice_is_sent_when_the_agent_forces_none(captured):
    seen: list[dict] = []

    async def provider(**kw):
        seen.append(kw)
        return _round(_tc("zoom"))

    d = _Driver(limits=LoopLimits(max_rounds=1))
    asyncio.run(run_agent_loop(
        driver=d, provider_call=provider, messages=[], tools=[], job_id="j",
        append_tool_result=_append, deadline_s=30.0))
    assert "tool_choice" not in seen[0]


def test_the_runner_never_decides_which_tool_is_forced():
    import inspect

    from meshpipeline.agents.loop import runner
    src = inspect.getsource(runner)
    assert "driver.forced_tool(" in src
    for domain in ("run_mesh", "submit_mesh", "configure_mesh", "submit_findings"):
        assert domain not in src, f"the runner must not name {domain}"
