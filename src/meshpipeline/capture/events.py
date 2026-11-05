# Responsibility: Read a run's captured events back, and report how complete the record is.
# Boundaries: a reader over what was written; completeness is reported, never repaired.
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)



_UNCONDITIONALLY_REQUIRED_EVENT_TYPES: frozenset[str] = frozenset({
    "intake_complete",
    "engine_select_run",
    "builder_attempt",
    "executor_run",
    "final_result_built",
})


def _reviewer_required_for(by_type: dict) -> bool:
    executor_events = by_type.get("executor_run", [])
    if not executor_events:
        return True
    any_solvable = any(bool(e.payload.get("success", False)) for e in executor_events)
    return any_solvable


def _planner_required_for(by_type: dict) -> bool:
    return any(bool(e.payload.get("planner_required"))
               for e in by_type.get("engine_select_run", []))


REQUIRED_EVENT_TYPES = _UNCONDITIONALLY_REQUIRED_EVENT_TYPES | {"reviewer_run"}

EXPORT_FIELD_GAPS: list[tuple[str, str]] = [
    ("intake_turns",                     "intake: full conversation turns JSONL not captured"),
    ("intake_system_snapshot",           "intake: system prompt snapshot not captured"),
    ("intake_llm_metadata",              "intake: per-turn token usage not captured"),
    ("request_txt",                      "intake: full request text not captured"),
    ("review_brief_txt",                 "intake: full review brief text not captured"),
    ("builder_message_histories",        "builder: full message history per attempt not captured"),
    ("builder_tool_call_histories",      "builder: full tool call records per attempt not captured"),
    ("builder_system_message_snapshots", "builder: system prompt per attempt not captured"),
    ("builder_full_responses",           "builder: final response text per attempt not captured"),
    ("executor_stdouts",                 "executor: full stdout per attempt not captured"),
    ("executor_stderrs",                 "executor: full stderr per attempt not captured"),
    ("attempt_log_snapshots",            "executor: attempt_log.txt snapshots not captured"),
    ("failed_gate",                      "classifier: the rejecting gate key not captured"),
    ("failed_axes",                      "classifier: the failing review axes not captured"),
    ("reviewer_tool_call_histories",     "reviewer: full tool call records not captured"),
    ("reviewer_reasoning_chains",        "reviewer: reasoning chain text not captured"),
    ("reviewer_full_responses",          "reviewer: full response text not captured"),
    ("reviewer_system_snapshots",        "reviewer: system prompt per invocation not captured"),
    ("reviewer_input_texts",             "reviewer: input context per invocation not captured"),
    ("reviewer_review_dirs",             "reviewer: workspace review dir paths not captured"),
    ("builder_tools_definition",         "prompts: builder tools schema not captured"),
    ("final_result",                     "terminal: the durable final_result facts not captured"),
    ("terminal_message",                 "terminal: the rendered user-facing message not captured"),
    ("geometry_source",                  "labels/record: geometry source identity not captured"),
    ("agent_model_configs",              "record: agent model configs not captured"),
    ("mesh_manifest",                    "attempt: mesh manifest content not captured"),
    ("chosen",                           "engine_select: chosen engine not captured"),
    ("source",                           "engine_select: selection source not captured"),
    ("system_snapshot",                  "planner: system prompt snapshot not captured"),
    ("response_text",                    "planner: raw response not captured"),
    ("plan",                             "planner: parsed plan not captured"),
]



@dataclass
class TrainingEvent:
    timestamp: datetime
    job_id: str
    event_type: str
    attempt: int | None
    payload: dict


@dataclass
class CompletenessReport:
    job_id: str
    is_export_ready: bool
    event_count: int
    event_count_by_type: dict
    ordering_valid: bool
    missing_event_types: list[str]
    attempt_count: int
    gaps: list[str]
    coverage: dict



