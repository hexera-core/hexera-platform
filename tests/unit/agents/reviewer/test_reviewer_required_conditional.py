# Responsibility: Verify a review is required exactly when an executor succeeded, and its absence then blocks export.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.training._jsonl_projection import log_from_dir

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}



def _write_events(tmp_path, job_id, events):
    path = tmp_path / job_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for i, e in enumerate(events):
            f.write(json.dumps({
                "schema_version": 2, "record_type": "span_event",
                "trace_id": e.get("job_id", ""), "span_id": f"s{i:04d}",
                "parent_span_id": None, "seq": i, "ts": e["timestamp"],
                "name": e["event_type"], "kind": "event", "component": "event",
                "attributes": ({"attempt": e["attempt"]}
                               if e.get("attempt") is not None else {}),
                "payload": e.get("payload", {}),
            }) + "\n")


_BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(offset_s: float = 0.0) -> str:
    return (_BASE_TS + timedelta(seconds=offset_s)).isoformat()


def _engine_select(job_id):
    return {
        "timestamp": _ts(0.5), "job_id": job_id, "event_type": "engine_select_run",
        "payload": {"chosen": "cfmesh", "source": "selector"},
    }


def _intake_complete(job_id):
    return {
        "timestamp": _ts(0), "job_id": job_id, "event_type": "intake_complete",
        "payload": {
            "domain": "aero", "max_turns_reached": False,
            "intake_turns": [{"role": "user", "content": "build it"}],
            "intake_system_snapshot": "sys",
            "intake_llm_metadata": [{"turn": 1, "finish_reason": "stop", "usage": {}}],
            "request_txt": "build a mesh", "review_brief_txt": "make it good",
        },
    }


def _builder(job_id, attempt):
    return {
        "timestamp": _ts(10 * attempt), "job_id": job_id,
        "event_type": "builder_attempt", "attempt": attempt,
        "payload": {
            "builder_message_histories": [], "builder_tool_call_histories": [],
            "builder_system_message_snapshots": "sys", "builder_full_responses": "",
            "builder_call_metadata": [], "builder_pruning_events": [],
            "builder_tools_definition": [],
        },
    }


def _executor(job_id, attempt, success):
    return {
        "timestamp": _ts(10 * attempt + 1), "job_id": job_id,
        "event_type": "executor_run",
        "payload": {
            "success": success, "output_len": 100, "workspace": "/ws",
            "executor_stdouts": "ok", "executor_stderrs": "",
            "attempt_log_snapshots": "", "mesh_manifest": {"patch_types": {}},
        },
    }


def _terminal(job_id):
    return {
        "timestamp": _ts(60), "job_id": job_id, "event_type": "final_result_built",
        "payload": {
            "final_result": {"schema_version": 1, "status": "succeeded"}, "terminal_message": "Mesh generation completed successfully.",
            "geometry_source": _GEOMETRY_SOURCE, "agent_model_configs": {},
        },
    }


def _reviewer(job_id, attempt, verdict):
    return {
        "timestamp": _ts(10 * attempt + 5), "job_id": job_id,
        "event_type": "reviewer_run", "attempt": attempt,
        "payload": {
            "verdict": verdict, "tool_calls": 5,
            "reviewer_tool_call_histories": [],
            "reviewer_reasoning_chains": ["because"],
            "reviewer_full_responses": [verdict],
            "reviewer_system_snapshots": "sys",
            "reviewer_input_texts": "input",
            "reviewer_review_dirs": "/tmp/r",
        },
    }



def test_no_reviewer_event_when_all_executors_unsolvable_is_export_ready(tmp_path):
    job_id = "f-pipe-2-case"
    events = [
        _intake_complete(job_id),
        _engine_select(job_id),
        _builder(job_id, 1), _executor(job_id, 1, success=False),
        _builder(job_id, 2), _executor(job_id, 2, success=False),
        _builder(job_id, 3), _executor(job_id, 3, success=False),
        _builder(job_id, 4), _executor(job_id, 4, success=False),
        _terminal(job_id),
    ]
    _write_events(tmp_path, job_id, events)
    rep = log_from_dir(job_id, tmp_path).validate_completeness()
    assert "reviewer_run" not in rep.missing_event_types, (
        f"reviewer_run must be excluded from missing_event_types on the "
        f"F-PIPE-2 path - got {rep.missing_event_types}"
    )
    assert rep.is_export_ready is True, (
        f"F-PIPE-2 unsolvable run must be export-ready. report={rep}"
    )



def test_reviewer_required_when_an_executor_succeeded(tmp_path):
    job_id = "standard-success-case"
    events = [
        _intake_complete(job_id),
        _engine_select(job_id),
        _builder(job_id, 1), _executor(job_id, 1, success=True),
        _reviewer(job_id, 1, "PASS"),
        _terminal(job_id),
    ]
    _write_events(tmp_path, job_id, events)
    rep = log_from_dir(job_id, tmp_path).validate_completeness()
    assert rep.is_export_ready is True


def test_reviewer_missing_when_executor_succeeded_is_NOT_export_ready(tmp_path):
    job_id = "missing-reviewer-bug-case"
    events = [
        _intake_complete(job_id),
        _engine_select(job_id),
        _builder(job_id, 1), _executor(job_id, 1, success=True),
        _terminal(job_id),
    ]
    _write_events(tmp_path, job_id, events)
    rep = log_from_dir(job_id, tmp_path).validate_completeness()
    assert "reviewer_run" in rep.missing_event_types
    assert rep.is_export_ready is False



def test_no_executors_falls_back_to_requiring_reviewer(tmp_path):
    job_id = "pre-build-fail-case"
    events = [
        _intake_complete(job_id),
        _engine_select(job_id),
        _builder(job_id, 1),
        _terminal(job_id),
    ]
    _write_events(tmp_path, job_id, events)
    rep = log_from_dir(job_id, tmp_path).validate_completeness()
    assert "reviewer_run" in rep.missing_event_types



def test_reviewer_fields_not_counted_as_gaps_on_f_pipe_2(tmp_path):
    job_id = "f-pipe-2-gaps"
    events = [
        _intake_complete(job_id),
        _engine_select(job_id),
        _builder(job_id, 1), _executor(job_id, 1, success=False),
        _builder(job_id, 2), _executor(job_id, 2, success=False),
        _terminal(job_id),
    ]
    _write_events(tmp_path, job_id, events)
    rep = log_from_dir(job_id, tmp_path).validate_completeness()
    for g in rep.gaps:
        assert "reviewer:" not in g, (
            f"reviewer field {g!r} must not be a gap when reviewer was bypassed (F-PIPE-2)"
        )


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
