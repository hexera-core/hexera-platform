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

Guidance: reference_length_m REPORTS the metres value of ONE of the user's quoted units - if they sized the far-field in 'chords' and stated the chord, this is that chord in metres; null when they quoted no reference length. It changes nothing about how you size the box - it is how the delivered box is JUDGED, in the user's own unit. domain_margin stays in body-lengths L, measured from the body's bounding box, and each key names a DIRECTION - do not guess them: 'up' is UPSTREAM and 'down' is DOWNSTREAM along the flow; 'vert' is ABOVE AND BELOW the body, perpendicular to the flow - this is where a lifting body needs room and it must never be 0; 'side' is SPANWISE, and it is the only one that may be 0, for a slab whose symmetry planes sit on its own end faces. Size the far-field from your own knowledge of standard external-aero practice so the outer boundaries never disturb the flow around the body. It scales with the regime (a compressible/transonic case needs a MUCH larger domain than a low-speed one, to avoid blockage and wave reflection) and a lifting body needs extra length downstream for its wake. Do NOT under-size it - a cramped far-field is a common failure the reviewer rejects. n_layers 3-5. first_layer_rel 0.25-0.5. Quality: START WITH 'balanced'. Choose 'strict' only when a PREVIOUS attempt of this job MEASURED widespread skew (the critique will say so) - strict enforces checkMesh skew<4 but its mechanics suppress prism-layer inflation on small or sharp geometry, so defaulting to it trades a failure you might get for a layer failure you will get.

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


#: The block's own name. It is written by the measurement package and checked here, so a dict from
#: some other artefact cannot arrive in the planner's prompt wearing this key.
GEOMETRY_AGENT_BLOCK_SCHEMA = "geometry_agent.planner_block.v1"

#: Every key the planner is willing to put in front of a model, and nothing else. The block is
#: composed by a separate pinned distribution: an allowlist is what stops a field added there from
#: reaching a customer's plan here without anyone deciding it should.
#:
#: `seed_point_m` is deliberately absent and its absence is asserted by a test. The seed check has
#: never run once (dev_plan_v3 W8, the card reads `untestable 240`), the agent's seed differs from
#: the brief's on 46 of 61 cases with 4 outside the part's own bounding box, and neither planner
#: prompt has a seed field for it to land in.
GEOMETRY_AGENT_BLOCK_KEYS = (
    "status", "missing", "missing_because", "representation",
    "agent_forecast_cells", "agent_forecast_low", "agent_forecast_basis", "forecast_calibration",
    "customer_cell_cap", "inlet_bore_m", "inlet_opening_id", "smallest_port_min_dim_m",
    "places", "source_sha256", "agent_git_sha", "units",
    #: WORDS, never a number. The measurement package removes every digit from the description before
    #: it is stored, and it strips out the fields its own ledger found untrustworthy before the block
    #: is composed, so what arrives under this key is already the part a planner may act on. The key is
    #: absent when nothing looked, which is what leaves the prompt where it is today.
    "look",
    #: THE SURVEY: what the customer's own answers settled about this file and what they did not,
    #: every value carrying its kind. Written by the measurement package's `contract.deliver`, checked
    #: by its own validator below, and absent where no survey was composed for these bytes.
    "survey",
    #: What the measurement has and will not state as a place, with the reason: a distance on a wall
    #: shell that could be the bore or the outside, a thin place whose side is not settled. A refusal
    #: that travels is a planner that knows to be careful there; dropped, it was silence.
    "places_refused",
    #: The coverage facts beside the places: points provably outside the flow domain (a leak check),
    #: the named wall regions, and which two facts were refused by name. Measured, in metres.
    "coverage",
    #: From the wired chain (`geometry_agent.chain.job`): intake's write-up with confirmed and assumed
    #: kept apart, the flow patches with who each role came from, and the envelope the PLAN costs on
    #: the builder emulator. Absent on a job the chain did not run.
    "intake", "flow_patches", "plan_envelope",
)

#: What the planner is never handed, whatever the block says, with the reason it is refused.
GEOMETRY_AGENT_BLOCK_REFUSED = {
    "seed_point_m": "the seed check has never run and no planner prompt has a field for it",
    "cell_estimate_refined": "the refinement-ball estimate, off by a median 1.86x and a max 576x",
}


