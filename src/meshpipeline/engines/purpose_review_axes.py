# Responsibility: State what each workflow requires of any mesh, independently of the mesher that produced it.
# Boundaries: a declaration table; these axes are purpose-owned and must not be restated per engine.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import RenderViewRequirement
from meshpipeline.engines.review_types import ReviewAxis

# USE-CASE review axes - what each WORKFLOW requires of any mesh, independent of the
# engine that produced it. The reviewer runtime unions these with the selected engine's
# mesh-class axes (dedup by name). SAME no-setpoint invariant as the engine rubrics:
# WHAT to check + WHY + failure signals, never a numeric bar (chord multiples, y+, …).

# THE FEA CHECKLIST (Areen, 2026-10-22). An FEA job is reviewed against what a structural engineer
# needs of the delivered files - the right part, every CAD face named in every solver file, valid
# elements of the right type, enough detail through walls and at hot spots, each solver's limits,
# honest notes, and files that load with matching counts. The hard pass/fail is the measured
# checks' (engines/fea_review.measured_valid); these axes explain the mesh and add concerns, and a
# review can never fail an FEA mesh the measurements call valid (agents/reviewer/review_policy).
# Each axis is judged from the MEASURED FEA FACTS block of the brief ("fea_facts"), never from the
# 3D preview's surface groups: the preview shows the outer surface as one group by design.
_STRUCTURAL_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="fea_right_part", validation_axis="integrity",
        guidance=("Check the mesh is the part the user confirmed: its size and units match the "
                  "confirmed part, it is one continuous solid where the part is one body, its "
                  "volume follows the CAD, and no feature of the part (a hole, a boss, a rib, a "
                  "flange) is missing or added."),
        concern="The mesh does not look like the part you confirmed",
        failure_signals=("the meshed solid is a different size or in a different unit than confirmed",
                         "disconnected solid fragments where the part is one body",
                         "a feature of the CAD missing from the meshed solid"),
        evidence=("render", "fea_facts", "brief"),
        requires=(RenderViewRequirement("iso"),),
    ),
    ReviewAxis(
        name="fea_faces_named", validation_axis="conformance",
        guidance=("Check every CAD face is its own named group (Face_k) in all three solver files, "
                  "using the named-face counts per file in the measured FEA facts against the "
                  "CAD's own face count; and that any face the user named with a role landed on the "
                  "faces they meant. The 3D preview shows the outer surface as one group by design - "
                  "that is never a missing decomposition."),
        concern="Not every face of your part is a named group in every solver file",
        failure_signals=("a solver file names fewer faces than the CAD has",
                         "a face the user named with a role placed on the wrong faces"),
        evidence=("fea_facts", "brief"),
    ),
    ReviewAxis(
        name="fea_element_validity", validation_axis="integrity",
        guidance=("Check the measured validity: no inverted or degenerate element, and the element "
                  "type each solver expects (quadratic tetrahedra: C3D10, CTETRA with mid-side "
                  "nodes, SOLID187)."),
        concern="Some elements are not valid for the solvers they are for",
        failure_signals=("inverted or degenerate elements measured",
                         "a solver file written with a different element type than its solver expects"),
        evidence=("fea_facts", "quality_metrics"),
    ),
    ReviewAxis(
        name="fea_detail", validation_axis="quality",
        guidance=("Check there is enough detail where stress lives: elements through every thin "
                  "wall as the plan's rule asks, and refinement at the hot spots (holes, fillets, "
                  "notches, inside corners) - read from the measured through-wall count and the hot "
                  "spots short of their rule."),
        concern="The mesh is coarser than its rules ask through a wall or at a hot spot",
        failure_signals=("a thin wall resolved by too few elements through its thickness",
                         "a hot spot left short of its refinement rule"),
        evidence=("fea_facts", "quality_metrics"),
    ),
    ReviewAxis(
        name="fea_solver_limits", validation_axis="quality",
        guidance=("Check the element shape figures against each solver's own published limits "
                  "(the per-solver lines of the quality checks), and say which solver a figure "
                  "past a limit matters for."),
        concern="Some element shapes are past a limit of the solver you will use",
        failure_signals=("element shape figures past a solver's published limit, widespread rather "
                         "than at sharp features",),
        evidence=("quality_metrics", "fea_facts"),
    ),
    ReviewAxis(
        name="fea_honest_notes", validation_axis="conformance",
        guidance=("Check the delivery says plainly what it gave up: a plan coarsened to fit the "
                  "element limit names what that left short, and elements set aside at a cusp or a "
                  "micro detail are named with why stress there is not meaningful."),
        concern="The delivery does not say what was coarsened or set aside",
        failure_signals=("a coarsened plan with nothing named as short",
                         "elements set aside with no reason given"),
        evidence=("fea_facts",),
    ),
    ReviewAxis(
        name="fea_files_load", validation_axis="solvability",
        guidance=("Check all three solver files were written and agree: the same element count in "
                  "each, the same named faces, so the user can load whichever file their solver "
                  "reads."),
        concern="The solver files do not all load with the same mesh",
        failure_signals=("a solver file missing", "element or named-face counts that differ between files"),
        evidence=("fea_facts",),
    ),
)

