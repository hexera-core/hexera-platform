"""A port the customer named and the mesh never got is a refusal, not a silence.

WHY THIS EXISTS. `conjugate.verify` has had the check since the first conjugate solve died in the
preconditioner - "the outlet patch exists on the fluid region with ZERO faces, so no coolant
reaches it" - and the whole section sits behind `if (plan := report.ports) is not None`.
`plan.ports` comes from `plan_ports`, which chooses among the six BLOCK FACES the coolant is flush
with. A real customer part is not a block cut out of a channel array: its ports are MOUTHS, carved
into a face at a centre and a diameter the customer confirmed, and they arrive in
`plan.port_patches`. So on the product's own path `ports_from` returned None and the port section
was never reached.

EVERY NUMBER BELOW IS FROM JOB b6762d5c, 2026-11-13 - the first customer plate to go through the
whole product - read off its own `conjugate_plan.json` and `conjugate_report.json`:

    topoSet   port_outlet   0 faces      the box the customer's outlet was placed in
              port_inlet    1,714
              port_mouth_3  1,743        declared an opening, then declared not a port
              wall_outer    3,457        = 1,714 + 1,743, so the coolant has exactly two mouths
    createPatch   "Removed zero-sized patch outlet"

The mesh shipped with an inlet, a sealed mouth and no outlet, nothing fixing its pressure. Its
report said `"ports": null` and listed three problems, none of them this one.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import plan as PL

#: The run's own plan.port_patches, verbatim. `outlet` first because that is the order the
#: geometry stage proposed them in, and the order must not be what picks the inlet.
PORT_PATCHES = [
    {"name": "outlet", "type": "outlet", "face": "wall_outer", "diameter_m": 0.0118,
     "centre_m": [0.065923414495, 0.033446178113, 0.07540532726]},
    {"name": "inlet", "type": "inlet", "face": "wall_outer", "diameter_m": 0.008,
     "centre_m": [0.099881949608, 0.054681178113, 0.043305688999]},
    {"name": "mouth_3", "type": "wall", "face": "wall_outer", "diameter_m": 0.008,
     "centre_m": [0.089339605589, 0.054681178113, 0.077388253039]},
]

INTERFACE = 21618626


def _plan(**over):
    kw = {"background_cell_m": 0.0003520562041666667, "levels": 1, "cells_across": 12,
          "narrowest_m": 0.002112337225, "expect_fluid_volume_m3": 5.3661786893e-05,
          "expect_fluid_pieces": 2, "volume_tol": 0.05,
          "block_lo": (-0.06157654, 0.02011018, -0.008590401),
          "block_hi": (0.1159235, 0.06194618, 0.0924096)}
    kw.update(over)
    kw["port_patches"] = tuple(dict(p) for p in (over.get("port_patches", PORT_PATCHES)))
    return PL.ConjugatePlan(**kw)


def _report(fluid_patches):
    """The run's two regions, with the fluid's patch list under test."""
    fluid = C.RegionReport(name="fluid", cells=66624895, volume_m3=5.31682e-05, pieces=5,
                           mesh_ok=False, patches=dict(fluid_patches))
    solid = C.RegionReport(name="solid", cells=260006505, volume_m3=0.000223093, pieces=1,
                           mesh_ok=False,
                           patches={"solid_to_fluid": INTERFACE, "wall_outer": 546551})
    return C.ConjugateReport(fluid=fluid, solid=solid, interface_faces_fluid=INTERFACE,
                             interface_faces_solid=INTERFACE, ports=PL.ports_from(_plan()),
                             patch_names={})


def test_the_product_path_now_has_a_port_plan_at_all():
    """The plan the run actually carried: no block faces, three mouths."""
    plan = _plan()
    assert plan.inlet == "" and plan.outlet == ""
    ports = PL.ports_from(plan)
    assert ports is not None, "every port check in verify() is behind this not being None"
    assert (ports.inlet, ports.outlet) == ("inlet", "outlet")
    assert ports.walls == ("mouth_3",)
    assert ports.cut is True


def test_a_sealed_part_is_still_a_sealed_part():
    """No inlet and no outlet is a legal plan - the branching test part has no ports at all - and
    must not start reading as one because a wall-typed mouth was declared."""
    only_walls = [dict(PORT_PATCHES[2])]
    assert PL.ports_from(_plan(port_patches=only_walls)) is None
    assert PL.ports_from(_plan(port_patches=[])) is None


def test_the_outlet_that_never_got_a_face_is_named_as_the_reason():
    got = C.verify(_report({"fluid_to_solid": INTERFACE, "inlet": 1714, "mouth_3": 1743}),
                   expect_fluid_volume_m3=5.3661786893e-05, expect_fluid_pieces=2,
                   narrowest_m=0.002112337225)
    said = " | ".join(got.problems)
    assert "outlet" in said and "no patch of that name" in said, said
    assert "'inlet', 'mouth_3'" in said, "the reader is not told what the mesh does have"
    assert got.flowable is False
    assert got.outlet_faces == 0


def test_a_port_that_survived_the_split_empty_is_caught_the_same_way():
    """createPatch removes a zero-faced patch, but it does not always get the chance - a port
    renamed before the split can arrive with the name and none of the faces."""
    got = C.verify(_report({"fluid_to_solid": INTERFACE, "inlet": 1714, "outlet": 0,
                            "mouth_3": 1743}), expect_fluid_pieces=2)
    said = " | ".join(got.problems)
    assert "ZERO faces" in said and "preconditioner" in said, said


def test_the_sealed_mouth_is_reported_with_the_size_it_carries():
    """1,743 faces against the inlet's 1,714 is what a mis-assigned port looks like from here, so
    the note has to carry the count and not just the name."""
    got = C.verify(_report({"fluid_to_solid": INTERFACE, "inlet": 1714, "mouth_3": 1743}),
                   expect_fluid_pieces=2)
    note = " | ".join(got.notes)
    assert "mouth_3 (1,743 faces)" in note, note
    assert "cut on" not in note, "that is the wording for a block face, not for a carved mouth"


def test_a_working_pair_of_cut_ports_raises_nothing():
    got = C.verify(_report({"fluid_to_solid": INTERFACE, "inlet": 1714, "outlet": 1743}),
                   expect_fluid_pieces=5)
    # "report" contains "port", so the filter has to be the port check's own words.
    assert [p for p in got.problems if "patch" in p or "coolant" in p] == [], got.problems
    assert got.flowable is True
    assert got.inlet_faces == 1714 and got.outlet_faces == 1743


def test_the_block_face_road_still_reaches_the_same_type():
    """The other caller is unchanged: a plan that names block faces keeps its own names, its own
    walls and its own reason, and is NOT marked cut."""
    plan = _plan(inlet="ymin", outlet="ymax", port_walls=("xmin",), port_reason="measured",
                 port_patches=[])
    ports = PL.ports_from(plan)
    assert (ports.inlet, ports.outlet, ports.walls) == ("ymin", "ymax", ("xmin",))
    assert ports.cut is False
    assert ports.reason == "measured"


@pytest.mark.parametrize("missing", ["inlet", "outlet"])
def test_one_named_port_is_enough_to_turn_the_check_on(missing):
    """Half a port plan is the most dangerous shape there is, because the half that is missing is
    the one nothing would have said anything about."""
    cut = [p for p in PORT_PATCHES if p["type"] != missing]
    ports = PL.ports_from(_plan(port_patches=cut))
    assert ports is not None
    assert getattr(ports, missing) == ""
