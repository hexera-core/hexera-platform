# Responsibility: Verify an annular opening - a bore with a centre rod or hub standing in it - is proposed, confirmed and declared as the ring the flow crosses, not the whole bore.
# The e2e corpus sweep (2026-10-17) failed every annular trainshape at the port gate: the scout
# carried only the bore, the declaration a full disc, and the gate held a 5,700 mm2 annulus to
# annular_001's 17,900 mm2 bore (0.32 times).
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.cad.cad_tessellate import stands_in_opening

R_OUT, R_BORE, R_ROD, L = 50.0, 40.0, 30.0, 300.0           # mm
BORE_MM2 = math.pi * R_BORE ** 2
ROD_MM2 = math.pi * R_ROD ** 2
RING_MM2 = BORE_MM2 - ROD_MM2


# the one reading of what stands in an opening

def _hole(r=0.04):
    return {"area_m2": math.pi * r * r, "centroid": (0.0, 0.0, 0.0), "wh_m": (2 * r, 2 * r)}


def test_a_rod_end_in_the_bore_plane_stands_in_the_opening():
    assert stands_in_opening(_hole(), (1, 0, 0), math.pi * 0.03 ** 2, (0.0, 0.0, 0.0), (0.06, 0.06))


def test_a_flange_band_round_the_same_hole_does_not():
    # centred on the hole and in its plane, smaller in area than the bore - but wider than it
    band = math.pi * (0.07 ** 2 - 0.06 ** 2)
    assert band < _hole()["area_m2"]
    assert not stands_in_opening(_hole(), (1, 0, 0), band, (0.0, 0.0, 0.0), (0.14, 0.14))


def test_a_face_out_of_the_plane_or_off_the_hole_does_not():
    rod = math.pi * 0.03 ** 2
    assert not stands_in_opening(_hole(), (1, 0, 0), rod, (0.01, 0.0, 0.0), (0.06, 0.06))
    assert not stands_in_opening(_hole(), (1, 0, 0), rod, (0.0, 0.05, 0.0), (0.06, 0.06))
    assert not stands_in_opening(_hole(), (1, 0, 0), 2 * _hole()["area_m2"], (0, 0, 0), None)


# the CAD scout

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound
    HAVE_OCP = True
except Exception:  # noqa: BLE001
    HAVE_OCP = False

needs_ocp = pytest.mark.skipif(not HAVE_OCP, reason="OCP not available")


def _cyl(r, length, origin=(0, 0, 0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*origin), gp_Dir(1, 0, 0)), r, length).Shape()


def _annulus_shape(rod: bool = True):
    """annular_001's make-up: a tube and, as a solid of its own, the rod down its middle, their
    ends flush."""
    tube = BRepAlgoAPI_Cut(_cyl(R_OUT, L), _cyl(R_BORE, L)).Shape()
    if not rod:
        return tube
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    b.Add(comp, tube)
    b.Add(comp, _cyl(R_ROD, L))
    return comp


def _step(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _scout(path: Path):
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t", unit=LengthUnit.millimetre,
        scale_to_metres=1e-3, basis=ResolutionBasis.file_declared, evidence="test")
    return scout_cad(path, prepared=from_occ_transfer(interp, parser_applied_unit(path)))


@needs_ocp
def test_the_scout_carries_the_rod_in_an_annular_mouth(tmp_path):
    r = _scout(_step(_annulus_shape(), tmp_path / "annulus.step"))
    assert r.body_kind == "pipe_wall" and [o.kind for o in r.openings] == ["ring", "ring"]
    for o in r.openings:
        d = o.as_dict()
        # the bore stays the size the pipe is known by; the rod and the ring are carried beside it
        assert d["diameter_mm"] == pytest.approx(2 * R_BORE, rel=0.01)
        assert d["inner_diameter_mm"] == pytest.approx(2 * R_ROD, rel=0.01)
        assert d["flow_area_mm2"] == pytest.approx(RING_MM2, rel=0.01)
        # ... and they agree exactly: pi/4 (D^2 - d^2) is the measured ring
        ring = math.pi / 4 * (d["diameter_mm"] ** 2 - d["inner_diameter_mm"] ** 2)
        assert ring == pytest.approx(d["flow_area_mm2"], rel=1e-3)


