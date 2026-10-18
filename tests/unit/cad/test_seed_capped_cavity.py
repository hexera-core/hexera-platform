# Responsibility: Verify the internal carve seeds a hollow wall (a body whose ports are rings) in the
# cavity the wall closes with its port caps - never in the exterior void and never in the metal.
# bend_elbow_021 (a flanged 162.64-degree reducing elbow, declared a body) was seeded 0.5 mm
# upstream of its own inlet cap: the old search stepped from the inlet toward the outlet, a line
# that runs almost along the inlet plane on a bend that turns back on itself, and accepted the
# first point not in the metal. snappyHexMesh kept the exterior void (an 'outer' patch of 8579
# faces) and the run failed at finalize.
import math

import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.GC import GC_MakeArcOfCircle, GC_MakeSegment
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_IN, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.cad_tessellate import CarveRays, tessellate_internal
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)
from meshpipeline.engines.port_binding import declaration_targets

# mm: a flanged U-bend that turns 170 degrees, so the outlet mouth sits beside and slightly
# BEHIND the inlet mouth - the line from inlet to outlet leaves the inlet almost along its plane
R_IN, R_OUT, R_FLANGE, T_FLANGE = 15.0, 18.0, 30.0, 8.0
L1, R_BEND, TURN_DEG, L2 = 20.0, 45.0, 170.0, 40.0


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _write_step(shape, path):
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    assert w.Write(str(path)) is not None
    return path


def _bend_points(extend=0.0):
    """Spine of the bend: inlet mouth, bend start, bend end, outlet mouth, and the outlet's
    flow direction. `extend` pushes both mouths outward along the spine (for the bore cut)."""
    a = math.radians(TURN_DEG)
    start = (-extend, 0.0, 0.0)
    b0 = (L1, 0.0, 0.0)
    b1 = (L1 + R_BEND * math.sin(a), R_BEND - R_BEND * math.cos(a), 0.0)
    out_dir = (math.cos(a), math.sin(a), 0.0)
    end = tuple(b1[k] + (L2 + extend) * out_dir[k] for k in range(3))
    return start, b0, b1, end, out_dir


def _swept_disc(radius, extend=0.0):
    start, b0, b1, end, _ = _bend_points(extend)
    seg1 = BRepBuilderAPI_MakeEdge(GC_MakeSegment(gp_Pnt(*start), gp_Pnt(*b0)).Value()).Edge()
    arc = BRepBuilderAPI_MakeEdge(
        GC_MakeArcOfCircle(gp_Pnt(*b0), gp_Vec(1, 0, 0), gp_Pnt(*b1)).Value()).Edge()
    seg2 = BRepBuilderAPI_MakeEdge(GC_MakeSegment(gp_Pnt(*b1), gp_Pnt(*end)).Value()).Edge()
    spine = BRepBuilderAPI_MakeWire(seg1, arc, seg2).Wire()
    circle = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(*start), gp_Dir(1, 0, 0)), radius)).Edge()
    disc = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(circle).Wire()).Face()
    return BRepOffsetAPI_MakePipe(spine, disc).Shape()


def _flanged_bend(path):
    """The WALL of a flanged U-bend - metal only, as a machined part is modelled. Each end face is
    a flange ring whose inner wire is the bore: the declared port faces."""
    _, _, _, end, out_dir = _bend_points()
    flange_in = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), R_FLANGE, T_FLANGE).Shape()
    back = tuple(end[k] - T_FLANGE * out_dir[k] for k in range(3))
    flange_out = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*back), gp_Dir(*out_dir)), R_FLANGE, T_FLANGE).Shape()
    body = BRepAlgoAPI_Fuse(BRepAlgoAPI_Fuse(_swept_disc(R_OUT), flange_in).Shape(),
                            flange_out).Shape()
    wall = BRepAlgoAPI_Cut(body, _swept_disc(R_IN, extend=1.0)).Shape()
    return _write_step(wall, path), wall


def _state(shape, p_mm):
    e = TopExp_Explorer(shape, TopAbs_SOLID)
    cls = BRepClass3d_SolidClassifier(TopoDS.Solid_s(e.Current()))
    cls.Perform(gp_Pnt(*p_mm), 1e-7)
    return cls.State()


def _declare(inlet_mm, outlet_mm, d_mm):
    return declaration_targets([
        {"name": "inlet", "type": "inlet", "near_mm": list(inlet_mm), "diameter_mm": d_mm},
        {"name": "outlet", "type": "outlet", "near_mm": list(outlet_mm), "diameter_mm": d_mm},
        {"name": "wall", "type": "wall"}])


def test_a_flanged_bend_that_turns_back_is_seeded_in_its_bore_not_before_its_inlet(tmp_path):
    step, wall = _flanged_bend(tmp_path / "bend.step")
    _, _, _, end, _ = _bend_points()
    assert end[0] < 0.0, "the outlet mouth must sit behind the inlet plane for this to bite"
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declare((0, 0, 0), end, 2 * R_IN))
    p_mm = [v * 1000.0 for v in t["interior_point"]]
    # the flow region is exactly the bore swept along the bend, between the two port caps
    assert _state(_swept_disc(R_IN), p_mm) == TopAbs_IN, (
        f"seed {p_mm} mm is not in the bend's bore - a carve seeded there keeps the exterior")
    assert _state(wall, p_mm) != TopAbs_IN, f"seed {p_mm} mm is in the metal"