# WHY THIS NOTE NO LONGER CARRIES A RATIO OF ITS OWN.
#
# It used to say "it has come in at about 0.52x the cells actually delivered... treat it as a FLOOR -
# the real mesh has usually been about twice it". Both halves became wrong on 2026-09-25, when a
# per-engine correction of the emulator's forecast shipped.
#
# MEASURED in the running api container (agent wheel 0.1.0+g4a6ba99954):
#     hexera.correction_info('external', 'snappy') -> multiplier 1.8391, applied True
# So a snappy/external job whose raw envelope is 1.00M now arrives here as 1.84M, and this note told
# the planner the real mesh is usually about twice THAT - roughly 3.7M. Measured delivered/raw on the
# 31 geometries the factor was fitted on: median 1.839, p90 2.113, max 2.121. The planner was being
# aimed about 1.7x above the worst case ever recorded on that population.
#
# The 0.52 was also pooled across arms that disagree: FACTS_ONLY_CALIBRATION's own by_representation
# is carve 0.69, external 0.42, fluid_domain 0.34, so the single figure was wrong for every arm even
# before the correction existed.
#
# A number repeated in prose beside the field that carries it is a second thing to keep in step, and
# this is what the first divergence cost. The note now points at the field.

#: How to read the block, sent ONLY when a block is present. It says the two things a model cannot
#: work out from the keys: that the cell envelope systematically UNDER-counts before a plan exists,
#: and that the places are measured rather than suggested.
_AGENT_BLOCK_NOTE = """

ABOUT "geometry_agent" IN THE DICT ABOVE: it is a separate measurement of the customer's own file, in metres, made before any plan existed. Read it as follows.
- agent_forecast_cells is an ENVELOPE, not an estimate of your plan: read it as a floor and do NOT shave max_cells towards it. HOW FAR OUT IT HAS BEEN IS IN forecast_calibration, WHICH IS MEASURED PER RUN - read median_ratio and p90_ratio there and use those, not a number from this note. The envelope you are given may ALREADY have been corrected for the error that calibration describes, so multiplying it again by that same ratio counts the correction twice.
- inlet_bore_m is the bore the builder will size from. cells_across_diameter is defined relative to exactly this number.
- smallest_port_min_dim_m is the smallest port the mesh has to resolve; your wall cell has to fit several cells across it.
- places are MEASURED locations where this part needs more attention than normal, in the order the part needs it: the flow path first (a throat, a plate or baffle in the passage, a junction, a change of section, a bend, a passage end), then what limits the cell size (a narrow gap, a thin separation, a narrow passage, a small or tilted port mouth, a thin wall). Each has "where_m" (null with "where_missing" when it has no position) and "measurement". They describe the geometry, not the mesh: name them in "focus" and decide the settings yourself.
- places_refused lists what the measurement has but will not state as a place, with the reason. Treat those spots as unknown, not as ordinary.
- status "degraded" means the listed fields in "missing" could not be measured. Fields that are absent were not measured; do not infer a value for them.
- If this block and the customer's own text disagree, the CUSTOMER is right: they can see the part and this is a measurement of a file."""

#: How to read `look`, sent ONLY when a look is present. It rides with the look and not with the block,
#: because a note describing a key the model will not find is the failure the block's own note avoids.
#:
#: The two tiers are the whole of it. A look is words from rendered views and it carries no number; what
#: separates the two groups is not confidence, it is a measurement of how often each field was right over
#: 322 parts, and the block already states which group each field is in. Nothing here asks the model to
#: judge that for itself.
_AGENT_LOOK_NOTE = """

ABOUT "look" INSIDE "geometry_agent": a vision model was shown rendered views of this part and described it in words. It is NOT a measurement and it contains no number - every digit was removed before it was stored.
- "relied_on" was measured against 322 parts and earned its place. Attachments and flanges in particular are found at full recall and no measurement reports them at all, so if one is named, it is there. An "inside_is_plain" of true means nothing was seen across the passage, and nothing has been.
- "candidates" are places to look at, not facts. The identity is right about two times in three and is not reproducible; an internal feature repeats as a finding but not as a wording. Size for them if it is cheap to; do not justify a level by one of them alone.
- "withheld" lists what the look said that you are NOT being shown, with the reason. Do not ask for it and do not infer it. The measured dict above already has the orientation, the symmetry and the opening classes exactly.
- "at_places", when present, is one finding per place the MEASUREMENT chose and drew before the model saw the picture: "where_m" is a measured coordinate and "the_look_says" is the model's words about that spot. A null "where_m" is never a guess, and "declined" is the look saying it could not read that place.
- The look never overrides a measured number, a port, a bore or a count. Where the two disagree, the measurement is right."""


