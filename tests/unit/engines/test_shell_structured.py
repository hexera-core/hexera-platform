# Responsibility: Hold the structured shell mesh to its rules (SHMESH-38..43): a ring of quads round every round hole of
# a flat face (an even count, at least 8, never fewer than the plan put round it), mapped grids on four-sided faces
# with one count per class of shared curves, a face with washers cut into a grid of cells, the user's "free mesh" /
# "no washers" in the notes turning them off, the regularity measure, and the same file twice giving the same mesh.
# Boundaries: numpy on small made-up meshes, and gmsh/OpenCASCADE in this process on made-up plates; no solver.
from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pytest
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_structure as SS
from meshpipeline.engines.gmsh import shell_structured as ST
from meshpipeline.engines.shell_mesh import ShellMesh


# ------------------------------------------------------------------------------------------- the plans
def test_a_washer_counts_evenly_and_never_coarser_than_the_plan_round_its_hole():
    # a 22 mm hole in a 10 mm face, 8 elements round it in the plan: 12 round, the ring's outer elements no longer
    # than the face's, the ring 1.4 to 2 times the hole
    n, rows, ratio = ST._count(11.0, 2 * math.pi * 11 / 8, 10.0)
    assert n % ST.WASHER_STEP == 0 and n >= ST.WASHER_MIN
    assert ST.WASHER_RING[0] <= ratio <= ST.WASHER_RING[1]
    assert 2 * math.pi * 11.0 * ratio / n <= 10.0 + 1e-9
    # the plan's own count round the hole is kept (24 round: never fewer)
    assert ST._count(11.0, 2 * math.pi * 11 / 24, 10.0)[0] >= 24
    # a small hole: 8 round
    assert ST._count(3.0, 2 * math.pi * 3 / 8, 10.0)[0] == 8


def test_an_o_grid_box_matches_its_ring_and_the_users_edges():
    got = ST._boxed(9.0, 2 * math.pi * 9 / 8, 10.0)
    assert got is not None
    n, rows, ratio, b = got
    assert n % ST.BOX_STEP == 0                                   # even quarters
    seg = 2 * b / (n / 4)
    assert ST.BOX_SEG[0] * 10.0 - 1e-9 <= seg <= ST.BOX_SEG[1] * 10.0 + 1e-9
    assert b - ratio * 9.0 >= ST.BOX_GAP * 10.0 - 1e-9            # the box clear of the ring
    assert ST.WASHER_RING[0] <= ratio <= ST.WASHER_RING[1]
    # under the user's shortest edge an O-grid never puts an edge under it round the hole or across the ring
    for r in (9.0, 11.0, 20.0):
        got = ST._boxed(r, 2 * math.pi * r / 8, 10.0, low_mm=5.0, high_mm=15.0)
        if got is not None:
            n, rows, ratio, b = got
            assert 2 * math.pi * r / n >= 5.0 - 1e-9 and (ratio * r - r) / rows >= 5.0 - 1e-9
            assert 2 * b / (n / 4) <= 0.98 * 15.0 + 1e-9
    # a hole too small for its ring to meet a 10 mm grid inside 5..15 mm edges: no O-grid (a free box instead)
    assert ST._boxed(7.0, 2 * math.pi * 7 / 8, 10.0, low_mm=5.0, high_mm=15.0) is None


def test_a_chain_takes_its_opposite_sides_count_spread_by_length():
    assert ST._spread(4, [5.5, 5.5]) == [2, 2]
    assert sum(ST._spread(7, [1.0, 2.0, 4.0])) == 7
    assert ST._spread(1, [1.0, 1.0]) is None


def test_lines_nearer_than_the_snap_are_one_and_an_edge_wins():
    got = ST._cluster([(0.0, "edge"), (4.0, "0"), (50.0, "1"), (52.0, "2"), (100.0, "edge")], 5.0)
    assert got == [(0.0, True, [0]), (51.0, False, [1, 2]), (100.0, True, [])]


# ------------------------------------------------------------------------------------------- the measure
def _grid(nx: int, ny: int) -> ShellMesh:
    xyz = np.array([[i, j, 0.0] for j in range(ny + 1) for i in range(nx + 1)], dtype=float)
    q = np.array([[j * (nx + 1) + i, j * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i]
                  for j in range(ny) for i in range(nx)], dtype=np.int64)
    n = len(q)
    return ShellMesh(xyz=xyz, quads=q, trias=np.zeros((0, 3), dtype=np.int64), wall=np.ones(n), mid=["Mid_1"] * n,
                     part=["Part_1"] * n)


