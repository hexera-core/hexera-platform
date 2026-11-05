# Responsibility: Verify a section is complete only with every required field, and conditional gaps are excused.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.capture_authority import seed_events

from meshpipeline.capture.events import (
    _SECTION_FIELD_CHECKS,
    EXPORT_FIELD_GAPS,
    EventLog,
)

CAPTURE_OWNER = "owner-training-tests"

# gap entries are the human descriptions from EXPORT_FIELD_GAPS, keyed here by field
_GAP_DESC = dict(EXPORT_FIELD_GAPS)


def _ts(offset_s: float = 0.0) -> str:
    return (datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            + timedelta(seconds=offset_s)).isoformat()


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i, e in enumerate(events):
            rec = {"schema_version": 2, "record_type": "span_event",
                   "trace_id": e.get("job_id", ""), "span_id": f"s{i:04d}",
                   "parent_span_id": None, "seq": i, "ts": e["timestamp"],
                   "name": e["event_type"], "kind": "event", "component": "event",
                   "attributes": {}, "payload": e.get("payload", {})}
            f.write(json.dumps(rec) + "\n")


def _event_for(section: str, *, drop: str | None = None, job_id: str = "job-cc") -> dict:
    event_type, fields = _SECTION_FIELD_CHECKS[section]
    payload = {f: ("cfmesh" if f == "chosen" else "captured-value")
               for f in fields if f != drop}
    return {"timestamp": _ts(), "job_id": job_id, "event_type": event_type,
            "payload": payload}


@pytest.mark.parametrize("section", sorted(_SECTION_FIELD_CHECKS))
def test_complete_payload_marks_section_complete_and_clears_gaps(capture_authority, section, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-cc", [_event_for(section)])
    report = EventLog("job-cc", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage[section] == "complete"
    _, fields = _SECTION_FIELD_CHECKS[section]
    assert not [f for f in fields if _GAP_DESC.get(f) in report.gaps], report.gaps


# labels coverage derives from the reviewer VERDICT (not the terminal payload), and a
# planner gap is EXCUSED when no driver engine ran - both are the conditional rule
# tested separately below, so the generic partial property applies to the rest.
# A section can only be asserted to NAME a gap if at least one of its fields is gap-registered.
# "accountability" (agent_run) is deliberately reported-but-not-gating, so it has no gap entry and
# is excluded here structurally rather than by name - a new gap-registered section is picked up
# automatically, and a section that stops being gated stops being asserted.
_GAP_REGISTERED = {s for s, (_et, fields) in _SECTION_FIELD_CHECKS.items()
                   if any(f in _GAP_DESC for f in fields)}
_UNCONDITIONAL = sorted(_GAP_REGISTERED - {"labels", "planner"})


@pytest.mark.parametrize("section", _UNCONDITIONAL)
def test_missing_required_field_marks_section_partial_and_names_the_gap(capture_authority, section, tmp_path):
    _, fields = _SECTION_FIELD_CHECKS[section]
    victim = sorted(fields)[0]
    seed_events(capture_authority, CAPTURE_OWNER, "job-cp", [_event_for(section, drop=victim, job_id="job-cp")])
    report = EventLog("job-cp", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage[section] == "partial"
    assert _GAP_DESC.get(victim) in report.gaps, (victim, report.gaps)


def test_conditional_sections_excuse_gaps_only_when_the_node_legitimately_did_not_run(capture_authority, tmp_path):
    # no planner_run, engine_select chose a NON-driver engine → planner gaps excused
    seed_events(capture_authority, CAPTURE_OWNER, "job-x", [_event_for("engine_select", job_id="job-x")])
    report = EventLog("job-x", owner_id=CAPTURE_OWNER).validate_completeness()
    _, planner_fields = _SECTION_FIELD_CHECKS["planner"]
    assert not [f for f in planner_fields if _GAP_DESC.get(f) in report.gaps]
    # a final_result_built WITHOUT labels fields still reports the labels gap (not excused)
    seed_events(capture_authority, CAPTURE_OWNER, "job-y", [_event_for("final_result", job_id="job-y")])
    report = EventLog("job-y", owner_id=CAPTURE_OWNER).validate_completeness()
    assert report.coverage["labels"] == "none"       # no verdict yet
    _, label_fields = _SECTION_FIELD_CHECKS["labels"]
    assert [f for f in label_fields if _GAP_DESC.get(f) in report.gaps]


@pytest.mark.parametrize("section", sorted(_SECTION_FIELD_CHECKS))
def test_sections_are_independent(capture_authority, section, tmp_path):
    seed_events(capture_authority, CAPTURE_OWNER, "job-oa", [_event_for(section, job_id="job-oa")])
    report = EventLog("job-oa", owner_id=CAPTURE_OWNER).validate_completeness()
    for other in _SECTION_FIELD_CHECKS:
        if other != section:
            assert report.coverage.get(other) != "complete", (section, other)


def test_every_export_gap_field_is_owned_by_exactly_one_section():
    owned = [f for _, fields in _SECTION_FIELD_CHECKS.values() for f in fields]
    assert len(owned) == len(set(owned)), "a field is claimed by two sections"
    gap_fields = {fname for fname, _ in EXPORT_FIELD_GAPS}
    assert gap_fields <= set(owned), gap_fields - set(owned)