#: How to read `survey`, sent ONLY when a survey is present, for the same reason the look's note rides
#: with the look.
#:
#: IT NAMES THE LOOK ROW, and it had to. `unsettled` carries one row per thing nobody settled, and the
#: platform puts a row there for WHAT HAPPENED TO THE LOOK (`geometry_survey.with_the_look_state`): a look
#: that failed, a look still running, a look never taken. Every other row names mouths, and this note used to
#: say only that - "a mouth listed there has no role anybody confirmed" - so the one row with no mouth in it
#: reached the model with nothing telling it how to read it. `look` is absent in exactly those three states,
#: so `_AGENT_LOOK_NOTE` does not ride either: this row is the only thing the model gets, and a row nobody
#: explained is a row nobody uses.
_AGENT_CHAIN_NOTE = """

ABOUT "intake", "flow_patches" AND "plan_envelope" INSIDE "geometry_agent": the job ran through the geometry chain.
- intake: "confirmed" is what the customer answered; "assumed" is a default nobody confirmed (report back on those); "from_the_brief" is what their own text said.
- flow_patches: each opening's role and who it came from (customer, brief or the geometry agent's own decision). A role the customer confirmed is theirs; do not change it.
- plan_envelope: the cells the geometry agent's plan costs on the builder's own sizing, with its source. It is an envelope, not a target."""


_AGENT_COVERAGE_NOTE = """

ABOUT "coverage" INSIDE "geometry_agent": "outside" holds points provably outside the flow domain (outside every closed surface of the part), usable as a leak check; "wall" names the part's surface regions by shape; "refused" names facts that were decided not to be given because the number would describe the tessellation rather than the part."""


_AGENT_SURVEY_NOTE = """

ABOUT "survey" INSIDE "geometry_agent": the customer was shown what the measurement could not settle about THIS file and asked. Every value says what kind of claim it is ("kinds" is the legend).
- "confirmed" is the customer's own answer, with when they gave it. A role there says which measured mouth is which; it is the customer's decision about their part, and nothing else in this message overrides it.
- "unsettled" is what nobody settled. A mouth listed there has no role anybody confirmed: do not reason about it as an inlet or an outlet.
- One "unsettled" row may have "about": "look" and no mouths. It says what happened to the LOOK of this part, and there are three things it can say: the look was never taken, the look has been queued and has not come back yet, or the look FAILED. In all three there is no "look" key above and nothing in this message is a look finding. A look that failed is NOT a part with nothing to report: treat the absence as unknown, never as clear.
- A "cell_budget" is the customer's own number. "stated" means they wrote it; "confirmed" means they chose it when shown what resolving the part costs, and max_cells must not exceed it. customer_cell_cap above is the same number.
- The survey describes the part. It names no mesh setting and predicts nothing about the mesh: the settings are yours."""


def _survey_is_sound(survey: object, job_id: str) -> bool:
    """The measurement package's own validator over the survey, or a refusal. Never raises.

    `contract.deliver.check_survey_block` is the contract in executable form: every value carries a
    known kind, no digit from the look, no field the trust ledger withholds from a model, and the
    block small enough that nothing ever has to be cut. It is the package's function and it is run
    here rather than trusted, because the block is authored in a separately pinned distribution. An
    image that cannot import it cannot vouch for the block, so the key is refused rather than guessed.
    """
    try:
        from geometry_agent.contract.deliver import check_survey_block
    except Exception as exc:                       # noqa: BLE001 - no validator is no survey
        logger.info("Planner: the survey was refused, its validator is not installed here - job_id=%s: %s",
                    job_id, exc)
        return False
    try:
        check_survey_block(survey)
    except Exception as exc:                       # noqa: BLE001 - a broken contract is a refused key
        logger.warning("Planner: the survey broke its contract and was refused - job_id=%s: %s", job_id, exc)
        return False
    return True


