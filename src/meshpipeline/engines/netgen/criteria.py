# Responsibility: Declare the measurable bars and review axes a Netgen mesh (tetrahedra with prism layers, as an OpenFOAM polyMesh) is judged against.
# Boundaries: this engine's contribution only; the OpenFOAM-wide bars are engines/openfoam_criteria.py's.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    TargetKind,
)
from meshpipeline.engines.openfoam_criteria import FATAL_TOPOLOGY as _FATAL_TOPOLOGY
from meshpipeline.engines.openfoam_criteria import MAX_NON_ORTHO as _MAX_NON_ORTHO
from meshpipeline.engines.openfoam_criteria import OF_SNAPPY_GUIDE as _OF_SNAPPY_GUIDE
from meshpipeline.engines.review_types import Criterion, ReviewAxis

_NETGEN_DOC = "https://docu.ngsolve.org/latest/i-tutorials/unit-4.4-boundarylayer/boundarylayer.html"

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
        key="timed_out", label="Build completes within compute budget",
        op="==", threshold=False, gating=True,
        rationale=("A plan too fine to build within the budget cannot be reproduced or iterated; "
                   "the fix is coarser sizing, not more time."),
        evidence_url=_NETGEN_DOC,
    ),
    Criterion(
        key="rc", label="Netgen and the OpenFOAM conversion exit cleanly", op="==", threshold=0,
        gating=True,
        rationale=("A nonzero exit means no rung of the ladder produced a volume that converts to a "
                   "valid polyMesh - what is on disk is absent or partial, not a usable mesh."),
        evidence_url=_NETGEN_DOC,
    ),
    _FATAL_TOPOLOGY,
    _MAX_NON_ORTHO,
    Criterion(
        key="layer_coverage_pct", label="Prism boundary layers inflated", op=">", threshold=0.0,
        gating=False,
        rationale=("Netgen grows prism layers off the wall; coverage is the share of the REQUESTED "
                   "layer cells the wall got, counted on the delivered mesh (engines/layer_census). "
                   "Advisory - the target coverage depends on the case's y+ requirement."),
        evidence_url=_NETGEN_DOC,
    ),
)

REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="wall_fidelity", validation_axis="quality",
        guidance=("Netgen remeshes the staged wall before it fills the volume, sized from the local "
                  "passage: judge whether the meshed wall still follows the part - small branches, "
                  "openings and sharp rims present, nothing rounded off that the flow depends on."),
        concern="The meshed wall has lost a feature of your part",
        failure_signals=("a branch, opening or rim of the part is missing or rounded off",
                         "the meshed surface no longer recognisably matches the part"),
        evidence=("render", "geometry", "brief"),
        visual_only=True,
        requires=(RenderViewRequirement("iso"),
                  RenderTargetRequirement(TargetKind.PATCH, "patch:*")),
        evidence_url=_NETGEN_DOC,
    ),
    ReviewAxis(
        name="tet_cell_quality", validation_axis="quality",
        guidance=("Check the tetrahedra and the layer prisms are well shaped where the flow is: no "
                  "flat slivers at sharp edges, no abrupt size jumps between the layer stack and "
                  "the fill."),
        concern="Cells degrade at sharp edges or where the layers meet the fill",
        failure_signals=("sliver cells at sharp edges or narrow junctions",
                         "an abrupt size jump between the last layer and the tetrahedra"),
        evidence=("render", "quality_metrics"),
        requires=(MetricRequirement("max_non_ortho"),
                  RenderTargetRequirement(TargetKind.PATCH, "patch:*")),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    ReviewAxis(
        name="near_wall_layers_generated", validation_axis="quality",
        guidance=("Judge the layers from the MEASURED figures in the mesh info - the layers counted "
                  "on the delivered mesh against the number the request asked for (coverage %, mean "
                  "layers, the share of the wall with all of them). Fewer than requested is a "
                  "shortfall; the mesh info also says when the ladder had to build a thinner stack "
                  "or none."),
        concern="The near-wall cells needed to resolve flow at the surface are missing",
        failure_signals=("measured layers well short of the number the request asked for",
                         "no near-wall layers where the workflow expects them"),
        evidence=("layer_report", "inspect_region", "render"),
        visual_only=True,
        requires=(RenderTargetRequirement(TargetKind.PATCH, "patch:*"),),
        evidence_url=_NETGEN_DOC,
    ),
)