@needs_ocp
def test_a_plain_bore_carries_no_centre_body(tmp_path):
    r = _scout(_step(_annulus_shape(rod=False), tmp_path / "tube.step"))
    for o in r.openings:
        d = o.as_dict()
        assert "inner_diameter_mm" not in d and "flow_area_mm2" not in d


@needs_ocp
def test_the_staging_reads_the_same_ring_as_the_scout(tmp_path):
    from meshpipeline.application.geometry_confirmation import patches_from
    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    from meshpipeline.engines.port_binding import bind_intake, declaration_targets
    from meshpipeline.engines.region_check import expected_port_areas, record_port_openings
    step = _step(_annulus_shape(), tmp_path / "annulus.step")
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t", unit=LengthUnit.millimetre,
        scale_to_metres=1e-3, basis=ResolutionBasis.file_declared, evidence="test")
    # the declaration the scout's proposal makes, through the staging and binding production runs
    r = _scout(step)
    body = SimpleNamespace(flow="internal", input_kind="body-surface",
                           openings=[SimpleNamespace(**{**o.as_dict(), "width_mm": None,
                                                        "height_mm": None}) for o in r.openings])
    patches = patches_from(body)
    t = tessellate_internal(step, tmp_path / "stls", prepared=from_occ_transfer(interp, LengthUnit.millimetre),
                            fluid_solid=False, declared_ports=declaration_targets(patches))
    t, _wall, _note = bind_intake(t, patches, bore=True)
    measured = record_port_openings(tmp_path, t["openings"], bore=True, intake_patches=patches)
    # the opening the port gate holds the mesh to is the ring the staging measured, and the ring
    # the user confirmed is the same size
    expected = expected_port_areas(patches, measured)
    for name in ("inlet", "outlet"):
        area, source = expected[name]
        assert source == "measured" and area * 1e6 == pytest.approx(RING_MM2, rel=0.01)


# the triangle scout (STL, OBJ, ... and the stage's "Add an opening")

def _occ_tris(shape, deflection=0.2):
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, deflection, False, 0.2, True)
    tris = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            pts = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                if face.Orientation() == TopAbs_REVERSED:
                    b, c = c, b
                tris.append([[pts[k - 1].X(), pts[k - 1].Y(), pts[k - 1].Z()] for k in (a, b, c)])
        ex.Next()
    return np.asarray(tris, dtype=float)


@needs_ocp
def test_a_triangle_files_annular_mouth_carries_its_rod():
    from meshpipeline.cad.open_ends import find_holes
    holes = find_holes(_occ_tris(_annulus_shape()))
    ends = [h for h in holes if abs(abs(h.normal[0]) - 1.0) < 1e-3]
    assert len(ends) == 2
    for h in ends:
        d = h.as_dict(mm_per_unit=1.0)
        assert d["diameter_mm"] == pytest.approx(2 * R_BORE, rel=0.01)
        assert d["inner_diameter_mm"] == pytest.approx(2 * R_ROD, rel=0.01)
        assert d["flow_area_mm2"] == pytest.approx(RING_MM2, rel=0.02)


@needs_ocp
def test_a_triangle_files_plain_bore_carries_no_centre_body():
    from meshpipeline.cad.open_ends import find_holes
    for h in find_holes(_occ_tris(_annulus_shape(rod=False))):
        assert h.filled == 0.0 and "inner_diameter_mm" not in h.as_dict()


# the confirmation: what the user confirms is declared as the ring

def _body(**opening):
    o = {"id": 1, "name": "inlet", "role": "inlet", "centroid_mm": [0.0, 0.0, 0.0],
         "diameter_mm": 2 * R_BORE, "width_mm": None, "height_mm": None,
         "inner_diameter_mm": None, "flow_area_mm2": None}
    o.update(opening)
    return SimpleNamespace(flow="internal", input_kind="body-surface",
                           openings=[SimpleNamespace(**o)])


