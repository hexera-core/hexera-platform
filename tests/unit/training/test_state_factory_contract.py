# Responsibility: Verify the pipeline-state factory returns every operational field and is closed to unknown ones.
from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


from tests.capture_authority import seed_events

from meshpipeline.pipeline.state_factory import make_pipeline_state

CAPTURE_OWNER = "owner-training-tests"

OPERATIONAL_FIELDS = frozenset({
    "job_id", "user_id",
    "session_id",
    "geometry",
    "domain", "engine_params",
    "openfoam_workspace",
    "executor_output", "executor_success", "mesh_manifest",
    "reviewer_result", "reviewer_verdict", "reviewer_feedback",
    "reviewer_patch_checks", "reviewer_axis_findings", "reviewer_rebuild_required",
    "reviewer_tool_calls",
    "classifier_result", "retry_count", "builder_mode",
    "outcome_message",
    "api_failure",
    "request_txt", "review_brief_txt",
    "builder_noop_count",
    "agent_model_configs",
    "user_dispute",
})

DELETED_FIELDS = frozenset({
    "intake_turns", "intake_system_snapshot", "intake_llm_metadata",
    "intake_max_turns_reached",
    "builder_input_messages", "builder_system_message_snapshots",
    "builder_tools_definition", "builder_tool_call_histories",
    "builder_full_responses", "builder_message_histories",
    "builder_call_metadata", "builder_pruning_events",
    "executor_successes", "executor_stdouts", "executor_stderrs",
    "attempt_log_snapshots",
    # retired with the classifier's LLM (it is deterministic; it makes no model call)
    "classifier_input_messages", "classifier_system_snapshots",
    "classifier_full_responses", "classifier_finish_reasons", "classifier_usages",
    "classifier_rag_fix_chunks", "classifier_rag_similarity_scores",
    # the deterministic classifier's per-attempt capture - events.jsonl, never state
    "classifier_sections", "classifier_summaries", "classifier_builder_modes",
    "classifier_failed_gates", "classifier_failed_axes", "classifier_error_sources",
    "reviewer_tool_call_histories", "reviewer_system_snapshots",
    "reviewer_review_dirs", "reviewer_reasoning_chains",
    "reviewer_full_responses", "reviewer_input_texts",
    "final_result", "terminal_message", "terminal_message",
})

def test_factory_returns_all_operational_fields():
    s = make_pipeline_state("job-1", "user-1")
    missing = OPERATIONAL_FIELDS - set(s)
    assert not missing, f"Missing fields: {sorted(missing)}"


def test_default_messages_is_empty_list():
    s = make_pipeline_state("j", "u")
    assert s["messages"] == []


def test_default_session_id_is_empty_string():
    s = make_pipeline_state("j", "u")
    assert s["session_id"] == ""


def test_default_geometry_is_absent():
    s = make_pipeline_state("j", "u")
    assert s["geometry"] == {}


def test_default_flow_conditions():
    s = make_pipeline_state("j", "u")
    assert s["domain"] == ""
    assert "mach" not in s
    assert "aoa" not in s
    assert "turbulence" not in s


def test_default_executor_fields():
    s = make_pipeline_state("j", "u")
    assert s["openfoam_workspace"] == ""
    assert s["executor_output"] == ""
    assert s["executor_success"] is False
    assert s["mesh_manifest"] == {}


def test_default_reviewer_fields():
    s = make_pipeline_state("j", "u")
    assert s["reviewer_result"] == ""
    assert s["reviewer_verdict"] == ""
    assert s["reviewer_feedback"] == ""
    assert s["reviewer_patch_checks"] == {}
    assert s["reviewer_axis_findings"] == [] #: canonical typed LIST
    assert s["reviewer_rebuild_required"] is False
    assert s["reviewer_tool_calls"] == []


def test_default_classifier_fields():
    s = make_pipeline_state("j", "u")
    assert s["classifier_result"] == {}
    assert s["retry_count"] == 0
    assert s["builder_mode"] == "initial"


def test_default_misc_fields():
    s = make_pipeline_state("j", "u")
    assert s["outcome_message"] == ""
    assert s["api_failure"] == ""
    assert s["request_txt"] == ""
    assert s["review_brief_txt"] == ""
    assert s["builder_noop_count"] == 0
    assert s["agent_model_configs"] == {}


def test_default_agent_model_configs_none_becomes_empty_dict():
    s = make_pipeline_state("j", "u", agent_model_configs=None)
    assert s["agent_model_configs"] == {}


def test_default_messages_none_becomes_empty_list():
    s = make_pipeline_state("j", "u", messages=None)
    assert s["messages"] == []



def test_job_id_and_user_id_set():
    s = make_pipeline_state("job-42", "user-99")
    assert s["job_id"] == "job-42"
    assert s["user_id"] == "user-99"


def test_session_id_override():
    s = make_pipeline_state("j", "u", session_id="sess-5")
    assert s["session_id"] == "sess-5"


def test_messages_override():
    msgs = [{"role": "user", "content": "hi"}]
    s = make_pipeline_state("j", "u", messages=msgs)
    assert s["messages"] is msgs


def test_geometry_override(tmp_path):
    from tests._geometry_support import geometry_state
    g = geometry_state(tmp_path)
    s = make_pipeline_state("j", "u", geometry=g)
    assert s["geometry"] == g


def test_domain_override():
    s = make_pipeline_state("j", "u", domain="aero")
    assert s["domain"] == "aero"