def _validated_agent_block(block: object, job_id: str) -> dict | None:
    """The measurement package's block, checked before it reaches a prompt. None to add no key.

    Three things are checked and each has cost something before. That the block names itself, so a
    stray dict cannot ride in under this key. That it declares a `status`, because a partial block
    silently missing two numbers is read by a model as a part that has neither. And that every key
    is one this planner chose to show, because the block is authored in a separately pinned
    distribution that may move without this file.
    """
    if not isinstance(block, dict) or not block:
        return None
    if str(block.get("schema") or "") != GEOMETRY_AGENT_BLOCK_SCHEMA:
        logger.warning("Planner: a geometry block named %r was refused - job_id=%s",
                       block.get("schema"), job_id)
        return None
    if str(block.get("status") or "") not in ("ok", "degraded"):
        # A block with no status is the failure mode this whole contract exists to prevent: the
        # prompt calls the measured dict complete, so a silent absence reads as a measurement.
        logger.warning("Planner: a geometry block with no status was refused - job_id=%s", job_id)
        return None
    out = {k: block[k] for k in GEOMETRY_AGENT_BLOCK_KEYS if k in block}
    if "survey" in out and not _survey_is_sound(out["survey"], job_id):
        # The survey alone is refused. The measurement beside it is still a measurement.
        out.pop("survey")
    dropped = sorted(set(block) - set(out) - {"schema", "facts_sha256", "facts_schema_version", "survey"})
    if dropped:
        logger.info("Planner: geometry block keys with no reader here were dropped - job_id=%s: %s",
                    job_id, dropped)
    return out or None


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
                         geometry_agent: dict | None = None,
                         surface=None) -> dict | None:
    return (await plan_with_accounting(
        workspace=workspace, job_id=job_id, request_txt=request_txt,
        prior_feedback=prior_feedback, previous_plan=previous_plan,
        flow_regime=flow_regime, mesh_fidelity=mesh_fidelity,
        geometry_agent=geometry_agent, surface=surface)).plan


async def plan_with_accounting(*, workspace, job_id: str, request_txt: str,
                               prior_feedback: str = "",
                               previous_plan: dict | None = None,
                               flow_regime: str = "external",
                               mesh_fidelity: str = "",
                               publish: ExecutionEventPublisher | None = None,
                               attempt: int = 1,
                               native_attempt: int = 1,
                               plan_call: int = 0,
                               geometry_agent: dict | None = None,
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
        # THE SECOND CHANNEL. `user` below cuts `request_txt[:2000]` and then serialises this dict
        # AFTER the cut, so a key added here is never truncated however long the customer's brief
        # is. Everything in `docs/handover_consumers.md` section 9 budgeted a block against
        # `min(700, max(320, 2000 - len - 40))` characters of prose; that scarcity is a property of
        # the string, not of the handover, and it ends at this line.
        #
        # ONE NAMED SUB-OBJECT, not seven loose keys. The prompt above already promises the geometry
        # "has ALREADY been measured for you" and names four things; seven more bare keys of
        # different authorship read as more of the same, while one named object carries its own
        # provenance and, when the agent did not run, is visibly ABSENT rather than silently missing
        # from a dict the prompt calls complete.
        #
        # None is the whole fail-open contract at this boundary: no key, and the dict the planner
        # serialises is byte-for-byte the dict it serialises today.
        _agent_block = _validated_agent_block(geometry_agent, job_id)
        if _agent_block is not None:
            metrics["geometry_agent"] = _agent_block
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
        # The note rides with the block and only with it. The system prompt above is not touched:
        # with no block it would describe a key the model will not find, and a prompt that promises
        # a measurement that is not there is the exact failure this phase exists to remove.
        _agent_note = _AGENT_BLOCK_NOTE if _agent_block is not None else ""
        # The look's note rides with the look, one gate further in than the block's own note. With the
        # vision setting off, or a look that failed, the block carries no `look` key and this string is
        # empty, so the user message is byte for byte the message with only the measurement.
        if _agent_block is not None and _agent_block.get("look"):
            _agent_note += _AGENT_LOOK_NOTE
        # And the survey's note with the survey, one gate further in again: absent unless the survey
        # was switched on and composed, so every message without one is the message it was before.
        if _agent_block is not None and _agent_block.get("survey"):
            _agent_note += _AGENT_SURVEY_NOTE
        # the chain's and the coverage's notes ride with their keys, one gate further in again, so a block
        # without them is the message it was before they existed
        if _agent_block is not None and (_agent_block.get("intake") or _agent_block.get("plan_envelope")
                                         or _agent_block.get("flow_patches")):
            _agent_note += _AGENT_CHAIN_NOTE
        if _agent_block is not None and _agent_block.get("coverage"):
            _agent_note += _AGENT_COVERAGE_NOTE
        user = ("REQUEST:\n" + (request_txt or "").strip()[:2000]
                + "\n\nMEASURED GEOMETRY (metres):\n" + json.dumps(metrics, indent=1)
                + _agent_note + _fid_note)
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
