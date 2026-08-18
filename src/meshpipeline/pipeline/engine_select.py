# Responsibility: Resolve which engine a run will use, from its purpose and geometry.
# Boundaries: selection from declared capabilities; it validates no geometry.
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from meshpipeline.engines.purposes import flow_topology
from meshpipeline.engines.registry import default_engine, engines_producing_topology, get_spec

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


def _log_selection(job_id: str, payload: dict) -> None:
    try:
        _chosen = payload.get("chosen")
        if _chosen and "planner_required" not in payload:
            payload = {**payload, "planner_required": bool(get_spec(_chosen).build_driver)}
    except Exception as exc:
        logger.debug("engine_select: could not declare planner_required: %s", exc)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        # the engine is selected once per run, so the phase alone identifies the operation
        TrainingLogger(job_id).log("engine_select_run", payload, op_id="engine-select")
    except Exception as exc:
        logger.warning("engine_select: could not log training event: %s", exc)


async def _publish(job_id: str, text: str, op_id: str) -> None:
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        _pub = execution_publisher(job_id, agent="engine_select")
        await _pub.astage(op_id=op_id)
        await _pub.anote(text, op_id=op_id)
    except StaleExecutionPublish:
        # A lost claim is not a stream hiccup: a superseded generation must not go on
        # selecting an engine for a job a newer one owns.
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        pass


async def node_engine_select(state: PipelineState) -> dict:
    job_id = state.get("job_id", "unknown")

    if state.get("engine"):
        # A pin that names no known engine must NOT reach the executor: get_spec() picks
        # the gates from it, so an unknown name once meant validating the mesh against a
        # different engine's rubric. Fail the job loudly instead.
        get_spec(state["engine"])
        source = "dispute" if state.get("user_dispute") else "user"
        logger.info("node_engine_select: engine %r pinned (%s) - job_id=%s",
                    state.get("engine"), source, job_id)
        _log_selection(job_id, {"chosen": state.get("engine"), "source": source,
                                "model": None, "usage": None})
        return {}

    # Topology is DERIVED from the declared purpose, not read back out of engine_params
    # (which is where the two could disagree).
    _topology = flow_topology(state.get("purpose", "")) if state.get("purpose") else ""
    if _topology == "internal":
        candidates = engines_producing_topology("internal")
        # Prefer the default engine when it qualifies, else the first in a STABLE order.
        # Was `candidates[0]` - whichever engine happened to come first in the catalog
        # dict, so opening internal on another engine silently re-pointed this branch.
        forced = (default_engine() if default_engine() in candidates
                  else (sorted(candidates)[0] if candidates else default_engine()))
        logger.info("node_engine_select: topology=internal → %s (spec: %s) - job_id=%s",
                    forced, candidates, job_id)
        await _publish(job_id, f"Mesh engine: {forced}", "forced")
        _log_selection(job_id, {"chosen": forced, "source": "topology_internal",
                                "model": None, "usage": None})
        return {"engine": forced}

    # direct dispatch without any declaration: the deterministic default
    engine = default_engine()
    logger.info("node_engine_select: no pin, no declaration → default %s - job_id=%s",
                engine, job_id)
    await _publish(job_id, f"Mesh engine: {engine}", "resolved")
    _log_selection(job_id, {"chosen": engine, "source": "default",
                            "model": None, "usage": None})
    return {"engine": engine}
