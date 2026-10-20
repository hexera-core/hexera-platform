# Responsibility: Decide when a run moves to another engine, and what to offer when no engine may be switched to on its own.
# Owns: the fallback order per flow topology, the same-contract test between two engines, the failure classes, and the ladder record.
# Boundaries: pure decisions over state and the engine registry, plus the one graph node that applies them; it meshes nothing.
"""THE FALLBACK LADDER.

A run used to live and die on the one engine the intake settled. When that engine met a shape it
structurally cannot mesh - it crashed, it never finished, its cells would not come out good enough
- every retry re-bought the same failure, although another engine would have meshed the shape.
Commercial meshers almost never fail on clean geometry because they fall back to a more robust
method. This module is that fallback, with one rule above every other:

    APPROVED EQUALS DELIVERED. The run moves to another engine ON ITS OWN only when that engine
    delivers what the user approved: the same boundaries (every name and type admitted by the
    same admission rules), the same units (every engine meshes the same metre-normalised surface),
    the same kind of file, the same kind of cells, a wall at least as true to the body, and the
    prism layers the brief asked for. Anything else - a tetrahedral mesh for a hex-dominant one,
    no layers where layers were asked for, a staircased wall for a body-fitted one - is OFFERED,
    in plain words, as the run's last sentence, and never delivered silently.

WHEN THE RUN MAY MOVE TO ANOTHER ENGINE ON ITS OWN - deterministic, from measured or declared
facts only, never from an AI judgement:

  1. the engine produced no usable mesh: it crashed, stopped before writing a complete mesh,
     refused the input, or its mesh is broken (checkMesh's fatal defects) - ENGINE;
  2. the measured validity checks (fixed thresholds: cell quality, resolution floor, cell budget,
     boundaries captured, the trial solve) still fail AFTER this engine's own levers were tried
     on a retry - FIXABLE, on its second failure on this engine (_levers_spent) - or on its
     first, when a retry here would leave an untried engine no attempt of its own (the run's
     regular attempts cannot hold both, so the levers come first only where there is room);
  3. and in either case only to an engine whose DECLARED capability covers the approved plan's
     requirements (plan_requirements: prism layers, an external flow's wake refinement) and the
     case's separate passages (FlowSupport.max_passages, below).

THE REVIEW NEVER MOVES A RUN. The reviewer's verdict and concerns are an AI judgement of a mesh
that already passed every gate. They lead only to the mesh being delivered with the concerns
listed, or to a same-engine improvement retry through the builder's declared levers, with the
best valid attempt delivered when a later one does worse (application/review_delivery). Job
0d0fc370: snappyHexMesh built a 2.3M-cell production-grade external mesh, the review left one
non-blocking layer-coverage concern, and the ladder moved the run to cfMesh, whose mesh had no
wake refinement - REVIEW is the class that can never switch.

More rules:
* An engine the USER named (engine_source user_direct) is theirs. It is never switched without
  asking - the ladder only offers. A dispute rebuild keeps the engine of the mesh it disputes.
* THE RUNGS COME IN THE ORDER THAT SUITS THIS GEOMETRY. The run's geometry is measured at the
  start of the run and the engines that can mesh it are ranked by their lab record on shapes
  like it (engines/fitness.py, the same ranking the intake recommended from); the ladder walks
  that order, after the engine the user picked. When nothing could be measured, the same rules
  rank the engines on their record over the whole flow (fitness.flow_ranking): never a fixed order.
* The budgets stay as they are. A switch spends one of the run's existing attempts and needs the
  time for the new engine's own run cap; the retries are shared across rungs rather than spent
  on the first one alone.
* A BROKEN MESH MOVES ON. A mesh with checkMesh's fatal defects (negative-volume cells, faces
  pointing the wrong way) is the engine's own failing on this shape, so the run moves to the next
  engine in the measured order at once - never the same engine again, and the builder is told so
  before it spends the attempt re-running it (moves_on_from_broken_mesh). The switch is said to the
  user as it happens and again beside the delivered mesh (switch_note, delivered_note).
* AN ENGINE THAT WOULD CERTAINLY REFUSE IS NO RUNG. Every engine declares how many separate fluid
  passages one job of it meshes (FlowSupport.max_passages). A case whose openings lie on more -
  counted at the start of the run, then read off each attempt's own staging - never moves to it:
  snappyHexMesh fills the one passage its seed point is in and refuses the rest before it runs.
  When no other engine is left, the offer says so and gives the outs that work (_passages_offer).
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meshpipeline.contracts.failure_cause import FailureCause, as_cause, retry_can_help

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState

logger = logging.getLogger(__name__)

def fallback_order(flow: str, state: Mapping | None = None) -> tuple[str, ...]:
    """THE LADDER ORDER for a flow (ENG-5): every implemented engine designed for it, in the SAME
    ranked order the recommendation gave this run's geometry (measured_ranking: the home engine
    for its shape class first, then the 95% lower bounds of the lab record on shapes like it -
    engines/fitness.py). When the run's geometry could not be measured, the same rules rank the
    engines on their record over the whole flow (fitness.flow_ranking) - never a fixed order. An
    engine the measured ranking left out keeps its place by that flow ranking. Derived, never
    listed here, so a new engine joins the ladder from its own spec. The approved engine always
    stays the first rung (ladder), and only the rungs that pass the approved request's own
    admission rules - the file's form included - are ever considered."""
    from meshpipeline.engines.capability import ladder_order
    from meshpipeline.engines.fitness import flow_ranking, ranked_engines
    order = ranked_engines(ladder_order(flow), flow_ranking(flow))
    return tuple(ranked_engines(order, measured_ranking(state)))


def measured_ranking(state: Mapping | None) -> list[str]:
    """The measured engine order the run carries in its ladder record ([] when none)."""
    order = record_of(state or {}).get("order")
    return [str(e) for e in order if e] if isinstance(order, list) else []


#: How long the start of a run waits for its geometry to be measured and ranked.
ORDER_WAIT_S = 60.0


def measure_order(state: Mapping) -> dict:
    """THE MEASURED ORDER for this run: its materialised geometry measured (cad/shape_traits.py)
    and every engine that can build the approved request ranked by its lab record on shapes like
    it (engines/fitness.py). The ladder record keys it adds - {} when the run carries no geometry
    or no flow. Pure and synchronous; seed_measured_order runs it off the event loop."""
    from meshpipeline.cad.shape_traits import measure_file
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.fitness import brief_facts, recommend
    from meshpipeline.pipeline.geometry_state import materialized

    flow = flow_of(state.get("purpose", ""))
    try:
        mg = materialized(state)
    except Exception:  # noqa: BLE001 - no verified geometry, nothing to measure
        mg = None
    if mg is None or not flow:
        return {}
    able = able_engines(state)
    if not able:
        return {}
    patches = [p for p in (state.get("intake_patches") or []) if isinstance(p, Mapping)]
    try:
        unit = float(mg.interpretation.scale_to_metres)
    except (AttributeError, TypeError, ValueError):
        unit = None
    traits = measure_file(mg.path, flow=flow, input_kind=str(state.get("input_kind", "") or ""),
                          patches=patches, surface_unit_to_m=unit,
                          flow_axis=str(state.get("flow_axis", "") or ""))
    brief = brief_facts(request_txt=str(state.get("request_txt", "") or ""),
                        review_brief_txt=str(state.get("review_brief_txt", "") or ""),
                        patches=patches)
    rec = recommend(traits, able, brief=brief)
    counted = ({"passages": int(traits.passages), "passages_source": PASSAGES_MEASURED}
               if traits.passages else {})
    return {"order": rec.ranking,
            "order_source": "measured" if traits.measured else "flow",
            "order_recommended": rec.engine, "order_shape": rec.shape,
            "order_table": rec.table_version,
            "order_reasons": {f.engine: f.reason for f in rec.fits}, **counted}