def _washer(n: int = 12, r: float = 1.0, R: float = 1.8, twist: float = 0.0) -> ShellMesh:
    a = np.arange(n) * 2 * math.pi / n
    inner = np.column_stack([r * np.cos(a), r * np.sin(a), np.zeros(n)])
    outer = np.column_stack([R * np.cos(a + twist), R * np.sin(a + twist), np.zeros(n)])
    xyz = np.vstack([inner, outer])
    q = np.array([[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)], dtype=np.int64)
    return ShellMesh(xyz=xyz, quads=q, trias=np.zeros((0, 3), dtype=np.int64), wall=np.ones(n), mid=["Mid_1"] * n,
                     part=["Part_1"] * n)


def test_a_mapped_grid_is_fully_regular():
    got = SS.measure(_grid(6, 4))
    assert got["valence4"] == 1.0 and got["rect"] == 1.0 and got["aligned"] == 1.0 and got["holes"] == 0


def test_a_ring_round_a_hole_is_seen_and_counted():
    # (a lone ring's outer edge is a free circle too: the hole is the 2 mm one)
    got = next(h for h in SS.holes(_washer()) if abs(h["diameter_mm"] - 2.0) < 1e-6)
    assert got["ring"] is True and got["around"] == 12 and abs(got["ring_diameter_mm"] - 3.6) < 1e-6
    # a triangle touching the hole: no ring
    m = _washer()
    m.trias = np.array([[0, 1, 13], [0, 13, 12]], dtype=np.int64)     # the first quad cut in two
    m.quads = m.quads[1:]
    assert next(h for h in SS.holes(m) if abs(h["diameter_mm"] - 2.0) < 1e-6)["ring"] is False


# ------------------------------------------------------------------------------------------- the notes
def test_free_mesh_in_the_notes_is_read_into_the_pattern():
    notes = CRITERIA + "\nFree mesh please, no washers"
    answer = criteria_answer()
    answer["element_kind"] = [{"what": "free_mesh", "value": 0, "quote": "Free mesh please"},
                              {"what": "no_washers", "value": 0, "quote": "no washers"}]
    req, dropped, _na = MR.check(json.dumps(answer), notes)
    assert req["kind"]["structure"] == "free"
    assert any(r["field"] == "free_mesh" for r in req["rows"])
    assert any("two mesh patterns" in d for d in dropped)          # the first one written is kept
    # nothing said: no pattern (the shell mesh is structured), and the kind keeps its shape
    req2, _d, _n = MR.check(json.dumps(criteria_answer()), CRITERIA)
    assert "structure" not in req2["kind"] and not MR.is_empty(req2)


# ------------------------------------------------------------------------------------------- the mesh
pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")


def _plate_with_holes(tmp_path, holes, size=(300.0, 200.0, 10.0)):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    from meshpipeline.cad import midsurface as ms

    L, W, T = size
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), L, W, T).Shape()
    for x, y, d in holes:
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), d / 2,
                                                                T + 2).Shape()).Shape()
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "midsurface.brep"
    BRepTools.Write_s(surf, str(brep))
    return brep, rec


def _mesh(brep, rec, requirements=None):
    from meshpipeline.engines.gmsh import shell_adapter as SA

    return SA.mesh_shell(brep, rec, mm_per_unit=1.0, requirements=requirements)


def _criteria(g: float = 10.0, pattern: str | None = None) -> dict:
    answer = criteria_answer()
    notes = CRITERIA
    if pattern:
        notes += f"\n{pattern}"
        answer["element_kind"] = [{"what": "free_mesh" if pattern == "free mesh" else "no_washers", "value": 0,
                                   "quote": pattern}]
    req, dropped, _na = MR.check(json.dumps(answer), notes)
    assert dropped == []
    return req


def _sha(mesh) -> str:
    h = hashlib.sha256()
    for a in (mesh.xyz, mesh.quads, mesh.trias):
        h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    """A 300 x 200 x 10 mm plate with a 2 x 3 pattern of 18 mm bolt holes (a channel web's)."""
    return _plate_with_holes(tmp_path_factory.mktemp("web"),
                             [(x, y, 18.0) for x in (75.0, 150.0, 225.0) for y in (65.0, 135.0)])


