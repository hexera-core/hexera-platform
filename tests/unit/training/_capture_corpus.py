# Responsibility: Build the shared spine of the capture corpora the training tests seed.
# Boundaries: the events every corpus shares; a role's own payload stays with the test that owns that role's contract.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: A fixed origin: capture ordering is asserted, so the corpus cannot drift with wall-clock time.
ORIGIN = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def ts(offset_s: float = 0.0) -> str:
    return (ORIGIN + timedelta(seconds=offset_s)).isoformat()


def write(path: Path, events: list[dict]) -> None:
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


def intake_full(job_id: str, ts_offset: float = 0) -> dict:
    return {
        "timestamp": ts(ts_offset), "job_id": job_id,
        "event_type": "intake_complete",
        "payload": {
            "domain": "aero", "max_turns_reached": False,
            "finish_reason": "stop", "mach": 0.8, "aoa": 2.0, "turbulence": "kOmegaSST",
            "intake_turns": [{"role": "user", "content": "mesh this"}],
            "intake_system_snapshot": "You are intake...",
            "intake_llm_metadata": [{"turn": 1, "finish_reason": "stop"}],
            "request_txt": "mesh at Mach 0.8",
            "review_brief_txt": "check boundary layer",
        },
    }


def builder_full(job_id: str, ts_offset: float = 10, attempt: int = 1) -> dict:
    return {
        "timestamp": ts(ts_offset), "job_id": job_id,
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


def executor_full(job_id: str, ts_offset: float = 20, success: bool = True) -> dict:
    return {
        "timestamp": ts(ts_offset), "job_id": job_id,
        "event_type": "executor_run",
        "payload": {
            "success": success, "output_len": 1800, "workspace": f"/ws/{job_id}",
            "executor_stdouts":      "Mesh generated.\n",
            "executor_stderrs":      "" if success else "Error: surface not closed\n",
            "attempt_log_snapshots": "Attempt 1: done\n",
            "mesh_manifest":         {"schema_version": "1.0"} if success else {},
        },
    }


#: The three roles that precede the role under test in every corpus. Named so a cross-role
#: regression names the role it broke.
PRECEDING_ROLES = ("intake", "builder", "executor")

__all__ = ["ORIGIN", "PRECEDING_ROLES", "builder_full", "executor_full", "intake_full",
           "ts", "write"]
