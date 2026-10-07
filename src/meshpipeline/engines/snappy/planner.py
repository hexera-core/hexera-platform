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

Guidance: reference_length_m REPORTS the metres value of ONE of the user's quoted units - if they sized the far-field in 'chords' and stated the chord, this is that chord in metres; null when they quoted no reference length. When a reference length is stated, domain_margin is in THAT unit - the number the user asked for is the number you write ('5 diameters upstream' is up: 5), the box is built as multiples of it from the body's bounding box, and the delivered box is judged in it; with no stated reference, domain_margin is in body-lengths L (the streamwise extent), measured from the body's bounding box. Each key names a DIRECTION - do not guess them: 'up' is UPSTREAM and 'down' is DOWNSTREAM along the flow; 'vert' is ABOVE AND BELOW the body, perpendicular to the flow - this is where a lifting body needs room and it must never be 0; 'side' is SPANWISE, and it is the only one that may be 0, for a slab whose symmetry planes sit on its own end faces. Size the far-field from your own knowledge of standard external-aero practice so the outer boundaries never disturb the flow around the body. It scales with the regime (a compressible/transonic case needs a MUCH larger domain than a low-speed one, to avoid blockage and wave reflection) and a lifting body needs extra length downstream for its wake. Do NOT under-size it - a cramped far-field is a common failure the reviewer rejects. n_layers 3-5. first_layer_rel 0.25-0.5. Quality: START WITH 'balanced'. Choose 'strict' only when a PREVIOUS attempt of this job MEASURED widespread skew (the critique will say so) - strict enforces checkMesh skew<4 but its mechanics suppress prism-layer inflation on small or sharp geometry, so defaulting to it trades a failure you might get for a layer failure you will get.

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

Guidance: cells_across_diameter 16-40 - how many cells span the bore. Use the HIGH end for curved/bending or branching passages (to capture secondary flow), the low end for a plain straight duct. surface_level 1-3 (wall refinement). feature_level = surface_level+1 (sharpens the inlet/outlet rims). n_layers 3-6 - internal wall-bounded flow is MORE layer-critical than external aero (y+ and pressure drop depend on it). first_layer_rel 0.2-0.4. Quality: START WITH 'balanced'; choose 'strict' only when a previous attempt of this job MEASURED widespread skew (strict suppresses layer inflation, and internal flow is layer-critical). max_cells is a lever - raise it when a long or geometrically complex passage at your chosen resolution needs the cells, but NEVER exceed max_cells_HARD_CEILING (the compute limit). There is NO domain_margin here - the part is the domain, so spend every cell inside the passage.