_SECTION_FIELD_CHECKS: dict[str, tuple[str, frozenset[str]]] = {
    "intake": ("intake_complete", frozenset({
        "intake_turns",
        "intake_system_snapshot",
        "intake_llm_metadata",
        "request_txt",
        "review_brief_txt",
    })),
    # CANONICAL accountability, emitted by BOTH Builder strategies (interactive loop and
    # engine-owned deterministic driver) and by the Reviewer. Reported, not gating: see
    # tests/unit/capture/test_agent_run_capture.py for the structural reason.
    "accountability": ("agent_run", frozenset({"role", "exit", "tally", "extension"})),
    "builder_attempts": ("builder_attempt", frozenset({
        "builder_message_histories",
        "builder_tool_call_histories",
        "builder_system_message_snapshots",
        "builder_full_responses",
        "builder_tools_definition",
    })),
    "executor": ("executor_run", frozenset({
        "executor_stdouts",
        "executor_stderrs",
        "attempt_log_snapshots",
        "mesh_manifest",
    })),
    # The classifier is DETERMINISTIC - it makes no model call, so there is no LLM
    # interaction to capture. What we record is the DECISION and the declared signals it
    # routed on: the rejecting gate key, and the review axes the reviewer marked failing.
    "classifier": ("classifier_run", frozenset({
        "failed_gate",
        "failed_axes",
    })),
    "engine_select": ("engine_select_run", frozenset({
        "chosen",
        "source",
    })),
    "planner": ("planner_run", frozenset({
        "system_snapshot",
        "response_text",
        "plan",
    })),
    "reviewer": ("reviewer_run", frozenset({
        "reviewer_tool_call_histories",
        "reviewer_reasoning_chains",
        "reviewer_full_responses",
        "reviewer_system_snapshots",
        "reviewer_input_texts",
        "reviewer_review_dirs",
    })),
    # The terminal group is DETERMINISTIC, not an LLM episode: no model composes the final
    # verdict, so there is no prompt/response pair to capture - only the durable facts and the
    # message rendered from them.
    "final_result": ("final_result_built", frozenset({
        "final_result",
        "terminal_message",
    })),
    "labels": ("final_result_built", frozenset({
        "geometry_source",
        "agent_model_configs",
    })),
}



