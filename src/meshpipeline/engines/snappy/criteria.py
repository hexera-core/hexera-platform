# Responsibility: Declare the measurable bars and review axes a snappyHexMesh mesh is judged against.
# Boundaries: this engine's contribution only.
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

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
            key="timed_out", label="Build completes within compute budget",
            op="==", threshold=False, gating=True,
            rationale=(
                "A mesh too fine to build within the compute budget cannot be "
                "reproduced or iterated; the fix is a coarser budget, not more time."),
            evidence_url=_OF_SNAPPY_GUIDE,
        ),
        Criterion(
            key="rc", label="snappyHexMesh exits cleanly", op="==", threshold=0, gating=True,
            rationale=(
                "A nonzero exit means the castellate/snap/addLayers sequence aborted - "
                "the polyMesh on disk is absent or partial, not a usable mesh."),
            evidence_url=_OF_SNAPPY_GUIDE,
        ),
        Criterion(
            key="wall_faces", label="Body captured on the wall patch", op=">", threshold=0, gating=True,
            rationale=(
                "Zero wall-patch faces means the carve leaked (locationInMesh ended up "
                "outside the fluid or the surface was not sealed) - the mesh does not "
                "contain the body at all."),
            evidence_url=_OF_SNAPPY_GUIDE,
        ),
        _FATAL_TOPOLOGY,
        Criterion(
            key="skew_fraction", label="Skewed faces are localized", op="<=", threshold=5e-4, gating=True,
            rationale=(
                "checkMesh flags faces above the skewness bars (maxInternalSkewness 4, "
                "maxBoundarySkewness 20 - the standard meshQualityControls values). On "
                "a body-fitted hex+prism external-aero mesh, a handful of flagged faces "
                "at a wing-body junction is a known mesher limit and fully solvable; "
                "widespread skew is not. The honest production signal is therefore the "
                "FRACTION of flagged faces (localization ≤ 0.05% of all faces), not the "
                "single worst value."),
            evidence_url=_OF_SNAPPY_GUIDE,
        ),
        _MAX_NON_ORTHO,
        Criterion(
            # The manifest reports this as layer_coverage_pct (engines/snappy/finalize.py);
            # the key MUST match or the row never evaluates - which is exactly what happened:
            # with the old key "layer_coverage" the measured value was always None, the
            # advisory silently skipped, and the layer call fell entirely to the LLM
            # reviewer's judgment (whose improvised thresholds failed 41.9% while passing
            # 46.1% on same-purpose parts).
            key="layer_coverage_pct", label="Prism boundary layers inflated", op=">",
            threshold=0.0,
            gating=False,
            rationale=(
                "addLayersControls inflates prism layers off the wall for near-wall "
                "resolution; coverage % reports how much of the wall actually received "
                "them (layers can locally collapse at sharp features). Advisory - the "
                "target coverage depends on the case's y+ requirement."),
            evidence_url=_OF_SNAPPY_GUIDE,
        ),
)

# The SEMANTIC review layer - what snappy's body-FITTED hex+prism mesh can fail at
# beyond the machine bars. These INTERPRET the render + measured layer/skew evidence;
# the numeric coverage/skew bars themselves are CRITERIA_ROWS above.
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="surface_capture", validation_axis="quality",
        guidance=("Judge this primarily from the MEASURED surface-capture deviation in the "
                  "mesh info - the distance from the snapped wall to the input CAD, as a "
                  "fraction of the cell. That number is ground truth for snap quality; a "
                  "render CANNOT show it reliably (the body is small and faceting is "
                  "sub-pixel, so do not call staircasing from a blurry view, and do not call a "
                  "snap clean just because the face count is high). A near-zero mean and p95 "
                  "mean the wall sits on the CAD (a clean snap); a large deviation, or much of "
                  "the wall lying well off the CAD, means staircasing or lost features. Use "
                  "the render only to corroborate the SHAPE - that the mesh is of the RIGHT "
                  "geometry with no gross holes or missing parts - and report the mismatch if "
                  "what you see is not that geometry."),
        concern="The mesh does not follow your geometry's surface",
        failure_signals=("the measured surface-capture deviation is large - the wall strays "
                         "well off the input CAD over much of its area",
                         "the rendered shape is not the submitted geometry / has gross holes",
                         "a required feature region shows a large local deviation"),
        evidence=("surface_deviation", "render", "surface_features"),
        # HYBRID: the snapped wall is a boundary patch - confirm its shape against the input from a
        # standard view and by isolating the patches. (surface_deviation is measured but only when a
        # CAD reference exists, so it stays an informal evidence hint, not a hard obligation.)
        requires=(RenderViewRequirement("iso"),
                  RenderTargetRequirement(TargetKind.PATCH, "patch:*")),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    ReviewAxis(
        name="prism_layer_coverage", validation_axis="quality",
        guidance=("Judge this from the MEASURED coverage % reported in the mesh info (overall "
                  "and per wall patch) - that number is ground truth. The layer band is a tiny "
                  "fraction of the body length and is NOT resolvable in a whole-body render or "
                  "an internal slice, so never infer 'no layers' from a view where the near-wall "
                  "looks dense/black - cite the number. Then interpret whether that coverage is "
                  "adequate for the near-wall resolution the workflow needs, and whether the "
                  "missing fraction is in tolerable places (sharp trailing edge, tight concave "
                  "junction) rather than across whole patches (the per-patch numbers show WHERE)."),
        concern="The boundary layer does not cover enough of the wall to trust near-wall results",
        failure_signals=("measured coverage too low for the near-wall resolution the workflow needs",
                         "a whole wall patch near zero coverage while others are covered",
                         "layers collapsed into slivers (very low thickness at high face count)"),
        evidence=("layer_report", "inspect_region", "render"),
        # HYBRID: a measured cell-quality metric that can actually fail at review (max_non_ortho is
        # always measured and advisory), plus a sectional region inspection where layers live. The
        # REGION obligation is engine-owned applicability-gated: it applies only when the job
        # authored inspection regions (see _target_applicability in spec.py).
        requires=(MetricRequirement("max_non_ortho"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
    ReviewAxis(
        name="feature_refinement_transition", validation_axis="quality",
        guidance=("Check refinement transitions between coarse and fine regions are "
                  "gradual and that feature-edge refinement did not create isolated "
                  "over-refined shells or abrupt jumps. This is a spatial-PATTERN judgment "
                  "(where the fine cells sit relative to features) that no single number "
                  "captures - judge it from the render, at a scale where the transition cells "
                  "are resolved; if you cannot resolve them, zoom in or say so."),
        concern="Refinement around sharp features is uneven or leaves bad cells behind",
        failure_signals=("abrupt cell-size jumps between refinement regions",
                         "isolated over-refined shells wrapping every feature edge"),
        evidence=("render", "inspect_region"),
        # Visual because the property IS spatial: where the fine cells sit relative to
        # features. A cell-growth-ratio scalar would summarise the transition without
        # locating it, so a mesh with one bad shell and an acceptable mean ratio would
        # pass a numeric anchor it should not.
        visual_only=True,
        evidence_url=_OF_SNAPPY_GUIDE,
    ),
)
