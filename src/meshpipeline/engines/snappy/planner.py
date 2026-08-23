# Responsibility: Derive a snappyHexMesh strategy from the geometry, deterministically.
# Boundaries: it computes values the model then chooses among; it authors no dictionary.
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.event_stream import (
    ExecutionEventPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts.model_inference import ModelRoundResult

logger = logging.getLogger(__name__)

PLANNER_SYSTEM = """You are a senior CFD MESH-PLANNING engineer. You do NOT build the mesh - you decide the STRATEGY the builder will execute with snappyHexMesh (body-fitted, hex-dominant, with prism boundary layers).

Think like a mesh engineer sizing a job: WHAT is being simulated, WHERE the gradients live (walls -> boundary layers), where resolution must go (leading/trailing edges, wing-body junctions, the wake), how big the far-field must be, and the cell budget. The geometry has ALREADY been measured for you (size, surface area, thinnest feature, and budget-derived refinement levels) - trust those numbers; do not exceed the budget levels.

Reply with ONLY a JSON object (no prose, no markdown fences) - the plan the builder passes to configure_mesh:
{
  "approach": "<one line: the meshing approach for THIS geometry+physics>",
  "domain_margin": {"up": <n>, "down": <n>, "side": <n>, "vert": <n>},
  "reference_length_m": <metres or null>,
  "n_layers": <int>,
  "first_layer_rel": <float>,
  "quality": "balanced" | "strict",
  "max_cells": <int>,
  "focus": "<where resolution/attention must go>",
  "risks": "<the likely failure mode + the ONE strategy change to make if it happens>"
}

Guidance: reference_length_m REPORTS the metres value of ONE of the user's quoted units - if they sized the far-field in 'chords' and stated the chord, this is that chord in metres; null when they quoted no reference length. It changes nothing about how you size the box - it is how the delivered box is JUDGED, in the user's own unit. domain_margin stays in body-lengths L, measured from the body's bounding box, and each key names a DIRECTION - do not guess them: 'up' is UPSTREAM and 'down' is DOWNSTREAM along the flow; 'vert' is ABOVE AND BELOW the body, perpendicular to the flow - this is where a lifting body needs room and it must never be 0; 'side' is SPANWISE, and it is the only one that may be 0, for a slab whose symmetry planes sit on its own end faces. Size the far-field from your own knowledge of standard external-aero practice so the outer boundaries never disturb the flow around the body. It scales with the regime (a compressible/transonic case needs a MUCH larger domain than a low-speed one, to avoid blockage and wave reflection) and a lifting body needs extra length downstream for its wake. Do NOT under-size it - a cramped far-field is a common failure the reviewer rejects. n_layers 3-5. first_layer_rel 0.25-0.5. Use quality 'strict' when you expect skew trouble (e.g. layers compressing at a concave wing-body junction) - it enforces checkMesh skew<4 at a small coverage cost.

max_cells is YOURS to set and it is a real lever: a large far-field WITH a well-resolved body legitimately needs more cells, so RAISE it when the physics demands (a cramped budget forces either an under-resolved wall or a too-small domain - both get rejected). BUT never exceed max_cells_HARD_CEILING (shown in the metrics - the compute limit); a mesh above it cannot be built. If the ideal job would need more than the ceiling, spend that ceiling wisely - the far-field must be big enough that the reviewer accepts it, so if you must trade, keep the domain adequate and let the far-field cells be coarser rather than cramping the domain.

REVISING: if you are shown your PREVIOUS plan together with a concrete critique of why it failed, you MUST change the offending value(s) by a MEANINGFUL amount - do NOT restate the same numbers. If the mesh EXCEEDED the cell budget, do not shave it - the actual count inflates past max_cells, so set max_cells low enough that the RESULT lands well under the ceiling (aim 15-20% under, scaled by how far over the last attempt measured). If the far-field was called inadequate, increase the margins substantially (think 2-4x, not a nudge); if the body was under-resolved or the larger domain now needs it, raise max_cells to match. Treat the critique as a measurement of how far off you were, and correct by that much."""


INTERNAL_PLANNER_SYSTEM = """You are a senior CFD MESH-PLANNING engineer planning an INTERNAL (through-flow) mesh with snappyHexMesh. The fluid VOLUME itself is the domain - there is NO far-field box. The solid has already been split into wall + inlet + outlet patches; you decide how finely to resolve the flow passage and the near-wall prism layers.

Think like a mesh engineer sizing an internal job: the gradients live at the WALL (boundary layer, y+, pressure drop) and in curved/branching passages (a bend drives secondary/Dean vortices; a sudden area change drives separation). Resolution is set RELATIVE TO THE BORE (hydraulic diameter), so it generalises across pipe sizes.

Reply with ONLY a JSON object (no prose, no markdown fences):
{
  "approach": "<one line: the meshing approach for THIS passage+physics>",
  "cells_across_diameter": <int>,
  "surface_level": <int>,
  "feature_level": <int>,
  "n_layers": <int>,
  "first_layer_rel": <float>,
  "quality": "balanced" | "strict",
  "max_cells": <int>,
  "focus": "<where resolution/attention must go>",
  "risks": "<the likely failure mode + the ONE strategy change to make if it happens>"
}

Guidance: cells_across_diameter 16-40 - how many cells span the bore. Use the HIGH end for curved/bending or branching passages (to capture secondary flow), the low end for a plain straight duct. surface_level 1-3 (wall refinement). feature_level = surface_level+1 (sharpens the inlet/outlet rims). n_layers 3-6 - internal wall-bounded flow is MORE layer-critical than external aero (y+ and pressure drop depend on it). first_layer_rel 0.2-0.4. Use quality 'strict' when you expect skew at a tight bend or a junction. max_cells is a lever - raise it when a long or geometrically complex passage at your chosen resolution needs the cells, but NEVER exceed max_cells_HARD_CEILING (the compute limit). There is NO domain_margin here - the part is the domain, so spend every cell inside the passage.

REVISING: if shown your PREVIOUS plan with a concrete critique, change the offending value(s) by a MEANINGFUL amount - do not restate the same numbers. Widespread skew -> quality 'strict' and/or fewer/thinner layers; a timeout or over-budget -> lower cells_across_diameter or surface_level; layers collapsing -> reduce n_layers and thin first_layer_rel. Treat the critique as a measurement of how far off you were."""


def overshoot_corrected_budget(prev_budget: object, prev_actual: object, *, ceiling: int,
                               margin: float = 0.85, floor: int = 200_000) -> int | None:
    """The budget the NEXT attempt should request, from what the last one measured.

    A plan's max_cells is a request, not a result: the mesher refines around the geometry and
    lands where it lands - a wall-resolved aerofoil asked for 1.8M and produced 4.32M. When the
    result exceeds the compute ceiling, the inflation ratio is now a MEASUREMENT, so the correction
    is arithmetic: request ceiling * (asked/got), minus a margin so the answer lands 15% under the
    cap instead of kissing it. Left to the model, the corrections decayed - 43%, then 7%, then 7% -
    and a defect-free mesh 6% over the cap was destroyed on the fifth attempt with nothing
    delivered.

    None means "no measured overshoot to correct" - first attempts, missing numbers, or a previous
    result already inside the ceiling - and the caller falls through to the model's own budget.
    """
    try:
        budget, actual = float(prev_budget), float(prev_actual)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if budget <= 0 or actual <= 0 or actual <= ceiling:
        return None
    return max(floor, min(ceiling, int(ceiling * (budget / actual) * margin)))


def clamp_cell_budget(raw: object, *, ceiling: int, default: int = 4_000_000) -> int:
    import math as _math

    v = raw
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = int(str(v).strip())
        except (TypeError, ValueError):
            return min(default, ceiling)
    if isinstance(v, float) and not _math.isfinite(v):
        return min(default, ceiling)
    iv = int(v)
    if iv <= 0:
        return min(default, ceiling)
    return min(iv, ceiling)


def _log_plan_event(job_id: str, payload: dict, op_id: str = "",
                    attempt: int | None = None) -> None:
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log("planner_run", payload, op_id=op_id or "planner",
                                   attempt=attempt)
    except Exception as exc:
        logger.warning("Planner: could not log training event: %s", exc)


@dataclass(frozen=True)
class PlanOutcome:

    plan: dict | None = None
    round: ModelRoundResult | None = None
    failure_marker: str = ""


async def make_mesh_plan(*, workspace, job_id: str, request_txt: str,
                         prior_feedback: str = "",
                         previous_plan: dict | None = None,
                         flow_regime: str = "external",
                         mesh_fidelity: str = "",
                         surface=None) -> dict | None:
    return (await plan_with_accounting(
        workspace=workspace, job_id=job_id, request_txt=request_txt,
        prior_feedback=prior_feedback, previous_plan=previous_plan,
        flow_regime=flow_regime, mesh_fidelity=mesh_fidelity, surface=surface)).plan


async def plan_with_accounting(*, workspace, job_id: str, request_txt: str,
                               prior_feedback: str = "",
                               previous_plan: dict | None = None,
                               flow_regime: str = "external",
                               mesh_fidelity: str = "",
                               publish: ExecutionEventPublisher | None = None,
                               attempt: int = 1,
                               native_attempt: int = 1,
                               surface=None) -> PlanOutcome:
    stl = Path(workspace) / "input.stl"
    if not stl.exists():
        # NO PROVIDER CALL HAPPENS ON THIS PATH, so no reasoning is published for it.
        # "Deterministic" describes the DRIVER, not this planner: when it does reach
        # the router below, that is a real model round and it is traced like any other.
        return PlanOutcome()          # no provider call was made at all
    # FENCE - before the Planner's independent model call. A fenced worker must not spend
    # provider budget planning a mesh a newer generation already owns.
    from meshpipeline.contracts import execution_guard as _fence
    await _fence.assert_still_owner("planner model call")
    try:
        from meshpipeline.cad.analysis import analyze_surface, recommend_refinement
        from meshpipeline.cad.prepared_surface import require_metre_surface
        a = analyze_surface(require_metre_surface(surface, workspace, "input.stl"))
        r = recommend_refinement(a, max_cells=4_000_000)      # a reference budget for the levels shown
        metrics = {
            "diag_m": round(a["diag"], 3),
            "extent_m": [round(x, 3) for x in a["extent"]],
            "surface_area_m2": round(a["surface_area"], 1),
            "thinnest_feature_m": round(a["min_feature"], 6),
            "recommended_surface_level": r["surface_level"],
            "recommended_feature_level": r["feature_level"],
            "affordable_level_at_4M_cells": r.get("afford_level"),
            "max_cells_HARD_CEILING": polcfg.CELL_HARD_LIMIT,   # compute limit - never exceed this
        }
        # The mesh-detail preference is qualitative, bounded, advisory context, never a cell target.
        from meshpipeline.pipeline.enums import authoring_tier
        _tier = authoring_tier(mesh_fidelity).value if mesh_fidelity else ""
        _fid_note = ""
        if _tier:
            _fid_note = (
                f"\n\nMESH DETAIL PREFERENCE: {_tier}. This is a QUALITATIVE speed/detail preference,"
                " not an exact cell target: 'draft' favours speed (coarser), 'max' favours detail"
                " (finer within the ceiling), 'standard' is balanced. Let it nudge your levels/budget"
                " a little; you may still propose only values the schema allows, max_cells stays bounded"
                " by max_cells_HARD_CEILING, and the reviewer does NOT check whether a tier-specific"
                " cell count was reached.")
        user = ("REQUEST:\n" + (request_txt or "").strip()[:2000]
                + "\n\nMEASURED GEOMETRY (metres):\n" + json.dumps(metrics, indent=1) + _fid_note)
        if prior_feedback and previous_plan:
            user += ("\n\nYour PREVIOUS plan (which you must REVISE, not repeat) was:\n"
                     + json.dumps(previous_plan, indent=1)
                     + "\n\nIt FAILED - the concrete critique:\n" + prior_feedback.strip()[:1400]
                     + "\n\nOutput a REVISED plan that DIRECTLY fixes this. Change the specific "
                       "value(s) the critique names by a MEANINGFUL amount (do not restate the same "
                       "numbers); raise max_cells too if the fix (bigger domain / finer body) needs it.")
        elif prior_feedback:
            user += ("\n\nThe PREVIOUS attempt FAILED - REVISE the plan to fix this specific problem:\n"
                     + prior_feedback.strip()[:1400])
        _system = INTERNAL_PLANNER_SYSTEM if flow_regime == "internal" else PLANNER_SYSTEM
        messages = [{"role": "system", "content": _system},
                    {"role": "user", "content": user}]
        # PUBLIC TRACE - a real provider round, so it gets the same lifecycle every
        # other one gets. The planner is reached through the deterministic Snappy
        # driver, which does not make it deterministic.
        _rid, _t0 = await _plan_trace_begin(publish, job_id, attempt, native_attempt)
        # The plan is a real round on the builder's lane, so its thinking streams into the card
        # _plan_trace_begin just opened instead of landing whole once the plan is already decided.
        # Offered only to a router that declares it, exactly as the builder's round does. The
        # sink is optional, and this call goes through a module-level facade that callers legitimately
        # replace; passing an argument such a stand-in never declared raises inside the broad
        # try below, which does not fail loudly - it silently yields NO plan at all.
        from meshpipeline.agents.loop.tracing import accepts_reasoning
        _kw = {}
        _sink = _plan_reasoning_sink(publish, job_id, attempt, _rid)
        if _sink is not None and accepts_reasoning(llm_router.call_planner_model):
            _kw["on_reasoning"] = _sink
        round_result = await llm_router.call_planner_model(
            messages, tools=None, tool_choice="none", job_id=job_id, **_kw)
        if round_result.failure_marker:
            await _plan_trace_end(publish, _rid, _t0, None, phase="failed")
            logger.warning("Planner: model call failed (%s) - builder will self-plan - job_id=%s",
                           round_result.failure_marker, job_id)
            _log_plan_event(job_id, {"status": "api_failure",
                                     "api_failure": round_result.failure_marker,
                                     "system_snapshot": _system,
                                     "user_message": user},
                            op_id=f"planner:{attempt}:{native_attempt}", attempt=attempt)
            return PlanOutcome(None, round_result, round_result.failure_marker)
        await _plan_trace_end(publish, _rid, _t0, round_result)
        content = round_result.assistant_text
        m = re.search(r"\{.*\}", content, re.S)
        plan = json.loads(m.group(0)) if m else None
        _had_usage = bool(round_result.input_tokens or round_result.output_tokens)
        _log_plan_event(job_id, {
            "status": "ok" if plan else "unparseable",
            "flow_regime": flow_regime,
            "is_revision": bool(prior_feedback or previous_plan),
            "system_snapshot": _system,
            "user_message": user,
            "response_text": content,
            "plan": plan,
            "usage": {"prompt_tokens": round_result.input_tokens,
                      "completion_tokens": round_result.output_tokens}
            if _had_usage else None,
        }, op_id=f"planner:{attempt}:{native_attempt}", attempt=attempt)
        if plan:
            logger.info("Planner: plan for %s - approach=%r quality=%s n_layers=%s max_cells=%s",
                        job_id, str(plan.get("approach"))[:60], plan.get("quality"),
                        plan.get("n_layers"), plan.get("max_cells"))
        return PlanOutcome(plan, round_result)
    except StaleExecutionPublish:
        raise
    except Exception:
        logger.exception("make_mesh_plan failed - job_id=%s", job_id)
        return PlanOutcome()


def _plan_reasoning_sink(publish: ExecutionEventPublisher | None, job_id: str, attempt: int,
                         rid: str):
    # The planner publishes through the same ownership-checked contract the builder's rounds use,
    # so it takes the awaitable sink rather than the synchronous one.
    from meshpipeline.agents.loop.tracing import ExecutionTraceContext, areasoning_sink
    if publish is None:
        return None                          # an untraced plan keeps exactly its old path
    return areasoning_sink(ExecutionTraceContext(publisher=publish, job_id=str(job_id),
                                                 role="builder", attempt=int(attempt)), rid)


async def _plan_trace_begin(publish: ExecutionEventPublisher | None, job_id: str, attempt: int,
                            native_attempt: int = 1):
    import time as _t
    if publish is None:
        return "", 0.0
    try:
        from meshpipeline.trace.policy import reasoning_id
        # Each re-plan is its own ROUND of this attempt. Without the native pass here every
        # re-plan republished one reasoning id, so the passes overwrote each other's trace.
        rid = reasoning_id(str(job_id), "builder", int(attempt), int(native_attempt) - 1)
        await publish.areasoning(rid, "started", status="active")
        return rid, _t.monotonic()
    except StaleExecutionPublish:
        raise
    except Exception:          # observability never costs a plan
        return "", _t.monotonic()


async def _plan_trace_end(publish: ExecutionEventPublisher | None, rid: str, t0: float, result,
                          *, phase: str = "completed") -> None:
    import time as _t
    if not rid or publish is None:
        return
    try:
        tokens = int(getattr(result, "reasoning_tokens", 0) or 0) if result else 0
        content = str(getattr(result, "reasoning_text", "") or "") if result else ""
        await publish.areasoning(rid, phase, duration_ms=int((_t.monotonic() - t0) * 1000),
                                 token_count=tokens or None, content=content or None,
                                 status="success" if phase == "completed" else "failure")
    except StaleExecutionPublish:
        raise
    except Exception:
        pass
