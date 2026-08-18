# Responsibility: Verify the reviewer's captured payload carries its verdict, feedback and tool history.
import ast
from pathlib import Path

import pytest
from tests.capture_authority import seed_events
from tests.unit.training._capture_corpus import (
    builder_full as _builder_full,
)
from tests.unit.training._capture_corpus import (
    executor_full as _executor_full,
)
from tests.unit.training._capture_corpus import (
    intake_full as _intake_full,
)
from tests.unit.training._capture_corpus import (
    ts as _ts,
)

from meshpipeline.capture.events import (
    EventLog,
    format_report,
)

CAPTURE_OWNER = "owner-training-tests"

_REVIEWER_CONTENT_FIELDS = frozenset({
    "reviewer_tool_call_histories",
    "reviewer_reasoning_chains",
    "reviewer_full_responses",
    "reviewer_system_snapshots",
    "reviewer_input_texts",
    "reviewer_review_dirs",
})


def _reviewer_run_full(
    job_id: str,
    ts_offset: float = 30,
    attempt: int = 1,
    verdict: str = "PASS",
) -> dict:
    reasoning = (
        "All patches present and correctly sized."
        if verdict == "PASS"
        else "Inlet patch has incorrect face count - domain construction failed."
    )
    reviewer_result = f"<<{verdict}>>\n{reasoning}"
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "reviewer_run", "attempt": attempt,
        "payload": {
            "verdict":    verdict,
            "tool_calls": 8,
            "reviewer_tool_call_histories": [
                {"call_num": 1, "tool": "set_camera_preset", "args": {"preset": "iso"}, "result": "Camera moved to iso."},
                {"call_num": 3, "tool": "submit_findings",   "args": {"reasoning": reasoning}, "result": "Findings accepted."},
            ],
            "reviewer_reasoning_chains":  [reasoning],
            "reviewer_full_responses":    [reviewer_result],
            "reviewer_system_snapshots":  "You are reviewer...",
            "reviewer_input_texts":       "Domain bbox: X -5→15 m...\nMESH INFO:\n  Geometry: wing.step",
            "reviewer_review_dirs":       f"/ws/{job_id}/review_{attempt}",
        },
    }


def _reviewer_run_phase1(job_id: str, ts_offset: float = 30, attempt: int = 1) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "reviewer_run", "attempt": attempt,
        "payload": {
            "verdict":    "PASS",
            "tool_calls": 8,
        },
    }


def _successful_job_full_reviewer(job_id: str, verdict: str = "PASS") -> list[dict]:
    return [
        _intake_full(job_id),
        _builder_full(job_id, ts_offset=10),
        _executor_full(job_id, ts_offset=20),
        _reviewer_run_full(job_id, ts_offset=30, verdict=verdict),
        {"timestamp": _ts(40), "job_id": job_id, "event_type": "final_result_built",
         "payload": {"closing_len": 150, "api_failure": False}},
    ]


def _successful_job_phase1_reviewer(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _builder_full(job_id, ts_offset=10),
        _executor_full(job_id, ts_offset=20),
        _reviewer_run_phase1(job_id, ts_offset=30),
        {"timestamp": _ts(40), "job_id": job_id, "event_type": "final_result_built",
         "payload": {"closing_len": 150, "api_failure": False}},
    ]


def _classifier_run_full(
    job_id: str,
    ts_offset: float = 25,
    attempt: int = 1,
) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "classifier_run", "attempt": attempt,
        "payload": {
            "section":       "MESH",
            "error_source":  "executor_fail",
            "summary":       "fix surface",
            "failed_gate":   "manifest_valid",
            "failed_axes":   [],
            "builder_mode":  "retry",
            "deterministic": True,
        },
    }


def _all_phases_retry_job(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _builder_full(job_id, ts_offset=10, attempt=1),
        _executor_full(job_id, ts_offset=20, success=False),
        _classifier_run_full(job_id, ts_offset=25, attempt=1),
        _builder_full(job_id, ts_offset=30, attempt=2),
        _executor_full(job_id, ts_offset=40, success=True),
        _reviewer_run_full(job_id, ts_offset=50, attempt=2, verdict="PASS"),
        {"timestamp": _ts(60), "job_id": job_id, "event_type": "final_result_built",
         "payload": {"closing_len": 150, "api_failure": False}},
    ]

