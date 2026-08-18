# Responsibility: Verify the event log loads, filters and groups by attempt, skipping malformed lines.
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.capture_authority import seed_events
from tests.unit.training._jsonl_projection import log_from_path

from meshpipeline.capture.events import (
    EXPORT_FIELD_GAPS,
    REQUIRED_EVENT_TYPES,
    EventLog,
    format_report,
)

CAPTURE_OWNER = "owner-training-tests"


def _ts(offset_s: float = 0.0) -> str:
    return (datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            + timedelta(seconds=offset_s)).isoformat()


def _event(event_type: str, payload: dict | None = None,
           attempt: int | None = None, job_id: str = "job-x",
           ts_offset: float = 0.0) -> dict:
    e: dict = {
        "timestamp": _ts(ts_offset),
        "job_id":    job_id,
        "event_type": event_type,
        "payload":   payload or {},
    }
    if attempt is not None:
        e["attempt"] = attempt
    return e


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


def _successful_job(job_id: str = "job-pass") -> list[dict]:
    return [
        _event("intake_complete", {
            "domain": "aero", "request_txt_len": 200,
            "review_brief_txt_len": 80, "turns_count": 4,
            "max_turns_reached": False, "finish_reason": "stop",
        }, job_id=job_id, ts_offset=0),
        _event("engine_select_run", {"chosen": "cfmesh", "source": "selector"}, job_id=job_id, ts_offset=1),
        _event("builder_attempt", {
            "mode": "initial", "executor_success": True,
            "noop": False, "noop_count": 0, "tool_calls": 12, "response_len": 300,
        }, attempt=1, job_id=job_id, ts_offset=10),
        _event("executor_run", {
            "success": True, "output_len": 1800, "workspace": f"/ws/{job_id}/attempt_1",
        }, job_id=job_id, ts_offset=20),
        _event("reviewer_run", {
            "verdict": "PASS", "tool_calls": 7,
        }, attempt=1, job_id=job_id, ts_offset=30),
        _event("final_result_built", {
            "closing_len": 180, "api_failure": False,
        }, job_id=job_id, ts_offset=40),
    ]


def _retry_job(job_id: str = "job-retry") -> list[dict]:
    return [
        _event("intake_complete", {
            "domain": "aero", "request_txt_len": 150,
            "review_brief_txt_len": 60, "turns_count": 3,
            "max_turns_reached": False, "finish_reason": "stop",
        }, job_id=job_id, ts_offset=0),
        _event("engine_select_run", {"chosen": "cfmesh", "source": "selector"}, job_id=job_id, ts_offset=1),
        _event("builder_attempt", {
            "mode": "initial", "executor_success": False,
            "noop": False, "noop_count": 0, "tool_calls": 14, "response_len": 250,
        }, attempt=1, job_id=job_id, ts_offset=10),
        _event("executor_run", {
            "success": False, "output_len": 400, "workspace": f"/ws/{job_id}/attempt_1",
        }, job_id=job_id, ts_offset=20),
        _event("classifier_run", {
            "section": "MESH", "error_source": "executor_error",
            "finish_reason": "stop",
        }, attempt=1, job_id=job_id, ts_offset=25),
        _event("builder_attempt", {
            "mode": "retry", "executor_success": True,
            "noop": False, "noop_count": 0, "tool_calls": 10, "response_len": 220,
        }, attempt=2, job_id=job_id, ts_offset=35),
        _event("executor_run", {
            "success": True, "output_len": 2100, "workspace": f"/ws/{job_id}/attempt_2",
        }, job_id=job_id, ts_offset=45),
        _event("reviewer_run", {
            "verdict": "PASS", "tool_calls": 9,
        }, attempt=2, job_id=job_id, ts_offset=55),
        _event("final_result_built", {
            "closing_len": 165, "api_failure": False,
        }, job_id=job_id, ts_offset=60),
    ]



def test_load_returns_empty_when_file_missing(tmp_path):
    log = EventLog("job-missing", owner_id=CAPTURE_OWNER)
    assert log.load() == []


