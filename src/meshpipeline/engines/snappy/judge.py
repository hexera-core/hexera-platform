# Responsibility: Interpret snappyHexMesh's own output into the facts the gates measure.
# Boundaries: parsing and interpretation; it sets no threshold.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _judge_measurements(result: dict, q: dict, wall_faces: int) -> dict:
    return {
        "timed_out":     bool(result.get("timed_out")),
        # rc None means "no run result recorded" - treat as clean exit (historic
        # behavior: the judge only failed on an explicit nonzero rc).
        "rc":            0 if result.get("rc") is None else result.get("rc"),
        "wall_faces":    wall_faces,
        "fatal":         q.get("fatal", []),
        "skew_fraction": q.get("skew_fraction", 0.0) or 0.0,
        "max_non_ortho": q.get("max_non_ortho"),
        "layer_coverage": result.get("layer_coverage"),
    }


def _repair_message(key: str, result: dict, q: dict) -> str:
    if key == "timed_out":
        return ("TIMEOUT - the mesh is too fine to build in budget. This is the ONE case to "
                "REDUCE the budget: lower max_cells and/or surface_level; fewer n_layers also "
                "helps. Do NOT enlarge anything.")
    if key == "rc":
        # -3 is RC_INFRASTRUCTURE: the run never reached snappyHexMesh at all. Describing that as
        # a geometry problem sent a planner enlarging its domain 20->25->30 and 30->40->50 across
        # four attempts over 46 minutes, each one taking 0:00 against a 13-minute average, because
        # nothing was ever dispatched. Worse, the advice is what sustains the failure: re-planning
        # changes the payload, and it is the payload changing under one operation identity that
        # the submission claim refuses. The only honest instruction is to change NOTHING.
        from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE
        if result.get("rc") == RC_INFRASTRUCTURE and \
                "[CLOUD_RUN_RESULT_UNCOLLECTED]" in str(result.get("log_tail") or ""):
            # The run FINISHED; its output was too large (or too broken) to bring back. The
            # remote's own summary rides in the tail. When the cause is the archive cap, the
            # mesh is simply bigger than the exchange can return: the repair IS a smaller
            # mesh - the opposite of the never-started advice below.
            _tail = str(result.get("log_tail") or "")
            _rq = result.get("remote_quality") or {}
            if "over the" in _tail and "cap" in _tail:
                return (f"RESULT TOO LARGE TO COLLECT - the mesh ran to completion "
                        f"({_rq.get('cells')} cells) but its result archive exceeded the "
                        "exchange's size cap on the way back. REDUCE max_cells and/or "
                        "surface_level (and any local refinement) so the delivered mesh fits; "
                        "do NOT enlarge anything.")
            return ("RESULT NOT COLLECTED - the mesh ran to completion but its output could not "
                    "be brought back from the remote runner. Nothing in this plan caused it; "
                    "resubmit this plan unchanged.")
        if result.get("rc") == RC_INFRASTRUCTURE:
            return ("INFRASTRUCTURE failure - the mesh run never started, so nothing about this "
                    "plan caused it and nothing in it can fix it. Do NOT change the domain, the "
                    "levels, the layers or the budget: a different plan is a different payload, "
                    "and that is what the submission claim rejects. Resubmit this plan unchanged.")
        if result.get("rc") in (137, -9):
            # SIGKILL: the container runtime's answer to a process past its memory limit. The
            # 'setup issue' advice below (strict quality, a larger domain) makes the next mesh
            # BIGGER - shell_tube_bundle_009 lost two 30-minute runs to exactly that.
            return ("snappyHexMesh was KILLED FOR MEMORY (exit 137) - the mesh outgrew the "
                    "remote task's memory. This is a size problem, not a setup problem: REDUCE "
                    "max_cells, lower surface_level and any local refinement, and use fewer "
                    "layers; do NOT enlarge the domain or raise quality.")
        return ("snappyHexMesh FAILED (nonzero exit) - a setup issue (domain point, feature "
                "file, or over-aggressive levels). Try quality='strict' and a slightly larger "
                "domain_margin. Not a budget problem - do not raise max_cells.")
    if key == "wall_faces":
        return ("CARVE LEAKED - 0 wall-patch faces: the body was not sealed/captured. ENLARGE "
                "domain_margin so the far-corner carve point stays clearly in the fluid. Not a "
                "budget problem - do not raise max_cells.")
    if key == "fatal":
        _fatal = q.get("fatal", [])
        _fj = " ".join(str(f) for f in _fatal).lower()
        if "orient" in _fj or "negative" in _fj:
            return (f"fatal {_fatal} - prism LAYERS are inverting cells at the concave "
                    "wing-body junction; this is NOT a resolution problem. REDUCE n_layers "
                    "(e.g. 5→3). If quality is 'strict' and the layers also fail to inflate, "
                    "switch to 'balanced' - strict's layer mechanics are usually what starves "
                    "them. Do NOT raise max_cells - a finer mesh makes junction "
                    "layer-inversion WORSE.")
        return (f"fatal {_fatal} - a topology/carve defect. Set quality='strict'; if it is a "
                "carve/seal issue, enlarge domain_margin. Not a budget problem.")
    if key == "skew_fraction":
        skew_frac = q.get("skew_fraction", 0.0) or 0.0
        return (f"WIDESPREAD skewness - {q.get('skew_faces')} faces ({skew_frac * 100:.3f}%), "
                "not just the junction. Set quality='strict'; if it persists, REDUCE n_layers "
                "(layer compression at the junction drives skew). Raising max_cells will not "
                "fix it.")
    return f"criterion '{key}' failed"


def _judge_snappy(result: dict, q: dict, wall_faces: int) -> tuple[bool, str]:
    from meshpipeline.engines.quality_criteria import production_grade
    ok, failing = production_grade("snappy", _judge_measurements(result, q, wall_faces))
    if ok:
        return True, "production-grade"
    return False, _repair_message(failing["key"], result, q)


__all__ = ["_judge_measurements", "_judge_snappy", "_repair_message"]
