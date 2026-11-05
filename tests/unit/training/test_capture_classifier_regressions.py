# Responsibility: Verify the classifier's captured payload carries the declared gate and its verbatim feedback.
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

# The classifier is DETERMINISTIC - it makes no model call, so there are no LLM artifacts
# to capture. What the corpus records is the DECLARED signal it routed on.
_CLASSIFIER_CONTENT_FIELDS = frozenset({
    "failed_gate",
    "failed_axes",
})


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
            "summary":       "[QUALITY] min SICN 0.03 below the 0.1 floor - reduce size.value",
            "error_source":  "executor_fail",
            "failed_gate":   "sicn_floor",
            "failed_axes":   [],
            "builder_mode":  "retry",
            "deterministic": True,
        },
    }


def _classifier_run_phase1(job_id: str, ts_offset: float = 25, attempt: int = 1) -> dict:
    return {
        "timestamp": _ts(ts_offset), "job_id": job_id,
        "event_type": "classifier_run", "attempt": attempt,
        "payload": {
            "section":      "MESH",
            "error_source": "executor_fail",
        },
    }


def _retry_job_full(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _builder_full(job_id, ts_offset=10, attempt=1),
        _executor_full(job_id, ts_offset=20, success=False),
        _classifier_run_full(job_id, ts_offset=25, attempt=1),
        _builder_full(job_id, ts_offset=30, attempt=2),
        _executor_full(job_id, ts_offset=40, success=True),
        {"timestamp": _ts(50), "job_id": job_id, "event_type": "reviewer_run",
         "attempt": 2, "payload": {"verdict": "PASS", "tool_calls": 7}},
        {"timestamp": _ts(60), "job_id": job_id, "event_type": "final_result_built",
         "payload": {"closing_len": 150, "api_failure": False}},
    ]


def _retry_job_phase1_classifier(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _builder_full(job_id, ts_offset=10, attempt=1),
        _executor_full(job_id, ts_offset=20, success=False),
        _classifier_run_phase1(job_id, ts_offset=25, attempt=1),
        _builder_full(job_id, ts_offset=30, attempt=2),
        _executor_full(job_id, ts_offset=40, success=True),
        {"timestamp": _ts(50), "job_id": job_id, "event_type": "reviewer_run",
         "attempt": 2, "payload": {"verdict": "PASS", "tool_calls": 7}},
        {"timestamp": _ts(60), "job_id": job_id, "event_type": "final_result_built",
         "payload": {"closing_len": 150, "api_failure": False}},
    ]


def _successful_no_retry_job(job_id: str) -> list[dict]:
    return [
        _intake_full(job_id),
        _builder_full(job_id, ts_offset=10),
        _executor_full(job_id, ts_offset=20, success=True),
        {"timestamp": _ts(30), "job_id": job_id, "event_type": "reviewer_run",
         "attempt": 1, "payload": {"verdict": "PASS", "tool_calls": 7}},
        {"timestamp": _ts(40), "job_id": job_id, "event_type": "final_result_built",
         "payload": {"closing_len": 150, "api_failure": False}},
    ]