REVISING: if shown your PREVIOUS plan with a concrete critique, change the offending value(s) by a MEANINGFUL amount - do not restate the same numbers. Widespread skew -> quality 'strict' and/or fewer/thinner layers; a timeout or over-budget -> lower cells_across_diameter or surface_level. Low or zero LAYER COVERAGE: if quality was 'strict', switch to 'balanced' FIRST (strict's layer mechanics are usually the cause); and never respond by thinning first_layer_rel further - layers that collapsed collapse HARDER when thinner (a real case walked 0.35 -> 0.25 and coverage fell 42% -> 24%). If the previous revision moved a knob and the measured coverage got WORSE, reverse that knob past its original value instead of continuing. Treat the critique as a measurement of how far off you were."""


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


#: After a pass runs out of time the next one must be MEANINGFULLY smaller. The wall cell of an
#: internal plan is bore / cells_across_diameter and the near-wall cells dominate, so the count
#: goes with cells_across squared: 0.7 of it is about half the cells. A proposal already at or
#: below the COARSER_ENOUGH fraction of the timed-out value (about 28% fewer cells across, or a
#: 40% smaller budget) is accepted as it stands.
TIMEOUT_CELLS_ACROSS_FACTOR = 0.7
TIMEOUT_BUDGET_FACTOR = 0.5
COARSER_ENOUGH_CELLS_ACROSS = 0.85
COARSER_ENOUGH_BUDGET = 0.6
_MIN_CELLS_ACROSS = 8                    # the internal driver's own floor (drivers.py)
_MIN_TIMEOUT_BUDGET = 200_000


def coarsen_after_timeout(timed_out: dict, proposed: dict, *, ceiling: int,
                          internal: bool) -> tuple[dict, list[str]]:
    """The plan for the pass after one that RAN OUT OF TIME - never the same mesh again.

    Job 470c3eb9: snappyHexMesh ran 50 minutes, the result was reported as "never started",
    the planner was told to resubmit unchanged, and pass 2 rebuilt the identical case for
    another 50 minutes. Left to the model, a timeout is not reliably answered with a smaller
    mesh, so it is enforced here, by arithmetic, against the plan that timed out:

    - the proposal is kept when it is already meaningfully coarser - fewer cells across the bore
      (internal) or a much smaller cell budget;
    - otherwise the timed-out plan's resolution is cut to about half the cells: cells across
      the bore x0.7 (internal; the wall cell grows ~1.4x) and the cell budget x0.5, which also
      halves what local thin-feature refinement may spend. Lowering surface_level alone is
      NOT coarser here: the internal wall cell is bore / cells_across whatever the level.

    Returns (strategy, changes): `changes` names each value cut, in plain words, empty when the
    proposal stood. When nothing is left to cut (cells across already at its floor and the
    budget at its own), the strategy comes back unchanged with no changes - the caller's
    identical-case stop then refuses to run it again.
    """
    out = dict(proposed or {})
    t_budget = clamp_cell_budget((timed_out or {}).get("max_cells"), ceiling=ceiling)
    p_budget = clamp_cell_budget(out.get("max_cells"), ceiling=ceiling)

    def _ca(plan: dict) -> int:
        try:
            return max(_MIN_CELLS_ACROSS, int(plan.get("cells_across_diameter", 24)))
        except (TypeError, ValueError):
            return 24

    t_ca, p_ca = _ca(timed_out or {}), _ca(out)
    coarser = p_budget <= COARSER_ENOUGH_BUDGET * t_budget
    if internal:
        coarser = coarser or p_ca <= int(COARSER_ENOUGH_CELLS_ACROSS * t_ca)
    if coarser:
        return out, []
    changes: list[str] = []
    if internal:
        ca = max(_MIN_CELLS_ACROSS, min(p_ca, int(TIMEOUT_CELLS_ACROSS_FACTOR * t_ca)))
        if ca < t_ca:
            out["cells_across_diameter"] = ca
            changes.append(f"cells across the bore cut from {t_ca} to {ca}")
    budget = max(_MIN_TIMEOUT_BUDGET, min(p_budget, int(TIMEOUT_BUDGET_FACTOR * t_budget)))
    if budget < t_budget:
        out["max_cells"] = budget
        changes.append(f"cell budget cut from {t_budget / 1e6:.2g} M to {budget / 1e6:.2g} M")
    return out, changes


#: The headroom a refined plan's cell budget keeps over the least the refinement costs (the count
#: grown by the square of the factor): snappy's maxGlobalCells stops refining at the budget, so a
#: plan asked for more cells across under its old budget would come out as coarse as before.
REFINE_BUDGET_HEADROOM = 1.5
#: The share of the hard cell limit a refined attempt's projected count (the count grown by the
#: square of the factor) may reach before the factor is held back to fit it.
REFINE_CEILING_SHARE = 0.95


def refine_after_under_resolved(gated: dict | None, proposed: dict, *, measured: object,
                                needed: object, cells: object = None,
                                ceiling: int) -> tuple[dict, list[str]]:
    """The internal plan for the attempt after a mesh too coarse across its passage - one that
    actually puts the floor (plus one) across it.

    The internal wall cell is bore / cells_across_diameter, whatever the surface level. Job
    02ed0d14's re-plan was told to "refine the wall surface level", raised it expecting twice the
    cells across, and moved cells_across only 24 -> 28: the narrowest passage went 8.3 -> 9.8 and
    the attempt was spent. So the number is set here, by arithmetic, against the plan whose mesh
    `measured` cells across where `needed` were: cells across x (needed + 1) / measured, never
    fewer than proposed. The cell budget follows (the least the refinement costs, with headroom,
    up to `ceiling`), or snappy would stop refining at the old one.

    Returns (strategy, changes) - `changes` in plain words, empty when the proposal already asks
    for enough (or nothing is known to refine against)."""
    import math

    def _finite(v: object) -> float | None:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v) if math.isfinite(float(v)) else None

    out = dict(proposed or {})
    m, n = _finite(measured), _finite(needed)
    if not gated or m is None or n is None or m <= 0.0 or m >= n:
        return out, []

    def _ca(plan: dict) -> int:
        try:
            return max(_MIN_CELLS_ACROSS, int(plan.get("cells_across_diameter", 24)))
        except (TypeError, ValueError):
            return 24

    factor = (n + 1.0) / m
    c = _finite(cells)
    # ... WITHIN THE HARD LIMIT. A wall-bound mesh grows with the square of the factor (its wall
    # cells and the prism layers on them), and a rebuild projected past `ceiling` is refused at
    # the manifest gate after a full run: annular_001 at 5.45 M cells and 10 across was sent to
    # x1.3 (~9 M). Over it, the factor is held to the limit, though never below what puts the
    # floor itself across (needed / measured) - past THAT, no rebuild can help, and the retry
    # policy does not start one (failure_cause.retry_can_help).
    held = c is not None and c > 0.0 and c * factor ** 2 > REFINE_CEILING_SHARE * float(ceiling)
    if held and c is not None:
        factor = max(n / m, min(factor, math.sqrt(REFINE_CEILING_SHARE * float(ceiling) / c)))
        # rounded DOWN to a whole count, never under the floor's own: rounding up put
        # annular_001's rebuild at 8.005 M cells against the 8 M limit
        want = max(int(math.ceil(_ca(gated) * n / m)), int(math.floor(_ca(gated) * factor)))
    else:
        want = int(math.ceil(_ca(gated) * factor))
    have = _ca(out)
    changes: list[str] = []
    if have < want:
        out["cells_across_diameter"] = want
        changes.append(f"cells across the bore raised from {have} to {want}")
    elif held and have > want:
        # a re-plan cannot ask for a rebuild the hard limit would refuse after a full run
        out["cells_across_diameter"] = want
        changes.append(f"cells across the bore held to {want} (asked {have}) by the "
                       f"{float(ceiling) / 1e6:.2g} M cell limit")
    if c is not None and c > 0.0:
        floor_budget = min(int(ceiling), int(REFINE_BUDGET_HEADROOM * c * factor ** 2))
        p_budget = clamp_cell_budget(out.get("max_cells"), ceiling=ceiling)
        if p_budget < floor_budget:
            out["max_cells"] = floor_budget
            changes.append(f"cell budget raised from {p_budget / 1e6:.2g} M to "
                           f"{floor_budget / 1e6:.2g} M")
    return out, changes


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
                               plan_call: int = 0,
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
                            op_id=f"planner:{attempt}:{native_attempt}:{plan_call}",
                            attempt=attempt)
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
            # The op identity carries WHICH planner round of this node execution this is
            # (plan_call, from the driver run's own control flow): the initial plan and a
            # repair re-plan are separate operations, not one operation arriving twice with
            # two payloads (that spelling was quarantined as a CONFLICTING replay).
        }, op_id=f"planner:{attempt}:{native_attempt}:{plan_call}", attempt=attempt)
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