def test_every_bolt_hole_gets_a_washer_and_the_field_is_mapped(web):
    brep, rec = web
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["washers"] == 6 and s["holes"] == 6
    assert s["washers_ogrid"] + s["washers_boxed"] == 6               # the plate was cut into a grid
    assert s["mapped_faces"] >= s["faces"] // 2
    got = SS.measure(mesh)
    assert got["holes"] == 6 and got["holes_ringed"] == 6
    assert all(h["around"] % 4 == 0 and h["around"] >= 8 for h in got["hole_list"])
    assert got["valence4"] > 0.8 and len(mesh.trias) == 0
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
    assert any("Washers on 6 of 6" in n for n in mesh.notes)
    # every line of the criteria list is met at least as before: inside every quality limit
    rows = {r["id"]: r for r in applied["requirements"]["rows"]}
    assert all(r["met"] is not False for r in rows.values())
    # the same file twice: the same mesh
    again, _a = _mesh(brep, rec, _criteria())
    assert _sha(again) == _sha(mesh)


def test_free_mesh_in_the_notes_turns_washers_and_mapping_off(web):
    brep, rec = web
    mesh, applied = _mesh(brep, rec, _criteria(pattern="free mesh"))
    s = applied["quality"]["structure"]
    assert s["washers"] == 0 and s["mapped_faces"] == 0 and s["switched_off"] == "free"
    assert SS.measure(mesh)["holes_ringed"] == 0
    row = next(r for r in applied["requirements"]["rows"] if r["field"] == "free_mesh")
    assert row["met"] is True


def test_no_washers_keeps_the_mapped_faces(tmp_path):
    brep, rec = _plate_with_holes(tmp_path, [(100.0, 100.0, 20.0)], size=(200.0, 200.0, 10.0))
    mesh, applied = _mesh(brep, rec, _criteria(pattern="no washers"))
    s = applied["quality"]["structure"]
    assert s["washers"] == 0 and s["switched_off"] == "no_washers"


def test_a_round_hole_too_big_for_a_washer_is_left_to_the_field(tmp_path):
    brep, rec = _plate_with_holes(tmp_path, [(100.0, 100.0, 80.0)], size=(200.0, 200.0, 10.0))
    _m, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["washers"] == 0 and s["no_washer"] == {"larger than the washer limit": 1}


def test_a_junction_keeps_one_node_per_point_and_a_rectangle_is_one_grid(tmp_path):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt

    from meshpipeline.cad import midsurface as ms

    # a tee: a 200 x 100 x 5 flange with a 100 mm high, 5 mm web standing on its middle
    flange = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 200, 100, 5).Shape()
    web = BRepPrimAPI_MakeBox(gp_Pnt(0, 47.5, 5), 200, 5, 100).Shape()
    rec, surf = ms.extract(BRepAlgoAPI_Fuse(flange, web).Shape(), mm_per_unit=1.0)
    brep = tmp_path / "tee.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec)
    assert applied["quality"]["structure"]["mapped_faces"] == applied["quality"]["structure"]["faces"]
    assert len(mesh.trias) == 0 and SS.measure(mesh)["rect"] > 0.99
    # no two nodes at one point (the junction's rows share their nodes)
    from scipy.spatial import cKDTree

    pairs = cKDTree(mesh.xyz).query_pairs(1e-6)
    assert not pairs
    assert applied["quality"]["edges"]["junction"] > 0


def test_a_grid_never_cuts_an_edge_another_face_shares(tmp_path):
    """A flange with bolt holes beside a web: lines drawn across the flange would cut the edge it shares with the
    web (the web would keep it whole: a crack), so that face is not gridded and the junction keeps its nodes."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    from meshpipeline.cad import midsurface as ms

    flange = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 300, 100, 5).Shape()
    for x in (75.0, 150.0, 225.0):
        flange = BRepAlgoAPI_Cut(flange, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, 25, -1), gp_Dir(0, 0, 1)), 7,
                                                                  7).Shape()).Shape()
    web = BRepPrimAPI_MakeBox(gp_Pnt(0, 47.5, 5), 300, 5, 100).Shape()
    rec, surf = ms.extract(BRepAlgoAPI_Fuse(flange, web).Shape(), mm_per_unit=1.0)
    brep = tmp_path / "flange.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec, _criteria())
    from scipy.spatial import cKDTree

    pairs = SS._edges(mesh)
    free = pairs[0][pairs[1] == 1]
    mids = 0.5 * (mesh.xyz[free[:, 0]] + mesh.xyz[free[:, 1]])
    # no free edge lies on another (a crack), and none along the junction line (y = 50, z = 2.5)
    d, _i = cKDTree(mids).query(mids, k=2)
    assert (d[:, 1] > 1e-6).all()
    assert not np.any((np.abs(mids[:, 1] - 50.0) < 1e-6) & (np.abs(mids[:, 2] - 2.5) < 1e-6))
    assert applied["quality"]["structure"]["washers"] == 3