_EXTERNAL_CFD_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="farfield_clearance", validation_axis="conformance",
        guidance=("Check the far-field domain leaves enough clearance around the body for "
                  "the declared external-flow analysis, judged against the brief - not a "
                  "fixed multiple you assume."),
        concern="The outer boundary sits too close to the body and will interfere with the flow",
        failure_signals=("body sitting too close to the inflow boundary",
                         "outflow boundary crowding the expected wake",
                         "top/side boundaries visibly constraining the flow around the body"),
        evidence=("render", "domain_extents", "brief"),
    ),
    ReviewAxis(
        name="domain_enclosure", validation_axis="integrity",
        guidance=("Check the body is fully enclosed by the fluid domain and not clipped "
                  "by a boundary - a clipped body is not the geometry the user submitted. "
                  "A body that stands on the ground rests on the ground wall patch by design: "
                  "its underside meeting that floor is the declared setup, not a clip."),
        concern="The body is not fully enclosed by the domain",
        failure_signals=("the body intersects or pokes through a domain boundary",
                         "part of the body clipped out of the fluid region"),
        evidence=("render", "domain_extents"),
    ),
    ReviewAxis(
        name="wake_resolution", validation_axis="quality",
        guidance=("When the request cares about wake behaviour, check the mesh downstream "
                  "of the body is plausibly resolved rather than dropping straight to "
                  "background coarseness behind it."),
        concern="The wake behind the body is not resolved - drag and separation will be wrong",
        failure_signals=("the wake region left at background coarseness when it matters",
                         "an abrupt refinement drop immediately behind the body"),
        evidence=("render", "mesh_script", "brief"),
    ),
)

_INTERNAL_CFD_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="flow_passage_preserved", validation_axis="conformance",
        guidance=("Check the inlet and outlet openings are preserved and the flow passage "
                  "is meshed open end to end - the fluid must be able to pass through the "
                  "cavity the user wants simulated."),
        concern="The flow passage is pinched or blocked - the fluid cannot pass through it",
        failure_signals=("an inlet or outlet opening occluded or meshed shut",
                         "the passage narrowed or sealed by over-thick near-wall cells"),
        evidence=("render", "inspect_region", "brief"),
    ),
    ReviewAxis(
        name="cavity_fill", validation_axis="integrity",
        guidance=("Check the cavity fills as one connected fluid region with no sealed "
                  "voids or disconnected pockets that would strand part of the flow."),
        concern="The cavity is not filled as one connected fluid volume",
        failure_signals=("disconnected fluid islands inside the cavity",
                         "unmeshed pockets where the fluid should be continuous"),
        evidence=("render", "region_count"),
    ),
    ReviewAxis(
        name="passage_near_wall_resolution", validation_axis="quality",
        guidance=("If the workflow needs near-wall resolution along the passage, check it "
                  "is present on the cavity walls rather than only in the core."),
        concern="The near-wall mesh in the passage is too coarse to resolve wall friction",
        failure_signals=("coarse near-wall cells along the passage where resolution matters",
                         "near-wall layers missing on the internal walls"),
        evidence=("inspect_region", "render"),
    ),
)

_CHT_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="region_split_completeness", validation_axis="conformance",
        guidance=("Check every region the user declared (the fluid and each solid) is present "
                  "as its own mesh after the split, and that no declared region collapsed to "
                  "empty or absorbed another - a conjugate simulation needs each material "
                  "region meshed separately."),
        concern="Not every region you declared came out as its own mesh",
        failure_signals=("a declared solid region missing after the split",
                         "two regions merged into one because their zones were not separated",
                         "a region left empty (its enclosing surface leaked)"),
        evidence=("region_summary", "region_cell_counts", "brief"),
    ),
    ReviewAxis(
        name="conformal_interface", validation_axis="integrity",
        guidance=("Check the fluid<->solid interfaces are conformal - the coupled patches on "
                  "either side share the same faces so heat can cross. A non-conformal or "
                  "missing interface breaks the thermal coupling the whole case exists for."),
        concern="The regions do not meet cleanly - the interface between them will not couple",
        failure_signals=("an expected fluid-solid interface patch missing on one side",
                         "interface face counts that do not match across the coupled pair",
                         "a gap between regions where they should touch"),
        evidence=("interface_patches", "region_summary", "render"),
    ),
    ReviewAxis(
        name="near_interface_resolution", validation_axis="quality",
        guidance=("Where the workflow cares about the thermal gradient, check the mesh is "
                  "resolved on BOTH sides of the interface rather than coarse in the solid - "
                  "the temperature gradient that drives the coupling lives right at the wall."),
        concern="The mesh is too coarse where the regions meet to resolve transfer across them",
        failure_signals=("solid region left at one cell through-thickness where a gradient matters",
                         "fluid near-wall resolution present but the solid side coarse"),
        evidence=("render", "inspect_region", "brief"),
    ),
)
