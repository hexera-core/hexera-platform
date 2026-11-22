# Responsibility: Verify INS-26 on built parts: the port STL the B-rep staging writes is the confirmed mouth, nothing
# more and nothing less - a tube end's metal ring is wall and the bore's cap is the port; a flange's bolt holes are
# sealed into the wall, not into the port; a counterbored port is capped at the counterbore's top, its floor and
# bore are the neck; a mouth on no flat face (a hole in a curved wall, a rounded lip beyond a step) sends the part
# to the surface staging instead of capping a flat face behind it; a solid rod's disc ports are unchanged.
# Boundaries: real OpenCASCADE solids written to STEP; the B-rep internal staging (cad_tessellate); no mesher.
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeRevol
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_scout import _prepared

from meshpipeline.cad.cad_tessellate import FlatFacesMissPort, tessellate_internal
from meshpipeline.cad.stl_io import read_stl_triangles
from meshpipeline.engines.port_binding import bind_intake, declaration_targets
from meshpipeline.engines.region_check import flow_area_m2


def _write(shape, path: Path) -> Path:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _cyl(r, z0, h, x=0.0, y=0.0):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), r, h).Shape()


def _tris(path) -> np.ndarray:
    return np.asarray(read_stl_triangles(Path(path)), dtype=float).reshape(-1, 3, 3) * 1000.0   # mm


def _area(t: np.ndarray) -> float:
    return float(0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1).sum())


def _open_edges(paths) -> int:
    t = np.concatenate([_tris(p) for p in paths])
    v = np.round(t.reshape(-1, 3) / 1e-6).astype(np.int64)
    _, inv = np.unique(v, axis=0, return_inverse=True)
    f = np.asarray(inv).reshape(-1, 3)
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return int((cnt == 1).sum())


def _ports(*rows):
    names = ["inlet"] + ([f"outlet_{i}" for i in range(1, len(rows))] if len(rows) > 2 else ["outlet"])
    out = [{"name": n, "type": "inlet" if n == "inlet" else "outlet", "near_mm": list(near), "diameter_mm": d}
           for n, (near, d) in zip(names, rows)]
    return out + [{"name": "wall", "type": "wall"}]


def _stage(path, ports, tmp, fluid_solid=False):
    return tessellate_internal(path, tmp / "staged", prepared=_prepared(path), fluid_solid=fluid_solid,
                               declared_ports=declaration_targets(ports))


def test_a_tube_end_ring_is_wall_and_the_bore_cap_is_the_port(tmp_path):
    """A 60 mm tube with a 40 mm bore, declared a body with 40 mm ports at its ends: each port STL is the bore's
    40 mm cap (1,257 mm2) - old code wrote the 1,571 mm2 end ring beside it (2,827 mm2) - the ring is in the wall,
    the staged surface is closed, and the record's flow area is the cap's."""
    tube = BRepAlgoAPI_Cut(_cyl(30.0, 0.0, 200.0), _cyl(20.0, -1.0, 202.0)).Shape()
    ports = _ports(((0, 0, 0), 40.0), ((0, 0, 200), 40.0))
    t = _stage(_write(tube, tmp_path / "tube.step"), ports, tmp_path)
    bore = math.pi * 20.0 ** 2
    for name in ("inlet", "outlet"):
        assert _area(_tris(t["stls"][name])) == pytest.approx(bore, rel=0.02), name
        assert float(t["openings"][name]["flow_area"]) * 1e6 == pytest.approx(bore, rel=0.02)
    wall = _tris(t["stls"]["wall"])
    ends = wall[np.all(np.abs(wall[:, :, 2]) < 1e-3, axis=1) | np.all(np.abs(wall[:, :, 2] - 200.0) < 1e-3, axis=1)]
    assert _area(ends) == pytest.approx(2.0 * math.pi * (30.0 ** 2 - 20.0 ** 2), rel=0.02)
    assert _open_edges(t["stls"].values()) == 0
    bound, _w, _n = bind_intake(t, ports, bore=True)
    # the gate's expected size is the mouth staged
    assert flow_area_m2(bound["openings"]["inlet"], bore=True, typed=bore * 1e-6) * 1e6 == pytest.approx(bore, rel=0.02)


