# Responsibility: Declare the measurable bars and review axes a snap-grid (placed-parts) mesh is judged against.
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
    OF_MESH_VALIDITY as _OF_MESH_VALIDITY,
)
from meshpipeline.engines.review_types import Criterion, ReviewAxis

#: The ECXML standard the mesher reads (JEDEC JEP181A); the file's numbers are the model.
_JEP181 = "https://www.jedec.org/standards-documents/docs/jep181a"

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
        key="timed_out", label="Build completes within compute budget",
        op="==", threshold=False, gating=True,
        rationale=("A model too fine to grid and check within the compute budget cannot be "
                   "reproduced or iterated; the fix is a smaller cell budget, not more time."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="rc", label="The snap-grid mesher and checkMesh exit cleanly", op="==", threshold=0,
        gating=True,
        rationale=("A nonzero exit means the file could not be placed, the grid could not keep "
                   "every layer within the budget, or checkMesh found a problem - the per-region "
                   "meshes on disk are absent or unchecked."),
        evidence_url=_JEP181,
    ),
    Criterion(
        key="regions_missing", label="Every part and air space has its own mesh", op="empty",
        threshold=None, gating=True,
        rationale=("One polyMesh per region (each part, each connected air space) is the "
                   "multi-region contract. A missing region is a part the solver cannot heat."),
        evidence_url=_JEP181,
    ),
    Criterion(
        key="interface_ok", label="Interfaces between parts are conformal", op="==",
        threshold=True, gating=True,
        rationale=("Both sides of every interface are the same grid faces, so their counts must "
                   "match each other and the grid. A mismatch means heat cannot cross there."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    _FATAL_TOPOLOGY,
    Criterion(
        key="skew_fraction", label="Skewed faces are localized (all regions)", op="<=",
        threshold=5e-4, gating=True,
        rationale=("A snap grid is hexahedral; only the faces pulled onto a round part's side "
                   "can skew. A few there is normal; widespread skew is not. Judged on the "
                   "fraction of flagged faces (<= 0.05%), not the single worst value."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    _MAX_NON_ORTHO,
    Criterion(
        key="thin_layers_short", label="Every thin layer has the cells across it that were asked",
        op="empty", threshold=None, gating=False,
        rationale=("Every layer always has at least one cell through it (a layer is never merged "
                   "away). A layer listed here has fewer cells across it than the plan asked for, "
                   "usually because the cell budget ran out; the temperature drop across it is "
                   "resolved more coarsely. Advisory - the mesh is still the file's model."),
        evidence_url=_JEP181,
    ),
)

# The SEMANTIC review layer - what a placed-parts mesh can fail at beyond the machine bars.
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="placed_part_capture", validation_axis="quality",
        guidance=("For EACH region, check the part sits where the file puts it and keeps its "
                  "shape: boxes are exact, round parts are snapped onto their circle, and every "
                  "thin layer (die attach, thermal pad, board copper) has cells through it. "
                  "Judge from sectioned views per region and the build report."),
        concern="A part is not captured as the file describes it",
        failure_signals=("a part missing, or merged into a neighbour",
                         "a round part left as a staircase",
                         "a thin layer with no cell through it"),
        evidence=("render", "region_summary", "quality_metrics"),
        requires=(RenderTargetRequirement(TargetKind.REGION, "region:*"),),
        evidence_url=_JEP181,
    ),
    ReviewAxis(
        name="interface_continuity", validation_axis="quality",
        guidance=("Check every interface between touching parts (and between a part and the air) "
                  "is continuous - one face on each side - and that no interface appears where "
                  "the file keeps two parts apart. The face-count match is the interfaces gate; "
                  "this looks at where the interfaces are."),
        concern="An interface between parts is missing, broken or invented",
        failure_signals=("two parts that touch in the file with no interface between them",
                         "an interface between parts the file keeps apart",
                         "cells near an interface badly distorted"),
        evidence=("inspect_region", "render", "quality_metrics"),
        requires=(HardGateRequirement("interfaces"),
                  MetricRequirement("max_non_ortho"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_OF_MESH_VALIDITY,
    ),
)
