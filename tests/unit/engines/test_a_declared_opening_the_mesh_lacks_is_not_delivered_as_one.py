"""A mouth the plan calls an opening must not reach the customer unrecorded.

MEASURED, on the delivered 283M-cell cold plate (job 57b613d7, 2026-11-15). `conjugate_plan.json`
ships INSIDE the customer bundle - `spec.py`'s Deliverable lists `M("conjugate_plan.json")` beside
`constant/` and `system/` - and the copy that shipped declares three `port_patches`, every one of
them `"type": "opening"`:

    mouth_1  11.8 mm   <- the capped lid port
    mouth_2   8.0 mm
    mouth_3   8.0 mm

The fluid region's `constant/fluid/polyMesh/boundary` holds three patches and they are
`fluid_to_solid`, `mouth_2` (1,306 faces) and `mouth_3` (1,334 faces). There is no `mouth_1`.
`topoSetPorts.log` found 2,640 faces under its box and then 0 after the subset, and
`createPatch.log` says "Removed zero-sized patch mouth_1 type patch at position 2". So the
customer opens one archive and finds a plan naming an opening the mesh beside it does not have,
and `quality.empty_patches` is `[]` because the gate aimed at this looks for a patch with no faces
and this patch was GONE.

WHY NOTHING CAUGHT IT. `conjugate.verify` already had the check - "a named patch with zero faces",
its own docstring - written after job b6762d5c lost an outlet exactly this way. It sits behind
`if (plan := report.ports) is not None`, and `report.ports` comes from `plan.ports_from`, which
ends `if not inlet and not outlet: return None`. Every mouth here is typed `opening` because the
customer asked for no ports to be named, so the whole section was skipped.

WHAT THE FIX IS, AND IS NOT. The plan is not rewritten. It is the ASK - written before the mesh
exists, feeding the native payload digest, and quoted by the reproducibility stamp - so editing
the delivered copy would erase the fact that the run requested a patch and lost it, which is the
most useful thing in that bundle. The mesh ANSWERS it instead, in `conjugate_report.json`, which
ships in the same tarball.
"""
from __future__ import annotations

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    ConjugateReport,
    RegionReport,
    verify,
)
from meshpipeline.engines.cfmesh_conjugate.plan import ConjugatePlan, ports_from

#: The three mouths exactly as the delivered plan carries them.
DELIVERED_MOUTHS = (
    {"name": "mouth_1", "type": "opening", "face": "wall_outer",
     "centre_m": (0.065923414495, 0.033446178113, 0.07540532726), "diameter_m": 0.0118},
    {"name": "mouth_2", "type": "opening", "face": "wall_outer",
     "centre_m": (0.099881949608, 0.054681178113, 0.043305688999), "diameter_m": 0.008},
    {"name": "mouth_3", "type": "opening", "face": "wall_outer",
     "centre_m": (0.089339605589, 0.054681178113, 0.077388253039), "diameter_m": 0.008},
)

#: Everything else in the plan is the delivered run's own numbers, so nothing here is invented.
DELIVERED_SIZING = dict(
    background_cell_m=0.0004025248, levels=1, expect_fluid_volume_m3=5.3661786893e-05,
    expect_fluid_pieces=2, volume_tol=0.05, narrowest_m=0.002112337225)

#: What the delivered fluid region actually carries.
DELIVERED_PATCHES = {"fluid_to_solid": 16582198, "mouth_2": 1306, "mouth_3": 1334}


def _plan(**over) -> ConjugatePlan:
    base = dict(DELIVERED_SIZING, port_patches=DELIVERED_MOUTHS, inlet="", outlet="")
    base.update(over)
    return ConjugatePlan(**base)


def _report(fluid_patches: dict[str, int], *, ports=None) -> ConjugateReport:
    iface = fluid_patches.get("fluid_to_solid", 16582198)
    return ConjugateReport(
        fluid=RegionReport(name="fluid", cells=91323244, mesh_ok=True, patches=dict(fluid_patches)),
        solid=RegionReport(name="solid", cells=192087711, mesh_ok=True,
                           patches={"wall_outer": 485526, "solid_to_fluid": iface}),
        interface_faces_fluid=iface, interface_faces_solid=iface, ports=ports)


