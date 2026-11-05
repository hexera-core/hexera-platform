# Responsibility: Verify the exported agent run reports real call counts, distinguishes strategies, carries no prose.
from __future__ import annotations

import types

from meshpipeline.capture.source import _builder_llm_calls, events_to_state
from meshpipeline.capture.trajectory import _builder_llm_calls as _traj_llm_calls


def _ev(event_type, payload, attempt=0):
    return types.SimpleNamespace(event_type=event_type, payload=payload, attempt=attempt,
                                 timestamp=types.SimpleNamespace(isoformat=lambda: "t"))


def _agent_run(role, attempt, rounds, tool_calls=0):
    return _ev("agent_run", {
        "role": role, "job_id": "j", "pipeline_attempt": attempt, "agent_attempt": attempt,
        "exit": "terminal_action", "failure_marker": "",
        "tally": {"rounds": rounds, "tool_calls": tool_calls, "provider_attempts": rounds,
                  "malformed_calls": 0, "plaintext_turns": 0, "progress_count": rounds,
                  "consecutive_no_progress": 0, "elapsed_s": 1.0},
        "extension": {"engine": "cfmesh", "strategy": "canonical_loop",
                      "submitted": True},
    }, attempt=attempt)


def _builder_attempt():
    return _ev("builder_attempt", {"mode": "initial", "spec_authored": True,
                                   "builder_message_histories": [{"role": "system"}]})


# agent_run is a first-class input
def test_canonical_records_are_consumed_not_write_only():
    events = [_builder_attempt(), _agent_run("builder", 0, 5)]
    state = events_to_state(events, {})
    assert "agent_run_records" in state, "the canonical record must reach the export"
    assert len(state["agent_run_records"]) == 1
    assert state["agent_run_records"][0]["role"] == "builder"


def test_the_export_carries_the_public_safe_accountability_fields():
    state = events_to_state([_builder_attempt(), _agent_run("builder", 0, 3, tool_calls=7)], {})
    rec = state["agent_run_records"][0]
    for field in ("role", "pipeline_attempt", "agent_attempt", "exit", "failure_marker",
                  "tally", "extension"):
        assert field in rec, field
    assert rec["tally"]["rounds"] == 3 and rec["tally"]["tool_calls"] == 7


def test_a_superseded_breadcrumb_is_never_exported_as_an_agent_run():
    events = [_builder_attempt(),
              _ev("agent_run_superseded", {"authoritative": False, "role": "builder",
                                           "exit": "superseded"})]
    state = events_to_state(events, {})
    assert state["agent_run_records"] == []


# llm_calls is truthful
def test_the_canonical_loop_gives_an_accurate_call_count():
    state = events_to_state([_builder_attempt(), _agent_run("builder", 0, 5)], {})
    assert state["builder_llm_calls"] == [5]


def test_multiple_rounds_are_counted_exactly():
    events = [_builder_attempt(), _builder_attempt(),
              _agent_run("builder", 0, 4), _agent_run("builder", 1, 11)]
    assert events_to_state(events, {})["builder_llm_calls"] == [4, 11]


def test_an_attempt_with_no_canonical_record_is_unknown_not_zero():
    state = events_to_state([_builder_attempt()], {})
    assert state["builder_llm_calls"] == [None], "absent, never a fabricated zero"


def test_a_superseded_attempt_reports_unknown_rather_than_zero():
    events = [_builder_attempt(),
              _ev("agent_run_superseded", {"authoritative": False, "role": "builder"})]
    assert events_to_state(events, {})["builder_llm_calls"] == [None]


def test_reviewer_records_never_become_builder_call_counts():
    events = [_builder_attempt(), _agent_run("reviewer", 0, 9)]
    state = events_to_state(events, {})
    assert state["builder_llm_calls"] == [None], "a reviewer run is not a builder call count"
    assert len(state["agent_run_records"]) == 1, "but it is still exported"


def test_the_trajectory_helper_reports_absent_for_an_unknown_attempt():
    assert _traj_llm_calls({1: 6}, 1) == 6
    assert _traj_llm_calls({1: 6}, 2) is None
    assert _traj_llm_calls({}, 1) is None


def test_the_source_helper_pads_to_the_attempt_count():
    assert _builder_llm_calls([], 3) == [None, None, None]
    assert _builder_llm_calls([_agent_run("builder", 1, 2)], 3) == [None, 2, None]


def test_the_canonical_event_is_reported_but_not_gating():
    from meshpipeline.capture.events import _SECTION_FIELD_CHECKS, EXPORT_FIELD_GAPS
    assert "accountability" in _SECTION_FIELD_CHECKS, "coverage is reported"
    event_type, keys = _SECTION_FIELD_CHECKS["accountability"]
    assert event_type == "agent_run" and {"role", "exit", "tally"} <= keys
    gap_names = {f for f, _ in EXPORT_FIELD_GAPS}
    assert not (keys & gap_names), "reported, not gating"
    assert "agent_run_records" not in gap_names


# the deterministic Snappy Builder strategy
def _snappy_run(attempt, rounds, *, submitted=True, exit_="terminal_action"):
    ev = _agent_run("builder", attempt, rounds)
    ev.payload["exit"] = exit_
    ev.payload["extension"] = {"engine": "snappy", "strategy": "engine_driver",
                               "plan_calls": rounds, "authored_spec": True,
                               "run_mesh_calls": 1, "run_mesh_successes": int(submitted),
                               "submitted": submitted, "auto_submitted": False}
    return ev


def test_the_deterministic_driver_reports_its_real_planning_calls():
    state = events_to_state([_builder_attempt(), _snappy_run(0, 2)], {})
    assert state["builder_llm_calls"] == [2]


def test_a_deterministic_build_with_no_planning_call_reports_zero_not_unknown():
    state = events_to_state([_builder_attempt(), _snappy_run(0, 0)], {})
    assert state["builder_llm_calls"] == [0], "a real zero is a fact, not an absence"


def test_the_two_builder_strategies_are_distinguishable_in_the_export():
    events = [_builder_attempt(), _builder_attempt(),
              _snappy_run(0, 1), _agent_run("builder", 1, 6)]
    recs = events_to_state(events, {})["agent_run_records"]
    strategies = [r["extension"].get("strategy") for r in recs]
    assert "engine_driver" in strategies, "the deterministic strategy names itself"
    assert "canonical_loop" in strategies, (
        "an interactive invocation must not be indistinguishable from a deterministic one")


def test_deterministic_success_and_failure_records_are_distinguishable():
    ok = events_to_state([_builder_attempt(), _snappy_run(0, 1)], {})["agent_run_records"][0]
    bad = events_to_state([_builder_attempt(),
                           _snappy_run(0, 1, submitted=False, exit_="attempts_exhausted")],
                          {})["agent_run_records"][0]
    assert ok["exit"] == "terminal_action" and ok["extension"]["submitted"] is True
    assert bad["exit"] == "attempts_exhausted" and bad["extension"]["submitted"] is False


def test_the_export_never_carries_prompts_reasoning_or_raw_arguments():
    state = events_to_state([_builder_attempt(), _agent_run("builder", 0, 3)], {})
    blob = str(state["agent_run_records"])
    for leak in ("system_prompt", "reasoning", "raw_arguments", "arguments", "signed_url"):
        assert leak not in blob, f"the canonical export leaked {leak}"