def test_a_thick_walled_body_is_never_seeded_in_its_metal(tmp_path):
    # A machined block with an off-centre bore: its volume centroid sits deep in the METAL. The old
    # search tried rod-semantics points first even for a body and took that one - a carve seeded
    # there meshes the block, whole, with every declared patch still present.
    block = BRepPrimAPI_MakeBox(gp_Pnt(0, -50, -50), 100.0, 100.0, 100.0).Shape()
    bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(-1, 30, 0), gp_Dir(1, 0, 0)), 8.0, 102.0).Shape()
    wall = BRepAlgoAPI_Cut(block, bore).Shape()
    step = _write_step(wall, tmp_path / "block.step")
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declare((0, 30, 0), (100, 30, 0), 16.0))
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    assert _state(wall, (x, y, z)) != TopAbs_IN, f"seed ({x}, {y}, {z}) mm is in the metal"
    assert math.hypot(y - 30.0, z) < 8.0 and 0.0 < x < 100.0, (
        f"seed ({x}, {y}, {z}) mm is not in the bore")


def test_a_casing_with_a_hub_on_its_axis_is_seeded_in_the_annulus(tmp_path):
    # A casing whose hub is strutted to it, all one body: the hub fills the mouth's axis and more
    # than half its radius, so every on-axis and half-radius point is metal. The flow is the
    # annulus between hub and casing.
    casing = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 50.0, 100.0).Shape(),
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-1, 0, 0), gp_Dir(1, 0, 0)), 40.0, 102.0).Shape(),
    ).Shape()
    hub = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(5, 0, 0), gp_Dir(1, 0, 0)), 30.0, 90.0).Shape()
    strut = BRepPrimAPI_MakeBox(gp_Pnt(45, 25, -3), 10.0, 20.0, 6.0).Shape()
    wall = BRepAlgoAPI_Fuse(BRepAlgoAPI_Fuse(casing, hub).Shape(), strut).Shape()
    step = _write_step(wall, tmp_path / "casing.step")
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declare((0, 0, 0), (100, 0, 0), 80.0))
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    assert _state(wall, (x, y, z)) != TopAbs_IN, f"seed ({x}, {y}, {z}) mm is in the metal"
    assert 30.0 < math.hypot(y, z) < 40.0 and 0.0 < x < 100.0, (
        f"seed ({x}, {y}, {z}) mm is not in the annulus between hub and casing")


def test_a_centre_rod_modelled_as_its_own_solid_is_metal_too(tmp_path):
    # annular_004: a casing and a centre rod as TWO solids in one file. The rod fills 0.85 of the
    # bore, so the only flow is a thin annulus - and the rod is closed in by the staged surface
    # just as the annulus is, so only a metal test over every solid keeps the seed out of it.
    casing = BRepAlgoAPI_Cut(
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 50.0, 100.0).Shape(),
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-1, 0, 0), gp_Dir(1, 0, 0)), 40.0, 102.0).Shape(),
    ).Shape()
    rod = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(5, 0, 0), gp_Dir(1, 0, 0)), 34.0, 90.0).Shape()
    both = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(both)
    builder.Add(both, casing)
    builder.Add(both, rod)
    step = _write_step(both, tmp_path / "casing_rod.step")
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(), fluid_solid=False,
                            declared_ports=_declare((0, 0, 0), (100, 0, 0), 80.0))
    x, y, z = (v * 1000.0 for v in t["interior_point"])
    assert _state(casing, (x, y, z)) != TopAbs_IN and _state(rod, (x, y, z)) != TopAbs_IN, (
        f"seed ({x}, {y}, {z}) mm is in the metal")
    assert 34.0 < math.hypot(y, z) < 40.0 and 0.0 < x < 100.0, (
        f"seed ({x}, {y}, {z}) mm is not in the annulus between rod and casing")


def _cube(lo, hi):
    v = [(x, y, z) for x in (lo, hi) for y in (lo, hi) for z in (lo, hi)]
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return [t for a, b, c, d in quads for t in ((v[a], v[b], v[c]), (v[a], v[c], v[d]))]


def test_a_closed_surface_stops_every_ray_from_inside_and_not_from_outside():
    rays = CarveRays(_cube(0.0, 1.0))
    inside = rays.enclosed_clearance((0.5, 0.5, 0.5))
    assert inside is not None and inside == pytest.approx(0.5, rel=0.1)
    assert rays.enclosed_clearance((1.5, 0.5, 0.5)) is None
    # an open box (one face missing) closes nothing: the rays out through the gap escape
    assert CarveRays(_cube(0.0, 1.0)[2:]).enclosed_clearance((0.5, 0.5, 0.5)) is None
    # from a point on a face, straight in crosses nothing; straight through crosses the far face
    assert rays.clear_between((0.5, 0.5, 0.0), (0.5, 0.5, 0.5))
    assert not rays.clear_between((0.5, 0.5, 0.0), (0.5, 0.5, 1.5))