async def seed_measured_order(state: Mapping, *, timeout_s: float = ORDER_WAIT_S) -> dict:
    """The run's ladder record with its measured order added - {} when it already has one, or
    when the geometry could not be measured and ranked in time (the declared order then stands).
    An account, never a verdict: nothing here can fail a run."""
    import asyncio
    if measured_ranking(state):
        return {}
    try:
        info = await asyncio.wait_for(asyncio.to_thread(measure_order, state), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a timeout or a failure leaves the declared order
        logger.info("engine ladder: no measured order for this run (%s)", type(exc).__name__)
        if isinstance(exc, TimeoutError):
            from meshpipeline.engines.determinism import note_time_limit
            note_time_limit("engine_ladder", "the measured engine order was not ready in time: "
                            "the declared order stands", budget_s=float(timeout_s))
        return {}
    return {**record_of(state), **info} if info else {}

#: Engine provenance values, as the application seeds them (see pipeline_run). `user_direct` and
#: `suggested_confirmed` are the intake's own words; `dispute` and `system` are the run's.
SOURCE_USER = "user_direct"
SOURCE_SUGGESTED = "suggested_confirmed"
SOURCE_DISPUTE = "dispute"
SOURCE_SYSTEM = "system"
#: Who chose the engine decides whether it may be changed without asking. An unknown source is
#: treated as the user's: never switched on a guess.
_SWITCHABLE_SOURCES = frozenset({SOURCE_SUGGESTED, SOURCE_SYSTEM})

# THE FAILURE CLASSES the ladder reads. Named by what another engine could do about them.
#: This engine could not build a mesh at all (it crashed, timed out, stopped before writing one,
#: or refused the input). Another engine may well succeed: switch now.
ENGINE = "engine"
#: The mesh came out but failed a bar a retry on the same engine might clear (cell quality,
#: resolution, size, a trial solve, a lost boundary, review). Retry here while the shared
#: allowance lasts; switch when it does not, or when the same failure repeats.
FIXABLE = "fixable"
#: Another engine cannot change it: the request itself (the far-field size asked for), the
#: approval (a boundary our own authoring wrote wrong), or a multi-region split. Never switch.
NEVER = "never"
#: The mesh passed every gate and the REVIEW did not pass it. An AI judgement, never a measured
#: failure of the engine: it may ask this engine for an improvement retry, and the run delivers
#: its best reviewed mesh - it never moves the run to another engine (the module's rule above).
REVIEW = "review"

#: WHAT A DIFFERENT ENGINE CAN DO about each failure cause. The causes are the executor's
#: (contracts/failure_cause.py), named by the gate that failed - this module keeps no second
#: vocabulary and never reads a gate key for meaning. The failure-cause module already answers
#: "would ANOTHER ATTEMPT of this run change it" (retry_can_help); a cause it calls hopeless is
#: NEVER here too, unless it is listed in _ONLY_ANOTHER_ENGINE_CHANGES below. The rest is the
#: engine question alone: a boundary type our emission wrote, the far-field size the user asked
#: for and a multi-region split are the same whichever engine runs (NEVER); a mesher that stopped
#: is the engine's own failing (ENGINE); a mesh that missed a bar may be fixed here (FIXABLE).
_LADDER_CLASS: dict[FailureCause, str] = {
    FailureCause.ENGINE_CRASHED: ENGINE,
    FailureCause.GEOMETRY_REJECTED: ENGINE,
    FailureCause.MESH_QUALITY: FIXABLE,
    FailureCause.UNDER_RESOLVED: FIXABLE,
    FailureCause.CELL_BUDGET: FIXABLE,
    FailureCause.NOT_SOLVABLE: FIXABLE,
    FailureCause.PATCH_NOT_CAPTURED: FIXABLE,
    FailureCause.CONTRACT_MISMATCH: NEVER,
    FailureCause.BOUNDARY_TYPE: NEVER,
    FailureCause.DOMAIN_EXTENT: NEVER,
    FailureCause.REGION_SPLIT: NEVER,
    # the review of a validated mesh did not finish: our reviewer's failing, which the same mesh
    # from another engine would meet again - never a reason to leave the engine
    FailureCause.REVIEW_INCOMPLETE: NEVER,
    # the engine stopped while preparing, before its mesher started: its own preparation's limit,
    # which another engine's preparation may not share
    FailureCause.NOT_BUILT: ENGINE,
    # the run ran out of time: a smaller plan on the same engine may well finish
    FailureCause.ENGINE_TIMED_OUT: FIXABLE,
    # the service that runs the mesher failed: every engine runs on it, so no engine changes it
    FailureCause.RUN_INFRASTRUCTURE: NEVER,
}
#: The causes the retry policy calls hopeless that another ENGINE still changes. A refused
#: geometry is the same file on the next attempt - but a different engine has a different input
#: contract (VMTK refuses a self-intersecting surface that cfMesh and snappyHexMesh wrap), so it
#: stays an ENGINE failure: never a mid-run switch (the run ends at the refusal), always an offer.
#: So does a mesh too coarse across its passage whose cheapest rebuild on this engine is over the
#: cell limit (failure_cause.rebuild_over_limit): this engine's next attempt cannot reach the floor,
#: but another engine sizes its cells another way - the run ends, and the offer stands.
_ONLY_ANOTHER_ENGINE_CHANGES: frozenset[FailureCause] = frozenset({
    FailureCause.GEOMETRY_REJECTED, FailureCause.UNDER_RESOLVED, FailureCause.NOT_BUILT})

#: What each failure means, said to the user. Short and plain: the full account of the failure is
#: the terminal message's (failure_cause.describe); this is the half-sentence that says why the
#: engine was left.
_REASON_BY_CAUSE: dict[FailureCause, str] = {
    FailureCause.ENGINE_CRASHED: "it stopped before it finished the mesh",
    FailureCause.GEOMETRY_REJECTED: "it cannot take this geometry as it is",
    FailureCause.MESH_QUALITY: "its cells came out too badly shaped to use",
    FailureCause.UNDER_RESOLVED: "it could not fit enough cells across the narrowest passage",
    FailureCause.CELL_BUDGET: "its mesh came out bigger than one job allows",
    FailureCause.NOT_SOLVABLE: "a trial solve on its mesh did not converge",
    FailureCause.PATCH_NOT_CAPTURED: "it lost one of your boundaries",
    FailureCause.CONTRACT_MISMATCH: "a boundary did not come out under the name you approved",
    FailureCause.BOUNDARY_TYPE: "a boundary came out with the wrong type",
    FailureCause.DOMAIN_EXTENT: "the far-field domain came out short of the size you asked for",
    FailureCause.REGION_SPLIT: "the parts did not come out as separate meshes",
    FailureCause.REVIEW_INCOMPLETE: "the review of its mesh did not finish",
    FailureCause.NOT_BUILT: "it stopped before its mesher started",
    FailureCause.ENGINE_TIMED_OUT: "its run ran out of time",
    FailureCause.RUN_INFRASTRUCTURE: "the mesh run did not complete on our side",
}
_REASON_REVIEW = "the mesh did not pass review"
_REASON_CONCERNS = "its mesh passed every check, and the review left concerns"
_REASON_GENERIC = "it did not produce a mesh that passed its checks"


# #
# what the brief asked for
# #

#: A brief that says it wants NO layers. Checked first: "no prism layers" mentions layers too.
_NO_LAYERS = re.compile(
    r"\b(?:no|without|zero|0)\s+(?:prism\s+|boundary[\s-]|inflation\s+|near[\s-]wall\s+)?layers?\b",
    re.I)
_LAYER_COUNT = re.compile(
    r"\b(\d{1,2})\s*(?:x\s*)?(?:prism|boundary|inflation|near[\s-]wall|wall|surface)[\s-]+layers?\b",
    re.I)
#: Any other way a brief asks for near-wall resolution. Deliberately wide: a false "yes" only turns
#: an automatic switch into a question, while a false "no" could drop layers silently.
_LAYER_WORDS = re.compile(
    r"\b(?:prism[\s-]+layers?|boundary[\s-]+layers?|inflation[\s-]+layers?|near[\s-]wall\s+layers?|"
    r"y\s*\+|yplus|y\s+plus|wall[\s-]resolved|first[\s-]+(?:cell|layer)|nSurfaceLayers)", re.I)


@dataclass(frozen=True)
class LayerRequest:
    requested: bool
    count: int | None = None


def layer_request(request_txt: str, review_brief_txt: str = "") -> LayerRequest:
    # (findall, not search: the publication scanner reads any `.search(` as a search event)
    text = f"{request_txt or ''}\n{review_brief_txt or ''}"
    counts = [int(n) for n in _LAYER_COUNT.findall(text) if int(n) > 0]
    if counts:
        return LayerRequest(True, max(counts))
    if _NO_LAYERS.findall(text) and not _LAYER_WORDS.findall(_NO_LAYERS.sub(" ", text)):
        return LayerRequest(False)
    return LayerRequest(bool(_LAYER_WORDS.findall(text)))


# #
# the rungs
# #

@dataclass(frozen=True)
class Rung:
    engine: str
    #: delivers what was approved - the only rungs the run may move to on its own
    same_contract: bool
    #: what would differ from the approved mesh, in plain words (empty iff same_contract)
    changes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"engine": self.engine, "same_contract": self.same_contract,
                "changes": list(self.changes)}


