# Responsibility: Choose which recorded source an export is built from, and reconstruct state from it.
# Boundaries: the durable authority is preferred over the local projection.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)



class ExportSourceError(RuntimeError):
    pass



def _builder_llm_calls(agent_run_events: list, attempts: int) -> list:
    rounds_by_attempt: dict[int, int] = {}
    for e in agent_run_events:
        payload = e.payload if isinstance(e.payload, dict) else {}
        if payload.get("role") != "builder":
            continue
        tally = payload.get("tally") or {}
        rounds = tally.get("rounds")
        if not isinstance(rounds, int):
            continue
        attempt = payload.get("pipeline_attempt")
        if not isinstance(attempt, int):
            continue
        # one record per attempt; if an attempt somehow emitted twice, the last one wins
        rounds_by_attempt[attempt] = rounds
    return [rounds_by_attempt.get(i) for i in range(attempts)]


def events_to_state(events: list, operational_state: dict) -> dict:
    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e.event_type, []).append(e)

    intake_events     = by_type.get("intake_complete", [])
    builder_events    = by_type.get("builder_attempt", [])
    executor_events   = by_type.get("executor_run", [])
    classifier_events = by_type.get("classifier_run", [])
    reviewer_events   = by_type.get("reviewer_run", [])
    terminal_events   = by_type.get("final_result_built", [])
    engine_events     = by_type.get("engine_select_run", [])
    # CANONICAL accountability. `agent_run` is the authoritative per-invocation record every
    # migrated agent emits; `agent_run_superseded` is deliberately NOT read here - an abandoned
    # generation is not an agent run and must never be exported as one.
    agent_run_events  = by_type.get("agent_run", [])

    intake = intake_events[0].payload if intake_events else {}
    terminal = terminal_events[0].payload if terminal_events else {}

    # WHICH engine produced this mesh. engine_select DECLARES it (`chosen`) on every
    # resolution path; intake's `mesh_engine` is the user's pin and the fallback. Without
    # this the exporter's `state.get("engine")` was always absent and every sample was
    # labelled engine="unknown" - a blank column in the corpus, not a loud failure.
    engine = (
        (engine_events[-1].payload.get("chosen", "") if engine_events else "")
        or intake.get("mesh_engine", "")
        or operational_state.get("engine", "")
    )

    _intake_turn_meta = [
        {
            "turn":              e.payload.get("turn"),
            "finish_reason":     e.payload.get("finish_reason"),
            "usage":             e.payload.get("usage"),
            "completed":         False,
            "max_turns_reached": e.payload.get("max_turns_reached", False),
        }
        for e in by_type.get("intake_turn", [])
    ]

    _last_rv      = reviewer_events[-1].payload if reviewer_events else {}
    _verdict_raw  = _last_rv.get("verdict", "")
    from meshpipeline.contracts.review_outcome import normalize_verdict
    reviewer_verdict  = normalize_verdict(_verdict_raw)
    # Prefer the per-round reasonings; fall back to the final-blob field
    # (the only carrier on error paths where no round completed).
    _chains       = _last_rv.get("reviewer_round_reasonings") or _last_rv.get("reviewer_reasoning_chains", [])
    reviewer_feedback = _chains[0] if (_verdict_raw == "FAIL" and _chains) else ""

    executor_successes = [bool(e.payload.get("success", False)) for e in executor_events]

    return {
        "openfoam_workspace":  operational_state.get("openfoam_workspace", ""),
        "job_id":              operational_state.get("job_id", ""),
        "user_id":             operational_state.get("user_id", ""),
        "api_failure":         operational_state.get("api_failure", ""),
        "session_id":          operational_state.get("session_id", ""),
        "executor_output":     operational_state.get("executor_output", ""),
        "mesh_manifest":       operational_state.get("mesh_manifest", {}),
        "builder_noop_count":  operational_state.get("builder_noop_count", 0),
        "builder_mode":        operational_state.get("builder_mode", ""),
        "executor_success":    executor_successes[-1] if executor_successes else False,
        "solvability_failed":  operational_state.get("solvability_failed", False),
        "domain":              intake.get("domain", operational_state.get("domain", "")),
        "engine":              engine,
        "purpose":             intake.get("purpose", operational_state.get("purpose", "")),
        "input_kind":          intake.get("input_kind", operational_state.get("input_kind", "")),
        "intake_patches":      intake.get("intake_patches", operational_state.get("intake_patches", [])),
        "dimensionality":      intake.get("dimensionality", operational_state.get("dimensionality", "")),

        "intake_turns":             intake.get("intake_turns", []),
        "intake_system_snapshot":   intake.get("intake_system_snapshot", ""),
        "intake_llm_metadata":      _intake_turn_meta + intake.get("intake_llm_metadata", []),
        "intake_max_turns_reached": intake.get("max_turns_reached", False),
        "request_txt":              intake.get("request_txt", ""),
        "review_brief_txt":         intake.get("review_brief_txt", ""),

        "builder_tools_definition": (
            builder_events[-1].payload.get("builder_tools_definition", [])
            if builder_events else []
        ),
        "builder_message_histories": [
            e.payload.get("builder_message_histories", []) for e in builder_events
        ],
        "builder_system_message_snapshots": [
            e.payload.get("builder_system_message_snapshots", "") for e in builder_events
        ],
        "builder_tool_call_histories": [
            e.payload.get("builder_tool_call_histories", []) for e in builder_events
        ],
        "builder_full_responses": [
            e.payload.get("builder_full_responses", "") for e in builder_events
        ],
        "builder_input_messages": [],
        "agent_run_records": [e.payload for e in agent_run_events],
        "builder_llm_calls": _builder_llm_calls(agent_run_events, len(builder_events)),

        "executor_successes":   executor_successes,
        "executor_stdouts":     [e.payload.get("executor_stdouts", "") for e in executor_events],
        "executor_stderrs":     [e.payload.get("executor_stderrs", "") for e in executor_events],
        "attempt_log_snapshots": [
            e.payload.get("attempt_log_snapshots", "") for e in executor_events[:-1]
        ] if executor_events else [],

        # The classifier is DETERMINISTIC: no prompt, no response, no token usage. Its
        # decision and the DECLARED signals it routed on ARE the record. `section` is read
        # from the payload, never re-parsed out of prose.
        "classifier_sections": [
            e.payload.get("section", "") for e in classifier_events
        ],
        "classifier_summaries": [
            e.payload.get("summary", "") for e in classifier_events
        ],
        "classifier_builder_modes": [
            e.payload.get("builder_mode", "") for e in classifier_events
        ],
        "classifier_failed_gates": [
            e.payload.get("failed_gate", "") for e in classifier_events
        ],
        "classifier_failed_axes": [
            e.payload.get("failed_axes", []) for e in classifier_events
        ],
        "classifier_error_sources": [
            e.payload.get("error_source", "") for e in classifier_events
        ],
        "classifier_result": (
            {"section": classifier_events[-1].payload.get("section", "")}
            if classifier_events else {}
        ),

        "reviewer_tool_call_histories": [
            e.payload.get("reviewer_tool_call_histories", []) for e in reviewer_events
        ],
        "reviewer_system_snapshots": [
            e.payload.get("reviewer_system_snapshots", "") for e in reviewer_events
        ],
        "reviewer_review_dirs": [
            e.payload.get("reviewer_review_dirs", "") for e in reviewer_events
        ],
        "reviewer_reasoning_chains": [
            e.payload.get("reviewer_reasoning_chains", []) for e in reviewer_events
        ],
        "reviewer_full_responses": [
            e.payload.get("reviewer_full_responses", []) for e in reviewer_events
        ],
        "reviewer_input_texts": [
            e.payload.get("reviewer_input_texts", "") for e in reviewer_events
        ],
        "reviewer_verdict":        reviewer_verdict,
        "reviewer_feedback":       reviewer_feedback,
        "reviewer_result": (
            _last_rv.get("reviewer_full_responses", [""])[0]
            if _last_rv.get("reviewer_full_responses") else ""
        ),
        # The reviewer's structured verdict, from the last reviewer_run event. These were
        # hardcoded blank - the reviewer emits them (visual.py/metric.py), and the
        # classifier routes retries on rebuild_required, so a blank corpus lost the label.
        "reviewer_patch_checks":     _last_rv.get("patch_checks", {}) or {},
        "reviewer_axis_findings":    _last_rv.get("axis_findings", []) or [],
        "reviewer_rebuild_required": bool(_last_rv.get("rebuild_required", False)),
        "reviewer_tool_calls": [
            e.payload.get("tool_calls", 0) for e in reviewer_events
        ],

        # The terminal record is the DURABLE final_result and the message rendered from it -
        # application-owned facts, not a model turn.
        "final_result":            terminal.get("final_result", {}) or {},
        "outcome_message":         terminal.get("terminal_message", ""),

        "geometry_source":     terminal.get("geometry_source",
                                            operational_state.get("geometry_source")),
        "agent_model_configs": terminal.get("agent_model_configs",
                                            operational_state.get("agent_model_configs", {})),

        "retry_count": len(builder_events),
    }



