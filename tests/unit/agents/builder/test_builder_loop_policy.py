# Responsibility: Verify the builder's own progress vocabulary - what repeats, what redirects, and when it terminates.
from __future__ import annotations

import asyncio
import types

import pytest

from meshpipeline.agents.builder.loop_policy import (
    BUILDER_NO_PROGRESS_THRESHOLD,
    BUILDER_STUCK_WARNING_AT,
    CONFIGURE_REDIRECT_AT,
    BuilderLoopPolicy,
    action_signature,
    category_of,
    normalize_arguments,
)
from meshpipeline.agents.loop.progress import advance, stalled
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
)


def _policy(**kw):
    kw.setdefault("engine", "snappy")
    kw.setdefault("mode", "initial")
    kw.setdefault("limits_", LoopLimits(max_rounds=60,
                                        no_progress_threshold=BUILDER_NO_PROGRESS_THRESHOLD))
    kw.setdefault("executor", None)
    return BuilderLoopPolicy(**kw)


def _round(policy, *executions):
    for tool, args, result in executions:
        policy.note_execution(tool, args, result)
    return policy.observe(LoopTally(rounds=1))


# thresholds are per-agent
def test_builder_keeps_threshold_four_and_reviewer_keeps_three():
    from meshpipeline.agents.reviewer.loop_policy import REVIEWER_NO_PROGRESS_THRESHOLD
    assert BUILDER_NO_PROGRESS_THRESHOLD == 4
    assert REVIEWER_NO_PROGRESS_THRESHOLD == 3
    assert BUILDER_STUCK_WARNING_AT == 3 and CONFIGURE_REDIRECT_AT == 2


def test_the_policy_declares_its_own_threshold_to_the_shared_runtime():
    p = _policy()
    assert p.limits().no_progress_threshold == 4
    assert stalled(3, p.limits()) is False and stalled(4, p.limits()) is True


# action identity
def test_the_same_configuration_serialised_differently_is_the_same_action():
    a = {"max_cells": 4_000_000, "quality": "strict"}
    b = {"quality": "strict", "max_cells": 4_000_000}
    assert normalize_arguments(a) == normalize_arguments(b)
    assert action_signature("configure_mesh", a) == action_signature("configure_mesh", b)


def test_a_materially_different_configuration_is_a_different_action():
    assert action_signature("configure_mesh", {"max_cells": 1}) != \
        action_signature("configure_mesh", {"max_cells": 2})


def test_tools_are_categorised_in_the_builders_own_vocabulary():
    assert category_of("configure_mesh") == "authoring"
    assert category_of("run_mesh") == "execution"
    assert category_of("submit_mesh") == "delivery"
    assert category_of("nonsense") == "unknown"


# progress rules
def test_a_successful_native_run_is_progress():
    p = _policy()
    obs = _round(p, ("run_mesh", {"engine": "snappy"}, {"mesh_ok": True}))
    assert obs.made_progress is True and p.run_mesh_successes == 1


def test_a_repeated_failed_run_is_not_progress():
    p = _policy()
    args, bad = {"engine": "snappy"}, {"mesh_ok": False, "success": False}
    assert _round(p, ("run_mesh", args, bad)).made_progress is False
    assert _round(p, ("run_mesh", args, bad)).made_progress is False
    assert p.repeat_count == 2


def test_authoring_a_spec_that_did_not_exist_is_progress():
    p = _policy()
    assert _round(p, ("configure_mesh", {"max_cells": 1}, {"success": True})).made_progress is True


def test_reserialising_the_same_configuration_is_not_progress():
    p = _policy()
    _round(p, ("configure_mesh", {"a": 1, "b": 2}, {"success": True}))
    obs = _round(p, ("configure_mesh", {"b": 2, "a": 1}, {"success": True}))
    assert obs.made_progress is False, "key order is cosmetic, not a new action"


def test_a_materially_changed_configuration_is_progress():
    p = _policy()
    _round(p, ("configure_mesh", {"max_cells": 1}, {"success": True}))
    assert _round(p, ("configure_mesh", {"max_cells": 2},
                      {"success": True})).made_progress is True


def test_a_round_with_no_action_is_not_progress():
    assert _policy().observe(LoopTally(rounds=1)).made_progress is False


def test_a_successful_submission_is_progress():
    p = _policy()
    assert _round(p, ("submit_mesh", {}, {"success": True})).made_progress is True


# the preserved warning and termination points
def test_the_configure_redirect_fires_on_the_second_repeat():
    p = _policy()
    args = {"max_cells": 1}
    _round(p, ("configure_mesh", args, {"success": False}))          # repeat 1 - no hint
    assert p.correction(LoopStage.running, LoopTally(rounds=1),
                        ProgressObservation(False, "s", "")) is None
    obs = _round(p, ("configure_mesh", args, {"success": False}))    # repeat 2 - redirect
    note = p.correction(LoopStage.running, LoopTally(rounds=2), obs)
    assert note and "Call run_mesh NOW" in note


def test_the_strategy_warning_fires_on_the_third_identical_action():
    p = _policy()
    args = {"engine": "snappy"}
    for _ in range(2):
        _round(p, ("run_mesh", args, {"mesh_ok": False}))
    obs = _round(p, ("run_mesh", args, {"mesh_ok": False}))          # repeat 3
    assert p.repeat_count == BUILDER_STUCK_WARNING_AT
    note = p.correction(LoopStage.running, LoopTally(rounds=3), obs)
    assert note and "CHANGE your strategy NOW" in note
    assert "One more identical call aborts the attempt." in note


