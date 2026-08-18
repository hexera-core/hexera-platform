# Responsibility: Write what a finished run leaves behind.
# Boundaries: every write is best-effort.
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg

logger = logging.getLogger(__name__)

#: The state keys the conversation export is allowed to carry. An ALLOW-LIST, not a redaction pass:
#: a key added to the pipeline state is not exported until it is named here deliberately.
EXPORT_FIELDS = frozenset({
    "job_id", "user_id", "session_id", "geometry",
    "domain", "engine_params",
    "openfoam_workspace", "executor_output", "executor_success",
    "executor_failed_gate", "mesh_manifest",
    "reviewer_result", "reviewer_verdict", "reviewer_feedback",
    "reviewer_patch_checks", "reviewer_axis_findings",
    "reviewer_rebuild_required", "reviewer_tool_calls",
    "classifier_result", "retry_count", "builder_mode",
    "outcome_message", "api_failure",
    "request_txt", "review_brief_txt",
    "builder_noop_count", "agent_model_configs",
    "solvability_failed",
    "intake_patches", "dimensionality", "purpose", "input_kind",
    "user_dispute", "dispute_flag_findings", "builder_flag_responses", "engine",
})

#: The artifacts the viewer/dispute surface reloads from, in preference order.
_PREVIEW_CANDIDATES = ("mesh.msh", "mesh.stl")


def _serializable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return {k: _serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serializable(i) for i in obj]
    return obj


def capture_terminal_record(job_id: str, *, generation: int, final_result: dict,
                            terminal_message: str, geometry_source, agent_model_configs: dict,
                            jlog) -> None:
    try:
        from meshpipeline.capture.logger import TrainingLogger

        TrainingLogger(job_id).log("final_result_built", op_id=f"terminal:g{generation}", payload={
            "final_result": final_result,
            "terminal_message": terminal_message,
            "geometry_source": geometry_source,
            "agent_model_configs": agent_model_configs,
        })
    except Exception as exc:                       # noqa: BLE001 - capture never affects a verdict
        jlog.warning("final_result: capture event failed (%s)", exc)


def copy_viewer_preview(job_id: str, workspace: str, *, jlog) -> None:

    try:
        if not workspace:
            return
        for name in _PREVIEW_CANDIDATES:
            src = Path(workspace) / name
            if src.exists():
                dst = Path(rtcfg.JOBS_DIR) / job_id / f"preview{src.suffix}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                return
    except Exception as exc:                       # noqa: BLE001
        jlog.warning("preview copy failed: %s", exc)


def enqueue_conversation_export(job_id: str, state, *, created_at, ended_at, jlog) -> None:
    if not polcfg.MODES.data_collection_enabled:
        return
    try:
        from meshpipeline.contracts.training_export import enqueue_export

        payload = _serializable({k: state.get(k) for k in EXPORT_FIELDS})
        enqueue_export(job_id, payload, created_at=created_at, ended_at=ended_at)
        jlog.info("Export task enqueued")
    except Exception as exc:                       # noqa: BLE001
        jlog.error("Failed to enqueue export task: %s", exc)