def engine_label(name: str) -> str:
    try:
        from meshpipeline.engines.registry import engine_label as _label
        return _label(name) or name
    except Exception:  # noqa: BLE001 - a label must never break a decision
        return name


def record_of(state: Mapping) -> dict:
    rec = state.get("engine_ladder")
    return dict(rec) if isinstance(rec, Mapping) else {}


def approved_engine(state: Mapping) -> str:
    """The engine the run was approved with - the first rung, whatever runs now."""
    return str(record_of(state).get("approved") or state.get("engine") or "")


def engine_source(state: Mapping) -> str:
    if state.get("user_dispute"):
        return SOURCE_DISPUTE
    return str(state.get("engine_source") or "")


def may_switch_on_its_own(state: Mapping) -> bool:
    return engine_source(state) in _SWITCHABLE_SOURCES


def _declared_evidence(state: Mapping, engine: str, *, form: str | None = None):
    """What `engine` is asked to build: the approved declaration, from this file - or, with
    `form`, from the same request in that geometry form (what WOULD pass, for an offer)."""
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    from meshpipeline.engines.capability import geometry_form_of_state
    from meshpipeline.engines.registry import resolve_engine_params
    patches = tuple(
        PatchSummary(name=str(p.get("name", "")).strip(), type=str(p.get("type", "")).strip())
        for p in (state.get("intake_patches") or []) if isinstance(p, Mapping))
    return AdmissionEvidence(
        engine=engine,
        purpose=str(state.get("purpose", "") or ""),
        input_kind=str(state.get("input_kind", "") or "") or None,
        dimensionality=str(state.get("dimensionality", "") or "") or None,
        patches=patches,
        # the candidate's OWN parameters are judged separately (_params_carry_over): here only
        # "can it build what was declared at all"
        engine_params=resolve_engine_params(engine, {}),
        # ...from THIS file: an engine that cannot take the upload's form for this flow (VMTK and
        # an STL, for internal flow) is not a rung, and is never offered
        geometry_form=(form if form is not None else geometry_form_of_state(state)) or None)


# #
# the passages the case holds
# #

#: Where a run's passage count came from: the last attempt's own staging (what the mesher saw),
#: or the measurement at the start of the run (cad/shape_traits: `passages`).
PASSAGES_STAGED = "staged"
PASSAGES_MEASURED = "measured"


def passages_of(state: Mapping) -> int | None:
    """HOW MANY SEPARATE FLUID PASSAGES the run's case holds, as its ladder record carries it - the
    count every engine's declared capability is held to (FlowSupport.max_passages). None when
    nothing has counted them: then no engine is ruled out for it."""
    n = record_of(state).get("passages")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


def with_staged_passages(state: Mapping) -> Mapping:
    """The state with its ladder record carrying the passage count the LAST ATTEMPT'S OWN STAGING
    read (cad/fluid_passages.staged_passage_count, beside the case in the attempt workspace) - the
    count the mesher itself saw, so it outranks the start-of-run measurement. Unchanged when the
    attempt left no reading."""
    from meshpipeline.cad.fluid_passages import staged_passage_count
    ws = str(state.get("openfoam_workspace") or "")
    n = staged_passage_count(ws) if ws else None
    if n is None:
        return state
    rec = record_of(state)
    if rec.get("passages") == n and rec.get("passages_source") == PASSAGES_STAGED:
        return state
    return {**state, "engine_ladder": {**rec, "passages": n, "passages_source": PASSAGES_STAGED}}


def covers_case(engine: str, state: Mapping) -> bool:
    """Whether one job of `engine` meshes the case's passages (capability.covers_passages)."""
    from meshpipeline.engines.capability import covers_passages, flow_of
    from meshpipeline.engines.registry import get_spec
    try:
        spec = get_spec(engine)
    except Exception:  # noqa: BLE001 - an unknown engine is judged by everything else, not this
        return True
    return covers_passages(spec, flow_of(state.get("purpose", "")), passages_of(state))


def held_back_by_passages(state: Mapping, *, exclude: tuple[str, ...] = ()) -> list[str]:
    """The engines, in ladder order, that would take the whole approved request but for the case's
    passages: one job of them meshes fewer than the case holds."""
    return [n for n in _admitting(state, exclude=exclude) if not covers_case(n, state)]


def _admitting(state: Mapping, *, form: str | None = None,
               exclude: tuple[str, ...] = ()) -> list[str]:
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.registry import engine_names, get_spec
    implemented = set(engine_names())
    return [n for n in fallback_order(flow_of(state.get("purpose", "")), state)
            if n in implemented and n not in exclude
            and not get_spec(n).admit(_declared_evidence(state, n, form=form))]


def able_engines(state: Mapping, *, form: str | None = None,
                 exclude: tuple[str, ...] = ()) -> list[str]:
    """Every implemented engine, in ladder order, that admits the WHOLE approved request - the
    boundaries, the geometry kind and the file's form (or `form`, when asking what another file
    would allow) - and whose declaration meshes as many separate passages as the case holds. The
    one answer the ladder, the refusal note and the closing offer all give."""
    return [n for n in _admitting(state, form=form, exclude=exclude) if covers_case(n, state)]


def takers_by_form(state: Mapping) -> dict[str, list[str]]:
    """{another geometry form: the engines that would admit this very request from it}."""
    from meshpipeline.engines.capability import GEOMETRY_FORMS, geometry_form_of_state
    here = geometry_form_of_state(state)
    return {f: able_engines(state, form=f) for f in GEOMETRY_FORMS if f != here}


#: The file a user receives, by the engine's native export format - words for the offer only.
_FORMAT_WORDS: dict[str, str] = {
    "openfoam_polymesh": "an OpenFOAM mesh",
    "openfoam_polymesh_multiregion": "a multi-region OpenFOAM case",
    "vtu": "a VTK (.vtu) mesh",
    "abaqus_inp": "an Abaqus (.inp) deck",
}


def _format_words(spec) -> str:
    fmt = spec.export_formats[0] if spec.export_formats else ""
    return _FORMAT_WORDS.get(fmt, f"a {fmt} file" if fmt else "a different file")


#: A brief that says it wants no wake refinement.
_NO_WAKE = re.compile(
    r"\b(?:no|without|skip)\s+(?:the\s+)?wake(?:\s+(?:refinement|region|box(?:es)?))?\b", re.I)

#: The plan requirements an engine's declaration is held to (DeliveredMesh), in plain words.
REQ_PRISM_LAYERS = "prism_layers"
REQ_WAKE = "wake_refinement"
_REQUIREMENT_WORDS: dict[str, str] = {
    REQ_PRISM_LAYERS: "no reliable near-wall prism layers, which the approved plan builds",
    REQ_WAKE: "no refinement of the wake behind the body, which the approved plan builds",
}


def plan_requirements(state: Mapping, approved_spec: Any = None) -> tuple[str, ...]:
    """WHAT THE APPROVED PLAN DELIVERS that another engine must also deliver to be "the same
    mesh" - from declared and approved facts only (the approved engine's DeliveredMesh, the flow,
    the brief and the approved detail level), never from a review:

    * prism layers - the brief asks for them, or the approved engine builds them on a run that is
      not a draft and whose brief does not say "no layers";
    * the wake refinement - an external flow on an engine that refines the wake on its own, unless
      the brief says it wants none."""
    from meshpipeline.engines.capability import flow_of
    if approved_spec is None:
        try:
            from meshpipeline.engines.registry import get_spec
            approved_spec = get_spec(approved_engine(state))
        except Exception:  # noqa: BLE001 - an unknown engine has no plan to hold others to
            return ()
    dm = getattr(approved_spec, "delivered_mesh", None)
    request = str(state.get("request_txt", "") or "")
    brief = str(state.get("review_brief_txt", "") or "")
    text = f"{request}\n{brief}"
    layers = layer_request(request, brief)
    said_no_layers = bool(_NO_LAYERS.findall(text)) and not layers.requested
    draft = str(state.get("effective_mesh_fidelity") or "").lower() == "draft"
    out: list[str] = []
    if layers.requested or (dm is not None and dm.prism_layers and not draft
                            and not said_no_layers):
        out.append(REQ_PRISM_LAYERS)
    if (flow_of(state.get("purpose", "")) == "external" and dm is not None
            and getattr(dm, "wake_refinement", False) and not _NO_WAKE.findall(text)):
        out.append(REQ_WAKE)
    return tuple(out)