def _said(rep) -> str:
    return " ".join(rep.problems + rep.notes)


# # the reconciliation must not depend on a port plan existing


def test_the_no_roles_road_is_the_one_a_customer_lands_on():
    """Pinned, because it is the premise of everything below.

    A customer who asks for no ports to be named gets every mouth typed `opening`, and
    `ports_from` hands back None for that - which is correct, there IS no inlet/outlet pair - so
    any check living behind `report.ports` is unreachable on exactly the jobs that matter.
    """
    assert ports_from(_plan()) is None


def test_a_declared_mouth_missing_from_the_mesh_is_recorded():
    rep = _report(DELIVERED_PATCHES)
    verify(rep, port_patches=list(DELIVERED_MOUTHS))
    assert rep.port_patches_dropped == ("mouth_1",), (
        "the mesh has no mouth_1 and the plan beside it calls mouth_1 an opening")
    assert rep.port_patches_planned == ("mouth_1", "mouth_2", "mouth_3")
    assert "mouth_1" in _said(rep), "and the report has to say so in words, not only in a field"


def test_it_says_the_plan_still_names_it():
    """The point a customer needs: the two files in this tarball disagree, and here is why."""
    rep = _report(DELIVERED_PATCHES)
    verify(rep, port_patches=list(DELIVERED_MOUTHS))
    said = _said(rep)
    assert "conjugate_plan.json" in said, (
        "the record must name the file that still claims the opening, or the customer has to "
        "work out for themselves which of the two to believe")


def test_the_mesh_is_not_failed_for_it():
    """The mesh is CORRECT - the lid port is capped, so dropping the patch is right.

    This belongs in notes, not problems: refusing a 283M-cell mesh over a metadata mismatch is
    the opposite of delivering the mesh the customer asked for.
    """
    rep = _report(DELIVERED_PATCHES)
    verify(rep, port_patches=list(DELIVERED_MOUTHS))
    assert not any("mouth_1" in q for q in rep.problems), (
        "a capped mouth the customer told us about is not a defect in the mesh")
    assert any("mouth_1" in n for n in rep.notes)


def test_the_mouths_that_did_arrive_are_reported_with_their_face_counts():
    rep = _report(DELIVERED_PATCHES)
    verify(rep, port_patches=list(DELIVERED_MOUTHS))
    said = _said(rep)
    assert "1,306" in said, "mouth_2's face count is what proves coolant reaches it"
    assert "1,334" in said, "and mouth_3's"


def test_a_solid_patch_of_the_same_name_does_not_mask_a_drop():
    """Only the FLUID region counts, because only it can carry a port patch.

    The chain appends `topoSet -constant -region fluid` and `createPatch -overwrite -region fluid`
    AFTER the split, so a mouth is cut out of the coolant's own skin and never the metal's. A
    reconciliation that took the union of both regions would let a solid patch that happens to
    share the name hide a mouth the coolant never reached - a false negative, which is the one
    direction this check must not fail in.

    `wall_outer` is the real case, not a contrived one: it is the `face` value on all three of the
    delivered plan's mouths, and it exists on the solid region with 485,526 faces.
    """
    rep = _report({"fluid_to_solid": 10})          # solid carries wall_outer; fluid does not
    verify(rep, port_patches=[{"name": "wall_outer", "type": "opening", "face": "x",
                               "centre_m": (0, 0, 0), "diameter_m": 0.01}])
    assert rep.port_patches_dropped == ("wall_outer",), (
        "a mouth absent from the coolant was counted as delivered because the METAL has a patch "
        "of that name")


def test_nothing_declared_means_nothing_claimed():
    rep = _report(DELIVERED_PATCHES)
    verify(rep, port_patches=None)
    assert rep.port_patches_planned == ()
    assert rep.port_patches_dropped == ()
    assert "mouth" not in _said(rep)


def test_every_mouth_arriving_says_so_without_crying_wolf():
    rep = _report({"fluid_to_solid": 10, "mouth_2": 1306, "mouth_3": 1334})
    verify(rep, port_patches=list(DELIVERED_MOUTHS[1:]))
    assert rep.port_patches_dropped == ()
    assert "does NOT carry" not in _said(rep)


