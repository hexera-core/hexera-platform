# Responsibility: State what each workflow requires of any mesh, independently of the mesher that produced it.
# Boundaries: a declaration table; these axes are purpose-owned and must not be restated per engine.
from __future__ import annotations

from meshpipeline.engines.review_types import ReviewAxis

# USE-CASE review axes - what each WORKFLOW requires of any mesh, independent of the
# engine that produced it. The reviewer runtime unions these with the selected engine's
# mesh-class axes (dedup by name). SAME no-setpoint invariant as the engine rubrics:
# WHAT to check + WHY + failure signals, never a numeric bar (chord multiples, y+, …).
_STRUCTURAL_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="restraint_load_surfaces", validation_axis="conformance",
        guidance=("Check the surfaces the user designated for restraints, loads and "
                  "contact are tagged as named groups and cover the faces the user "
                  "actually meant - the semantic correctness of the tagging, not just "
                  "that some group exists."),
        concern="The surfaces you restrain or load are not resolved well enough to trust the stresses",
        failure_signals=("a restraint or load group placed on the wrong faces",
                         "a load-bearing surface the request named left untagged"),
        evidence=("groups", "region_summary", "brief"),
    ),
    ReviewAxis(
        name="solid_continuity", validation_axis="integrity",
        guidance=("Check the mesh is one continuous solid volume where the part is a "
                  "single body - a stiffness matrix cannot couple disconnected fragments."),
        concern="The solid is not continuous - the mesh has splits the part does not",
        failure_signals=("disconnected solid fragments where the part is one body",
                         "internal gaps or unmeshed pockets inside the solid"),
        evidence=("region_count", "quality_metrics"),
    ),
    ReviewAxis(
        name="stress_region_quality", validation_axis="quality",
        guidance=("Check element quality is adequate where stress concentrates - near "
                  "restraints, loads, fillets and thin sections - since that is where "
                  "poor elements corrupt the result the user cares about."),
        concern="The mesh is too coarse or too distorted where stress concentrates",
        failure_signals=("low-quality elements clustered at load/restraint/feature regions",
                         "thin sections resolved by a single distorted element through-thickness"),
        evidence=("quality_metrics", "groups", "render"),
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
                  "by a boundary - a clipped body is not the geometry the user submitted."),
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
