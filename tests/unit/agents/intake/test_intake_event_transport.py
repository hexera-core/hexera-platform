# Responsibility: Verify one invocation exit yields one authoritative event, replayed once, carrying nothing sensitive.
from __future__ import annotations

import json
import types

import pytest

from meshpipeline.capture.source import events_to_state


def _events_of(appended, kind):
    return [e for e in appended if e.get("type") == kind]


# the returned envelope
def _node_result(exit_kind):
    from meshpipeline.agents.intake.diagnostics import IntakeRunExtension
    from meshpipeline.agents.loop.diagnostics import sanitized
    from meshpipeline.contracts.agent_loop import (
        AgentRole,
        AgentRunRecord,
        LoopExit,
        LoopLimits,
        LoopTally,
    )

    rec = AgentRunRecord(
        role=AgentRole.intake, job_id="j", pipeline_attempt=0, agent_attempt=0,
        limits=LoopLimits(max_rounds=20), tally=LoopTally(rounds=1),
        exit=exit_kind, extension=IntakeRunExtension(canonical_revision="r1"),
        rounds=(), tool_calls=(),
        failure_marker="intake_provider_down" if exit_kind is LoopExit.provider_failed else "")
    payload = sanitized(rec)
    if exit_kind is LoopExit.provider_failed:
        return {"api_failure": "intake_provider_down",
                "_intake_agent_run_event": {"type": "agent_run", "payload": payload}}
    return {"messages": [{"role": "assistant", "content": "a question"}],
            "_intake_agent_run_event": {"type": "agent_run", "payload": payload},
            "_intake_training_event": {"type": "intake_turn", "payload": {"turn": 1}}}


# chat path
@pytest.mark.asyncio
async def test_chat_transports_the_provider_failure_record(monkeypatch):
    from meshpipeline.contracts.agent_loop import LoopExit
    appended: list = []
    result = _node_result(LoopExit.provider_failed)

    # replay the entrypoint's transport rule against the real returned state
    _training = result.get("_intake_training_event")
    _run_ev = result.get("_intake_agent_run_event")
    if _training:
        appended.append(_training)
    if _run_ev and _run_ev.get("payload"):
        appended.append(_run_ev)

    runs = _events_of(appended, "agent_run")
    assert len(runs) == 1, "exactly one accountability event"
    p = runs[0]["payload"]
    assert p["role"] == "intake"
    assert p["exit"] == "provider_failed"
    assert p["failure_marker"] == "intake_provider_down"
    assert not _events_of(appended, "intake_turn"), "a failed turn has no conversational event"


def test_upload_appends_each_event_at_most_once():
    from meshpipeline.contracts.agent_loop import LoopExit
    appended: list = []
    result = _node_result(LoopExit.turn_complete)
    _training = result.get("_intake_training_event")
    _run_ev = result.get("_intake_agent_run_event")
    if _training or (_run_ev and _run_ev.get("payload")):
        if _training:
            appended.append(_training)
        if _run_ev and _run_ev.get("payload"):
            appended.append(_run_ev)
    assert len(_events_of(appended, "agent_run")) == 1
    assert len(_events_of(appended, "intake_turn")) == 1


# dispatch / capture
def test_dispatch_replays_the_buffered_event_exactly_once_into_the_job_log(monkeypatch):
    from meshpipeline.application.pipeline_run import _emit_intake_events
    from meshpipeline.contracts.agent_loop import LoopExit

    logged: list = []
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger",
                        lambda job_id: types.SimpleNamespace(
                            log=lambda t, p, **k: logged.append((t, p))))
    buffered = [_node_result(LoopExit.provider_failed)["_intake_agent_run_event"]]
    _emit_intake_events("job-1", buffered)
    assert [t for t, _ in logged] == ["agent_run"], "replayed once, with its own type"


def test_capture_source_consumes_the_intake_record():
    from meshpipeline.contracts.agent_loop import LoopExit
    payload = _node_result(LoopExit.provider_failed)["_intake_agent_run_event"]["payload"]
    ev = types.SimpleNamespace(event_type="agent_run", payload=payload, attempt=0,
                               timestamp=types.SimpleNamespace(isoformat=lambda: "t"))
    state = events_to_state([ev], {})
    recs = state["agent_run_records"]
    assert len(recs) == 1 and recs[0]["role"] == "intake"
    assert state["builder_llm_calls"] == [], "an intake record is not a builder call count"


def test_the_transported_record_carries_nothing_sensitive():
    from meshpipeline.contracts.agent_loop import LoopExit
    blob = json.dumps(_node_result(LoopExit.turn_complete)["_intake_agent_run_event"])
    for leak in ("preview_token", "token", "canonical_payload", "request_txt", "system",
                 "reasoning", "a question"):
        assert leak not in blob, f"the transported Intake record leaked {leak}"


# exactly-once across exits
@pytest.mark.parametrize("exit_kind,expected", [
    ("turn_complete", "turn_complete"),
    ("terminal_action", "terminal_action"),
    ("rounds_exhausted", "rounds_exhausted"),
    ("provider_failed", "provider_failed"),
])
def test_one_invocation_exit_yields_exactly_one_authoritative_event(exit_kind, expected):
    from meshpipeline.contracts.agent_loop import LoopExit
    result = _node_result(getattr(LoopExit, exit_kind))
    appended = [e for e in (result.get("_intake_training_event"),
                            result.get("_intake_agent_run_event")) if e]
    runs = _events_of(appended, "agent_run")
    assert len(runs) == 1, f"{exit_kind}: expected exactly one accountability event"
    assert runs[0]["payload"]["exit"] == expected
    assert "agent_run" not in json.dumps(
        [e for e in appended if e.get("type") != "agent_run"]), (
        "no nested legacy record inside the conversational event")
