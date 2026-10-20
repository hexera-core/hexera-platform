# Responsibility: Build one run's reproducibility record: what went in, what every AI step decided and on what, what the mesher ran with, every time a clock changed a decision, and why each decision was made (the rule behind it).
# Owns: the record's shape and the file it is written to beside the run's workspace.
# Boundaries: reads what the run already holds (state, the mesher's stamp, the decision log); it decides nothing and a record that cannot be made never touches a verdict.
# Collaborates with: contracts/decision_cache (AI decisions), engines/determinism (the mesh stamp and worker-side time notes), application/why_record (the `why` section, docs/RULES.md), application/pipeline_run (calls it at the end of a run).
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Beside the run's workspace (the mesher's own stamp is engines/determinism.STAMP_FILE).
RUN_RECORD_FILE = "run_reproducibility.json"
#: 2: the `why` section (application/why_record): every decision with the rule behind it.
RECORD_VERSION = 2


def _file_sha256(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def ai_provenance() -> dict[str, dict[str, Any]]:
    """The model each AI step's ROUTE actually asks, and the sampling it actually sends. The
    shipped OpenAI Responses routes refuse temperature and top_p (they are dropped), and none
    takes a seed, so their record says so instead of listing a temperature nobody used."""
    from meshpipeline.agents.builder.settings import BUILDER_ROUTE
    from meshpipeline.agents.intake.settings import INTAKE_ROUTE
    from meshpipeline.agents.reviewer.settings import VISUAL_REVIEWER_ROUTE
    from meshpipeline.engines.snappy.settings import PLANNER_ROUTE

    out: dict[str, dict[str, Any]] = {}
    for step, route in (("intake", INTAKE_ROUTE), ("builder", BUILDER_ROUTE),
                        ("planner", PLANNER_ROUTE), ("reviewer", VISUAL_REVIEWER_ROUTE)):
        provider, model = route.primary.provider, route.primary.model
        responses = provider == "openai"
        out[step] = {"provider": provider, "model": model,
                     "sampling": ("provider default: the Responses API takes no temperature, "
                                  "top_p or seed" if responses else "temperature/top_p from settings"),
                     "standby": bool(getattr(route, "standby", None))}
    return out


def build(job_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """The run's reproducibility record (a dict; never raises)."""
    from meshpipeline.contracts.decision_cache import AI_DECISION_CACHE, decisions_for
    from meshpipeline.engines import determinism as det

    rec: dict[str, Any] = {"version": RECORD_VERSION, "job_id": job_id}
    try:
        geo = state.get("geometry") if isinstance(state.get("geometry"), Mapping) else {}
        local = str((geo or {}).get("local_path") or "")
        rec["input"] = {"file_sha256": _file_sha256(local) if local else None,
                        "interpretation": (geo or {}).get("interpretation")}
        rec["plan"] = {"request_sha256": _digest(state.get("request_txt") or ""),
                       "patches_sha256": _digest(state.get("intake_patches") or []),
                       "engine": state.get("engine"),
                       "fidelity": state.get("effective_mesh_fidelity")}
        rec["ai"] = {"cache_mode": AI_DECISION_CACHE, "routes": ai_provenance(),
                     "decisions": decisions_for(job_id)}
        ws = str(state.get("openfoam_workspace") or "")
        rec["mesh"] = det.read_stamp(ws) if ws else None
        # the clocks that still decide (TIME-6: the CAD repair steps), and every guard clock that
        # tripped outside a native run (TIME-3..5) - beside the case or in this process
        rec["worker_time_limited"] = det.drain_process_notes()
        rec["guard_trips"] = [*(det.guard_trips(ws) if ws else []),
                              *det.drain_process_guard_trips()]
    except Exception:  # noqa: BLE001 - a record never touches a verdict
        logger.warning("run reproducibility record incomplete", exc_info=True)
    try:
        from meshpipeline.application import why_record
        rec["why"] = why_record.build(job_id, state)
    except Exception:  # noqa: BLE001 - a record never touches a verdict
        logger.warning("run why record incomplete", exc_info=True)
    return rec


def write(job_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """Build the record and keep it beside the run's workspace; returns it."""
    rec = build(job_id, state)
    ws = str(state.get("openfoam_workspace") or "")
    if ws and Path(ws).is_dir():
        try:
            (Path(ws) / RUN_RECORD_FILE).write_text(json.dumps(rec, indent=1, default=str))
        except OSError:
            logger.warning("could not write %s", RUN_RECORD_FILE, exc_info=True)
    return rec


__all__ = ["RUN_RECORD_FILE", "ai_provenance", "build", "write"]
