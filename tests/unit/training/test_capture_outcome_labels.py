# Responsibility: Verify the terminal capture carries the durable result and rendered message, and no model prose.
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.capture_authority import seed_events

from meshpipeline.capture.events import (
    _SECTION_FIELD_CHECKS,
    EXPORT_FIELD_GAPS,
    EventLog,
    format_report,
)

CAPTURE_OWNER = "owner-training-tests"

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}

# The terminal record is deterministic: the durable facts, and the message rendered from them.
# There is no prompt/response pair, because no model composes the verdict.
_TERMINAL_CONTENT_FIELDS = frozenset({
    "final_result",
    "terminal_message",
})

_LABELS_FIELDS = frozenset({
    "geometry_source",
    "agent_model_configs",
})



def _ts(offset_s: float = 0.0) -> str:
    return (datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            + timedelta(seconds=offset_s)).isoformat()


def _write(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, e in enumerate(events):
            rec = {
                "schema_version": 2, "record_type": "span_event",
                "trace_id": e.get("job_id", ""), "span_id": f"s{i:04d}",
                "parent_span_id": None, "seq": i, "ts": e["timestamp"],
                "name": e["event_type"], "kind": "event", "component": "event",
                "attributes": ({"attempt": e["attempt"]}
                               if e.get("attempt") is not None else {}),
                "payload": e.get("payload", {}),
            }
            f.write(json.dumps(rec) + "\n")


def _engine_full(job_id: str) -> dict:
    return {
        "timestamp": _ts(0.5), "job_id": job_id, "event_type": "engine_select_run",
        "payload": {"chosen": "cfmesh", "source": "selector"},
    }


def _intake_full(job_id: str) -> dict:
    return {
        "timestamp": _ts(0), "job_id": job_id, "event_type": "intake_complete",
        "payload": {
            "domain": "aero", "max_turns_reached": False, "finish_reason": "stop",
            "mach": 0.8, "aoa": 2.0, "turbulence": "kOmegaSST",
            "intake_turns": [{"role": "user", "content": "mesh this"}],
            "intake_system_snapshot": "You are intake...",
            "intake_llm_metadata": [{"turn": 1, "finish_reason": "stop"}],
            "request_txt": "mesh at Mach 0.8",
            "review_brief_txt": "check boundary layer",
        },
    }


def _builder_full(job_id: str, ts_offset: float = 10, attempt: int = 1) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "builder_attempt", "attempt": attempt,
        "payload": {
            "mode": "initial", "executor_success": True,
            "noop": False, "noop_count": 0, "tool_calls": 3, "response_len": 500,
            "builder_message_histories":        [{"role": "user", "content": "build"}],
            "builder_tool_call_histories":      [{"name": "write_file", "input": {}}],
            "builder_system_message_snapshots": "You are builder...",
            "builder_full_responses":           "Here is the mesh script.",
            "builder_call_metadata":            [{"finish_reason": "end_turn"}],
            "builder_pruning_events":           [],
            "builder_tools_definition":         [{"name": "write_file"}],
        },
    }


def _executor_full(job_id: str, ts_offset: float = 20, success: bool = True) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "executor_run",
        "payload": {
            "success": success, "output_len": 1800, "workspace": f"/ws/{job_id}",
            "executor_stdouts": "Mesh generated.\n", "executor_stderrs": "",
            "attempt_log_snapshots": "Attempt 1: done\n",
            "mesh_manifest": {"schema_version": "1.0"},
        },
    }


def _classifier_full(job_id: str, ts_offset: float = 25, attempt: int = 1) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "classifier_run", "attempt": attempt,
        "payload": {
            "section": "MESH", "error_source": "executor_fail",
            "summary":       "fix",
            "failed_gate":   "manifest_valid",
            "failed_axes":   [],
            "builder_mode":  "retry",
            "deterministic": True,
        },
    }


def _reviewer_full(job_id: str, ts_offset: float = 50, attempt: int = 1) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "reviewer_run", "attempt": attempt,
        "payload": {
            "verdict": "PASS", "tool_calls": 8,
            "reviewer_tool_call_histories": [{"call_num": 1, "tool": "submit_findings", "args": {}, "result": "ok"}],
            "reviewer_reasoning_chains":    ["All patches present."],
            "reviewer_full_responses":      ["<<PASS>>\nAll patches present."],
            "reviewer_system_snapshots":    "You are reviewer...",
            "reviewer_input_texts":         "Domain bbox: ...",
            "reviewer_review_dirs":         f"/ws/{job_id}/review_1",
        },
    }