# # the two latent bugs underneath it


def test_absent_is_not_reported_as_zero_faces():
    """"not in the mesh" and "in the mesh carrying nothing" are different facts with different
    causes, and `patches.get(name, 0)` collapsed them."""
    mouths = ({**DELIVERED_MOUTHS[0], "type": "inlet"},
              {**DELIVERED_MOUTHS[1], "type": "outlet"},
              DELIVERED_MOUTHS[2])
    plan = _plan(port_patches=mouths)

    absent = _report({"fluid_to_solid": 10, "mouth_1": 7, "mouth_2": 1306}, ports=ports_from(plan))
    verify(absent, port_patches=list(mouths))
    about_absent = " ".join(q for q in absent.problems + absent.notes if "mouth_3" in q)

    empty = _report({"fluid_to_solid": 10, "mouth_1": 7, "mouth_2": 1306, "mouth_3": 0},
                    ports=ports_from(plan))
    verify(empty, port_patches=list(mouths))
    about_empty = " ".join(q for q in empty.problems + empty.notes if "mouth_3" in q)

    assert about_absent and about_empty, "both cases must be reported"
    assert about_absent != about_empty, (
        "a patch the mesh does not have and a patch carrying zero faces produced the same "
        "sentence, so the report cannot tell the customer which happened")
    assert "0 faces" not in about_absent, (
        "a patch that is not in the mesh was described as one the coolant reaches with 0 faces")


def test_a_port_pair_with_only_one_end_named_does_not_invent_a_missing_one():
    """`ports_from` returns a plan as soon as EITHER end is named.

    The other end then arrives as "", and looking that up found no patch called "" and announced
    a missing port on a part that never declared one.
    """
    plan = _plan(port_patches=({**DELIVERED_MOUTHS[0], "type": "inlet"},
                               DELIVERED_MOUTHS[1]))
    rep = _report({"fluid_to_solid": 10, "mouth_1": 7, "mouth_2": 1306}, ports=ports_from(plan))
    verify(rep, port_patches=list(plan.port_patches))
    assert not any("the outlet was to be ," in q or "the outlet was to be  " in q
                   for q in rep.problems), (
        "an outlet nobody named was reported missing: " + " | ".join(rep.problems))


def test_a_genuinely_missing_declared_inlet_is_still_a_problem():
    """The refusal that already works must keep working - it has saved two runs."""
    plan = _plan(port_patches=({**DELIVERED_MOUTHS[0], "type": "inlet"},
                               {**DELIVERED_MOUTHS[1], "type": "outlet"}))
    rep = _report({"fluid_to_solid": 10, "mouth_2": 1306}, ports=ports_from(plan))
    verify(rep, port_patches=list(plan.port_patches))
    assert any("mouth_1" in q for q in rep.problems), (
        "a DECLARED inlet the mesh does not carry is a real defect and must stay a problem")


# # and it reaches the file the customer opens


def test_the_drop_reaches_the_manifest_not_just_the_tarball(tmp_path):
    """conjugate_report.json ships, but only inside a multi-gigabyte bundle.

    The manifest is what the viewer, the verdict and the review read, so a divergence that stops
    at the report is one a human sees only by untarring the deliverable.
    """
    from meshpipeline.engines.cfmesh_conjugate.finalize import quality_of

    q = quality_of(tmp_path, {"regions": {}, "plan": {},
                              "port_patches_planned": ["mouth_1", "mouth_2"],
                              "port_patches_dropped": ["mouth_1"]})
    assert q.get("port_patches_dropped") == ["mouth_1"], (
        "the reconciliation never leaves conjugate_report.json, so nothing the customer looks at "
        "before downloading can show it")
    assert q.get("port_patches_planned") == ["mouth_1", "mouth_2"]


def test_the_report_body_carries_both_lists():
    """`conjugate_report.json` is a bundle member, so this is what actually ships."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import native as N

    src = inspect.getsource(N)
    assert '"port_patches_planned"' in src and '"port_patches_dropped"' in src, (
        "the reconciliation does not reach conjugate_report.json, so it never leaves the worker")