def test_load_returns_all_valid_events(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ok", _successful_job("job-ok"))
    log = EventLog("job-ok", owner_id=CAPTURE_OWNER)
    events = log.load()
    assert len(events) == 6   # incl. engine_select_run


def test_load_parses_event_fields(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-p", _successful_job("job-p"))
    events = EventLog("job-p", owner_id=CAPTURE_OWNER).load()
    first = events[0]
    assert first.event_type == "intake_complete"
    assert first.job_id == "job-p"
    assert first.attempt is None
    assert isinstance(first.timestamp, datetime)


def test_load_parses_attempt_field(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-a", _successful_job("job-a"))
    events = EventLog("job-a", owner_id=CAPTURE_OWNER).load()
    builder_event = next(e for e in events if e.event_type == "builder_attempt")
    assert builder_event.attempt == 1


def test_load_is_cached(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-c", _successful_job("job-c"))
    log = EventLog("job-c", owner_id=CAPTURE_OWNER)
    first_call  = log.load()
    second_call = log.load()
    assert first_call is second_call



def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "job-bad" / "events.jsonl"
    path.parent.mkdir()
    def _uni(e, i):
        return json.dumps({"schema_version": 2, "record_type": "span_event",
                           "trace_id": e.get("job_id", ""), "span_id": f"s{i}",
                           "parent_span_id": None, "seq": i, "ts": e["timestamp"],
                           "name": e["event_type"], "kind": "event",
                           "component": "event",
                           "attributes": ({"attempt": e["attempt"]}
                                          if e.get("attempt") is not None else {}),
                           "payload": e.get("payload", {})})
    with path.open("w") as f:
        f.write(_uni(_event("intake_complete"), 0) + "\n")
        f.write("NOT VALID JSON{\n")
        f.write(_uni(_event("builder_attempt", attempt=1), 1) + "\n")
        f.write("{missing_timestamp: true}\n")
        f.write(_uni(_event("final_result_built"), 2) + "\n")
    events = log_from_path("job-bad", path).load()
    assert len(events) == 3
    assert {e.event_type for e in events} == {"intake_complete", "builder_attempt", "final_result_built"}


def test_entirely_malformed_file_returns_empty(tmp_path):
    path = tmp_path / "job-junk" / "events.jsonl"
    path.parent.mkdir()
    path.write_text("garbage\nmore garbage\n{}\n")
    events = EventLog("job-junk", owner_id=CAPTURE_OWNER).load()
    assert events == []


def test_blank_lines_are_skipped_gracefully(tmp_path):
    path = tmp_path / "job-blank" / "events.jsonl"
    path.parent.mkdir()
    with path.open("w") as f:
        f.write("\n")
        e = _event("final_result_built")
        f.write(json.dumps({"schema_version": 2, "record_type": "span_event",
                            "trace_id": e.get("job_id", ""), "span_id": "s0",
                            "parent_span_id": None, "seq": 0, "ts": e["timestamp"],
                            "name": e["event_type"], "kind": "event",
                            "component": "event", "attributes": {},
                            "payload": e.get("payload", {})}) + "\n")
        f.write("\n\n")
    events = log_from_path("job-blank", path).load()
    assert len(events) == 1



def test_events_of_type_filters_correctly(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-f", _retry_job("job-f"))
    log = EventLog("job-f", owner_id=CAPTURE_OWNER)
    builder_events = log.events_of_type("builder_attempt")
    assert len(builder_events) == 2
    assert all(e.event_type == "builder_attempt" for e in builder_events)


def test_events_of_type_returns_empty_for_absent_type(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-g", _successful_job("job-g"))
    log = EventLog("job-g", owner_id=CAPTURE_OWNER)
    assert log.events_of_type("classifier_run") == []



def test_events_grouped_by_attempt(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-grp", _retry_job("job-grp"))
    log = EventLog("job-grp", owner_id=CAPTURE_OWNER)
    groups = log.events_by_attempt()
    assert 1 in groups
    assert any(e.event_type == "builder_attempt" for e in groups[1])
    assert any(e.event_type == "classifier_run"  for e in groups[1])
    assert 2 in groups
    assert any(e.event_type == "reviewer_run" for e in groups[2])


def test_events_without_attempt_go_into_bucket_zero(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-z", _successful_job("job-z"))
    log = EventLog("job-z", owner_id=CAPTURE_OWNER)
    groups = log.events_by_attempt()
    assert 0 in groups
    types_in_zero = {e.event_type for e in groups[0]}
    assert "intake_complete" in types_in_zero
    assert "final_result_built" in types_in_zero


def test_three_attempt_job_has_three_attempt_buckets(capture_authority, tmp_path):
    events = []
    for n in range(1, 4):
        ts = n * 10
        events += [
            _event("builder_attempt", {}, attempt=n, ts_offset=ts),
            _event("executor_run",    {}, ts_offset=ts + 1),
        ]
        if n < 3:
            events.append(_event("classifier_run", {}, attempt=n, ts_offset=ts + 2))
    events.append(_event("reviewer_run", {}, attempt=3, ts_offset=35))
    events.append(_event("final_result_built",   {}, ts_offset=40))

    seed_events(capture_authority, CAPTURE_OWNER, "job-3a", events)
    groups = EventLog("job-3a", owner_id=CAPTURE_OWNER).events_by_attempt()
    assert set(groups.keys()) >= {1, 2, 3}



def test_successful_job_has_no_missing_event_types(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ok2", _successful_job("job-ok2"))
    report = EventLog("job-ok2", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.missing_event_types == []


def test_successful_job_attempt_count_is_one(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ok3", _successful_job("job-ok3"))
    report = EventLog("job-ok3", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.attempt_count == 1


def test_successful_job_ordering_valid(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-ord", _successful_job("job-ord"))
    report = EventLog("job-ord", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.ordering_valid is True


def test_successful_job_coverage_intake_partial(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cov", _successful_job("job-cov"))
    report = EventLog("job-cov", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["intake"] == "partial"


def test_successful_job_coverage_classifier_na(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-nc", _successful_job("job-nc"))
    report = EventLog("job-nc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["classifier"] == "n/a"



def test_retry_job_attempt_count_is_two(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-r2", _retry_job("job-r2"))
    report = EventLog("job-r2", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.attempt_count == 2


def test_retry_job_classifier_coverage_partial(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-rc", _retry_job("job-rc"))
    report = EventLog("job-rc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["classifier"] == "partial"


def test_retry_job_has_no_missing_event_types(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-rm", _retry_job("job-rm"))
    report = EventLog("job-rm", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.missing_event_types == []



def test_missing_builder_and_executor_detected(capture_authority, tmp_path):
    events = [
        _event("intake_complete"),
        _event("final_result_built"),
    ]
    seed_events(capture_authority, CAPTURE_OWNER, "job-mis", events)
    report = EventLog("job-mis", owner_id=CAPTURE_OWNER).validate_completeness()
    missing = set(report.missing_event_types)
    assert "builder_attempt" in missing
    assert "executor_run"    in missing
    assert "reviewer_run"    in missing


def test_missing_intake_detected(capture_authority, tmp_path):
    events = [
        _event("builder_attempt", attempt=1),
        _event("executor_run"),
        _event("reviewer_run", attempt=1),
        _event("final_result_built"),
    ]
    seed_events(capture_authority, CAPTURE_OWNER, "job-noi", events)
    report = EventLog("job-noi", owner_id=CAPTURE_OWNER).validate_completeness()
    assert "intake_complete" in report.missing_event_types


def test_empty_log_has_all_required_types_missing(tmp_path):
    path = tmp_path / "job-empty" / "events.jsonl"
    path.parent.mkdir()
    path.write_text("")
    report = EventLog("job-empty", owner_id=CAPTURE_OWNER).validate_completeness()
    assert set(report.missing_event_types) == REQUIRED_EVENT_TYPES



def test_ordering_valid_for_ascending_timestamps(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-asc", _successful_job("job-asc"))
    report = EventLog("job-asc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.ordering_valid is True


def test_ordering_invalid_for_out_of_order_timestamps():
    events = [
        _event("intake_complete",  ts_offset=20),
        _event("builder_attempt",  ts_offset=10, attempt=1),
        _event("executor_run",     ts_offset=30),
        _event("reviewer_run",     ts_offset=40, attempt=1),
        _event("final_result_built",       ts_offset=50),
    ]
    # Ordering is a property of the EVENT STREAM, so it is checked over the records themselves.
    # The authority stamps rows as they are inserted, so seeding through it would always produce a
    # well-ordered result and the assertion would pass vacuously.
    report = EventLog.from_records("job-ood", [
        {"record_type": "span_event", "trace_id": "job-ood", "ts": e["timestamp"],
         "name": e["event_type"],
         "attributes": ({"attempt": e["attempt"]} if e.get("attempt") is not None else {}),
         "payload": e.get("payload", {})}
        for e in events
    ]).validate_completeness()
    assert report.ordering_valid is False



def test_is_export_ready_false_for_metadata_only_events(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-er", _successful_job("job-er"))
    report = EventLog("job-er", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is False


def test_is_export_ready_false_when_content_gaps_remain(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-er2", _successful_job("job-er2"))
    report = EventLog("job-er2", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.is_export_ready is False
    assert report.missing_event_types == []



def test_gaps_list_covers_all_non_classifier_export_field_gaps(capture_authority, tmp_path):
    from meshpipeline.capture.events import _SECTION_FIELD_CHECKS
    # excused for this fixture: classifier never ran, the planner is excused on
    # a non-snappy run, and the engine_select fields ARE covered by its event
    _excused = (_SECTION_FIELD_CHECKS["classifier"][1]
                | _SECTION_FIELD_CHECKS["planner"][1]
                | _SECTION_FIELD_CHECKS["engine_select"][1])
    seed_events(capture_authority, CAPTURE_OWNER, "job-gl", _successful_job("job-gl"))
    report = EventLog("job-gl", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_set = set(report.gaps)
    for fname, desc in EXPORT_FIELD_GAPS:
        if fname in _excused:
            continue
        assert desc in gap_set, f"Expected gap description not in report: {desc!r}"


def test_gaps_list_length_equals_export_field_gaps_minus_classifier(capture_authority, tmp_path):
    from meshpipeline.capture.events import _SECTION_FIELD_CHECKS
    _excused = (_SECTION_FIELD_CHECKS["classifier"][1]       # never ran
                | _SECTION_FIELD_CHECKS["planner"][1]         # non-snappy run
                | _SECTION_FIELD_CHECKS["engine_select"][1])  # covered by event
    seed_events(capture_authority, CAPTURE_OWNER, "job-gl2", _successful_job("job-gl2"))
    report = EventLog("job-gl2", owner_id=CAPTURE_OWNER).validate_completeness()
    assert len(report.gaps) == len(EXPORT_FIELD_GAPS) - len(_excused)


def test_gaps_include_intake_fields(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-gi", _successful_job("job-gi"))
    report = EventLog("job-gi", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "conversation turns" in gap_text
    assert "system prompt snapshot" in gap_text


def test_gaps_include_builder_fields(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-gb", _successful_job("job-gb"))
    report = EventLog("job-gb", owner_id=CAPTURE_OWNER).validate_completeness()
    gap_text = "\n".join(report.gaps)
    assert "message history" in gap_text
    assert "tool call records" in gap_text



def test_format_report_returns_non_empty_string(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr", _successful_job("job-fr"))
    report = EventLog("job-fr", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert isinstance(text, str)
    assert len(text) > 0


def test_format_report_contains_job_id(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr2", _successful_job("job-fr2"))
    report = EventLog("job-fr2", owner_id=CAPTURE_OWNER).validate_completeness()
    assert "job-fr2" in format_report(report)


def test_format_report_contains_export_ready_line(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr3", _successful_job("job-fr3"))
    report = EventLog("job-fr3", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert "Export-ready: False" in text


def test_format_report_lists_gap_count(capture_authority, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-fr4", _successful_job("job-fr4"))
    report = EventLog("job-fr4", owner_id=CAPTURE_OWNER).validate_completeness()
    text = format_report(report)
    assert str(len(EXPORT_FIELD_GAPS)) in text