def test_termination_is_reached_on_the_fourth_identical_action():
    p = _policy()
    args = {"engine": "snappy"}
    consecutive, signature = 0, None
    for i in range(4):
        obs = _round(p, ("run_mesh", args, {"mesh_ok": False}))
        _progress, consecutive, signature = advance(
            obs, progress_count=0, consecutive_no_progress=consecutive,
            last_signature=signature)
        stalled_now = stalled(consecutive, p.limits())
        assert stalled_now is (i == 3), f"round {i + 1} stalled={stalled_now}"
    assert consecutive == BUILDER_NO_PROGRESS_THRESHOLD


def test_a_changed_action_resets_the_repetition_count():
    p = _policy()
    for _ in range(3):
        _round(p, ("run_mesh", {"engine": "snappy"}, {"mesh_ok": False}))
    assert p.repeat_count == 3
    _round(p, ("run_mesh", {"engine": "snappy"}, {"mesh_ok": True}))   # a real advance
    assert p.repeat_count == 0


# forced tool progression
@pytest.mark.parametrize("tool,result,expected", [
    ("configure_mesh", {"success": True}, "run_mesh"),
    ("run_mesh", {"mesh_ok": True}, "submit_mesh"),
    ("run_mesh", {"mesh_already_production_grade": True}, "submit_mesh"),
    ("configure_mesh", {"mesh_already_production_grade": True}, "submit_mesh"),
    ("run_mesh", {"mesh_ok": False}, None),
    ("configure_mesh", {"success": False}, None),
    ("read_file", {"content": "x"}, None),
])
def test_the_forced_tool_table_is_preserved(tool, result, expected):
    p = _policy()
    _round(p, (tool, {}, result))
    assert p.forced_tool(LoopTally(rounds=1)) == expected


def test_a_forced_tool_is_cleared_once_the_state_no_longer_requires_it():
    p = _policy()
    _round(p, ("configure_mesh", {}, {"success": True}))
    assert p.forced_tool(LoopTally(rounds=1)) == "run_mesh"
    _round(p, ("run_mesh", {}, {"mesh_ok": False}))     # ran, not production-grade
    assert p.forced_tool(LoopTally(rounds=2)) is None, "the model adjusts the strategy"


def test_forced_tool_history_is_recorded_for_diagnostics():
    p = _policy()
    _round(p, ("configure_mesh", {}, {"success": True}))
    _round(p, ("run_mesh", {}, {"mesh_ok": True}))
    assert p.extension().forced_tools == ("run_mesh", "submit_mesh")


# malformed arguments
def test_a_malformed_call_is_never_dispatched_and_forces_nothing():
    from meshpipeline.agents.loop.accounting import ToolInvocation
    dispatched = []

    from meshpipeline.agents.builder.executor import BuilderToolResult

    async def _never(tool, args, *, call_index):
        if args is None:      # the executor's own malformed path - nothing is dispatched
            return BuilderToolResult(tool=tool, malformed=True, accepted=False,
                                     content="[SYSTEM] not valid JSON")
        dispatched.append(tool)
        raise AssertionError("a malformed call must not reach the domain action")

    p = _policy(executor=types.SimpleNamespace(run=_never))
    out = asyncio.run(p.execute(ToolInvocation(
        round_index=1, call_index=1, tool="run_mesh",
        raw_arguments="{not json", parsed=None)))
    assert dispatched == []
    assert out.accepted is False and "not valid JSON" in out.content
    assert p.malformed_calls == 1

    obs = p.observe(LoopTally(rounds=1))
    assert obs.made_progress is False, "a malformed call is never progress"
    assert p.forced_tool(LoopTally(rounds=1)) is None
    assert p.run_mesh_calls == 0, "no native run was counted for an undispatched call"


# diagnostics
def test_the_extension_carries_public_safe_engineering_facts_only():
    p = _policy()
    _round(p, ("configure_mesh", {"secret": "sk-live-x"}, {"success": True}))
    _round(p, ("run_mesh", {}, {"mesh_ok": True}))
    p.note_authored()
    p.note_truncated()
    ext = p.extension()
    assert ext.engine == "snappy" and ext.mode == "initial"
    assert ext.authored_spec is True and ext.run_mesh_successes == 1
    assert ext.truncated_rounds == 1
    flat = ext.sanitized()
    assert "sk-live-x" not in str(flat)
    for value in flat.values():
        assert isinstance(value, (str, int, float, bool, tuple))


def test_the_policy_is_attempt_local():
    for _ in range(2):
        p = _policy()
        assert p.forced_tool(LoopTally()) is None
        assert p.repeat_count == 0 and p.last_signature == ""
        assert p.authored_spec is False and p.submitted is False
        assert p.extension().forced_tools == ()


def test_the_policy_satisfies_the_loop_driver_protocol():
    p = _policy()
    for hook in ("limits", "category_of", "execute", "observe", "correction",
                 "on_plaintext", "extension", "forced_tool"):
        assert callable(getattr(p, hook)), hook
    assert p.role is AgentRole.builder