def test_an_annulus_is_declared_as_the_bore_and_its_centre_body():
    from meshpipeline.application.geometry_confirmation import patches_from
    from meshpipeline.engines.region_check import declared_port_area_m2
    p = patches_from(_body(inner_diameter_mm=2 * R_ROD, flow_area_mm2=RING_MM2))[0]
    assert p["diameter_mm"] == 2 * R_BORE and p["inner_diameter_mm"] == 2 * R_ROD
    # one size form: the validator refuses a diameter and an area together
    assert "area_mm2" not in p
    assert declared_port_area_m2(p) * 1e6 == pytest.approx(RING_MM2, rel=1e-9)


def test_a_bore_retyped_below_its_centre_body_is_a_plain_bore():
    from meshpipeline.application.geometry_confirmation import patches_from
    p = patches_from(_body(diameter_mm=50.0, inner_diameter_mm=2 * R_ROD))[0]
    assert p == {"name": "inlet", "type": "inlet", "diameter_mm": 50.0, "near_mm": [0.0, 0.0, 0.0]}


def test_a_rectangular_ring_round_a_body_states_its_flow_area():
    from meshpipeline.application.geometry_confirmation import patches_from
    p = patches_from(_body(diameter_mm=None, width_mm=300.0, height_mm=400.0,
                           flow_area_mm2=90_000.0))[0]
    assert p["area_mm2"] == 90_000.0 and "width_mm" not in p and "diameter_mm" not in p


def test_the_annulus_passes_the_intake_validation_and_the_binding_reads_it():
    from meshpipeline.agents.intake.validation import _validate_internal_ports
    from meshpipeline.application.geometry_confirmation import patches_from
    from meshpipeline.engines.port_binding import DeclaredPatch
    patches = patches_from(_body(inner_diameter_mm=2 * R_ROD))
    assert _validate_internal_ports(patches) == []
    dp = DeclaredPatch.from_intake(patches[0])
    assert dp.declared_area_m2() * 1e6 == pytest.approx(RING_MM2, rel=1e-9)


def test_the_words_say_it_is_an_annulus():
    from meshpipeline.application.geometry_confirmation import body_of, confirmation_message
    words = confirmation_message(body_of({
        "flow": "internal", "input_kind": "body-surface",
        "openings": [{"id": 1, "name": "inlet", "role": "inlet", "diameter_mm": 2 * R_BORE,
                      "inner_diameter_mm": 2 * R_ROD, "flow_area_mm2": RING_MM2}]}))
    assert "an annulus 80 mm across round a 60 mm centre body" in words
    assert "2,199 mm2 of flow" in words


def test_the_route_takes_the_centre_body_and_rereads_it_in_a_new_unit():
    from meshpipeline.api.v1.geometry import ConfirmedOpening, ConfirmIn, in_confirmed_unit
    o = ConfirmedOpening(id=1, name="inlet", role="inlet", diameter_mm=80.0,
                         inner_diameter_mm=60.0, flow_area_mm2=RING_MM2)
    assert ConfirmedOpening(id=1, name="x", role="inlet", inner_diameter_mm=-1.0,
                            flow_area_mm2=float("nan")).inner_diameter_mm is None
    body = ConfirmIn(input_kind="body-surface", flow="internal", openings=[o], scale_to_m=1e-3)
    # drawn as millimetres, confirmed as centimetres: every length x10, the area x100
    again = in_confirmed_unit(body, 1e-2).openings[0]
    assert again.inner_diameter_mm == pytest.approx(600.0)
    assert again.flow_area_mm2 == pytest.approx(RING_MM2 * 100.0)


def test_a_sizeless_record_drops_the_centre_body_with_the_other_lengths():
    from meshpipeline.application.geometry_confirmation import sizeless_record
    record = {"flow": "internal", "input_kind": "body-surface",
              "openings": [{"id": 1, "name": "inlet", "role": "inlet", "diameter_mm": 80.0,
                            "inner_diameter_mm": 60.0, "flow_area_mm2": RING_MM2}]}
    out = sizeless_record(record, scale_to_metres=1e-2, unit="cm")
    assert "inner_diameter_mm" not in out["openings"][0] and "flow_area_mm2" not in out["openings"][0]