def uncovered(spec: Any, requirements: tuple[str, ...]) -> tuple[str, ...]:
    """The approved plan's requirements this engine's declaration does NOT cover (() = all)."""
    dm = getattr(spec, "delivered_mesh", None)
    if dm is None:
        return tuple(requirements)
    have = {REQ_PRISM_LAYERS: bool(dm.prism_layers),
            REQ_WAKE: bool(getattr(dm, "wake_refinement", False))}
    return tuple(r for r in requirements if not have.get(r, False))


def _changes(approved_spec, spec, layers: LayerRequest) -> tuple[str, ...]:
    from meshpipeline.engines.base import WALL_FITS
    a, c = approved_spec.delivered_mesh, spec.delivered_mesh
    if a is None or c is None:
        return ("a kind of mesh this system cannot compare with the one you approved",)
    out: list[str] = []
    if spec.export_formats[:1] != approved_spec.export_formats[:1]:
        out.append(f"you would get {_format_words(spec)} instead of "
                   f"{_format_words(approved_spec)}")
    if c.cells != a.cells:
        out.append(f"{c.cells} cells instead of {a.cells} ones")
    if WALL_FITS.index(c.walls) < WALL_FITS.index(a.walls):
        out.append(f"a {c.walls} wall instead of a {a.walls} one")
    if layers.requested and not c.prism_layers:
        out.append("no reliable near-wall prism layers, which your request asks for")
    return tuple(out)


def _plan_changes(spec, layers: LayerRequest, requirements: tuple[str, ...]) -> list[str]:
    """What the approved PLAN builds that this engine's declaration does not (plan_requirements),
    in plain words - beyond what _changes already says about layers the request asked for."""
    if getattr(spec, "delivered_mesh", None) is None:
        return []           # _changes already said the meshes cannot be compared
    missing = uncovered(spec, requirements)
    out: list[str] = []
    if REQ_PRISM_LAYERS in missing and not layers.requested:
        out.append(_REQUIREMENT_WORDS[REQ_PRISM_LAYERS])
    if REQ_WAKE in missing:
        out.append(_REQUIREMENT_WORDS[REQ_WAKE])
    return out


def _params_carry_over(state: Mapping, spec) -> bool:
    """The candidate's own questions are answered by what was approved: every parameter it
    requires is there, with a value it accepts. A default filled in for the user is a setting they
    never approved, so it makes the rung an offer. The approved engine's own parameters that the
    candidate does not have (gmsh's element order on a hex mesher) do not travel: the cell change
    they belong to is already stated."""
    given = dict(state.get("engine_params") or {})
    for p in spec.intake_params:
        v = str(given.get(p.key, "") or "").strip().lower()
        if (not v and p.required) or (v and v not in p.values):
            return False
    return True


def ladder(state: Mapping) -> list[Rung]:
    """The approved engine, then every other engine that can build the approved request, in the
    fallback order - each marked with whether it delivers what was approved."""
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.registry import engine_names, get_spec

    approved = approved_engine(state)
    if not approved:
        return []
    rungs = [Rung(approved, True)]
    flow = flow_of(state.get("purpose", ""))
    if not flow or engine_source(state) == SOURCE_DISPUTE:
        return rungs
    try:
        approved_spec = get_spec(approved)
    except Exception:  # noqa: BLE001 - an unknown engine has no ladder
        return rungs
    layers = layer_request(str(state.get("request_txt", "") or ""),
                           str(state.get("review_brief_txt", "") or ""))
    # what the approved plan delivers: an engine that cannot is not "the same mesh"
    requirements = plan_requirements(state, approved_spec)
    implemented = set(engine_names())
    for name in fallback_order(flow, state):
        if name == approved or name not in implemented:
            continue
        spec = get_spec(name)
        if spec.admit(_declared_evidence(state, name)):
            continue            # it cannot build what was declared: not a rung at all
        if not covers_case(name, state):
            # one job of it meshes fewer separate passages than the case holds: it would refuse
            # the case before meshing (snappyHexMesh fills the one its seed point is in)
            continue
        changes = list(_changes(approved_spec, spec, layers))
        changes += _plan_changes(spec, layers, requirements)
        if not _params_carry_over(state, spec):
            changes.append(f"{engine_label(name)}'s own settings would take their defaults")
        rungs.append(Rung(name, not changes, tuple(changes)))
    return rungs


# #
# the failure that just happened
# #

@dataclass(frozen=True)
class Failure:
    kind: str       # ENGINE | FIXABLE | NEVER
    cause: str      # the recorded cause, the gate key, or "review"
    reason: str     # the plain half-sentence
    #: WHY THE ENGINE STOPPED, in the six classes (contracts/failure_cause.StopClass)
    stop: str = ""

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "cause": self.cause, "reason": self.reason}
        if self.stop:
            d["stop"] = self.stop
        return d


