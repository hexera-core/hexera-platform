# Responsibility: Declare the measurable bars and review axes a conjugate (coolant + metal) mesh is judged against.
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

#: The standard for stating what a mesh's own discretisation error is, and for refusing to state
#: one the sequence does not support. The coolant-volume bar is built on a measured error, not an
#: asserted one, and this is the practice that asks for that.
_ASME_VV20 = ("https://www.asme.org/codes-standards/find-codes-standards/"
              "v-v-20-standard-verification-validation-computational-fluid-dynamics-heat-transfer")
#: Microchannel cold plates as a field: Tuckerman and Pease, "High-performance heat sinking for
#: VLSI", IEEE Electron Device Letters 2 (1981) 126. The geometry this engine exists for.
_TUCKERMAN = "https://doi.org/10.1109/EDL.1981.25367"

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
        key="timed_out", label="Build completes within compute budget",
        op="==", threshold=False, gating=True,
        rationale=("A part too fine to mesh, split and check inside the compute budget cannot be "
                   "iterated on; the answer is fewer cells across the passages, not more time."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="rc", label="The mesher, the split and checkMesh all exit cleanly",
        op="==", threshold=0, gating=True,
        rationale=("A conjugate build is five OpenFOAM runs in a row - mesh, tag, split, check, "
                   "export. A nonzero exit means one of them stopped, and the regions on disk "
                   "are absent, partial or unchecked."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    _FATAL_TOPOLOGY,
    Criterion(
        key="coolant_error_over_tol",
        label="The meshed coolant is the coolant in the CAD", op="<=", threshold=1.0,
        gating=True,
        rationale=(
            "THE BAR THIS ENGINE STANDS ON. The mesh is cut out of a solid block by tagging the "
            "cells inside the coolant surface, so the one way it can be wrong without being "
            "broken is tagging the wrong ones - and a mesh of the wrong cells is a perfectly "
            "valid mesh of a part nobody sent. Measured: the unit cell meshed from a coolant "
            "surface wound inside-out came out 95.16% wrong by volume while checkMesh reported "
            "'Mesh OK'. "
            "A RATIO AND NOT A PERCENTAGE, because the bar is not a constant: it is the measured "
            "staircase error for the resolution used, with a 1.5x margin, and it rides in the "
            "plan. Stated as a fixed 5% here it became a SECOND reader of one fact, and the two "
            "disagreed - a two-channel plate came out 9.95% wrong at 5 cells across, which the "
            "resolution allows 19.89% of and this refused. 1.0 is 'exactly at the bar the plan "
            "recorded', so the criterion and the coolant_volume gate cannot drift apart."),
        evidence_url=_ASME_VV20,
    ),
    Criterion(
        key="coolant_error_pct",
        label="How far the meshed coolant is from the CAD, in per cent", op="<=", threshold=5.0,
        gating=False,
        rationale=(
            "REPORTED, NOT GATED, and 5% is here as the yardstick rather than the bar: it is the "
            "standing allowance, and the measured staircase error reaches it at about 13 cells "
            "across the passage (0.12%) but not at 5 (median 1.06%, worst 13.26%). The number "
            "that decides delivery is coolant_error_over_tol, against the resolution's own "
            "tolerance. This row exists so a reader sees the raw disagreement in the unit the "
            "part was specified in."),
        evidence_url=_ASME_VV20,
    ),
    Criterion(
        key="coolant_pieces_short",
        label="The coolant is in as many pieces as the part has passages", op="empty",
        threshold=None, gating=True,
        rationale=("A mesh can find the right total volume in the wrong number of places: one "
                   "network meshed as two disconnected halves has lost the slots that join the "
                   "levels, and a solver will run on it and report no flow between them. The "
                   "piece count is the independent reading that catches it."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="interface_ok", label="Heat can cross between the coolant and the metal",
        op="==", threshold=True, gating=True,
        rationale=("The two sides of the shared surface are the same faces of the same mesh - "
                   "measured at 93,600 on each side of the unit cell - so their counts must "
                   "match. A mismatch means the solver has nowhere to map the heat across and "
                   "solves the two regions as if they never touched."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="regions_missing", label="The coolant and the metal each have their own mesh",
        op="empty", threshold=None, gating=True,
        rationale=("One polyMesh per region is the coupled-case contract. A missing region is "
                   "either coolant the solver cannot flow or metal it cannot heat."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="empty_patches", label="Every named boundary has a surface behind it",
        op="empty", threshold=None, gating=True,
        rationale=("Both regions inherit every face of the block they were cut from, so a named "
                   "inlet the coolant never reaches still appears in the coolant's boundary list "
                   "with no faces. Nothing can be applied to it. Read per region: the pre-split "
                   "mesh survives at the root with both regions' faces added together."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    _MAX_NON_ORTHO,
    Criterion(
        key="max_skewness", label="Face skewness within solver tolerance", op="<=",
        threshold=4.0, gating=False,
        rationale=("The standard meshQualityControls bar (maxSkewness 4). The cells here are "
                   "cut-cell hexes, so skew only appears where a passage wall falls across a "
                   "cell: measured 0.03 in the coolant and 0.55 in the metal on the unit cell. "
                   "Advisory."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="resolution_short",
        label="Every passage has the cells across it the fidelity asks for", op="empty",
        threshold=None, gating=False,
        rationale=(
            "ADVISORY ON PURPOSE, and the measurements are why. The product's flow rule is 13 "
            "cells across the narrowest passage, and a real cold plate cannot hold it inside the "
            "cell budget: the manifold part at 13 across is about 10.2 M cells against a limit "
            "of 8 M. Nor does the count predict the error on any one part - seven cells across "
            "came out fourteen times worse than six on the unit cell, and the error grew "
            "steadily with refinement on the manifold. So the resolution is reported and the "
            "mesh is gated on the error itself (coolant_error_pct). Absent when the run did not "
            "measure it, which never reads as a pass."),
        evidence_url=_TUCKERMAN,
    ),
)

# The SEMANTIC review layer - what a conjugate mesh can fail at beyond the machine bars.
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="coolant_capture", validation_axis="conformance",
        guidance=("Look at the coolant region on its own and check it is the part's passages and "
                  "nothing else: every channel present over its whole length, the manifolds and "
                  "slots that join them open, no passage pinched shut and no pocket of metal "
                  "taken in as coolant. The volume bar is the coolant_volume gate; this is "
                  "whether the SHAPE is the part's."),
        concern="The meshed coolant is not the shape of the part's passages",
        failure_signals=("a channel that stops short of the manifold",
                         "a passage pinched closed where the part has it open",
                         "a blob of coolant where the part is solid metal",
                         "a slot joining two levels that is not there in the mesh"),
        evidence=("inspect_region", "render", "quality_metrics"),
        requires=(HardGateRequirement("coolant_volume"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_TUCKERMAN,
    ),
    ReviewAxis(
        name="metal_between_passages", validation_axis="quality",
        guidance=("Check the metal between and around the passages: the webs between adjacent "
                  "channels are there at their real thickness with cells through them, the lid "
                  "and base carry cells through their thickness, and no web has been meshed away "
                  "into coolant. The temperature drop through the metal is what a cold plate is "
                  "for, and a web with no cell through it cannot carry one."),
        concern="The metal between passages is missing, thinned or unresolved",
        failure_signals=("two adjacent channels joined through a web the part keeps solid",
                         "a web or a lid with no cell through its thickness",
                         "metal cells so coarse that the web is one cell wide"),
        evidence=("inspect_region", "render", "quality_metrics"),
        requires=(RenderTargetRequirement(TargetKind.REGION, "region:*"),),
        evidence_url=_TUCKERMAN,
    ),
    ReviewAxis(
        name="interface_continuity", validation_axis="quality",
        guidance=("Check the surface between the coolant and the metal is continuous over every "
                  "wetted wall and appears nowhere else. The face-count match is the interface "
                  "gate; this is whether the interface is in the right PLACES - wrapping every "
                  "passage, and not cutting across solid metal or across the open ports."),
        concern="The coupling surface is broken, missing or in the wrong place",
        failure_signals=("a stretch of passage wall with no interface on it",
                         "an interface running through solid metal",
                         "an interface across an inlet or outlet, which is open to the outside"),
        evidence=("inspect_region", "render", "quality_metrics"),
        requires=(HardGateRequirement("interface"),
                  MetricRequirement("max_non_ortho"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    ReviewAxis(
        name="passage_resolution", validation_axis="quality",
        guidance=("Judge from the MEASURED numbers, not from the look of it: the cells across "
                  "the narrowest passage the run used, and the coolant's own volume error. Say "
                  "plainly what that resolution supports - a pressure drop and a coolant "
                  "temperature rise need more cells across a channel than a conduction-dominated "
                  "temperature field does - and raise a concern when the error is near the bar "
                  "rather than comfortably inside it. Never call a passage resolved that the "
                  "numbers say is not."),
        concern="The resolution does not support what the mesh will be used for",
        failure_signals=("only a few cells across a channel that carries the pressure drop",
                         "a coolant volume error close to the resolution's bar",
                         "a passage resolved by the background cell with no refinement in it"),
        evidence=("quality_metrics", "render", "region_summary"),
        requires=(MetricRequirement("coolant_error_over_tol"),
                  RenderTargetRequirement(TargetKind.REGION, "region:*")),
        evidence_url=_ASME_VV20,
    ),
)

__all__ = ["CRITERIA_ROWS", "REVIEW_AXES"]