def test_a_flange_bolt_holes_are_sealed_into_the_wall_not_the_port(tmp_path):
    """A tube with a 100 mm flange at its inlet end, four 6 mm bolt holes through the flange: the inlet STL is the
    40 mm bore's cap alone; the bolt holes are sealed into the wall (old code capped all four into the port)."""
    body = BRepAlgoAPI_Fuse(_cyl(30.0, 0.0, 150.0), _cyl(50.0, 0.0, 10.0)).Shape()
    body = BRepAlgoAPI_Cut(body, _cyl(20.0, -1.0, 152.0)).Shape()
    for a in range(4):
        body = BRepAlgoAPI_Cut(body, _cyl(3.0, -1.0, 12.0, x=40.0 * math.cos(a * math.pi / 2),
                                          y=40.0 * math.sin(a * math.pi / 2))).Shape()
    ports = _ports(((0, 0, 0), 40.0), ((0, 0, 150), 40.0))
    t = _stage(_write(body, tmp_path / "flange.step"), ports, tmp_path)
    assert _area(_tris(t["stls"]["inlet"])) == pytest.approx(math.pi * 20.0 ** 2, rel=0.02)
    bolts = [s for s in t["sealed"]["undeclared_openings"] if abs(s["area"] * 1e6 - math.pi * 9.0) < 2.0]
    assert len(bolts) >= 4, t["sealed"]["undeclared_openings"]
    assert _open_edges(t["stls"].values()) == 0


def _counterbored_block(path: Path) -> Path:
    # a 40 x 30 x 20 mm block with a 10 mm blind pocket in the middle of its top; two 3 mm bores through it at
    # x = -12 and 12, each counterbored 5.9 mm from the top (z = 20) down to z = 14, each leaving at the bottom
    # through a 6 mm boss (z = -4 to 0)
    block = BRepPrimAPI_MakeBox(gp_Pnt(-20, -15, 0), 40, 30, 20).Shape()
    block = BRepAlgoAPI_Cut(block, _cyl(5.0, 17.0, 4.0)).Shape()
    for x in (-12.0, 12.0):
        block = BRepAlgoAPI_Fuse(block, _cyl(3.0, -4.0, 4.5, x=x)).Shape()
        block = BRepAlgoAPI_Cut(block, _cyl(1.5, -5.0, 27.0, x=x)).Shape()
        block = BRepAlgoAPI_Cut(block, _cyl(2.95, 14.0, 7.0, x=x)).Shape()
    return _write(block, path)


def test_a_counterbored_port_is_capped_at_the_counterbore_top(tmp_path):
    """Two counterbored ports in one top face (the GL45 cap's kind): the declared 5.9 mm mouths are capped in the
    top face (z = 20), the counterbores' floors (z = 14) and their 3 mm bores are the ports' necks; the 3 mm bottom
    ends stay their bores' caps. Old code capped each counterbore's floor: the port at z = 14."""
    ports = _ports(((-12, 0, 20), 5.9), ((12, 0, 20), 5.9), ((-12, 0, -4), 3.0), ((12, 0, -4), 3.0))
    t = _stage(_counterbored_block(tmp_path / "cbore.step"), ports, tmp_path)
    bound, _w, _n = bind_intake(t, ports, bore=True)
    for name in ("inlet", "outlet_1"):
        tri = _tris(bound["stls"][name])
        assert np.allclose(tri[:, :, 2], 20.0, atol=1e-3), (name, sorted(set(np.round(tri[:, :, 2].ravel(), 3))))
        assert _area(tri) == pytest.approx(math.pi * 2.95 ** 2, rel=0.02)
        assert float(bound["openings"][name]["flow_area"]) * 1e6 == pytest.approx(math.pi * 2.95 ** 2, rel=0.02)
    for name in ("outlet_2", "outlet_3"):
        tri = _tris(bound["stls"][name])
        assert np.allclose(tri[:, :, 2], -4.0, atol=1e-3)
        assert _area(tri) == pytest.approx(math.pi * 1.5 ** 2, rel=0.02)
    assert _open_edges(t["stls"].values()) == 0


def test_a_shoulder_inside_the_bore_is_not_the_mouth(tmp_path):
    """A tube whose bore steps from 40 to 30 mm 20 mm in from its inlet end: the inlet is the 40 mm bore's cap in the
    end plane (z = 0), not the shoulder's ring; the ring at the end is wall."""
    tube = BRepAlgoAPI_Cut(_cyl(30.0, 0.0, 120.0), _cyl(20.0, -1.0, 21.0)).Shape()
    tube = BRepAlgoAPI_Cut(tube, _cyl(15.0, 19.0, 102.0)).Shape()
    ports = _ports(((0, 0, 0), 40.0), ((0, 0, 120), 30.0))
    t = _stage(_write(tube, tmp_path / "shoulder.step"), ports, tmp_path)
    tri = _tris(t["stls"]["inlet"])
    assert np.allclose(tri[:, :, 2], 0.0, atol=1e-3)
    assert _area(tri) == pytest.approx(math.pi * 20.0 ** 2, rel=0.02)
    assert _area(_tris(t["stls"]["outlet"])) == pytest.approx(math.pi * 15.0 ** 2, rel=0.02)