class EventLog:

    def __init__(self, job_id: str, owner_id: str = "",
                 execution_generation: int | None = None) -> None:
        self.job_id = job_id
        self.owner_id = owner_id
        self.execution_generation = execution_generation
        self._events: list[TrainingEvent] | None = None

    @classmethod
    def from_records(cls, job_id: str, records: list[dict]) -> EventLog:
        log = cls(job_id)
        log._events = [ev for ev in (cls._event_from_record(job_id, r) for r in records)
                       if ev is not None]
        return log

    @staticmethod
    def _event_from_record(job_id: str, obj: dict) -> TrainingEvent | None:
        # content rides on span_event records; span_start/span_end are timeline markers
        if obj.get("record_type") != "span_event":
            return None
        attrs = obj.get("attributes") or {}
        ts = obj.get("ts")
        try:
            stamp = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        except ValueError:
            stamp = None
        if not isinstance(stamp, datetime):
            # A record we cannot place in time is dropped, not exported with a fabricated stamp
            # and not allowed to abort the export: one unreadable row must not decide the fate of
            # every other operation in the job.
            logger.warning("EventLog: dropping record %r for job %s - unusable timestamp %r",
                           obj.get("name"), job_id, ts)
            return None
        return TrainingEvent(
            timestamp=stamp,
            job_id=obj.get("trace_id", "") or job_id,
            event_type=obj["name"],
            attempt=attrs.get("attempt"),
            payload=obj.get("payload", {}),
        )

    def load(self) -> list[TrainingEvent]:
        if self._events is not None:
            return self._events
        self._events = []
        if not self.owner_id:
            # Without a tenant there is nothing safe to return: the alternative is reading across
            # owners, and an empty list is the honest answer to "whose events?" with no answer.
            logger.warning("EventLog: no owner scope for job %s - no events loaded", self.job_id)
            return self._events

        from meshpipeline.persistence.repositories import capture_repository as cap
        try:
            rows = cap.trusted_operations(owner_id=self.owner_id, job_id=self.job_id,
                                          execution_generation=self.execution_generation)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EventLog: capture authority unreadable for job %s: %s",
                           self.job_id, exc)
            return self._events

        for row in rows:
            if row["record_type"] != "span_event":
                continue                      # timeline records carry no event content
            self._events.append(TrainingEvent(
                timestamp=row["created_at"],
                job_id=self.job_id,
                event_type=row["name"],
                attempt=row["attempt"],
                payload=row["payload"] or {},
            ))
        return self._events

    def conflicts(self) -> list[dict]:
        if not self.owner_id:
            return []
        from meshpipeline.persistence.repositories import capture_repository as cap
        return cap.conflicted_operations(owner_id=self.owner_id, job_id=self.job_id)

    def events_of_type(self, event_type: str) -> list[TrainingEvent]:
        return [e for e in self.load() if e.event_type == event_type]

    def events_by_attempt(self) -> dict[int, list[TrainingEvent]]:
        groups: dict[int, list[TrainingEvent]] = {}
        for event in self.load():
            key = event.attempt if event.attempt is not None else 0
            groups.setdefault(key, []).append(event)
        return groups

    def validate_completeness(self) -> CompletenessReport:
        events = self.load()
        by_type: dict[str, list[TrainingEvent]] = {}
        for e in events:
            by_type.setdefault(e.event_type, []).append(e)

        timestamps = [e.timestamp for e in events]
        ordering_valid = all(a <= b for a, b in zip(timestamps, timestamps[1:]))

        _required = set(_UNCONDITIONALLY_REQUIRED_EVENT_TYPES)
        if _reviewer_required_for(by_type):
            _required.add("reviewer_run")
        missing = sorted(_required - set(by_type))

        attempt_count = len(by_type.get("builder_attempt", []))

        coverage: dict[str, str] = {
            "intake":           "partial" if ("intake_complete" in by_type or "intake_turn" in by_type) else "none",
            "builder_attempts": "partial" if "builder_attempt" in by_type else "none",
            "executor":         "partial" if "executor_run" in by_type else "none",
            "classifier":       "partial" if "classifier_run" in by_type else "n/a",
            "engine_select":    "partial" if "engine_select_run" in by_type else "none",
            "planner":          "partial" if "planner_run" in by_type
                                else ("none" if _planner_required_for(by_type) else "n/a"),
            "reviewer":         "partial" if "reviewer_run" in by_type else "none",
            "final_result":     "partial" if "final_result_built" in by_type else "none",
        }
        # observability-only surfaces: recorded when they happen, never gate
        # export (a run may legitimately never search or dispute)
        coverage["web_search"] = (str(len(by_type["web_search"])) + " events") \
            if "web_search" in by_type else "n/a"
        coverage["dispute"] = "recorded" if "dispute_context" in by_type else "n/a"
        has_verdict = any(e.payload.get("verdict") for e in by_type.get("reviewer_run", []))
        coverage["labels"] = "partial" if has_verdict else "none"

        _covered_fields: set[str] = set()
        for section, (event_type, required_keys) in _SECTION_FIELD_CHECKS.items():
            section_events = by_type.get(event_type, [])
            if not section_events:
                continue
            if all(required_keys.issubset(frozenset(e.payload.keys()))
                   for e in section_events):
                coverage[section] = "complete"
                _covered_fields.update(required_keys)

        _classifier_fields = _SECTION_FIELD_CHECKS["classifier"][1]
        _classifier_ran = bool(by_type.get("classifier_run"))
        _reviewer_fields = _SECTION_FIELD_CHECKS["reviewer"][1]
        _reviewer_should_have_run = _reviewer_required_for(by_type)
        _planner_fields = _SECTION_FIELD_CHECKS["planner"][1]
        _planner_should_have_run = _planner_required_for(by_type)
        gaps = [
            desc for fname, desc in EXPORT_FIELD_GAPS
            if fname not in _covered_fields
            and not (fname in _classifier_fields and not _classifier_ran)
            and not (fname in _reviewer_fields and not _reviewer_should_have_run)
            and not (fname in _planner_fields and not _planner_should_have_run)
        ]

        is_export_ready = (
            not missing
            and ordering_valid
            and not gaps
        )

        return CompletenessReport(
            job_id=self.job_id,
            is_export_ready=is_export_ready,
            event_count=len(events),
            event_count_by_type={k: len(v) for k, v in by_type.items()},
            ordering_valid=ordering_valid,
            missing_event_types=missing,
            attempt_count=attempt_count,
            gaps=gaps,
            coverage=coverage,
        )



def format_report(report: CompletenessReport) -> str:
    lines = [
        f"Training event log - job: {report.job_id}",
        f"  Events logged : {report.event_count}",
        f"  Event types   : {dict(sorted(report.event_count_by_type.items()))}",
        f"  Attempts      : {report.attempt_count}",
        f"  Ordering OK   : {report.ordering_valid}",
        "",
        "Coverage by section:",
    ]
    for section, status in sorted(report.coverage.items()):
        lines.append(f"  {section:<20} {status}")

    if report.missing_event_types:
        lines += ["", "Missing required event types:"]
        for t in report.missing_event_types:
            lines.append(f"  - {t}")

    total_gaps = len(EXPORT_FIELD_GAPS)
    remaining  = len(report.gaps)
    covered    = total_gaps - remaining
    lines += [
        "",
        f"Content gaps ({remaining} of {total_gaps} fields still not captured,"
        f" {covered} covered):",
    ]
    for g in report.gaps:
        lines.append(f"  - {g}")

    lines += [
        "",
        f"Export-ready: {report.is_export_ready}",
    ]
    if not report.is_export_ready and remaining > 0:
        lines.append(
            f"  Reason: {remaining} required training field(s) not found in event payloads."
        )
    return "\n".join(lines)
