# The interior-point finder assumed the input solid IS the fluid (a duct modeled as a solid
# rod). A real machined part is METAL with a channel through it - the channel is not inside
# the solid, every rod-semantics candidate was rejected, and the rocket nozzle died in prep.
# The hollow-wall fallback nudges each port centroid toward the other port (down the channel
# by construction) and accepts not-in-metal points. These tests pin both semantics.
import math

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_IN
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)


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


def _make_hollow_tube(path, length=200.0, r_out=30.0, r_in=20.0):
    axis = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    outer = BRepPrimAPI_MakeCylinder(axis, r_out, length).Shape()
    axis2 = gp_Ax2(gp_Pnt(0, 0, -1.0), gp_Dir(0, 0, 1))
    inner = BRepPrimAPI_MakeCylinder(axis2, r_in, length + 2.0).Shape()
    tube = BRepAlgoAPI_Cut(outer, inner).Shape()
    _write_step(tube, path)
    return tube


def _make_solid_rod(path, length=200.0, r=25.0):
    axis = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    rod = BRepPrimAPI_MakeCylinder(axis, r, length).Shape()
    _write_step(rod, path)
    return rod


def _classify(shape, p):
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    e = TopExp_Explorer(shape, TopAbs_SOLID)
    solid = TopoDS.Solid_s(e.Current())
    cls = BRepClass3d_SolidClassifier(solid)
    cls.Perform(gp_Pnt(*p), 1e-9)
    return cls.State()


def test_hollow_tube_gets_a_channel_point_not_a_metal_point(tmp_path):
    step = tmp_path / "tube.step"
    tube = _make_hollow_tube(step)
    out = tessellate_internal(step, tmp_path / "stls", prepared=_prepared())
    p = out["interior_point"]
    # the found point must be in the CHANNEL: near the axis, inside the bore radius, and
    # NOT inside the metal (the tessellation ran in metres - the STEP mm scale over 1000)
    r_xy = math.hypot(p[0], p[1])
    assert r_xy < 20.0 / 1000.0, f"point {p} is not inside the bore"
    assert _classify(tube, [v * 1000.0 for v in p]) != TopAbs_IN, (
        "the interior point sits inside the metal wall")


def test_solid_rod_keeps_the_old_inside_the_solid_semantics(tmp_path):
    step = tmp_path / "rod.step"
    rod = _make_solid_rod(step)
    out = tessellate_internal(step, tmp_path / "stls", prepared=_prepared())
    p = out["interior_point"]
    assert _classify(rod, [v * 1000.0 for v in p]) == TopAbs_IN, (
        "a fluid-volume (solid rod) input must still yield an inside-the-solid point")