@pytest.mark.parametrize("field, typ", [
    ("reviewer_tool_call_histories", list),
    ("reviewer_reasoning_chains", list),
    ("reviewer_full_responses", list),
    ("reviewer_system_snapshots", str),
    ("reviewer_input_texts", str),
    ("reviewer_review_dirs", str),
])
def test_reviewer_run_payload_captures(capture_authority, field, typ, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job", _successful_job_full_reviewer("job"))
    payload = EventLog("job", owner_id=CAPTURE_OWNER).events_of_type("reviewer_run")[0].payload
    assert field in payload, f"reviewer_run capture dropped required field {field!r}"
    assert isinstance(payload[field], typ)


def test_pass_review_payload_has_verdict_pass(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-pass", _successful_job_full_reviewer("job-pass", verdict="PASS"))
    events = EventLog("job-pass", owner_id=CAPTURE_OWNER).events_of_type("reviewer_run")
    p = events[0].payload
    assert p["verdict"] == "PASS"
    assert "<<PASS>>" in p["reviewer_full_responses"][0]


def test_fail_review_payload_has_verdict_fail_and_feedback(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fail", _successful_job_full_reviewer("job-fail", verdict="FAIL"))
    events = EventLog("job-fail", owner_id=CAPTURE_OWNER).events_of_type("reviewer_run")
    p = events[0].payload
    assert p["verdict"] == "FAIL"
    assert "<<FAIL>>" in p["reviewer_full_responses"][0]
    assert len(p["reviewer_reasoning_chains"][0]) > 0


def test_tool_call_history_records_tool_name_and_result(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-tc", _successful_job_full_reviewer("job-tc"))
    events = EventLog("job-tc", owner_id=CAPTURE_OWNER).events_of_type("reviewer_run")
    records = events[0].payload["reviewer_tool_call_histories"]
    assert any(r["tool"] == "submit_findings" for r in records)


def test_review_dir_path_captured_in_payload(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-rd", _successful_job_full_reviewer("job-rd"))
    events = EventLog("job-rd", owner_id=CAPTURE_OWNER).events_of_type("reviewer_run")
    review_dir = events[0].payload["reviewer_review_dirs"]
    assert "review_" in review_dir


def test_reviewer_coverage_complete_pass_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cc", _successful_job_full_reviewer("job-cc", verdict="PASS"))
    report = EventLog("job-cc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["reviewer"] == "complete"


def test_reviewer_coverage_complete_fail_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cf", _successful_job_full_reviewer("job-cf", verdict="FAIL"))
    report = EventLog("job-cf", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["reviewer"] == "complete"


def test_reviewer_coverage_partial_when_content_fields_missing(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cp", _successful_job_phase1_reviewer("job-cp"))
    report = EventLog("job-cp", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["reviewer"] == "partial"


def test_classifier_coverage_na_for_no_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cna", _successful_job_full_reviewer("job-cna"))
    report = EventLog("job-cna", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["classifier"] == "n/a"


def test_reviewer_gaps_removed_when_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-gr", _successful_job_full_reviewer("job-gr"))
    report = EventLog("job-gr", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "reviewer: full tool call records"    not in gap_text
    assert "reviewer: reasoning chain"           not in gap_text
    assert "reviewer: full response"             not in gap_text
    assert "reviewer: system prompt"             not in gap_text
    assert "reviewer: input context"             not in gap_text
    assert "reviewer: workspace review dir"      not in gap_text


def test_remaining_gaps_are_only_terminal_and_labels(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-rm", _all_phases_retry_job("job-rm"))
    report = EventLog("job-rm", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "terminal: the durable final_result"     in gap_text
    assert "terminal: the rendered user-facing"     in gap_text
    assert "labels/record: geometry source identity"          in gap_text
    assert "record: agent model configs"            in gap_text
    assert "intake:"                                not in gap_text
    assert "builder:"                               not in gap_text
    assert "executor:"                              not in gap_text
    assert "classifier:"                            not in gap_text
    assert "reviewer:"                              not in gap_text


def test_a_metadata_only_reviewer_event_closes_no_gap(capture_authority, tmp_path):
    # As with the classifier: the difference between the thin and full corpora must be exactly
    # the reviewer's own gaps, which says more than a count and needs no maintenance.
    seed_events(capture_authority, CAPTURE_OWNER, "job-p1", _successful_job_phase1_reviewer("job-p1"))
    seed_events(capture_authority, CAPTURE_OWNER, "job-full", _successful_job_full_reviewer("job-full"))
    thin = set(EventLog("job-p1", owner_id=CAPTURE_OWNER).validate_completeness().gaps)
    full = set(EventLog("job-full", owner_id=CAPTURE_OWNER).validate_completeness().gaps)
    assert full < thin, "the content fields closed no gap"
    assert {g for g in thin - full if "reviewer" in g} == thin - full


def test_is_export_ready_false_even_with_five_phases_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-nf", _all_phases_retry_job("job-nf"))
    report = EventLog("job-nf", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is False


def test_format_report_shows_reviewer_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr", _successful_job_full_reviewer("job-fr"))
    report = EventLog("job-fr", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert "reviewer" in text
    assert "complete" in text


def test_format_report_shows_correct_remaining_gap_count(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fc", _all_phases_retry_job("job-fc"))
    report = EventLog("job-fc", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert str(len(report.gaps)) in text


def test_node_reviewer_return_still_contains_reviewer_result():
    _check_reviewer_return_key("reviewer_result")


def test_node_reviewer_return_still_contains_reviewer_verdict():
    _check_reviewer_return_key("reviewer_verdict")


def _check_reviewer_return_key(key: str) -> None:
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "agents" / "reviewer" / "visual.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # node_reviewer now constructs its return via `_reviewer_return(reviewer_verdict=…)` -
        # accept both a bare return-dict literal AND a `_reviewer_return(...)` keyword.
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and k.value == key:
                    return
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_reviewer_return"):
            if any(kw.arg == key for kw in node.keywords):
                return
    raise AssertionError(
        f"reviewer.py: key {key!r} not found in any return dict or _reviewer_return(...) call - "
        "PipelineState capture may have been removed"
    )