def select_export_source(
    job_id: str,
    operational_state: dict,
    owner_id: str,
) -> tuple[dict, str]:
    from meshpipeline.capture.events import EventLog

    # Required, not defaulted: an export that falls back to a tenant found in its own input can
    # be pointed at another owner's capture by that input. The caller must name the tenant.
    _owner = owner_id
    if not _owner:
        raise ExportSourceError(
            f"No tenant scope for job {job_id!r} - capture records are owner-scoped and an "
            "export cannot be assembled without naming the owner.")
    log = EventLog(job_id, owner_id=_owner)

    if not log.load():
        logger.error(
            "training_export: export_source=none export_ready=false "
            "missing_fields=0 job_id=%s - no durable capture records",
            job_id,
        )
        raise ExportSourceError(
            f"No capture records for job {job_id!r} in the durable authority. "
            "Ensure the pipeline ran with DATA_COLLECTION_ENABLED."
        )

    report = log.validate_completeness()

    if not report.is_export_ready:
        _incomplete = [
            s for s, cov in report.coverage.items()
            if cov not in ("complete", "n/a")
        ]
        logger.error(
            "training_export: export_source=none export_ready=false "
            "missing_fields=%d job_id=%s - "
            "gaps=%d incomplete_sections=%s missing_event_types=%s",
            len(report.gaps), job_id,
            len(report.gaps), _incomplete, report.missing_event_types,
        )
        raise ExportSourceError(
            f"Event log not export-ready for job {job_id!r}: "
            f"{len(report.gaps)} payload gap(s), "
            f"missing event types: {report.missing_event_types}, "
            f"incomplete sections: {_incomplete}. "
            "Check TrainingLogger node coverage."
        )

    state = events_to_state(log.load(), operational_state)

    logger.info(
        "training_export: export_source=event_log "
        "export_ready=true missing_fields=0 job_id=%s "
        "(events=%d, attempts=%d)",
        job_id, report.event_count, report.attempt_count,
    )
    return state, "events"
