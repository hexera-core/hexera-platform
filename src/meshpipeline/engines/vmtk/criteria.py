# Responsibility: Declare the measurable bars and review axes a VMTK mesh is judged against.
# Boundaries: this engine's contribution only - its vocabulary is anatomical: openings, branches and layer regions.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    TargetKind,
)
from meshpipeline.engines.review_types import Criterion, ReviewAxis

_VMTK_GUIDE = "https://www.vmtk.org/tutorials/MeshGeneration.html"

# Min acceptable tet scaled-Jacobian - the single source shared by the gate and this row.
# The UNAMBIGUOUS invalidity test is a non-positive tet VOLUME, reported as `fatal`; this
# floor is only a conditioning guard against slivers. It is deliberately permissive: in ONE
# in-container run on a real branching lumen, a clean (zero-inverted) TetGen mesh bottomed
# out at scaled_jacobian ≈ 0.014, so a 0.05 bar would have false-rejected a VALID mesh -
# and a false reject is worse here than a sliver the reviewer can weigh. That is a single
# data point, not a distribution: treat 0.01 as "below any mesh we have actually seen pass",
# and re-derive it if real runs show clean meshes sitting lower.
QUALITY_FLOOR = 0.01

#: Fewest cells across the local passage diameter a delivered CFD mesh may have. Industry RANS
#: practice on internal flow is 20-40; the engine's default edge factor (0.15) puts about 13;
#: below 12 the core flow is not resolved and the mesh is a preview, not a deliverable. Measured
#: by check_mesh from the fill itself (passage_cells_across) on engine-staged CAD runs.
PASSAGE_MIN_CELLS_ACROSS = 12

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
        key="timed_out", label="Build completes within compute budget",
        op="==", threshold=False, gating=True,
        rationale=("A mesh too fine to build within the compute budget cannot be reproduced or "
                   "iterated; the fix is a coarser edge_length_factor, not more time."),
        evidence_url=_VMTK_GUIDE,
    ),
    Criterion(
        key="rc", label="VMTK pipeline exits cleanly", op="==", threshold=0, gating=True,
        rationale=("A nonzero exit means the centerline / remesh / volume-mesh pype aborted - the "
                   "mesh.vtu on disk is absent or partial, not a usable mesh."),
        evidence_url=_VMTK_GUIDE,
    ),
    Criterion(
        key="fatal", label="No inverted or degenerate tetrahedra", op="empty", threshold=None,
        gating=True,
        rationale=("A zero- or negative-volume tetrahedron makes the assembled operator singular. "
                   "Universally invalid, regardless of the solver the mesh feeds."),
        evidence_url=_VMTK_GUIDE,
    ),
    Criterion(
        key="min_quality", label="Tet quality clears the floor", op=">", threshold=QUALITY_FLOOR,
        gating=True,
        rationale=("Slivers (near-zero-quality tets) destroy conditioning even when their volume is "
                   "positive. The floor is the standard bar for an unstructured tetrahedral mesh; "
                   "below it the mesh is numerically fragile whatever the analysis."),
        evidence_url=_VMTK_GUIDE,
    ),
    Criterion(
        key="cells", label="Mesh is non-empty", op=">", threshold=0, gating=True,
        rationale=("Zero cells means the volume fill produced nothing - usually an unclosed lumen "
                   "surface (open profiles were not capped), so there was no interior to fill."),
        evidence_url=_VMTK_GUIDE,
    ),
    Criterion(
        key="passage_cells_across", label="Core flow resolved across the passage", op=">",
        threshold=PASSAGE_MIN_CELLS_ACROSS - 1, gating=True,
        rationale=("Cells across the local passage diameter, measured from the delivered fill. "
                   "Under twelve the velocity profile and pressure drop are not resolved for any "
                   "CFD use; industry RANS practice on internal flow is twenty to forty."),
        evidence_url=_VMTK_GUIDE,
    ),
    Criterion(
        key="layer_coverage", label="Near-wall layers inflated", op=">", threshold=0.0,
        gating=False,
        rationale=("Boundary layers are inflated inward from the lumen wall for near-wall "
                   "resolution; coverage % reports how much of the wall actually received them "
                   "(layers can collapse at tight bifurcations). Advisory - the target depends on "
                   "the case's near-wall requirement."),
        evidence_url=_VMTK_GUIDE,
    ),
)

