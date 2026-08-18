# Responsibility: Declare the measurable bars and review axes a cfMesh mesh is judged against.
# Boundaries: this engine's contribution only - 2D runs use a genuinely different binary, not a 3D mesh one cell deep.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    TargetKind,
)
from meshpipeline.engines.openfoam_criteria import (
    FATAL_TOPOLOGY as _FATAL_TOPOLOGY,
)
from meshpipeline.engines.openfoam_criteria import (
    MAX_NON_ORTHO as _MAX_NON_ORTHO,
)
from meshpipeline.engines.openfoam_criteria import (
    OF_SNAPPY_GUIDE as _OF_SNAPPY_GUIDE,
)
from meshpipeline.engines.review_types import Criterion, ReviewAxis

# Verified citations (curated by hand; see module docstring). The two OpenFOAM-wide ones live
# with the criteria that cite them, in `engines/openfoam_criteria.py`.
_CFMESH_HOME      = "https://cfmesh.com/cfmesh/"
_GMSH_DOC         = "https://gmsh.info/doc/texinfo/gmsh.html"


CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
            key="timed_out", label="Build completes within compute budget",
            op="==", threshold=False, gating=True,
            rationale=(
                "A meshDict too fine to build within the budget cannot be reproduced "
                "or iterated; the fix is coarser sizing, not more time."),
            evidence_url=_CFMESH_HOME,
        ),
        Criterion(
            key="rc", label="cartesianMesh exits cleanly", op="==", threshold=0, gating=True,
            rationale=(
                "A nonzero exit means the cut-cell workflow aborted - the polyMesh on "
                "disk is absent or partial, not a usable mesh."),
            evidence_url=_CFMESH_HOME,
        ),
        _FATAL_TOPOLOGY,
        _MAX_NON_ORTHO,
)

# The SEMANTIC review layer - what cfMesh's wrap-and-fill CUT-CELL mesh can fail at
# beyond the machine bars. Staircasing is INHERENT to cut-cell (not a defect); the
# concern is whether it is fine enough, and whether the requested refinements/layers
# actually materialized.
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="surface_staircasing_adequacy", validation_axis="quality",
        guidance=("cfMesh staircases the surface (cut-cell) by design - judge whether the "
                  "staircase is FINE ENOUGH to represent the body the workflow needs, not "
                  "whether it is absent. Coarser is acceptable for a draft; too coarse "
                  "loses a feature the request depends on."),
        concern="The stair-stepped surface is too coarse to represent your geometry",
        failure_signals=("staircasing so coarse a feature the request needs is lost",
                         "the wrapped surface no longer recognisably matches the body"),
        evidence=("render", "geometry", "brief"),
        # cut-cell staircasing is inherent and "fine enough for the workflow" is a judgment, not a
        # scalar - so visual_only (no metric anchor). But the judgement must be made on the CAPTURED
        # WALL at an inspectable scale, not the whole-domain opening: require a standard view AND an
        # isolated boundary patch, so a generic opening image cannot satisfy it. cfMesh always has
        # boundary patches, so the PATCH obligation is never vacuous.
        visual_only=True,
        requires=(RenderViewRequirement("iso"),
                  RenderTargetRequirement(TargetKind.PATCH, "patch:*")),
        evidence_url=_CFMESH_HOME,
    ),
    ReviewAxis(
        name="local_refinement_presence", validation_axis="quality",
        guidance=("Check that the local refinements the mesh script declares actually "
                  "materialized and stayed localized to their intended regions - the "
                  "reason the near-field is finer than the background."),
        concern="The refinement you asked for did not materialise where it was meant to",
        failure_signals=("a declared refinement region not visibly finer than the background",
                         "refinement bleeding across the whole domain instead of staying local"),
        evidence=("render", "mesh_script"),
        # A spatial-presence pattern (no scalar), but it must be shown by TARGET-SPECIFIC evidence,
        # never the opening image: require a PATCH inspection always, AND - only when the job
        # authored refinement regions (engine-owned applicability materializes them as REGION
        # targets) - the applicable REGION inspection. A region-less job gains no false REGION
        # obligation; an authored region absent from discovery stays missing evidence.
        visual_only=True,
        requires=(RenderTargetRequirement(TargetKind.PATCH, "patch:*"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_CFMESH_HOME,
    ),
    ReviewAxis(
        name="cut_cell_transition_quality", validation_axis="quality",
        guidance=("Check the cut-cell transitions near features and between refinement "
                  "regions are clean - no degenerate slivers at the surface, no abrupt "
                  "jumps between coarse and fine cells."),
        concern="Cells degrade where the mesh steps from coarse to fine",
        failure_signals=("sliver or degenerate cells where the cut cells meet the surface",
                         "an abrupt cell-size jump between neighbouring refinement regions"),
        evidence=("render", "quality_metrics"),
        # HYBRID: max_non_ortho is the always-measured advisory cell-quality metric that can fail at
        # review (skew_fraction is NOT emitted by cfMesh, so it is not required); plus a patch
        # inspection to see the cut-cell transitions. cfMesh always has boundary patches (PATCH is
        # unconditionally applicable - see spec.py), so this obligation is never vacuous.
        requires=(MetricRequirement("max_non_ortho"),
                  RenderTargetRequirement(TargetKind.PATCH, "patch:*")),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    ReviewAxis(
        name="near_wall_layers_generated", validation_axis="quality",
        guidance=("If the workflow calls for near-wall layers, confirm from a near-wall "
                  "slice that they were generated and are continuous along the wall - "
                  "cut-cell layer insertion is fragile and can silently produce none."),
        concern="The near-wall cells needed to resolve flow at the surface are missing",
        failure_signals=("no near-wall layers where the workflow expects them",
                         "layers present on part of the wall only, or collapsed into slivers"),
        evidence=("inspect_region", "render"),
        # cfMesh emits NO layer-coverage metric (that is a snappy hook), so this stays visual_only -
        # but "layers were generated along the wall" must be shown by WALL-PATCH-specific evidence,
        # never a generic opening image. Require a PATCH inspection; no metric is required, since
        # cfMesh does not emit one on every output path.
        visual_only=True,
        requires=(RenderTargetRequirement(TargetKind.PATCH, "patch:*"),),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
)
