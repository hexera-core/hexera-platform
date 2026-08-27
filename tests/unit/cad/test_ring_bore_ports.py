# A flanged thin-wall duct's declared ports must bind to the duct wall's END RINGS by
# what their inner wires enclose (the bore), never to the flange annuli rimming the same
# mouths - and the flange bores at a bound mouth must NOT be sealed as undeclared
# openings (job 11b50253, duct_circ_bend_red: wall 2 mm, bore 796 mm - the end ring
# measures 5014 mm² of metal around the 497,644 mm² bore the declaration states, so
# area-only matching found nothing and the job died pre-mesh).
import math

import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from pathlib import Path

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.cad.stl_io import read_stl_triangles
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)
from meshpipeline.engines.port_binding import bind_intake, declaration_targets


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


def _cyl(base, direction, r, length):
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, length).Shape()


R_BORE, R_OUT, R_FLANGE, LEN, FLANGE_T = 100.0, 102.0, 130.0, 300.0, 8.0


def _flanged_thin_tube(path):
    """The thin-ring assembly class: a 2 mm wall duct (bore r=100, outer r=102,
    z 0..300) with a loose flange plate hugging each end (annular ring r 102..130,
    8 mm thick). Three solids in one compound, exactly like the corpus assemblies -
    every end face is an annular ring, and at each mouth the flange's bore (204 mm)
    rims the very same hole as the duct's own end ring (200 mm bore)."""
    tube = BRepAlgoAPI_Cut(
        _cyl((0, 0, 0), (0, 0, 1), R_OUT, LEN),
        _cyl((0, 0, -1.0), (0, 0, 1), R_BORE, LEN + 2.0)).Shape()
    fl_a = BRepAlgoAPI_Cut(
        _cyl((0, 0, 0), (0, 0, 1), R_FLANGE, FLANGE_T),
        _cyl((0, 0, -1.0), (0, 0, 1), R_OUT, FLANGE_T + 2.0)).Shape()
    fl_b = BRepAlgoAPI_Cut(
        _cyl((0, 0, LEN - FLANGE_T), (0, 0, 1), R_FLANGE, FLANGE_T),
        _cyl((0, 0, LEN - FLANGE_T - 1.0), (0, 0, 1), R_OUT, FLANGE_T + 2.0)).Shape()
    comp = TopoDS_Compound(); bld = BRep_Builder(); bld.MakeCompound(comp)
    for s in (tube, fl_a, fl_b):
        bld.Add(comp, s)
    _write_step(comp, path)


INTAKE = [
    {"name": "duct_wall", "type": "wall"},
    {"name": "feed", "type": "inlet", "diameter_mm": 200.0, "near_mm": [0, 0, 0]},
    {"name": "drain", "type": "outlet", "diameter_mm": 200.0, "near_mm": [0, 0, 300]},
]


def _stl_area(path) -> float:
    total = 0.0
    for a, b, c in read_stl_triangles(Path(path)):
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        cx = u[1] * v[2] - u[2] * v[1]
        cy = u[2] * v[0] - u[0] * v[2]
        cz = u[0] * v[1] - u[1] * v[0]
        total += 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)
    return total


@pytest.fixture(scope="module")
def bound(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("flanged")
    step = tmp / "flanged_tube.step"
    _flanged_thin_tube(step)
    t = tessellate_internal(step, tmp / "stls", prepared=_prepared(),
                            declared_ports=declaration_targets(INTAKE))
    out, wall_key, note = bind_intake(t, INTAKE)
    return t, out, wall_key, note


def test_the_declared_bore_binds_the_duct_end_rings_not_the_flange_annuli(bound):
    t, out, wall_key, _note = bound
    assert wall_key == "duct_wall"
    assert set(out["openings"]) == {"feed", "drain"}
    ring_metal = math.pi * ((R_OUT / 1000.0) ** 2 - (R_BORE / 1000.0) ** 2)
    flange_metal = math.pi * ((R_FLANGE / 1000.0) ** 2 - (R_OUT / 1000.0) ** 2)
    for rec in out["openings"].values():
        # the bound face is the duct wall's thin END RING (its metal measures ~1269 mm²),
        # never the flange's ~20,400 mm² annulus around the same mouth
        assert rec["area"] == pytest.approx(ring_metal, rel=0.05), rec
        assert abs(rec["area"] - flange_metal) > flange_metal * 0.5
        bore = math.pi * (R_BORE / 1000.0) ** 2
        assert rec["opening"]["area"] == pytest.approx(bore, rel=0.02)
    assert out["openings"]["feed"]["centroid"][2] == pytest.approx(0.0, abs=1e-4)
    assert out["openings"]["drain"]["centroid"][2] == pytest.approx(LEN / 1000.0, abs=1e-4)


def test_the_flange_bores_at_a_mouth_are_not_sealed_as_undeclared_openings(bound):
    # each flange's 204 mm bore reads as "nothing fills it + sees the exterior" - it IS
    # the port's own mouth seen through the flange stack. Sealing it would cap the
    # declared opening into the wall: flow shut at its own inlet.
    t, _out, _wall_key, _note = bound
    assert t["sealed"]["undeclared_openings"] == []


def test_the_port_stls_span_the_full_mouth(bound):
    t, out, _wall_key, _note = bound
    full_mouth = math.pi * (R_OUT / 1000.0) ** 2      # end ring + bore cap
    for name in ("feed", "drain"):
        assert _stl_area(out["stls"][name]) == pytest.approx(full_mouth, rel=0.02)


def test_the_binding_evidence_carries_the_opening_the_declaration_matched(bound):
    _t, out, _wall_key, note = bound
    for row in out["binding"]["ports"]:
        assert row["opening_area_m2"] == pytest.approx(
            math.pi * (R_BORE / 1000.0) ** 2, rel=0.02)
    assert "ring face; opening" in note


def test_a_genuine_undeclared_hole_is_still_sealed_despite_nearby_ports(tmp_path):
    # the mouth guard must not swallow real leaks: a plain hole drilled through the duct
    # wall mid-span (its rims live on the curved faces, normals perpendicular to the
    # ports') is still detected and sealed into the wall
    step = tmp_path / "flanged_holed.step"
    tube = BRepAlgoAPI_Cut(
        _cyl((0, 0, 0), (0, 0, 1), R_OUT, LEN),
        _cyl((0, 0, -1.0), (0, 0, 1), R_BORE, LEN + 2.0)).Shape()
    drilled = BRepAlgoAPI_Cut(
        tube, _cyl((0, R_BORE - 5.0, 150.0), (0, 1, 0), 6.0, 20.0)).Shape()
    _write_step(drilled, step)
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(),
                            declared_ports=declaration_targets(INTAKE))
    sealed = t["sealed"]["undeclared_openings"]
    hole = math.pi * 0.006 ** 2
    assert len(sealed) == 2, sealed          # outer-surface gap + bore-surface gap
    assert all(s["area"] == pytest.approx(hole, rel=0.3) for s in sealed), sealed
