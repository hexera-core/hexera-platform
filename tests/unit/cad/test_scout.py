# Responsibility: Verify the geometry scout reads what a part is from its CAD alone - a pipe wall,
# the fluid inside a pipe, a body in a flow - and proposes its openings, sizes, a point inside the
# flow and first-guess roles, with every number in metres and every sticker numbered from 1.
# Boundaries: real OpenCASCADE solids written to STEP; no model, no rendering.
from __future__ import annotations

import math

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from pathlib import Path

from meshpipeline.cad.scout import UnreadableCad, scout_cad, write_view_stl
from meshpipeline.cad.unit_evidence import parser_applied_unit
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)


def _prepared(path: Path):
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=1e-3,
        basis=ResolutionBasis.file_declared, evidence="test")
    return from_occ_transfer(interp, parser_applied_unit(path))


def _write(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _cyl(radius: float, length: float, origin=(0, 0, 0), axis=(1, 0, 0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*origin), gp_Dir(*axis)), radius, length).Shape()


def _tube(path: Path, r_out=50.0, r_in=40.0, length=300.0) -> Path:
    """A pipe wall: a cylinder with its bore cut out. Its two ends are RINGS."""
    return _write(BRepAlgoAPI_Cut(_cyl(r_out, length), _cyl(r_in, length)).Shape(), path)


def _fluid(path: Path, r=40.0, length=300.0) -> Path:
    """The fluid inside that pipe: a plain cylinder. Its two ends are DISCS."""
    return _write(_cyl(r, length), path)


def _tee(path: Path) -> Path:
    """A tee's wall: a main run along x and a branch up z, one bore through both."""
    outer = BRepAlgoAPI_Fuse(_cyl(50.0, 300.0), _cyl(30.0, 150.0, (150, 0, 0), (0, 0, 1))).Shape()
    bore = BRepAlgoAPI_Fuse(_cyl(40.0, 300.0), _cyl(22.0, 150.0, (150, 0, 0), (0, 0, 1))).Shape()
    return _write(BRepAlgoAPI_Cut(outer, bore).Shape(), path)


def _scout(path: Path):
    return scout_cad(path, prepared=_prepared(path))


def test_a_pipe_wall_is_read_as_a_wall_with_two_ring_openings(tmp_path):
    r = _scout(_tube(tmp_path / "tube.step"))
    assert (r.body_kind, r.input_kind, r.flow) == ("pipe_wall", "body-surface", "internal")
    assert [o.kind for o in r.openings] == ["ring", "ring"]
    assert [o.role for o in r.openings] == ["inlet", "outlet"]
    # the opening is the BORE, not the ring of metal: 80 mm across, in metres
    for o in r.openings:
        assert o.equivalent_diameter == pytest.approx(0.080, rel=0.01)
        assert o.shape == "circle"
        assert o.clear_ahead and o.on_extremity
    # the inlet is the lower end along the pipe's axis, and both normals point out of the part
    assert r.openings[0].centroid[0] == pytest.approx(0.0, abs=1e-6)
    assert r.openings[0].normal[0] < 0 and r.openings[1].normal[0] > 0
    assert r.size == pytest.approx((0.300, 0.100, 0.100), rel=0.01)


def test_the_fluid_inside_the_pipe_is_read_as_a_fluid_body_with_disc_openings(tmp_path):
    r = _scout(_fluid(tmp_path / "fluid.step"))
    assert (r.body_kind, r.input_kind, r.flow) == ("single_solid", "fluid-domain", "internal")
    assert [o.kind for o in r.openings] == ["disc", "disc"]
    assert [o.equivalent_diameter for o in r.openings] == pytest.approx([0.080, 0.080], rel=0.01)


def test_the_point_inside_the_flow_is_in_the_fluid_and_out_of_the_metal(tmp_path):
    wall = _scout(_tube(tmp_path / "tube.step"))
    fluid = _scout(_fluid(tmp_path / "fluid.step"))
    for r in (wall, fluid):
        assert r.seed_point is not None
        x, y, z = r.seed_point
        assert 0.0 < x < 0.300
        assert math.hypot(y, z) < 0.040          # inside the bore, for both readings of the pipe


def test_a_box_is_a_body_in_a_flow_with_no_openings(tmp_path):
    r = _scout(_write(BRepPrimAPI_MakeBox(100.0, 50.0, 30.0).Shape(), tmp_path / "box.step"))
    assert (r.input_kind, r.flow) == ("solid-body", "external")
    assert r.openings == [] and r.seed_point is None
    assert any("solid body" in n for n in r.notes)


def test_a_tee_has_three_openings_one_of_them_the_feed(tmp_path):
    r = _scout(_tee(tmp_path / "tee.step"))
    assert r.input_kind == "body-surface"
    assert len(r.openings) == 3 and all(o.kind == "ring" for o in r.openings)
    assert [o.role for o in r.openings].count("inlet") == 1
    assert [o.role for o in r.openings].count("outlet") == 2
    # the branch is the small one and never the feed by the area rule
    branch = min(r.openings, key=lambda o: o.area)
    assert branch.role == "outlet" and branch.equivalent_diameter == pytest.approx(0.044, rel=0.02)
    assert branch.normal[2] > 0.99


def test_stickers_are_numbered_from_one_and_the_dict_carries_millimetres(tmp_path):
    d = _scout(_tee(tmp_path / "tee.step")).as_dict()
    assert [o["id"] for o in d["openings"]] == [1, 2, 3]
    assert len({o["name"] for o in d["openings"]}) == 3
    assert all("diameter_mm" in o and "centroid_mm" in o for o in d["openings"])
    assert d["size_mm"] == pytest.approx([300.0, 100.0, 200.0], rel=0.01)   # -50 to +150 in z
    assert 0.0 < d["confidence"]["openings"] <= 1.0


def test_the_view_skin_is_written_in_metres(tmp_path):
    src = _tube(tmp_path / "tube.step")
    stl = write_view_stl(src, tmp_path / "skin.stl", prepared=_prepared(src))
    from meshpipeline.cad.stl_io import read_stl_triangles
    tris = read_stl_triangles(stl)
    assert tris
    xs = [v[0] for t in tris for v in t]
    assert max(xs) == pytest.approx(0.300, rel=0.01)


def test_a_file_that_is_not_cad_is_refused_in_plain_words(tmp_path):
    good = _tube(tmp_path / "tube.step")
    bad = tmp_path / "bad.step"
    bad.write_text("this is not a STEP file")
    with pytest.raises(UnreadableCad, match="could not be read"):
        scout_cad(bad, prepared=_prepared(good))
