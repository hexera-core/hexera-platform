# Responsibility: Declare the measurable bars and review axes a multi-region snappyHexMesh mesh is judged against.
# Boundaries: this engine's contribution only.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    HardGateRequirement,
    MetricRequirement,
    RenderTargetRequirement,
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

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
        key="timed_out", label="Build completes within compute budget",
        op="==", threshold=False, gating=True,
        rationale=("A multi-region mesh too fine to build (the background snappyHexMesh pass + the split of "
                   "every region) within the compute budget cannot be reproduced or iterated; "
                   "the fix is a coarser budget, not more time."),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    Criterion(
        key="rc", label="snappyHexMesh + splitMeshRegions exit cleanly", op="==", threshold=0, gating=True,
        rationale=("A nonzero exit means the background carve or the region split aborted - the "
                   "per-region polyMesh set on disk is absent or partial, not a usable multi-region case."),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    Criterion(
        key="regions_missing", label="Every declared region survived the split", op="empty", threshold=None,
        gating=True,
        rationale=("splitMeshRegions must produce one polyMesh per declared region (the fluid and "
                   "each solid). A missing region means its cellZone leaked or two zones merged - "
                   "the coupled case cannot couple a region that was not meshed."),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    Criterion(
        key="interface_ok", label="Fluid-solid interfaces are conformal", op="==", threshold=True, gating=True,
        rationale=("Each fluid<->solid interface must expose coupled patches with MATCHING face "
                   "counts on both sides (splitMeshRegions creates conformal mappedWall pairs). A "
                   "mismatched or missing interface means heat cannot cross - the coupling the case "
                   "exists for is broken."),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    _FATAL_TOPOLOGY,
    Criterion(
        key="skew_fraction", label="Skewed faces are localized (all regions)", op="<=", threshold=5e-4,
        gating=True,
        rationale=("checkMesh flags faces above the standard skewness bars. As for any body-fitted "
                   "hex+prism mesh, a handful of flagged faces at a junction is a known mesher limit "
                   "and solvable; widespread skew is not. Judged on the FRACTION of flagged faces "
                   "across all regions (localization <= 0.05%), not the single worst value."),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    _MAX_NON_ORTHO,
    Criterion(
        key="layer_coverage", label="Prism layers inflated on the fluid walls", op=">", threshold=0.0,
        gating=False,
        rationale=("Prism layers off the fluid-side walls (including the fluid face of each "
                   "interface) resolve the near-wall gradient; coverage % reports how much of the "
                   "wall received them. Advisory - the target depends on the case's near-wall "
                   "resolution requirement."),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
)

# The SEMANTIC review layer - what a multi-region snappy multi-region mesh can fail at beyond the
# machine bars. These INTERPRET a sectioned render + per-region evidence; the numeric
# bars themselves are CRITERIA_ROWS above. (The user-declared conjugate_heat_transfer
# PURPOSE contributes its own axes too - the reviewer runtime unions them, deduped.)
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="per_region_surface_capture", validation_axis="quality",
        guidance=("For EACH region, check the snapped mesh conforms to that region's bounding "
                  "surface and preserves the features that matter - a solid region that lost its "
                  "shape, or a fluid passage that pinched shut, corrupts the coupled result. Judge "
                  "from sectioned surface views per region."),
        concern="A region's surface is not captured faithfully",
        failure_signals=("a region's surface stair-stepped (snap failed there)",
                         "a thin solid wall between fluid passages lost or merged away",
                         "a fluid passage pinched shut by over-thick layers"),
        evidence=("render", "region_summary", "quality_metrics"),
        # HYBRID: each region's surface is inspected sectionally (REGION). multiregion ALWAYS has
        # regions/interfaces, so REGION is unconditionally applicable (see spec.py).
        requires=(RenderTargetRequirement(TargetKind.REGION, "region:*"),),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    ReviewAxis(
        name="interface_layer_placement", validation_axis="quality",
        guidance=("Check the prism layers sit on the FLUID side of each fluid-solid interface "
                  "(where the interface boundary layer lives) and did not push INTO the solid or "
                  "collapse the interface - beyond the raw coverage number the criteria report."),
        concern="Boundary layers are misplaced at the interface between regions",
        failure_signals=("layers missing on the fluid face of an interface where the gradient matters",
                         "layers inflated into the solid region",
                         "the interface smeared or non-conformal after layer addition"),
        evidence=("layer_report", "inspect_region", "render"),
        # HYBRID: canonical INTERFACE evidence is the interfaces gate (bundle-owned) plus a measured
        # advisory cell-quality metric and a sectional region/interface inspection. The interfaces
        # obligation cannot be waived by a missing interface target - it is a gate the executor ran.
        requires=(HardGateRequirement("interfaces"),
                  MetricRequirement("max_non_ortho"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
)
