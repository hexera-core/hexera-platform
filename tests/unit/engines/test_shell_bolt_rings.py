"""SHMESH-51: bolt holes are found by what they are and what is round them, not by their size alone, and get two rings
of quads where those fit inside the user's edges and the room round them; where only one fits, the record says why.
Every part here is synthetic, built with the CAD kernel."""
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.engines import shell_structure as SS
from meshpipeline.engines.gmsh import shell_structured as ST
from meshpipeline.engines.shell_mesh import ShellMesh


def _rings_mesh(n: int, radii: list[float]) -> ShellMesh:
    """Concentric node circles joined in rings of quads (the first circle is the hole)."""
    a = np.arange(n) * 2 * math.pi / n
    xyz = np.vstack([np.column_stack([r * np.cos(a), r * np.sin(a), np.zeros(n)]) for r in radii])
    q = np.array([[k * n + i, k * n + (i + 1) % n, (k + 1) * n + (i + 1) % n, (k + 1) * n + i]
                  for k in range(len(radii) - 1) for i in range(n)], dtype=np.int64)
    m = len(q)
    return ShellMesh(xyz=xyz, quads=q, trias=np.zeros((0, 3), dtype=np.int64), wall=np.ones(m), mid=["Mid_1"] * m,
                     part=["Part_1"] * m)


def _hole(mesh: ShellMesh, d: float) -> dict:
    return next(h for h in SS.holes(mesh) if abs(h["diameter_mm"] - d) < 1e-6)


def test_the_rings_round_a_hole_are_counted_out_from_it():
    assert _hole(_rings_mesh(8, [1.0, 1.6]), 2.0)["rings"] == 1
    assert _hole(_rings_mesh(8, [1.0, 1.6, 2.4]), 2.0)["rings"] == 2
    assert _hole(_rings_mesh(12, [1.0, 1.5, 2.0, 2.6]), 2.0)["rings"] == 3
    # a ring cut by a triangle is no ring; the rings stop there
    m = _rings_mesh(8, [1.0, 1.6, 2.4])
    m.trias = np.array([[8, 9, 17], [8, 17, 16]], dtype=np.int64)       # the second ring's first quad in two
    m.quads = np.delete(m.quads, 8, axis=0)
    assert _hole(m, 2.0)["rings"] == 1


def test_a_bolt_holes_two_rings_stay_inside_the_users_edges():
    # a 17.5 mm hole at 10 mm with edges 5 to 15 mm: 8 round (12 would put 4.6 mm edges on the hole), each ring at
    # least 5 mm wide, the outer elements no longer than 0.98 x 15
    got = ST._count_bolt(8.75, 2 * math.pi * 8.75 / 8, 10.0, 5.0, 15.0)
    assert isinstance(got, tuple)
    n, rows, ratio, radii = got
    assert n == 8 and rows == 2 and len(radii) == 1
    s = math.sin(math.pi / n)
    rad = [1.0, *radii, ratio]
    assert 2 * 8.75 * s >= 5.0
    assert all((b - a) * 8.75 >= 5.0 - 1e-9 for a, b in zip(rad, rad[1:], strict=False))
    assert 2 * ratio * 8.75 * s <= 0.98 * 15.0 + 1e-9
    # no room: words, never a squeezed ring
    assert ST._count_bolt(8.75, 2 * math.pi * 8.75 / 8, 10.0, 5.0, 15.0, room_mm=(16.0, 16.0)) == \
        "no room for two rings"
    # edges asked for that no two rings can meet round a hole this size
    assert ST._count_bolt(3.0, 2 * math.pi * 3.0 / 8, 10.0, 5.0, 15.0).startswith("the edge lengths")


pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")


def _plates(tmp_path, plates):
    """Plates [(L, W, T, z, [(x, y, d)])] as one compound, extracted to a mid-surface."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad import midsurface as ms

    comp = TopoDS_Compound()
    bb = BRep_Builder()
    bb.MakeCompound(comp)
    for L, W, T, z, holes in plates:
        shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, z), L, W, T).Shape()
        for x, y, d in holes:
            shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z - 1), gp_Dir(0, 0, 1)), d / 2,
                                                                    T + 2).Shape()).Shape()
        bb.Add(comp, shape)
    rec, surf = ms.extract(comp if len(plates) > 1 else shape, mm_per_unit=1.0)
    brep = tmp_path / "midsurface.brep"
    BRepTools.Write_s(surf, str(brep))
    return brep, rec


def _mesh(brep, rec):
    from tests.unit.engines.test_shell_structured import _criteria

    from meshpipeline.engines.gmsh import shell_adapter as SA

    return SA.mesh_shell(brep, rec, mm_per_unit=1.0, requirements=_criteria())


def _check(applied):
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"]
    assert all(r["met"] is not False for r in applied["requirements"]["rows"]), applied["requirements"]["rows"]


def test_a_row_of_alike_holes_are_bolt_holes_with_two_rings(tmp_path):
    brep, rec = _plates(tmp_path, [(400.0, 150.0, 10.0, 0.0, [(80.0 + 80.0 * k, 75.0, 17.5) for k in range(4)])])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert s["bolt_holes"] == 4 and s["bolt_rings"] == {2: 4}, s.get("bolt_rings")
    assert all(h["bolt"] == "one of 4 alike holes in a row" and h["rings"] == 2 for h in s["hole_list"])
    assert all(h["rings"] >= 2 for h in SS.holes(mesh) if abs(h["diameter_mm"] - 17.5) < 0.2)
    assert any(n.startswith("Two rings round 4 of the 4 bolt holes") for n in mesh.notes), mesh.notes
    assert len(mesh.trias) == 0
    _check(applied)


def test_a_lone_hole_keeps_the_ring_its_size_gives(tmp_path):
    brep, rec = _plates(tmp_path, [(200.0, 200.0, 10.0, 0.0, [(100.0, 100.0, 17.5)])])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert "bolt" not in s["hole_list"][0] and "bolt_holes" not in s
    assert not any("bolt" in n for n in mesh.notes)


def test_holes_through_two_plates_are_bolt_holes(tmp_path):
    brep, rec = _plates(tmp_path, [(200.0, 200.0, 10.0, 0.0, [(100.0, 100.0, 17.5)]),
                                   (200.0, 200.0, 10.0, 40.0, [(100.0, 100.0, 17.5)])])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert [h["bolt"].startswith("it lines up with a hole in another plate") for h in s["hole_list"]] == [True, True]
    assert s["bolt_rings"] == {2: 2}
    assert len(mesh.trias) == 0
    _check(applied)


def test_bolt_holes_with_no_room_say_why(tmp_path):
    # three 17.5 mm holes 26 mm apart (8.5 mm between them): no room for two rings, and the line says so
    brep, rec = _plates(tmp_path, [(300.0, 150.0, 10.0, 0.0, [(124.0 + 26.0 * k, 75.0, 17.5) for k in range(3)])])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert all(h["one_ring"].startswith("no room for two rings (the next hole is") for h in s["hole_list"])
    assert all(h["rings"] <= 1 for h in s["hole_list"])
    assert not any(n.startswith("Two rings") for n in mesh.notes)
    _check(applied)
