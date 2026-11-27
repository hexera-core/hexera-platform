# Responsibility: Declare the measurable bars and review axes a conjugate (coolant + metal) mesh is judged against.
# Boundaries: this engine's contribution only.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    HardGateRequirement,
    MetricRequirement,
    RenderTargetRequirement,
    TargetKind,
)
from meshpipeline.engines.cfmesh_conjugate.layers import (
    KEPT_OF_OFFERED_FLOOR_PCT as _KEPT_FLOOR_PCT,
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
        key="peak_over_predicted",
        label="The mesher's memory came out as the budget predicted", op="<=", threshold=1.25,
        gating=False,
        rationale=("NOT A BAR ON THE MESH - a bar on the MODEL that chose the machine the mesh "
                   "was built on. budget.PEAK_BYTES_PER_CELL is 748.1 bytes a cell plus 416 MiB "
                   "fixed, fitted by hand on three builds, and contracts/mesh_machine picks Cloud "
                   "Run or one of five VMs from it. 1.0 is the fit exactly right; above 1.0 it "
                   "under-prices, which is the direction that gets a run killed rather than "
                   "refused, and budget.py records one earlier fit that under-priced by 27%. "
                   "Absent when the peak could not be attributed to this build, which never "
                   "reads as a pass. Advisory."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="layer_short",
        label="The prism layers grew as well as this engine's measured run", op="empty",
        threshold=None, gating=False,
        rationale=(
            "REPORTED, NOT GATED, AND THE CHOICE IS DELIBERATE. The bars are "
            "`cfmesh_conjugate/layers`' own - KEPT_OF_OFFERED_FLOOR_PCT 95.0, "
            "THICKNESS_FLOOR_PCT 75.0, "
            "LAYERS_FLOOR_FRACTION 0.75, ILLEGAL_PER_MILLION_CEILING 200.0 - and this row is the "
            "one place a miss is said where a reader and the reviewer both see it. It is not "
            "gating for the reason `layers.shortfalls` states: the layer pass MOVES the only copy "
            "of the coolant mesh and rewrites it in place, so refusing on a coverage miss throws "
            "away a finished mesh that nothing else has anything against, and the mesh is the "
            "deliverable. `layers.complain` keeps the refusals that mean NOTHING GREW, and those "
            "stop the run inside the mesher. "
            "WHAT WAS SHIPPED QUIETLY WITHOUT IT. Job 0ff6f9e5 (2026-11-16) grew layers through "
            "the product on all 16,582,198 faces of the conjugate interface and finished at "
            "88.5826% coverage, 71.9% of the asked thickness, 2.25 layers of 3 and 8,916 faces in "
            "error over 37,247,044 cells added - three of these four floors missed, and worse "
            "than the 90.7146% run the thresholds were written to refuse. The judgement ran "
            "inside `conjugate.build` and `native._as_dict` dropped it, so the delivered manifest "
            "published `near_wall_layers: 3` and said nothing about layers otherwise. Absent when "
            "no layer pass was judged, which never reads as a pass. "
            "AND IT IS A VERDICT ON THE FLOORS THAT HAD A FIGURE, not always on all four: a "
            "partly readable log leaves this row empty - a pass - on floors nobody evaluated, so "
            "`layer_floors_unjudged` below says how many of the four this row is actually about."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="layer_floors_unjudged",
        label="Every layer floor this engine judges by had a figure to judge it on",
        op="empty", threshold=None, gating=False,
        rationale=(
            "THE DENOMINATOR UNDER THE ROW ABOVE, and without it that row read a verdict on one "
            "floor as a verdict on four. `layers.shortfalls` evaluates four floors - "
            "KEPT_OF_OFFERED_FLOOR_PCT, THICKNESS_FLOOR_PCT, LAYERS_FLOOR_FRACTION and "
            "ILLEGAL_PER_MILLION_CEILING - and guards each on its own input, which for three of "
            "them is the presence of a figure and for the faces-in-error one is a POSITIVE count "
            "of cells added, because that test is a ratio. So a log it could read only part of "
            "comes back with the same empty shortfall list as a "
            "pass that cleared all four. MEASURED, SYNTHETIC, 2026-11-17: a layer log holding "
            "only the Extruding and Added lines published layer_coverage_pct 99.9 with "
            "layer_short empty, so one judged floor was reported as four - on the same payload "
            "whose near_wall_layers_measured said the layer count had not been measured at all. "
            "This row carries `layers.floors_unjudged`' own names for the floors that had "
            "nothing: empty means all four were evaluated, a non-empty list names the ones that "
            "were not, and ALL FOUR NAMED means a pass ran whose log left no floor anything to be "
            "judged on. The row is ABSENT only when NO LAYER PASS RAN - a different answer from "
            "an unreadable log, and until 2026-11-17 the two published the same empty payload. "
            "Neither ever reads as a pass. A SECOND MEASUREMENT, SYNTHETIC, 2026-11-17: a "
            "zero-layer pass (90,000 faces in error over 0 cells added) had a figure under every "
            "one of the four keys and was judged by three, because the faces-in-error floor had "
            "nothing to divide its count by - so this row counts the floors that were EVALUATED "
            "and not the keys that were present. NOT GATING, for the same reason the row above "
            "is not: the pass has "
            "already moved the only copy of the coolant mesh and rewritten it in place, and an "
            "unreadable log is a defect in the record rather than in the mesh."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="layer_coverage_pct",
        label="How much of the conjugate interface the prism layers covered",
        op=">", threshold=0.0, gating=False,
        rationale=(
            "THE SAME FIGURE `layer_short` ALREADY JUDGES, DECLARED AS A BAR SO THE REVIEW CAN "
            "CITE IT. `layer_short` is an `empty` row: it carries the miss as a SENTENCE in its "
            "`measured` list, and a sentence is not evidence. The reviewer's ledger is filled "
            "from `plan.required_metric_keys`, which `agents/reviewer/assurance` builds out of "
            "the axes' own `requires` and nothing else, so a key no axis asks for is never "
            "filed: `ledger.usable_metric('layer_coverage_pct')` returned None and a finding "
            "could not cite the number it was about. "
            "MEASURED, job 110daca9's 279,281,786-cell cold plate: the layer pass covered "
            "90.6422% of the 16,582,198-face interface and kept 2.00 of 3 layers, the review "
            "failed the near-interface axis on exactly that, and the only citable metric on file "
            "was non-orthogonality - a true number about a different property. "
            "THE BAR IS NO LONGER 92% AND THE ROW IS STILL HERE, which is the whole point. The "
            "threshold was `layers.COVERAGE_FLOOR_PCT`, 92.0, a number its own comment says was "
            "placed just above one defective run's 90.7146%. Job b8f09ae9 then measured what the "
            "extruder was ever OFFERED: it offered 91.1145% of the interface and KEPT 99.5641% of "
            "that, so of the 9.28 points of missing coverage 8.89 were declined up front by "
            "`featureAngle` and 0.40 were lost in repair. The 8.89 is this part's own shape and no "
            "setting of the layer pass can move it - which is why both good runs of the plate sit "
            "under 92% (90.6422% and 90.7174%). A bar there reported a shape as a defect. "
            "SO THE BAR ON THE PASS IS THE ROW BELOW, `layer_kept_of_offered_pct` against "
            "`layers.KEPT_OF_OFFERED_FLOOR_PCT`, and the only requirement left on coverage itself "
            "is that it is positive: a pass that covered nothing grew no layers, which "
            "`layers.complain` refuses outright. The figure stays declared because the reviewer "
            "has to be able to cite it. "
            "NOT GATING, for the reason the two rows above are not: the layer pass has already "
            "moved the only copy of the coolant mesh and rewritten it in place, so refusing on a "
            "coverage miss throws away a finished mesh nothing else has anything against. "
            "ABSENT when no layer pass was judged, which `eligibility.advisory_gaps` records as "
            "a stated gap on an advisory row and never as missing evidence - an unmeasured "
            "advisory number must not end a review, and once did. "
            "`engines/cfmesh` and `engines/snappy_multiregion` have both declared this key since "
            "they were written; this engine produced the figure (`layers.py`'s "
            "`layer_coverage_pct`) and declared no bar on it."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="layer_kept_of_offered_pct",
        label="How many of the faces the extruder offered kept their prism layers",
        op=">=", threshold=_KEPT_FLOOR_PCT, gating=False,
        rationale=(
            "THE PART OF THE COVERAGE FIGURE THE LAYER PASS ACTUALLY CONTROLS. Coverage is two "
            "things added together: the faces `featureAngle` declined to offer, which is the "
            "part's shape, and the faces that were offered and then lost, which is the mover. "
            "The row above used to compare the sum against 92% and so reported a shape as a "
            "defect on every run of a real cold plate. "
            "MEASURED, two runs of the same plate: job b8f09ae9 offered 91.1145% of its "
            "16,582,198-face interface and kept 99.5641% of what it offered; job cba7ac69 kept "
            "99.7599%. The floor of 95.0 clears both by four points. "
            "AND IT STILL CATCHES A REAL FAILURE, which is what a bar is for: on the width rig "
            "at `layers.SMOOTH_SURFACE_NORMALS` 5 nearly every face was offered and only about "
            "three quarters kept their layers - a mover failing, which the old coverage bar "
            "would have scored the same as a part that simply declines 9% of its own interface. "
            "NOT GATING, for the reason every layer row here is not: the pass has already moved "
            "the only copy of the coolant mesh and rewritten it in place, so refusing throws "
            "away a finished mesh nothing else has anything against. "
            "ABSENT ON AN OLDER RUN AND THAT IS NOT A FAILURE: `offered_pct` and "
            "`kept_of_offered_pct` exist only on runs read by the instrumentation that produced "
            "them, and `eligibility.advisory_gaps` records an unmeasured advisory number as a "
            "stated gap rather than as missing evidence."),
        evidence_url=_OF_MESH_VALIDITY,
    ),
    Criterion(
        key="resolution_short",
        label="Every passage has the cells across it the flow fidelity asks for (reported, never gated)", op="empty",
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
        # `layer_coverage_pct` is HERE because this is the axis about the interface and the
        # figure is measured ON it, per interface patch (`layers.shortfalls`). Declaring it
        # puts the key in `plan.required_metric_keys`, which is plan-wide, so the purpose's
        # own near-interface axis can cite it too - and it is NOT declared on that axis,
        # which is shared by every CHT engine and would oblige a key the others never write.
        requires=(HardGateRequirement("interface"),
                  MetricRequirement("max_non_ortho"),
                  MetricRequirement("layer_coverage_pct"),
                  # ...AND THE SHARE OF IT THE PASS ACTUALLY CONTROLS, declared here for
                  # exactly the reason above: a criterion alone does not reach the ledger.
                  # `collect_deterministic_evidence` files what `plan.required_metric_keys`
                  # asks for, and that set is built from these `requires` and nothing else -
                  # so declaring the bar in CRITERIA_ROWS without declaring the key here
                  # left `ledger.usable_metric("layer_kept_of_offered_pct")` returning None
                  # and a finding unable to cite the one number the pass is judged on. That
                  # is the same defect this comment block was written about, one row down.
                  MetricRequirement("layer_kept_of_offered_pct"),
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