def test_the_factory_signature_is_closed_to_unknown_fields():
    import pytest as _pytest
    with _pytest.raises(TypeError):
        make_pipeline_state("j", "u", mach=0.8)


def test_request_txt_and_review_brief_override():
    s = make_pipeline_state("j", "u", request_txt="mesh it", review_brief_txt="check patches")
    assert s["request_txt"] == "mesh it"
    assert s["review_brief_txt"] == "check patches"


def test_agent_model_configs_override():
    cfg_dict = {"builder": {"model": "gpt-4"}}
    s = make_pipeline_state("j", "u", agent_model_configs=cfg_dict)
    assert s["agent_model_configs"] is cfg_dict



def _tasks_src() -> str:
    return (APP_DIR / "application" / "pipeline_run.py").read_text()


def test_tasks_imports_make_pipeline_state():
    src = _tasks_src()
    assert "make_pipeline_state" in src, "tasks.py should import/use make_pipeline_state"


def test_tasks_export_fields_cover_all_operational_fields():
    from meshpipeline.application.post_terminal import EXPORT_FIELDS

    missing = OPERATIONAL_FIELDS - set(EXPORT_FIELDS)
    assert not missing, f"EXPORT_FIELDS missing operational fields: {sorted(missing)}"



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


def _retry_events(job_id: str) -> list[dict]:
    return [
        {"event_type": "intake_complete",  "job_id": job_id, "timestamp": _ts(0), "attempt": 0, "payload": {
            "intake_turns": [], "intake_system_snapshot": "sys", "intake_llm_metadata": [],
            "request_txt": "run cfd", "review_brief_txt": "check mesh", "domain": "aero",
            "max_turns_reached": False,
        }},
        {"event_type": "engine_select_run", "job_id": job_id, "timestamp": _ts(0.5),
         "payload": {"chosen": "cfmesh", "source": "selector"}},
        {"event_type": "builder_attempt",  "job_id": job_id, "timestamp": _ts(1), "attempt": 1, "payload": {
            "builder_message_histories": [{"role": "user", "content": "build"}],
            "builder_tool_call_histories": [], "builder_system_message_snapshots": "sys",
            "builder_full_responses": "done", "builder_call_metadata": [],
            "builder_pruning_events": [], "builder_tools_definition": [],
        }},
        {"event_type": "executor_run",     "job_id": job_id, "timestamp": _ts(2), "attempt": 1, "payload": {
            "success": False, "executor_stdouts": "", "executor_stderrs": "err",
            "attempt_log_snapshots": "", "mesh_manifest": {},
        }},
        {"event_type": "classifier_run",   "job_id": job_id, "timestamp": _ts(3), "attempt": 1, "payload": {
            "section": "MESH", "error_source": "executor_fail", "summary": "bad cell quality",
            "failed_gate": "manifest_valid", "failed_axes": [],
            "builder_mode": "retry", "deterministic": True,
        }},
        {"event_type": "builder_attempt",  "job_id": job_id, "timestamp": _ts(4), "attempt": 2, "payload": {
            "builder_message_histories": [{"role": "user", "content": "retry"}],
            "builder_tool_call_histories": [], "builder_system_message_snapshots": "sys2",
            "builder_full_responses": "fixed", "builder_call_metadata": [],
            "builder_pruning_events": [], "builder_tools_definition": [],
        }},
        {"event_type": "executor_run",     "job_id": job_id, "timestamp": _ts(5), "attempt": 2, "payload": {
            "success": True, "executor_stdouts": "ok", "executor_stderrs": "",
            "attempt_log_snapshots": "", "mesh_manifest": {},
        }},
        {"event_type": "reviewer_run",     "job_id": job_id, "timestamp": _ts(6), "attempt": 2, "payload": {
            "verdict": "PASS", "tool_calls": 2, "reviewer_tool_call_histories": [],
            "reviewer_reasoning_chains": ["ok"], "reviewer_full_responses": ["<<PASS>>"],
            "reviewer_system_snapshots": "sys", "reviewer_input_texts": "input",
            "reviewer_review_dirs": "/tmp/r",
        }},
        {"event_type": "final_result_built",       "job_id": job_id, "timestamp": _ts(7), "attempt": 0, "payload": {
            "final_result": {"schema_version": 1, "status": "succeeded"}, "terminal_message": "Mesh generation completed successfully.",
            "geometry_source": {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'},
            "agent_model_configs": {},
        }},
    ]


def test_export_source_is_events_for_complete_log(capture_authority, tmp_path):
    _redis_mod = types.ModuleType("redis"); _redis_mod.Redis = MagicMock()
    _celery_mod = types.ModuleType("celery"); _celery_mod.Celery = MagicMock()
    _celery_app_mod = types.ModuleType("worker.celery_app")
    _celery_app_mod.celery_app = MagicMock()

    job_id = "phase7-export-smoke"
    seed_events(capture_authority, CAPTURE_OWNER, job_id, _retry_events(job_id))

    fallback = make_pipeline_state(job_id=job_id, user_id="u", request_txt="run cfd") | {
        "builder_message_histories": [[{"role": "user", "content": "build"}]],
        "reviewer_verdict":          "<<PASS>>",
        "terminal_message":      "Mesh generated.",
    }

    with patch.dict(sys.modules, {
        "redis": _redis_mod,
        "celery": _celery_mod,
        "worker": types.ModuleType("worker"),
        "worker.celery_app": _celery_app_mod,
    }):
        from meshpipeline.capture.source import select_export_source
        _, source = select_export_source(job_id, fallback, owner_id=CAPTURE_OWNER)

    assert source == "events"