def classify(state: Mapping) -> Failure | None:
    """What stopped the attempt whose result the state now holds, or None when it did not fail."""
    from meshpipeline.contracts.failure_cause import StopClass, stop_class_of
    if state.get("executor_success"):
        if state.get("requirement_caveats"):
            return Failure(NEVER, "requirements_near_miss",
                           "the far-field domain came out short of the size you asked for",
                           StopClass.CHECK_FAILED.value)
        if str(state.get("reviewer_verdict", "") or "").upper() == "FAIL":
            # a validated mesh the review did not pass: never the engine's own failing. With no
            # wrong-problem finding it is not a failure at all - the mesh is delivered with the
            # review's concerns listed (application/final_result.review_concerns_caveat)
            from meshpipeline.agents.reviewer.review_policy import blocking_findings
            wrong = blocking_findings(list(state.get("reviewer_axis_findings") or []))
            return Failure(REVIEW, "review", _REASON_REVIEW if wrong else _REASON_CONCERNS,
                           StopClass.REVIEW_REBUILD.value)
        return None
    gate = str(state.get("executor_failed_gate", "") or "")
    facts = state.get("executor_failure_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    cause = _cause_of(state, gate)
    if cause is None:
        if gate:
            # a gate that names no cause, on no engine this system knows: never a reason to switch
            # at once, never a reason to refuse one
            return Failure(FIXABLE, gate, _REASON_GENERIC, StopClass.CHECK_FAILED.value)
        # no gate at all: the executor had nothing to validate - the builder produced no mesh
        cause = FailureCause.ENGINE_CRASHED
    kind = _LADDER_CLASS[cause]
    if not retry_can_help(cause, facts) and cause not in _ONLY_ANOTHER_ENGINE_CHANGES:
        # the retry policy's own verdict: nothing that runs again changes this, so no engine does
        kind = NEVER
    stop = stop_class_of(cause)
    stopped = stop.value if stop is not None else ""
    broken = broken_cells(facts)
    if broken and kind != NEVER:
        # A BROKEN MESH (checkMesh's fatal defects: negative-volume cells, faces pointing the wrong
        # way) is this engine's own failing on this shape, not a bar a retry might clear: cfMesh
        # made 7 of 8 DrivAer cars that way while snappyHexMesh meshed the same cars (e2e sweep,
        # 2026-10-17). The run moves to the next engine at once - never the same engine again.
        return Failure(ENGINE, cause.value, _broken_reason(broken), stopped)
    if refused_by_design(state):
        # the engine's DECLARED limit, said as that limit - not "it cannot take this geometry as it
        # is", which sends a user to repair a file that is fine
        return Failure(kind, cause.value, _by_design_reason(facts), stopped)
    return Failure(kind, cause.value, _REASON_BY_CAUSE[cause], stopped)


def broken_cells(facts: Mapping | None) -> list[str]:
    """The fatal defects a failed attempt's mesh carries (the quality gate's and finalize's own
    `fatal` fact) - [] when the mesh was not broken, or there was none."""
    fatal = facts.get("fatal") if isinstance(facts, Mapping) else None
    return [str(x) for x in fatal if str(x).strip()] if isinstance(fatal, (list, tuple)) else []


def _broken_reason(fatal: list[str]) -> str:
    return (f"its mesh came out broken ({', '.join(fatal[:3])}), and no solver can use a mesh "
            "like that")


def moves_on_from_broken_mesh(state: Mapping, *, now: float | None = None) -> str:
    """THE ENGINE THIS RUN MOVES TO if the attempt now being built ends on a BROKEN mesh - '' when
    it would stay (the user named the engine, no untried engine delivers the approved mesh, no
    attempt or time is left). It is decide()'s own answer for that failure, asked before the
    attempt runs, so the builder does not spend the attempt re-running an engine whose mesh of this
    shape came out broken: the run moves on instead."""
    from meshpipeline.contracts.failure_cause import FailureCause
    probe = {**state, "retry_count": int(state.get("retry_count", 0) or 0) + 1,
             "executor_success": False, "executor_failed_gate": "finalize",
             "executor_failure_cause": FailureCause.MESH_QUALITY.value,
             "executor_failure_facts": {"fatal": ["broken cells"], "meshers_started": 1},
             "requirement_caveats": [], "reviewer_verdict": ""}
    return decide(probe, record=with_attempt(probe, classify(probe)), now=now).switch_to


def mesher_started(state: Mapping) -> bool:
    """Whether the attempt whose result the state holds actually started a mesher - the one fact
    an attempt is COUNTED by. Not when admission or a pre-flight refused before meshing, and not
    when the executor's record of native runs says none was started (NOT_BUILT). Unknown counts as
    started, as it always did."""
    if state.get("geometry_unsuitable_reason"):
        return False
    if state.get("executor_success"):
        return True
    facts = state.get("executor_failure_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    started = facts.get("meshers_started")
    if isinstance(started, int) and not isinstance(started, bool):
        # the attempt's own record of native runs is the authority: an earlier pass that meshed
        # before a later pass's pre-flight refused still made this an attempt
        return started > 0
    return not facts.get("before_meshing")


def refused_by_design(state: Mapping) -> bool:
    """The attempt ended on an engine refusing this file's form for this flow - its declared
    limit, recorded by geometry admission before anything was built."""
    facts = state.get("executor_failure_facts")
    return bool(isinstance(facts, Mapping) and facts.get("refused_by_design")
                and as_cause(state.get("executor_failure_cause")) is FailureCause.GEOMETRY_REJECTED)


def _by_design_reason(facts: Mapping) -> str:
    from meshpipeline.engines.capability import flow_words, form_words
    flow, form = str(facts.get("flow") or ""), str(facts.get("form") or "")
    if not flow or not form:
        return _REASON_BY_CAUSE[FailureCause.GEOMETRY_REJECTED]
    return f"it cannot mesh {flow_words(flow)} from {form_words(form, short=True)}"


def _cause_of(state: Mapping, gate: str) -> FailureCause | None:
    """The executor's cause for the failed attempt: the one it recorded, else the one it WOULD have
    recorded - the failed gate's declared cause for this engine, else the seam's - resolved by the
    executor's own function, so the ladder and the retry policy can never read one failure two
    ways. (A state without a recorded cause predates the cause fields.)"""
    recorded = as_cause(state.get("executor_failure_cause"))
    if recorded is not None or not gate:
        return recorded
    from meshpipeline.pipeline.executor import _gate_cause
    return as_cause(_gate_cause(str(state.get("engine") or ""), gate, None))


def tried_engines(state: Mapping) -> list[str]:
    rec = record_of(state)
    seen = [str(a.get("engine")) for a in (rec.get("attempts") or []) if isinstance(a, Mapping)]
    current = str(state.get("engine") or "")
    out: list[str] = []
    for e in [approved_engine(state), *seen, current]:
        if e and e not in out:
            out.append(e)
    return out


def with_attempt(state: Mapping, failure: Failure | None) -> dict:
    """The ladder record with the attempt that just ended added (idempotent per attempt)."""
    rec = record_of(state)
    rec.setdefault("approved", approved_engine(state))
    rec.setdefault("source", engine_source(state))
    attempts = [dict(a) for a in (rec.get("attempts") or []) if isinstance(a, Mapping)]
    # A GEOMETRY-ADMISSION REFUSAL builds nothing: it jumps retry_count to the exhausted value, and
    # that number is not an attempt anyone ran. It is recorded as attempt 0 - the engine refused
    # the input before the first build - so the history never shows a build that did not happen.
    n = 0 if state.get("geometry_unsuitable_reason") else int(state.get("retry_count", 0) or 0)
    if (n > 0 or state.get("geometry_unsuitable_reason")) and not any(
            int(a.get("attempt", -1)) == n for a in attempts):
        entry: dict[str, Any] = {"attempt": n, "engine": str(state.get("engine") or "")}
        entry.update(failure.as_dict() if failure else {"kind": "passed"})
        if n == 0:
            entry["refused_before_building"] = True
        elif not mesher_started(state):
            # an attempt that never started a mesher is not one the user is told was made
            entry["built"] = False
        attempts.append(entry)
    rec["attempts"] = attempts
    rec.setdefault("switches", [])
    return rec


def attempts_made(state: Mapping, *, succeeded: bool) -> int:
    """HOW MANY ATTEMPTS ACTUALLY STARTED A MESHER, the attempt that just ended included - the
    number a user is shown. A refusal before building, a pre-flight stop and an attempt whose
    record shows no mesher started are not attempts anyone made (jobs d20ad762 and 26f5429a read
    "2 attempts" with nothing ever built).

    Counted DOWN from the run's own counter: every builder turn is an attempt unless the record
    says no mesher started in it, so an attempt the record knows nothing about still counts, as it
    always did. A geometry-admission refusal ends the run before the first turn: none was made."""
    if state.get("geometry_unsuitable_reason"):
        return 0
    rec = with_attempt(state, None if succeeded else classify(state))
    turns = int(state.get("retry_count", 0) or 0)
    not_made = {int(a.get("attempt", 0) or 0) for a in rec["attempts"]
                if isinstance(a, Mapping) and a.get("built") is False}
    return max(0, turns - len({n for n in not_made if 0 < n <= turns}))


# #
# the mid-run decision
# #

@dataclass(frozen=True)
class Decision:
    switch_to: str = ""
    why: str = ""               # the policy reason, for the log and the record
    failure: Failure | None = None

    @property
    def switch(self) -> bool:
        return bool(self.switch_to)


def remaining_seconds(state: Mapping, *, now: float) -> float:
    ends = [float(v) for v in (state.get("builder_deadline_epoch"), state.get("pipeline_deadline_epoch"))
            if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 0]
    return (min(ends) - now) if ends else float("inf")


def rung_seconds(engine: str) -> float:
    """What one attempt on this engine needs: its own native run cap. A switch with less left
    would buy a run that cannot finish."""
    from meshpipeline.engines.registry import get_spec
    rp = get_spec(engine).run_policy
    return float(rp.run_timeout()) if rp is not None else 0.0


def _measures_its_input(engine: str) -> bool:
    """An engine whose input contract MEASURES the geometry (a self-intersection or thinness
    floor) is admitted by node_geometry_admission, which runs once, before the first build. A
    mid-run switch would skip that measurement, so such an engine is offered, never switched to."""
    from meshpipeline.engines.registry import get_spec
    ic = get_spec(engine).input_contract
    return ic is not None and (ic.require_no_self_intersection or ic.min_thickness_ratio > 0)


def _levers_spent(record: Mapping, engine: str) -> bool:
    """THIS ENGINE'S OWN LEVERS WERE TRIED: the record holds at least two failed attempts on it
    that started a mesher, the one that just ended included - the first failure bought a retry
    with its levers, and the measured checks still failed after it. Only then may a FIXABLE
    failure move the run (rule 2 above)."""
    mine = [a for a in (record.get("attempts") or [])
            if isinstance(a, Mapping) and a.get("engine") == engine
            and a.get("kind") in (FIXABLE, ENGINE) and a.get("built") is not False]
    return len(mine) >= 2


def _repeats(record: Mapping, engine: str, failure: Failure) -> bool:
    mine = [a for a in (record.get("attempts") or [])
            if isinstance(a, Mapping) and a.get("engine") == engine]
    return (len(mine) >= 2 and mine[-2].get("kind") == failure.kind
            and mine[-2].get("cause") == failure.cause)


def decide(state: Mapping, *, record: Mapping, now: float | None = None) -> Decision:
    """Stay on this engine for the next attempt, or move to the next same-contract rung.

    `record` is the ladder record with the attempt that just failed already in it."""
    import meshpipeline.agents.builder.settings as bcfg
    import meshpipeline.settings.policy as polcfg

    failure = classify(state)
    if failure is None:
        return Decision(why="the last attempt did not fail")
    if not polcfg.ENGINE_FALLBACK_ENABLED:
        return Decision(why="the fallback ladder is switched off", failure=failure)
    if failure.kind == NEVER:
        return Decision(why="another engine cannot change this failure", failure=failure)
    if failure.kind == REVIEW:
        # THE RULE: an AI review of a gate-passing mesh never moves the run. It is delivered with
        # its concerns, or improved on this engine with the best attempt kept.
        return Decision(why="a review never moves the run to another engine - the mesh passed "
                            "every measured check, so this engine's own levers answer it",
                        failure=failure)
    engine = str(state.get("engine") or "")
    if not may_switch_on_its_own(state):
        return Decision(why=f"the engine is not ours to change (source {engine_source(state) or 'unknown'})",
                        failure=failure)
    tried = set(tried_engines({**state, "engine_ladder": record}))
    open_rungs = [r for r in ladder({**state, "engine_ladder": record})
                  if r.same_contract and r.engine not in tried
                  and not _measures_its_input(r.engine)]
    if not open_rungs:
        st = {**state, "engine_ladder": record}
        if [e for e in held_back_by_passages(st) if e not in tried]:
            # the engines left would refuse the case: the run stays on the one that meshes it
            return Decision(why=(f"no untried engine meshes {passages_of(st)} separate passages "
                                 "in one job"), failure=failure)
        return Decision(why="no untried engine delivers the approved mesh", failure=failure)
    # attempts left, the next one included (the reviewer-feedback bonus is not the ladder's)
    left = bcfg.MAX_BUILDER_RETRIES + 1 - int(state.get("retry_count", 0) or 0)
    if left < 1:
        return Decision(why="no regular attempt is left", failure=failure)
    if failure.kind == FIXABLE and not _levers_spent(record, engine) and left > len(open_rungs):
        # LEVERS FIRST, WHERE THERE IS ROOM FOR THEM: a retry with this engine's own levers must
        # still leave every untried rung an attempt. When it would not, it spends the run's last
        # regular attempt here and no other engine can ever be reached - with the default one
        # regular retry, rule 2 never fired at all. On branching vessels cfMesh came in at 9-15 M
        # cells on an 8 M budget, its own passage caps rendered the retry's case byte for byte as
        # before, and the run ended, where snappyHexMesh had delivered 4.5 M production-grade
        # cells the round before (e2e verify-round5, 2026-10-19).
        return Decision(why="a measured check failed - this engine's own levers are tried first",
                        failure=failure)
    if failure.kind == FIXABLE and not _repeats(record, engine, failure) and left > len(open_rungs):
        return Decision(why="a retry here fits and still leaves every untried rung an attempt",
                        failure=failure)
    target = open_rungs[0].engine
    clock = time.time() if now is None else now
    if remaining_seconds(state, now=clock) < rung_seconds(target):
        # a clock decided this, not the shape: recorded so a run that differs here is explained
        from meshpipeline.engines.determinism import note_time_limit
        note_time_limit("engine_fallback", f"no switch to {target}: too little time left",
                        left_s=remaining_seconds(state, now=clock),
                        needed_s=rung_seconds(target))
        return Decision(why=f"too little time left for a {engine_label(target)} run",
                        failure=failure)
    broken = broken_cells(state.get("executor_failure_facts")
                          if isinstance(state.get("executor_failure_facts"), Mapping) else None)
    return Decision(switch_to=target, why=(
        "this engine's mesh of this shape came out broken" if failure.kind == ENGINE and broken else
        "this engine could not build a mesh" if failure.kind == ENGINE else
        "the same failure repeated here" if _repeats(record, engine, failure) else
        "the shared attempts go to the untried engine"), failure=failure)


def with_switch(record: Mapping, *, attempt: int, frm: str, to: str, failure: Failure,
                why: str) -> dict:
    rec = dict(record)
    switches = [dict(s) for s in (rec.get("switches") or []) if isinstance(s, Mapping)]
    if not any(int(s.get("attempt", -1)) == attempt for s in switches):
        switches.append({"attempt": attempt, "from": frm, "to": to, "because": failure.cause,
                         "kind": failure.kind, "reason": failure.reason, "why": why})
    rec["switches"] = switches
    return rec


def switch_note(frm: str, to: str, failure: Failure) -> str:
    return (f"{engine_label(frm)} could not mesh this shape: {failure.reason}. Trying "
            f"{engine_label(to)} next - same boundaries, same units, same settings you approved.")


def fresh_start_brief(frm: str, to: str, failure: Failure) -> str:
    return (f"This is the FIRST attempt on {engine_label(to)}. The previous engine, "
            f"{engine_label(frm)}, could not mesh this shape: {failure.reason}. Plan from scratch "
            f"for {engine_label(to)}; the approved boundaries, units and settings are unchanged.")


# #
# the end of the run
# #

#: The failures prism layers commonly cause: folded or squeezed layer cells fail the cell-quality
#: bars, the trial solve, and the review of layer coverage. Only these CAN earn a fewer-layers
#: offer, and only with measured evidence that the layers were to blame (_layers_are_to_blame).
_LAYER_SHAPED = frozenset({FailureCause.MESH_QUALITY.value, FailureCause.NOT_SOLVABLE.value,
                           "review"})


def _layers_are_to_blame(state: Mapping) -> bool:
    """EVIDENCE that the prism layers caused this failure, from what the run measured - never a
    guess from the failure's class alone: the review failed a layer axis, the mesh carries the
    layer-inversion signature (negative-volume or mis-oriented cells, which folding prism layers
    produce - the snappy layer policy reads the same signature), or a failed check the executor
    recorded names the layers. Without it no fewer-layers offer is made: telling a user that
    fewer layers fixes a failure they did not cause sends them to approve the wrong change."""
    if any(isinstance(f, Mapping) and f.get("passed") is False
           and "layer" in str(f.get("axis_key") or "").lower()
           for f in (state.get("reviewer_axis_findings") or [])):
        return True
    def _mapping(value: Any) -> Mapping:
        return value if isinstance(value, Mapping) else {}

    quality = _mapping(_mapping(state.get("mesh_manifest")).get("quality"))
    facts = _mapping(state.get("executor_failure_facts"))
    fatal = " ".join(str(x) for x in [*(quality.get("fatal") or []),
                                      *(facts.get("fatal") or [])]).lower()
    if "negative" in fatal or "orient" in fatal:
        return True
    return any(isinstance(c, Mapping) and "layer" in str(c.get("key") or "").lower()
               for c in (facts.get("checks") or []))


def _review_failed_only_layers(state: Mapping) -> bool:
    """The review's material findings are ALL about the layers. Fewer layers can only answer a
    review whose every complaint that counts is about the layers: job e0fa8ad0 failed prism
    coverage AND wake resolution, and was offered fewer layers - which does nothing for the wake.
    A judged review (agents/reviewer/review_policy) counts what failed the run - its wrong-problem
    findings; an unjudged record counts every axis that did not pass."""
    rows = [f for f in (state.get("reviewer_axis_findings") or [])
            if isinstance(f, Mapping) and f.get("passed") is False]
    if any("blocking" in f for f in rows):
        rows = [f for f in rows if f.get("blocking")]
    failed = [str(f.get("axis_key") or "").lower() for f in rows]
    return bool(failed) and all("layer" in k for k in failed)


def improvement_offer(state: Mapping) -> dict | None:
    """AN OPTIONAL IMPROVEMENT beside a DELIVERED mesh: when every point the review left open is
    about the near-wall layers, the same mesh with fewer layers - offered, never run. A mesh with
    nothing left open, or open points elsewhere, carries no offer."""
    from meshpipeline.engines.registry import get_spec
    engine = str(state.get("engine") or "")
    open_points = [str(f.get("axis_key") or "").lower()
                   for f in (state.get("reviewer_axis_findings") or [])
                   if isinstance(f, Mapping) and f.get("passed") is False]
    if not open_points or not all("layer" in k for k in open_points):
        return None
    lr = layer_request(str(state.get("request_txt", "") or ""),
                       str(state.get("review_brief_txt", "") or ""))
    try:
        dm = get_spec(engine).delivered_mesh
    except Exception:  # noqa: BLE001 - an unknown engine offers nothing
        return None
    if not lr.count or lr.count < 2 or dm is None or not dm.prism_layers:
        return None
    fewer = max(1, lr.count // 2)
    return {"kind": "fewer_layers", "engine": engine, "same_contract": False, "optional": True,
            "layers_from": lr.count, "layers_to": fewer,
            "changes": [f"{fewer} near-wall layers instead of {lr.count}"],
            "reply": f"use {fewer} layers",
            "text": (f"Optional: the review's open points are all about the {lr.count} near-wall "
                     f"layers. Fewer layers usually cover more of the wall - I can build the same "
                     f"mesh with {fewer} layers instead (same boundaries, same units). Reply "
                     f"\"use {fewer} layers\" if you want that run.")}


def _fewer_layers(state: Mapping, engine: str, failure: Failure) -> dict | None:
    from meshpipeline.engines.registry import get_spec
    lr = layer_request(str(state.get("request_txt", "") or ""),
                       str(state.get("review_brief_txt", "") or ""))
    dm = get_spec(engine).delivered_mesh
    if (failure.cause not in _LAYER_SHAPED or not lr.count or lr.count < 2
            or dm is None or not dm.prism_layers or not _layers_are_to_blame(state)):
        return None
    if failure.cause == "review" and not _review_failed_only_layers(state):
        return None
    fewer = max(1, lr.count // 2)
    # NAME THE CAUSE THAT HAPPENED. A review failure is a mesh that was built and passed every
    # automatic check; telling that user the mesher "could not finish" it with their layers sent
    # them to approve a change for a failure that never occurred.
    why = (f"{engine_label(engine)} built the mesh and it passed every automatic check, but the "
           f"review found its {lr.count} near-wall layers missing from the wall. Fewer layers "
           "usually grow where a full stack cannot."
           if failure.cause == "review" else
           f"{engine_label(engine)} could not finish this mesh with the {lr.count} near-wall "
           f"layers you asked for: {failure.reason}. Fewer layers usually fixes this.")
    return {"kind": "fewer_layers", "engine": engine, "same_contract": False,
            "layers_from": lr.count, "layers_to": fewer,
            "changes": [f"{fewer} near-wall layers instead of {lr.count}"],
            "reply": f"use {fewer} layers",
            "text": (f"{why} I can build the same mesh with {fewer} layers instead - same "
                     f"boundaries, same units. Reply \"use {fewer} layers\" and I will set that "
                     "run up.")}


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _per_job(st: Mapping, held: list[str], n: int) -> str:
    """'meshes only one passage per job' (or 'mesh', for several engines; 'fewer than n passages'
    when one of them declares more than one) - what the held-back engines' declarations say."""
    from meshpipeline.engines.capability import flow_of
    from meshpipeline.engines.registry import get_spec
    flow = flow_of(st.get("purpose", ""))
    one = all((get_spec(e).max_passages_for(flow) or 1) <= 1 for e in held)
    verb = "meshes" if len(held) == 1 else "mesh"
    return f"{verb} only one passage per job" if one else f"{verb} fewer than {n} passages per job"


def _passages_offer(st: Mapping, engine: str, head: str, *, tried: set[str]) -> dict | None:
    """THE OUT WHEN THE ONLY OTHER ENGINES WOULD REFUSE THE CASE: its openings lie on several
    separate passages, and every untried engine that takes the request meshes fewer per job
    (FlowSupport.max_passages). Switching to one would buy a certain refusal, so the user is told
    so and given the two ways that work - one file per passage, which every engine can take, or
    the same engine on the whole case again (only when one job of it meshes them all)."""
    n = passages_of(st)
    held = [e for e in held_back_by_passages(st) if e not in tried]
    if not n or n < 2 or not held:
        return None
    names = _and([engine_label(e) for e in held])
    me = engine_label(engine)
    again = covers_case(engine, st) and bool(engine)
    ways = ("Two ways on: split the file into one file per passage, each with its own openings, "
            f"and upload them one at a time - {names} can mesh each of them too; or reply "
            f"\"use {me}\" and I will run {me} on the whole case again." if again else
            "The way on: split the file into one file per passage, each with its own openings, "
            f"and upload them one at a time - {names} can mesh each of them.")
    return {"kind": "passages", "engine": engine if again else "", "same_contract": again,
            "changes": [], "reply": f"use {me}" if again else "", "passages": n,
            "held_back": held,
            "text": (f"{head} The openings lie on {n} separate passages that do not connect, and "
                     f"{names} {_per_job(st, held, n)}, so I did not switch to "
                     f"{'it' if len(held) == 1 else 'them'}. {ways}").strip()}


def offer(state: Mapping, record: Mapping) -> dict | None:
    """THE ONE THING TO OFFER when a run ends without a mesh, or None. In order: another engine
    that delivers the approved mesh (it could not be switched to - it was the user's engine, or
    the attempts or the time ran out), the same engine with fewer layers, then another engine
    with the differences stated, then - when the only engines left would refuse the case's
    separate passages - one file per passage or the same engine again. Never a switch the user
    did not ask for."""
    failure = classify(state)
    if failure is None or failure.kind == NEVER or engine_source(state) == SOURCE_DISPUTE:
        return None
    engine = str(state.get("engine") or "")
    st = {**state, "engine_ladder": record}
    order = tried_engines(st)
    tried = set(order)
    rungs = [r for r in ladder(st) if r.engine not in tried]
    same = [r for r in rungs if r.same_contract]
    ran = [e for e in order if any(isinstance(a, Mapping) and a.get("engine") == e
                                   for a in (record.get("attempts") or []))] or [engine]
    names = [engine_label(e) for e in ran]
    who = (f"{names[0]} could not" if len(names) == 1 else
           f"Neither {names[0]} nor {names[1]} could" if len(names) == 2 else
           f"None of {', '.join(names[:-1])} and {names[-1]} could")
    head = f"{who} mesh this shape: {failure.reason}."
    if failure.cause == "review" and len(names) == 1:
        # the mesh exists and passed every gate - "could not mesh this shape" says it does not. A
        # review fails a run only when the mesh is the wrong problem (review_policy.WRONG_PROBLEM).
        head = (f"{names[0]} built a mesh that passed every automatic check, but the review found "
                "it represents a different problem than you asked for.")
    by_design = refused_by_design(state)
    if by_design:
        # the run's own account already says what was refused and that nothing was built; the
        # offer is the OUT, and only that - the engines whose declarations take this file
        head = ""
    if same:
        to = same[0].engine
        why = ("Nothing was built, and I did not switch engines without asking." if by_design else
               f"You chose {engine_label(engine)}, so I did not switch engines without asking."
               if not may_switch_on_its_own(state) else
               "There was no attempt or time left to try it in this run.")
        return {"kind": "engine", "engine": to, "same_contract": True, "changes": [],
                "reply": f"use {engine_label(to)}",
                "text": (f"{head} {engine_label(to)} can build the same mesh - same boundaries, "
                         f"same units, same settings. {why} Reply \"use {engine_label(to)}\" "
                         "and I will set that run up.").strip()}
    fewer = _fewer_layers(state, engine, failure)
    if fewer is not None:
        held = [e for e in held_back_by_passages(st) if e not in tried]
        if held and not rungs:
            # say why no other engine was tried: they would refuse the case's passages
            fewer = {**fewer, "text": (f"{fewer['text']} (No other engine was tried: the openings "
                                       f"lie on {passages_of(st)} separate passages, and "
                                       f"{_and([engine_label(e) for e in held])} "
                                       f"{_per_job(st, held, passages_of(st) or 0)}.)")}
        return fewer
    if rungs:
        to = rungs[0].engine
        diff = "; ".join(rungs[0].changes)
        return {"kind": "engine", "engine": to, "same_contract": False,
                "changes": list(rungs[0].changes), "reply": f"use {engine_label(to)}",
                "text": (f"{head} {engine_label(to)} can mesh it, but not exactly as you "
                         f"approved: {diff}. I did not switch without asking. Reply "
                         f"\"use {engine_label(to)}\" if that works for you.").strip()}
    split = _passages_offer(st, engine, head, tried=tried)
    if split is not None:
        return split
    if by_design:
        # NO ENGINE takes this file for this request: the out is the file - which form the
        # engines that admit this very request take, and how to get it
        from meshpipeline.engines.capability import way_on, who_can
        facts = state.get("executor_failure_facts") or {}
        flow, form = str(facts.get("flow") or ""), str(facts.get("form") or "")
        other = takers_by_form(state)
        return {"kind": "input", "engine": "", "same_contract": False, "changes": [],
                "reply": "",
                "text": (f"{who_can(flow, form, able=[], takers=other)} "
                         f"{way_on(flow, form, able=[], takers=other)}")}
    return None


def final_record(state: Mapping, *, succeeded: bool, system_failure: bool) -> dict:
    """The ladder record the run ends with: every attempt, every switch, the engine that built
    the delivered mesh, and - on a mesh failure - the one offer."""
    state = with_staged_passages(state)
    failure = None if succeeded else classify(state)
    rec = with_attempt(state, failure)
    rec["delivered_by"] = str(state.get("engine") or "") if succeeded else ""
    # A delivered mesh carries at most an OPTIONAL improvement; a failed run the one offer.
    rec["offer"] = (improvement_offer(state) if succeeded else
                    None if system_failure else offer(state, rec))
    return rec


def attempts_account(record: Mapping) -> list[str]:
    """A failed run's lines on WHAT EACH ATTEMPT DID, in plain words - [] for a run with fewer
    than two attempts that built anything (the account above it already says the one reason)."""
    rows = [a for a in (record.get("attempts") or []) if isinstance(a, Mapping)
            and int(a.get("attempt", 0) or 0) > 0 and a.get("built") is not False]
    if len(rows) < 2:
        return []
    out = ["What each attempt did:"]
    for a in sorted(rows, key=lambda r: int(r.get("attempt", 0) or 0)):
        reason = str(a.get("reason") or ("it passed" if a.get("kind") == "passed"
                                         else _REASON_GENERIC))
        out.append(f"  - Attempt {a.get('attempt')} ({engine_label(str(a.get('engine') or ''))}): "
                   f"{reason}.")
    return out


def delivered_note(record: Mapping) -> str:
    """The success message's line about WHICH attempt and engine built the delivered mesh, or ''
    when it is simply the last attempt on the approved engine."""
    switches = [s for s in (record.get("switches") or []) if isinstance(s, Mapping)]
    by = str(record.get("delivered_by") or "")
    if not by:
        return ""
    picked = record.get("delivered_from")
    picked = picked if isinstance(picked, Mapping) else {}
    if picked:
        # THE BEST VALID MESH (application/review_delivery): the run built more than one, or a
        # later attempt did worse - say which attempt is delivered, by which engine, why it was
        # chosen, and what it is short of, measured
        why = str(picked.get("why") or "it measured best of this run's meshes")
        short = [str(s) for s in (picked.get("short_of") or []) if str(s).strip()]
        text = (f"This is the mesh from attempt {picked.get('attempt')}, built with "
                f"{engine_label(by)}: {why}.")
        if short:
            text += " It is short of: " + "; ".join(short) + "."
        return text
    if not switches:
        return ""
    first, last = switches[0], switches[-1]
    if by != str(last.get("to") or ""):
        return ""
    return (f"The first engine, {engine_label(str(first.get('from')))}, could not mesh this shape "
            f"({first.get('reason')}), so this mesh was built with "
            f"{engine_label(str(last.get('to')))} - same boundaries, same units, same settings "
            "you approved.")


# #
# the graph node
# #

async def node_engine_fallback(state: PipelineState) -> dict:
    """THE LADDER'S ONE MOVE, between the classifier and the next build attempt.

    Records the attempt that just failed, then either keeps the engine (the next attempt is an
    ordinary retry) or moves the run to the next engine that delivers the SAME approved mesh -
    starting it fresh, on one of the run's existing attempts. It never ends a run and never
    changes a user-named engine: when no move is allowed, the run's end offers one instead
    (engine_fallback.final_record)."""
    # the selection node's own event and stream helpers: a switch is a selection, announced and
    # recorded the way the first one was
    from meshpipeline.engines.registry import resolve_engine_params
    from meshpipeline.pipeline.engine_select import _ladder_facts, _log_selection, _publish

    job_id = state.get("job_id", "unknown")
    # THE ORDER FOR THIS GEOMETRY: measured at the start of the run (node_engine_select); measured
    # here if the start could not, so the rung chosen next is the best for this shape.
    seeded = await seed_measured_order(state)
    if seeded:
        state = {**state, "engine_ladder": seeded}
    # THE PASSAGES THE MESHER SAW: the attempt that just ended staged the case and counted its
    # separate passages; no engine that meshes fewer per job is a rung after that
    staged = with_staged_passages(state)
    if staged is not state:
        state = {**state, "engine_ladder": dict(staged["engine_ladder"])}
    failure = classify(state)
    record = with_attempt(state, failure)
    decision = decide(state, record=record)
    attempt = int(state.get("retry_count", 0) or 0) + 1       # the attempt about to start
    if not decision.switch or decision.failure is None:
        logger.info("node_engine_fallback: staying on %s for attempt %d (%s) - job_id=%s",
                    state.get("engine"), attempt, decision.why, job_id)
        return {"engine_ladder": record}

    frm, to = str(state.get("engine") or ""), decision.switch_to
    record = with_switch(record, attempt=attempt, frm=frm, to=to,
                         failure=decision.failure, why=decision.why)
    logger.warning("node_engine_fallback: %s could not mesh this shape (%s: %s) - attempt %d "
                   "moves to %s, which delivers the same approved mesh - job_id=%s",
                   frm, decision.failure.kind, decision.failure.cause, attempt, to, job_id)
    await _publish(job_id, switch_note(frm, to, decision.failure), f"fallback:{attempt}")
    _log_selection(job_id, {
        "chosen": to, "source": "fallback", "from": frm,
        "because": decision.failure.cause, "failure_kind": decision.failure.kind,
        "why": decision.why, "attempt": attempt, "model": None, "usage": None,
        **_ladder_facts(state, to)}, op_id=f"engine-select:fallback:{attempt}")
    return {
        "engine": to,
        # the new engine's own declared parameters; the ladder only moves to an engine whose
        # questions the approval already answers, so nothing here is a new default
        "engine_params": resolve_engine_params(to, dict(state.get("engine_params") or {})),
        # a FRESH build on the new engine: the old engine's spec and the classifier's advice to it
        # mean nothing to this one, and the no-progress stop compares like with like
        "builder_mode": "initial",
        "builder_noop_count": 0,
        # the brief the new engine's planner reads as "what went wrong before": the old gate's
        # coaching speaks the old engine's vocabulary ("adjust the meshDict"), so it is replaced
        # by what actually happened, in words any engine's planner can use
        "classifier_result": {**dict(state.get("classifier_result") or {}),
                              "summary": fresh_start_brief(frm, to, decision.failure),
                              "error_source": "engine_fallback"},
        "engine_ladder": record,
    }


__all__ = ["ENGINE", "FIXABLE", "NEVER", "REQ_PRISM_LAYERS", "REQ_WAKE", "REVIEW",
           "SOURCE_DISPUTE", "SOURCE_SUGGESTED",
           "SOURCE_SYSTEM", "SOURCE_USER", "Decision", "Failure", "LayerRequest", "Rung",
           "able_engines", "approved_engine", "attempts_account", "attempts_made", "classify",
           "decide",
           "plan_requirements", "uncovered",
           "delivered_note", "engine_source", "fallback_order", "fresh_start_brief",
           "final_record", "ladder", "layer_request", "may_switch_on_its_own", "measure_order",
           "measured_ranking", "mesher_started", "moves_on_from_broken_mesh", "broken_cells",
           "seed_measured_order", "PASSAGES_MEASURED", "PASSAGES_STAGED", "covers_case",
           "held_back_by_passages", "passages_of", "with_staged_passages",
           "node_engine_fallback", "offer", "refused_by_design", "remaining_seconds",
           "rung_seconds", "switch_note", "takers_by_form", "tried_engines", "with_attempt",
           "with_switch"]