# The SEMANTIC review layer - what a centerline-based tet mesh can fail at beyond the machine
# bars. Each axis is HYBRID: it names, in `requires`, the measured metric(s) and the vascular
# render targets that must exist before it can be judged (see the module docstring).
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="lumen_fidelity", validation_axis="conformance",
        guidance=("Check the delivered wall reproduces the SUBMITTED lumen - a remesh that smoothed "
                  "the wall inward has quietly narrowed the passage the user asked about. Isolate "
                  "the lumen wall and compare its shape against the submitted lumen the mesh was "
                  "built from."),
        concern="The meshed wall no longer matches the submitted lumen - the geometry drifted",
        failure_signals=("the wall visibly smoothed away a bulge, notch or narrowing present in "
                         "the submitted lumen",
                         "the lumen cross-section pinched or ballooned relative to the input"),
        evidence=("layer_report", "brief"),
        evidence_url=_VMTK_GUIDE,
        requires=(RenderViewRequirement("iso"),
                  RenderTargetRequirement(TargetKind.LAYER_REGION, "layer_region:*")),
    ),
    ReviewAxis(
        name="opening_integrity", validation_axis="conformance",
        guidance=("Check every inlet/outlet cap is present and well-formed - one per open profile "
                  "of the submitted lumen. A cap that was lost, merged with another or left "
                  "unclosed is a boundary condition the solver cannot apply. Isolate each opening "
                  "and confirm it is a clean, flat cap."),
        concern="An inlet/outlet cap is missing, merged or malformed - the solver cannot use it",
        failure_signals=("fewer caps in the delivered mesh than open profiles in the submitted "
                         "lumen",
                         "two openings merged into one cap",
                         "a cap that is ragged, non-planar or not closed"),
        evidence=("brief",),
        evidence_url=_VMTK_GUIDE,
        requires=(RenderViewRequirement("iso"),
                  RenderTargetRequirement(TargetKind.OPENING, "opening:*")),
    ),
    ReviewAxis(
        name="connectivity", validation_axis="integrity",
        guidance=("Check every centerline branch survived into the volume mesh as an open, "
                  "connected passage. Navigate to each branch and confirm the lumen is present and "
                  "open there - a branch pinched shut by layers or lost to remeshing silently "
                  "changes the geometry the user asked about."),
        concern="A branch is missing or sealed off - the flow split the mesh represents is wrong",
        failure_signals=("a branch present in the centerlines with no open passage in the mesh",
                         "a bifurcation collapsed into a single passage",
                         "a branch sealed off by boundary layers"),
        evidence=("brief",),
        evidence_url=_VMTK_GUIDE,
        requires=(RenderTargetRequirement(TargetKind.BRANCH, "branch:*"),),
    ),
    ReviewAxis(
        name="local_anatomical_fidelity", validation_axis="quality",
        guidance=("Beyond the raw layer-coverage percentage, check the near-wall layers hold up "
                  "LOCALLY where wall shear matters - a layer that collapsed at one tight bend "
                  "averages away in a global coverage number. Inspect the wall region for stretches "
                  "where the layers thinned, collapsed or pinched the passage."),
        concern="The near-wall layers break down locally where the global coverage looks fine",
        failure_signals=("layers collapsed into slivers at a tight bend or bifurcation",
                         "layers absent along stretches of wall where the gradient matters",
                         "layers so thick they pinch the passage"),
        evidence=("layer_report", "quality_metrics"),
        evidence_url=_VMTK_GUIDE,
        requires=(MetricRequirement("layer_coverage"),
                  RenderTargetRequirement(TargetKind.LAYER_REGION, "layer_region:*")),
    ),
)