def test_executor_path_captures_the_declared_gate(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-a", _retry_job_full("job-a"))
    p = EventLog("job-a", owner_id=CAPTURE_OWNER).events_of_type("classifier_run")[0].payload
    assert p["failed_gate"] == "sicn_floor"
    assert p["failed_axes"] == []


def test_summary_is_the_verbatim_gate_feedback_not_a_resummary(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-b", _retry_job_full("job-b"))
    p = EventLog("job-b", owner_id=CAPTURE_OWNER).events_of_type("classifier_run")[0].payload
    assert p["summary"].startswith("[QUALITY] min SICN 0.03")
    assert "reduce size.value" in p["summary"]


def test_routing_decision_is_captured(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-c", _retry_job_full("job-c"))
    p = EventLog("job-c", owner_id=CAPTURE_OWNER).events_of_type("classifier_run")[0].payload
    assert p["builder_mode"] == "retry"
    assert p["deterministic"] is True


def test_no_llm_artifacts_are_captured(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-d", _retry_job_full("job-d"))
    p = EventLog("job-d", owner_id=CAPTURE_OWNER).events_of_type("classifier_run")[0].payload
    for dead in ("classifier_input_messages", "classifier_system_snapshots",
                 "classifier_full_responses", "classifier_usages", "finish_reason"):
        assert dead not in p


def test_full_payload_contains_section_and_error_source(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-f", _retry_job_full("job-f"))
    p = EventLog("job-f", owner_id=CAPTURE_OWNER).events_of_type("classifier_run")[0].payload
    assert "section"      in p
    assert "error_source" in p


def test_reviewer_fail_path_has_all_content_fields(capture_authority, tmp_path):
    ev = _classifier_run_full("job-rv")
    ev["payload"]["error_source"] = "reviewer_fail"
    ev["payload"]["failed_gate"]  = ""
    ev["payload"]["failed_axes"]  = ["prism_layer_coverage"]
    ev["payload"]["summary"]      = "Layers collapsed on the trailing edge."
    seed_events(capture_authority, CAPTURE_OWNER, "job-rv", [_intake_full("job-rv"), _builder_full("job-rv"), _executor_full("job-rv"),
            ev,
            {"timestamp": _ts(40), "job_id": "job-rv", "event_type": "reviewer_run",
             "attempt": 2, "payload": {"verdict": "PASS", "tool_calls": 5}},
            {"timestamp": _ts(50), "job_id": "job-rv", "event_type": "final_result_built",
             "payload": {"closing_len": 100, "api_failure": False}}])
    p = EventLog("job-rv", owner_id=CAPTURE_OWNER).events_of_type("classifier_run")[0].payload
    for field in _CLASSIFIER_CONTENT_FIELDS:
        assert field in p, f"Missing field: {field}"
    assert p["failed_axes"] == ["prism_layer_coverage"]


def test_classifier_coverage_complete_when_all_fields_present(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cc", _retry_job_full("job-cc"))
    report = EventLog("job-cc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["classifier"] == "complete"


def test_classifier_coverage_partial_when_content_fields_missing(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cp", _retry_job_phase1_classifier("job-cp"))
    report = EventLog("job-cp", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["classifier"] == "partial"


def test_classifier_coverage_na_when_no_classifier_events(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-na", _successful_no_retry_job("job-na"))
    report = EventLog("job-na", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["classifier"] == "n/a"


def test_no_retry_job_classifier_not_in_missing_event_types(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-nm", _successful_no_retry_job("job-nm"))
    report = EventLog("job-nm", owner_id=CAPTURE_OWNER).validate_completeness()
    assert "classifier_run" not in report.missing_event_types


def test_classifier_gaps_removed_when_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-gr", _retry_job_full("job-gr"))
    report = EventLog("job-gr", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "classifier: full LLM input"      not in gap_text
    assert "classifier: system prompt"        not in gap_text
    assert "classifier: full LLM response"    not in gap_text
    assert "classifier: token usage"          not in gap_text


def test_non_classifier_gaps_unchanged_after_classifier_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ng", _retry_job_full("job-ng"))
    report = EventLog("job-ng", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "reviewer: full tool call"         in gap_text
    assert "reviewer: reasoning chain"        in gap_text
    assert "reviewer: full response"          in gap_text
    assert "terminal: the durable final_result" in gap_text
    assert "terminal: the rendered user-facing" in gap_text
    assert "labels/record: geometry source identity"    in gap_text
    assert "record: agent model configs"      in gap_text


def test_a_metadata_only_classifier_event_closes_no_gap(capture_authority, tmp_path):
    # The same corpus twice, differing only in whether the classifier event carries its content
    # fields. Comparing the gap SETS states the claim without a constant to maintain.
    seed_events(capture_authority, CAPTURE_OWNER, "job-p1", _retry_job_phase1_classifier("job-p1"))
    seed_events(capture_authority, CAPTURE_OWNER, "job-full", _retry_job_full("job-full"))
    thin = set(EventLog("job-p1", owner_id=CAPTURE_OWNER).validate_completeness().gaps)
    full = set(EventLog("job-full", owner_id=CAPTURE_OWNER).validate_completeness().gaps)
    assert full < thin, "the content fields closed no gap"
    assert {g for g in thin - full if "classifier" in g} == thin - full


def test_is_export_ready_false_even_with_four_phases_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-nf", _retry_job_full("job-nf"))
    report = EventLog("job-nf", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is False
    assert len(report.gaps) > 0


def test_format_report_shows_classifier_complete(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr", _retry_job_full("job-fr"))
    report = EventLog("job-fr", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert "classifier" in text
    assert "complete" in text


def test_format_report_shows_correct_remaining_gap_count(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fc", _retry_job_full("job-fc"))
    report = EventLog("job-fc", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert str(len(report.gaps)) in text


def test_format_report_shows_na_for_no_retry_job(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fna", _successful_no_retry_job("job-fna"))
    report = EventLog("job-fna", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert "n/a" in text