def _final_result_full(
    job_id: str,
    ts_offset: float = 60,
    api_failure: bool = False,
    geometry_source: dict | None = _GEOMETRY_SOURCE,
    agent_model_configs: dict | None = None,
) -> dict:
    if agent_model_configs is None:
        agent_model_configs = {"builder": "claude-sonnet-4-6", "reviewer": "kimi-k2"}
    status = "failed" if api_failure else "succeeded"
    message = ("Mesh generation did not complete successfully."
               if api_failure else "Mesh generation completed successfully.")
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "final_result_built",
        "payload": {
            "api_failure": api_failure,
            "final_result": {"schema_version": 1, "job_id": job_id, "owner_id": "o",
                             "status": status, "engine": "cfmesh",
                             "outcome_code": "success" if not api_failure else "internal_pipeline_failure"},
            "terminal_message":    message,
            "geometry_source":     geometry_source,
            "agent_model_configs": agent_model_configs,
        },
    }


def _final_result_minimal(job_id: str, ts_offset: float = 60) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "final_result_built",
        "payload": {"api_failure": False},
    }


def _no_retry_job_full(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _engine_full(job_id),
        _builder_full(job_id, ts_offset=10),
        _executor_full(job_id, ts_offset=20),
        _reviewer_full(job_id, ts_offset=30),
        _final_result_full(job_id, ts_offset=40),
    ]


def _all_phases_retry_job(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _engine_full(job_id),
        _builder_full(job_id, ts_offset=10, attempt=1),
        _executor_full(job_id, ts_offset=20, success=False),
        _classifier_full(job_id, ts_offset=25, attempt=1),
        _builder_full(job_id, ts_offset=30, attempt=2),
        _executor_full(job_id, ts_offset=40, success=True),
        _reviewer_full(job_id, ts_offset=50, attempt=2),
        _final_result_full(job_id, ts_offset=60),
    ]


def _no_retry_job_minimal_terminal(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _engine_full(job_id),
        _builder_full(job_id, ts_offset=10),
        _executor_full(job_id, ts_offset=20),
        _reviewer_full(job_id, ts_offset=30),
        _final_result_minimal(job_id, ts_offset=40),
    ]



def test_all_export_gap_fields_are_covered_by_section_checks():
    covered = set()
    for _, (_, keys) in _SECTION_FIELD_CHECKS.items():
        covered.update(keys)
    missing = [f for f, _ in EXPORT_FIELD_GAPS if f not in covered]
    assert missing == [], f"Fields not in any section check: {missing}"