def test_a_mouth_in_a_curved_wall_is_not_sealed_behind_a_flat_face(tmp_path):
    """A capped tube whose outlet is a slot cut through its side near the bottom (63643's spray slots): the slot's
    mouth is a hole in the outer cylinder, the nearest flat faces are the slot's ceiling and floor. Old code staged
    the ceiling as the outlet and sealed the confirmed mouth into the wall; now the part's surface is staged."""
    tube = BRepAlgoAPI_Cut(_cyl(10.0, -5.0, 65.0), _cyl(7.0, 0.0, 61.0)).Shape()
    slot = BRepPrimAPI_MakeBox(gp_Pnt(5.0, -2.0, 3.0), 7.0, 4.0, 4.0).Shape()
    tube = BRepAlgoAPI_Cut(tube, slot).Shape()
    ports = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 60], "diameter_mm": 14.0},
             {"name": "outlet", "type": "outlet", "near_mm": [9.8, 0, 5.0], "area_mm2": 16.0},
             {"name": "wall", "type": "wall"}]
    with pytest.raises(FlatFacesMissPort, match="INS-26"):
        _stage(_write(tube, tmp_path / "slot.step"), ports, tmp_path)


def _lipped_tube(path: Path) -> Path:
    # revolved about z: a 30 mm tube, its bore 24 mm from z = 0 to 180, then 20 mm to the top (z = 200), where the
    # wall ends in a rounded lip (a half circle from the bore to the outside) - no flat face at the mouth
    r_out, r1, r2, h1, top = 15.0, 15.0 - 3.0, 10.0, 180.0, 200.0
    lip = 0.5 * (r_out - r2)
    edges = []
    order = [((r1, 0.0), (r_out, 0.0)), ((r_out, 0.0), (r_out, top)), "arc", ((r2, top), (r2, h1)),
             ((r2, h1), (r1, h1)), ((r1, h1), (r1, 0.0))]
    for seg in order:
        if seg == "arc":
            arc = GC_MakeArcOfCircle(gp_Pnt(r_out, 0, top), gp_Pnt(r2 + lip, 0, top + lip), gp_Pnt(r2, 0, top))
            edges.append(BRepBuilderAPI_MakeEdge(arc.Value()).Edge())
        else:
            (a, b), (c, d) = seg
            edges.append(BRepBuilderAPI_MakeEdge(gp_Pnt(a, 0, b), gp_Pnt(c, 0, d)).Edge())
    wire = BRepBuilderAPI_MakeWire()
    for e in edges:
        wire.Add(e)
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    solid = BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 2.0 * math.pi).Shape()
    return _write(solid, path)


def test_a_cap_behind_a_rounded_lip_is_no_mouth(tmp_path):
    """A tube whose bore narrows at a step 20 mm below a rounded lip (102521's nozzle): the only flat faces are the
    inlet end and the step, so old code capped the step's 20 mm bore 20 mm inside the confirmed 20 mm mouth - the
    last stretch cut off. Now the part's surface is staged."""
    ports = _ports(((0, 0, 0), 24.0), ((0, 0, 200), 20.0))
    with pytest.raises(FlatFacesMissPort, match="INS-26"):
        _stage(_lipped_tube(tmp_path / "lip.step"), ports, tmp_path)


def test_a_solid_rod_keeps_its_disc_ports(tmp_path):
    """A rod declared the fluid: its end discs are the ports, whole, as before."""
    rod = _cyl(25.0, 0.0, 200.0)
    ports = _ports(((0, 0, 0), 50.0), ((0, 0, 200), 50.0))
    t = _stage(_write(rod, tmp_path / "rod.step"), ports, tmp_path, fluid_solid=True)
    for name in ("inlet", "outlet"):
        assert _area(_tris(t["stls"][name])) == pytest.approx(math.pi * 25.0 ** 2, rel=0.02)
        assert "flow_area" not in t["openings"][name]


def test_a_declared_fluid_annulus_keeps_its_ring_as_the_lid(tmp_path):
    """A declared fluid domain shaped as an annulus: the ring faces are the openings, whole, as before."""
    ring = BRepAlgoAPI_Cut(_cyl(30.0, 0.0, 100.0), _cyl(20.0, -1.0, 102.0)).Shape()
    d_eq = 2.0 * math.sqrt(30.0 ** 2 - 20.0 ** 2)
    ports = _ports(((0, 0, 0), d_eq), ((0, 0, 100), d_eq))
    t = _stage(_write(ring, tmp_path / "annulus.step"), ports, tmp_path, fluid_solid=True)
    for name in ("inlet", "outlet"):
        assert _area(_tris(t["stls"][name])) == pytest.approx(math.pi * (30.0 ** 2 - 20.0 ** 2), rel=0.02)
