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
        Criterion(
            # THE SAME ROW SNAPPY DECLARES (engines/snappy/criteria.py), on the same manifest key.
            # cfMesh prints no layer report, so finalize COUNTS the layers on the mesh
            # (engines/layer_census.py) against the layers the request asked for; without this row
            # a cfMesh mesh's layers were never on its report card and the reviewer's layer axis
            # was a render judgement on a band too thin to see.
            key="layer_coverage_pct", label="Prism boundary layers inflated", op=">",
            threshold=0.0,
            gating=False,
            rationale=(
                "boundaryLayers extrudes prism layers off the wall for near-wall resolution; "
                "coverage % is the share of the REQUESTED layer cells the wall actually got, "
                "counted on the delivered mesh (cfMesh reports none itself). Advisory - the "
                "target coverage depends on the case's y+ requirement."),
            evidence_url=_CFMESH_HOME,
        ),
)

# The SEMANTIC review layer - what cfMesh's wrap-and-fill CUT-CELL mesh can fail at
# beyond the machine bars. Staircasing is INHERENT to cut-cell (not a defect); the
# concern is whether it is fine enough, and whether the requested refinements/layers
# actually materialized.
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="surface_staircasing_adequacy", validation_axis="quality",
        guidance=("cfMesh projects its boundary cells onto the surface, so the wall follows "
                  "the body as finely as the wall cell - judge whether the captured wall is FINE "
                  "ENOUGH to represent the body the workflow needs: coarser is acceptable for a "
                  "draft; too coarse rounds off a feature the request depends on."),
        concern="The meshed wall is too coarse to represent your geometry",
        failure_signals=("a feature the request needs is rounded off or lost at this wall cell",
                         "the meshed surface no longer recognisably matches the body"),
        evidence=("render", "geometry", "brief"),
        # how finely the wall follows the body is set by the wall cell, and "fine enough for the workflow" is a judgment, not a
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
        guidance=("If the workflow calls for near-wall layers, judge this from the MEASURED "
                  "layer figures in the mesh info - the layers COUNTED on the delivered mesh "
                  "against the number the request asked for (coverage %, mean layers per wall "
                  "face, the share of the wall with all of them). Those numbers are ground truth; "
                  "the layer band is far too thin to count in a render. Fewer layers than "
                  "requested is a shortfall even where the stack that exists is clean. Use a "
                  "near-wall slice to see WHERE layers are missing, and only judge from the "
                  "slice alone when the mesh info says the count could not be measured."),
        concern="The near-wall cells needed to resolve flow at the surface are missing",
        failure_signals=("measured layers well short of the number the request asked for",
                         "no near-wall layers where the workflow expects them",
                         "layers present on part of the wall only, or collapsed into slivers"),
        evidence=("layer_report", "inspect_region", "render"),
        # The finalize step COUNTS the layers on the mesh (engines/layer_census.py) and writes
        # layer_coverage_pct, as snappy's log parse does - but not on every path (a mesh with no
        # layers asked, a binary or oversized polyMesh), so the metric is cited when present and
        # not required; "layers were generated along the wall" must still be shown by
        # WALL-PATCH-specific evidence, never a generic opening image.
        visual_only=True,
        requires=(RenderTargetRequirement(TargetKind.PATCH, "patch:*"),),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
)