def test_full_payload_contains_the_durable_final_result(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-a", _no_retry_job_full("job-a"))
    events = EventLog("job-a", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")
    fr = events[0].payload["final_result"]
    assert isinstance(fr, dict) and fr["schema_version"] == 1
    assert fr["status"] in {"succeeded", "failed"}


def test_full_payload_contains_the_rendered_terminal_message(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-b", _no_retry_job_full("job-b"))
    events = EventLog("job-b", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")
    assert isinstance(events[0].payload["terminal_message"], str)
    assert events[0].payload["terminal_message"]


def test_the_terminal_record_carries_no_model_prompt_or_response(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-c", _no_retry_job_full("job-c"))
    p = EventLog("job-c", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")[0].payload
    for forbidden in ("outcome_input_message", "outcome_system_snapshot",
                      "outcome_full_response", "system_snapshot", "messages"):
        assert forbidden not in p, f"terminal record carries model-episode field {forbidden!r}"


def test_api_failure_terminal_record_still_has_all_content_fields(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-af", [
        _intake_full("job-af"),
        _engine_full("job-af"),
        _builder_full("job-af", ts_offset=10),
        _executor_full("job-af", ts_offset=20),
        _reviewer_full("job-af", ts_offset=30),
        _final_result_full("job-af", ts_offset=40, api_failure=True),
    ])
    events = EventLog("job-af", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")
    p = events[0].payload
    assert p["api_failure"] is True
    for field in _TERMINAL_CONTENT_FIELDS:
        assert field in p, f"Missing terminal field on api_failure path: {field}"
    for field in _LABELS_FIELDS:
        assert field in p, f"Missing labels field on api_failure path: {field}"



def test_full_payload_contains_the_geometry_source(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-d", _no_retry_job_full("job-d"))
    events = EventLog("job-d", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")
    assert "geometry_source" in events[0].payload
    assert events[0].payload["geometry_source"]["sha256"] == _GEOMETRY_SOURCE["sha256"]


def test_full_payload_contains_agent_model_configs(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-f", _no_retry_job_full("job-f"))
    events = EventLog("job-f", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")
    assert "agent_model_configs" in events[0].payload
    assert isinstance(events[0].payload["agent_model_configs"], dict)


def test_agent_model_configs_dict_is_preserved(capture_authority, tmp_path):
    configs = {"builder": "claude-opus-4-7", "reviewer": "kimi-k2", "classifier": "deepseek-v3"}
    seed_events(capture_authority, CAPTURE_OWNER, "job-mc", [
        _intake_full("job-mc"),
        _engine_full("job-mc"),
        _builder_full("job-mc"),
        _executor_full("job-mc"),
        _reviewer_full("job-mc"),
        _final_result_full("job-mc", agent_model_configs=configs),
    ])
    events = EventLog("job-mc", owner_id=CAPTURE_OWNER).events_of_type("final_result_built")
    assert events[0].payload["agent_model_configs"] == configs



def test_terminal_coverage_complete_when_all_fields_present(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cc", _no_retry_job_full("job-cc"))
    report = EventLog("job-cc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["final_result"] == "complete"


def test_labels_coverage_complete_when_all_fields_present(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-lc", _no_retry_job_full("job-lc"))
    report = EventLog("job-lc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["labels"] == "complete"


def test_terminal_coverage_partial_when_content_fields_missing(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cp", _no_retry_job_minimal_terminal("job-cp"))
    report = EventLog("job-cp", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["final_result"] == "partial"


def test_labels_coverage_partial_when_labels_fields_missing(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-lp", _no_retry_job_minimal_terminal("job-lp"))
    report = EventLog("job-lp", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["labels"] == "partial"



def test_intake_coverage_still_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ic", _no_retry_job_full("job-ic"))
    assert EventLog("job-ic", owner_id=CAPTURE_OWNER).validate_completeness().coverage["intake"] == "complete"


def test_builder_coverage_still_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-bc", _no_retry_job_full("job-bc"))
    assert EventLog("job-bc", owner_id=CAPTURE_OWNER).validate_completeness().coverage["builder_attempts"] == "complete"


def test_executor_coverage_still_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ec", _no_retry_job_full("job-ec"))
    assert EventLog("job-ec", owner_id=CAPTURE_OWNER).validate_completeness().coverage["executor"] == "complete"


def test_reviewer_coverage_still_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-rc", _no_retry_job_full("job-rc"))
    assert EventLog("job-rc", owner_id=CAPTURE_OWNER).validate_completeness().coverage["reviewer"] == "complete"


def test_classifier_na_for_no_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-na", _no_retry_job_full("job-na"))
    assert EventLog("job-na", owner_id=CAPTURE_OWNER).validate_completeness().coverage["classifier"] == "n/a"



def test_zero_gaps_for_fully_covered_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-z", _all_phases_retry_job("job-z"))
    report = EventLog("job-z", owner_id=CAPTURE_OWNER).validate_completeness()
    assert len(report.gaps) == 0


def test_no_gaps_for_fully_covered_no_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-5g", _no_retry_job_full("job-5g"))
    report = EventLog("job-5g", owner_id=CAPTURE_OWNER).validate_completeness()
    assert len(report.gaps) == 0


def test_no_classifier_gaps_for_no_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cg", _no_retry_job_full("job-cg"))
    report = EventLog("job-cg", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "classifier:" not in gap_text
    assert "terminal:"   not in gap_text
    assert "reviewer:"   not in gap_text
    assert "executor:"   not in gap_text
    assert "builder:"    not in gap_text
    assert "intake:"     not in gap_text
    assert "labels/"     not in gap_text
    assert "record:"     not in gap_text



def test_is_export_ready_true_for_fully_covered_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-er", _all_phases_retry_job("job-er"))
    report = EventLog("job-er", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is True
    assert len(report.gaps) == 0


def test_is_export_ready_true_for_no_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ef", _no_retry_job_full("job-ef"))
    report = EventLog("job-ef", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is True
    assert len(report.gaps) == 0


def test_is_export_ready_false_when_terminal_fields_missing(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-em", _no_retry_job_minimal_terminal("job-em"))
    report = EventLog("job-em", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is False


def test_is_export_ready_false_when_any_builder_attempt_missing_fields(capture_authority, tmp_path):
    events = [
        _intake_full("job-mb"),
        _engine_full("job-mb"),
        _builder_full("job-mb", ts_offset=10, attempt=1),
        _executor_full("job-mb", ts_offset=20, success=False),
        _classifier_full("job-mb", ts_offset=25, attempt=1),
        {
            "timestamp": _ts(30), "job_id": "job-mb",
            "event_type": "builder_attempt", "attempt": 2,
            "payload": {"mode": "retry", "executor_success": True},
        },
        _executor_full("job-mb", ts_offset=40),
        _reviewer_full("job-mb", ts_offset=50, attempt=2),
        _final_result_full("job-mb", ts_offset=60),
    ]
    seed_events(capture_authority, CAPTURE_OWNER, "job-mb", events)
    report = EventLog("job-mb", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is False
    assert report.coverage["builder_attempts"] == "partial"



def test_format_report_shows_all_sections_complete_for_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr", _all_phases_retry_job("job-fr"))
    report = EventLog("job-fr", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert "final_result         complete" in text
    assert "labels               complete" in text
    assert "Export-ready: True"            in text


def test_format_report_shows_zero_gaps_for_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fz", _all_phases_retry_job("job-fz"))
    report = EventLog("job-fz", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert "0 of 31 fields" in text


