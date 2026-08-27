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

from pathlib import Path

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.cad.stl_io import read_stl_triangles
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


# #
# PORT-MOUTH SEALING. A hollow part's declared port face is the metal's ANNULAR end
# ring; writing only the ring leaves the bore hole open, the channel connects to the
# exterior void through the mouths, and the snappy carve keeps channel+exterior as ONE
# region (jobs 95bd0197 and 0de57541 delivered meshes with a spurious 'outer' patch).
# The port STL must span the WHOLE opening: ring + a cap over the bore hole.
# #

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


def test_hollow_tube_port_stls_seal_the_full_mouth(tmp_path):
    step = tmp_path / "tube.step"
    _make_hollow_tube(step)                      # r_out=30mm, r_in=20mm → metres below
    out = tessellate_internal(step, tmp_path / "stls", prepared=_prepared())
    full_mouth = math.pi * 0.030 ** 2            # ring + bore cap = the FULL end disc
    ring_only = math.pi * (0.030 ** 2 - 0.020 ** 2)
    for port in ("inlet", "outlet"):
        area = _stl_area(out["stls"][port])
        assert area > ring_only * 1.5, (
            f"{port}.stl covers only the annular ring ({area:.6f} m²) - the bore hole "
            "is open and the channel leaks to the exterior void")
        assert area == pytest.approx(full_mouth, rel=0.02), (
            f"{port}.stl area {area:.6f} m² should approximate the full end cap "
            f"{full_mouth:.6f} m² (annular ring + bore cap)")


def test_solid_rod_port_stls_are_unchanged_single_disc(tmp_path):
    step = tmp_path / "rod.step"
    _make_solid_rod(step)                        # r=25mm
    out = tessellate_internal(step, tmp_path / "stls", prepared=_prepared())
    disc = math.pi * 0.025 ** 2
    for port in ("inlet", "outlet"):
        area = _stl_area(out["stls"][port])
        assert area == pytest.approx(disc, rel=0.02), (
            f"{port}.stl area {area:.6f} m² must stay the single-wire disc "
            f"{disc:.6f} m² - a solid-model port face already spans the opening and "
            "must NOT grow a cap")


def test_hollow_tube_wall_plus_ports_bound_a_closed_region(tmp_path):
    # WATERTIGHTNESS: with the mouths sealed, wall+inlet+outlet triangles leave NO
    # boundary edge (an edge used by exactly one triangle). The cap is built from the
    # ring face's own inner wire, so its rim discretization must match the ring's and
    # the bore wall's - a cap meshed off-grid (a T-junction crack) or a missing cap rim
    # shows up as count-1 edges. The bore-rim circles legitimately carry a count of
    # THREE (ring + cap + bore wall meet there - the ring is a flange on the channel's
    # closed boundary of bore wall + caps), so the assertion is no-count-1, not
    # everywhere-count-2. The caps' presence itself is pinned by the area test above.
    step = tmp_path / "tube.step"
    _make_hollow_tube(step)
    out = tessellate_internal(step, tmp_path / "stls", prepared=_prepared())
    edges: dict = {}
    for name in ("wall", "inlet", "outlet"):
        for tri in read_stl_triangles(Path(out["stls"][name])):
            keys = [tuple(round(c, 9) for c in v) for v in tri]
            for i in range(3):
                e = frozenset((keys[i], keys[(i + 1) % 3]))
                edges[e] = edges.get(e, 0) + 1
    boundary = [e for e, n in edges.items() if n == 1]
    assert not boundary, (
        f"{len(boundary)} boundary edge(s) used by only one triangle - the combined "
        "wall+inlet+outlet surface has a crack or an open mouth and does not bound a "
        "closed region")
    rim_flange = [e for e, n in edges.items() if n == 3]
    assert rim_flange, (
        "no ring+cap+bore-wall flange edges found on the bore rims - the cap did not "
        "land on the ring's inner wire discretization")
