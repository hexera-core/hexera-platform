# Responsibility: Hold the structured shell mesh to its rules (SHMESH-39..44): a ring of quads round every round hole of
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
    assert n % 4 == 0 and n % ST.WASHER_STEP == 0                 # even quarters (the hole starts on the diagonal)
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


def test_near_lines_are_one_only_where_every_ring_keeps_its_room():
    """SHMESH-48i, a real channel web (50 holes out of step, 5+ sizes apart): a chain of box sides each a little from
    the next was one line at their mean, pulling a side past its ring ("a ring too near ... for its box")."""
    vals = [(10.0, "0"), (14.0, "1"), (18.0, "2"), (22.0, "3")]
    # side 0 may go no higher than 11, side 3 no lower than 21: no one place suits all four
    limits = {"0": (5.0, 11.0), "1": (8.0, 20.0), "2": (12.0, 24.0), "3": (21.0, 30.0)}
    got = ST._cluster(vals, 5.0, limits)
    assert [g[2] for g in got] == [[0, 1], [2, 3]]
    for at, _edge, sides in got:
        assert all(limits[str(s)][0] <= at <= limits[str(s)][1] for s in sides)
    # an edge takes a side only where the side may go
    assert ST._cluster([(0.0, "edge"), (4.0, "0")], 5.0, {"0": (2.0, 6.0)}) == [(0.0, True, []), (4.0, False, [0])]
    assert ST._cluster([(0.0, "edge"), (4.0, "0")], 5.0, {"0": (-1.0, 6.0)}) == [(0.0, True, [0])]


def test_lines_kept_apart_move_apart_to_no_sliver():
    """SHMESH-48m, a real top plate: two box sides 0.64 mm apart that no one place suits (each ring's room) stayed two
    lines and left a 0.64 mm cell; now each moves away from the other inside its own limits, to the gap asked."""
    vals = [(10.0, "0"), (10.64, "1")]
    limits = {"0": (5.0, 10.2), "1": (10.5, 16.0)}
    got = ST._cluster(vals, 5.0, limits, min_gap=4.2)
    assert [g[2] for g in got] == [[0], [1]]
    assert got[1][0] - got[0][0] >= 4.2 - 1e-9
    assert all(limits[str(g[2][0])][0] - 1e-9 <= g[0] <= limits[str(g[2][0])][1] + 1e-9 for g in got)
    # (without the gap asked: as before)
    assert [g[0] for g in ST._cluster(vals, 5.0, limits)] == [10.0, 10.64]


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
    assert s["washers"] == 0 and s["no_washer"] == {"larger than the washer limit (6 element sizes across)": 1}


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


# ------------------------------------------------------------------------------------------- the washer guard
def test_a_ring_that_cannot_be_built_keeps_gmshs_mesh_there(web, monkeypatch):
    """SHMESH-45, per washer: a washer whose own ring fails a check costs nothing but its ring."""
    brep, rec = web
    real = ST.ring_mesh
    calls = {"n": 0}

    def flaky(gmsh, w, index, xyz, mm):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("a washer's hole and ring nodes do not face each other")
        return real(gmsh, w, index, xyz, mm)

    monkeypatch.setattr(ST, "ring_mesh", flaky)
    monkeypatch.setattr(ST, "BLOCKS", False)          # every washer then takes its own ring
    mesh, applied = _mesh(brep, rec, _criteria())
    q = applied["quality"]
    assert q["valid"] and mesh.n_elements > 0
    assert q["structure"]["washers"] == 5
    assert q["structure"]["no_washer"] == {"its ring could not be built (gmsh's mesh kept there)": 1}
    assert "guard" not in q["structure"]
    # conformal: no free edge but the plate's rim and its six holes
    pairs = SS._edges(mesh)
    assert len(SS._loops(pairs[0][pairs[1] == 1])) == 7


def test_gridded_steps_that_fail_keep_the_round_rings(web, monkeypatch):
    """SHMESH-45g, the guard's middle step: when only the gridded/mapped steps fail, the part keeps its round washers
    (the free mesh is the last step, not the first)."""
    brep, rec = web
    real = ST.map_faces

    def broken(*a, **k):
        raise RuntimeError("Unknown OpenCASCADE entity of dimension 2 with tag 4")

    monkeypatch.setattr(ST, "map_faces", broken)
    monkeypatch.setattr(ST, "block_faces", broken)
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["guard"]["kept"] == "round rings only" and "tag 4" in s["guard"]["why"]
    assert s["washers"] == s["holes"] > 0 and applied["quality"]["valid"]
    assert real is not ST.map_faces


def test_structured_steps_that_fail_fall_back_to_the_free_mesh(web, monkeypatch):
    """SHMESH-45, whole step: any failure of the structured steps gives the mesh "free mesh" gives, said."""
    brep, rec = web
    free, _a = _mesh(brep, rec, _criteria(pattern="free mesh"))

    def broken(*_a, **_k):
        raise ValueError("a washer's hole and ring nodes do not face each other")

    monkeypatch.setattr(ST, "plan_washers", broken)
    mesh, applied = _mesh(brep, rec, _criteria())
    guard = applied["quality"]["structure"]["guard"]
    assert guard["kept"] == "free mesh" and "do not face each other" in guard["why"]
    assert _sha(mesh) == _sha(free)
    assert any("SHMESH-45" in n for n in mesh.notes)


def test_a_hole_drawn_as_several_edges_still_gets_its_washer(tmp_path):
    """SHMESH-45b, the cause on a real column: a CAD circle split into 2 to 5 edges, unevenly, left the washer's hole
    nodes out of step with its ring's ("a washer's hole and ring nodes do not face each other"). The round free holes
    of flat faces are drawn again as one circle each, so every one gets its ring."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec

    from meshpipeline.cad import midsurface as ms

    def hole(x, y, cuts):
        circ = gp_Circ(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)), 10.0)
        angs = [math.radians(a) for a in cuts] + [math.radians(cuts[0]) + 2 * math.pi]
        mw = BRepBuilderAPI_MakeWire()
        for a, b in zip(angs[:-1], angs[1:], strict=True):
            mw.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(circ, a, b, True).Value()).Edge())
        return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(mw.Wire()).Face(), gp_Vec(0, 0, 12)).Shape()

    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 300, 200, 10).Shape()
    for (x, y), cuts in (((60, 60), [0, 180]), ((150, 60), [0, 100, 250]), ((240, 60), [0, 72, 144, 216, 288]),
                         ((60, 140), [0, 150]), ((150, 140), [0, 120, 240]), ((240, 140), [0, 90, 180, 270])):
        shape = BRepAlgoAPI_Cut(shape, hole(x, y, cuts)).Shape()      # no unify: the circles stay split
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "split.brep"
    BRepTools.Write_s(surf, str(brep))
    for req in (_criteria(), None):
        mesh, applied = _mesh(brep, rec, req)
        s = applied["quality"]["structure"]
        # (only the holes whose ring had to follow uneven hole nodes on a first mesh are drawn again, SHMESH-45k: the
        # uneven splits - and, where the face is gridded, its O-grid holes to start where they are cut, SHMESH-49c)
        assert (s["holes_redrawn"] == 6) if s["washers_ogrid"] else (1 <= s["holes_redrawn"] <= 4), s
        assert s["washers"] == 6 and s["no_washer"] == {}, s
        assert all(h["edges"] == 1 for h in s["hole_list"] if h["edges_before"] != h["edges"])
        assert SS.measure(mesh)["holes_ringed"] == 6 and applied["quality"]["valid"]


def test_every_hole_has_its_line_in_the_record(web, monkeypatch):
    """SHMESH-45c: one line per hole of a flat face - face, part, diameter, CAD edges, shared, planned ring, room, why."""
    brep, rec = web
    real = ST.ring_mesh
    calls = {"n": 0}

    def flaky(gmsh, w, index, xyz, mm):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("a washer's hole and ring nodes do not face each other")
        return real(gmsh, w, index, xyz, mm)

    monkeypatch.setattr(ST, "ring_mesh", flaky)
    monkeypatch.setattr(ST, "BLOCKS", False)
    _mesh_, applied = _mesh(brep, rec, _criteria())
    lines = applied["quality"]["structure"]["hole_list"]
    assert len(lines) == 6
    for h in lines:
        assert {"face", "part", "diameter_mm", "edges", "edges_before", "shared", "to_edge", "to_hole",
                "washer"} <= set(h)
        assert abs(h["diameter_mm"] - 18.0) < 0.1 and h["shared"] is False
    refused = [h for h in lines if not h["washer"]]
    assert len(refused) == 1 and "do not face each other" in refused[0]["why"]
    assert all(h.get("kind") == "ring in a free face" and h["planned_round"] >= 8 for h in lines if h["washer"])


def _bspline_hole_plate(tmp_path, holes):
    """A 19.05 mm plate whose holes are drawn as B-spline pieces through points of a circle, each piece off the radius
    by up to 3e-4 of it (a mid-surface's fitted hole), meeting end to end: [((x, y), diameter, [cut angles])]."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.GeomAPI import GeomAPI_PointsToBSpline
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TColgp import TColgp_Array1OfPnt

    from meshpipeline.cad import midsurface as ms

    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 300, 200, 19.05).Shape()
    for (x, y), d, cuts in holes:
        r = d / 2
        angs = [math.radians(a) for a in cuts] + [math.radians(cuts[0]) + 2 * math.pi]
        mw = BRepBuilderAPI_MakeWire()
        for i, (a, b) in enumerate(zip(angs[:-1], angs[1:], strict=True)):
            arr = TColgp_Array1OfPnt(1, 9)
            for j in range(9):
                t = a + (b - a) * j / 8
                rr = r if j in (0, 8) else r * (1 + 3e-4 * math.sin(3 * t + i))
                arr.SetValue(j + 1, gp_Pnt(x + rr * math.cos(t), y + rr * math.sin(t), -1))
            mw.Add(BRepBuilderAPI_MakeEdge(GeomAPI_PointsToBSpline(arr).Curve()).Edge())
        tool = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(mw.Wire()).Face(), gp_Vec(0, 0, 21)).Shape()
        shape = BRepAlgoAPI_Cut(shape, tool).Shape()
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "bspline.brep"
    BRepTools.Write_s(surf, str(brep))
    return brep, rec


BSPLINE_HOLES = [((60, 60), 15.47, [5, 95, 185, 275]), ((150, 60), 17.98, [0, 120, 250]),
                 ((240, 60), 15.47, [0, 80, 200, 300]), ((60, 140), 17.98, [30, 150, 270]),
                 ((150, 140), 15.47, [0, 90, 180, 270]), ((240, 140), 46.07, [0, 130, 250])]


@pytest.mark.parametrize("redraw", [True, False])
def test_holes_in_fitted_pieces_get_their_washers(tmp_path, monkeypatch, redraw):
    """SHMESH-45d, class B of a real column: holes of 3 or 4 B-spline pieces (not exact arcs: the SHMESH-45b redraw
    once refused them) got rings whose nodes did not face the hole's. Now the redraw takes pieces that are one circle
    within 1e-3 of the radius; and without it the ring's nodes follow the hole's. SHMESH-45e: the 46 mm hole (4.6 sizes
    at 10 mm, the user's global size) gets a washer, two rows."""
    brep, rec = _bspline_hole_plate(tmp_path, BSPLINE_HOLES)
    monkeypatch.setattr(ST, "RESEAT", redraw)
    monkeypatch.setattr(SA_mod(), "STRUCTURED_GUARD", False)        # nothing hidden by the whole-step guard
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["washers"] == 6 and s["no_washer"] == {}, s["hole_list"]
    big = next(h for h in s["hole_list"] if h["diameter_mm"] > 40)
    assert big["washer"] and big["planned_rows"] == 2
    if redraw:
        assert s["holes_redrawn"] == 6 and {h["edges_before"] for h in s["hole_list"]} == {3, 4}
        assert {h["edges"] for h in s["hole_list"]} == {1}
    got = SS.measure(mesh)
    assert got["holes_ringed"] == 6 and applied["quality"]["valid"]


def SA_mod():
    from meshpipeline.engines.gmsh import shell_adapter as SA

    return SA


def test_a_large_hole_needs_the_users_size_for_a_washer():
    """SHMESH-45e: the count for a 46 mm hole at 10 mm: two rows, inside the user's 5..15 mm edges."""
    n, rows, ratio = ST._count(23.0, 10.0, 10.0, 5.0, 15.0)
    assert rows == 2 and 2 * math.pi * 23.0 / n >= 5.0 and (ratio - 1) * 23.0 / rows >= 5.0
    assert 2 * math.pi * 23.0 * ratio / n <= 0.98 * 15.0
    assert ST.WASHER_ACROSS_USER * 10.0 > 46.07 > ST.WASHER_ACROSS * 10.0


def test_a_square_hole_gets_a_square_washer(tmp_path):
    """SHMESH-45f, class A of a real column: four-sided holes of straight edges (square, diamond; our extraction keeps
    a round hole round - checked on plain, split, countersunk and counterbored 21 mm holes in a 19 mm plate - so these
    are the CAD's own) get a square ring, mapped, inside the user's edges."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt, gp_Vec

    from meshpipeline.cad import midsurface as ms

    shape = BRepPrimAPI_MakeBox(gp_Pnt(-175, -175, 0), 350, 350, 19.05).Shape()
    for (x, y), turn in (((-110, -110), 0), ((110, -110), 0), ((-110, 110), 45), ((110, 110), 45)):
        poly = BRepBuilderAPI_MakePolygon()
        for j in range(4):
            a = math.radians(turn + 45 + 90 * j)
            poly.Add(gp_Pnt(x + 9.3 * math.sqrt(2) * math.cos(a), y + 9.3 * math.sqrt(2) * math.sin(a), -1))
        poly.Close()
        tool = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(), gp_Vec(0, 0, 21)).Shape()
        shape = BRepAlgoAPI_Cut(shape, tool).Shape()
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "square.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["washers"] == 4 and s["no_washer"] == {}, s["hole_list"]
    # (SHMESH-48h: the plate is gridded, each square ring in a free box of the grid)
    assert all(h["shape"] == "square" and h["kind"] == "square ring in a free box of a gridded face"
               for h in s["hole_list"]), s["hole_list"]
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"]
    rows = {r["field"]: r for r in applied["requirements"]["rows"]}
    assert rows["max_edge"]["met"] is True
    # each hole's ring is a grid: every element touching a hole is a quad, and the plate's elements round the rings
    # are no worse for it (inside Nastran's limits above)
    pairs = SS._edges(mesh)
    loops = SS._loops(pairs[0][pairs[1] == 1])
    assert len(loops) == 5
    holes = sorted(loops, key=len)[:4]
    tri_nodes = {int(v) for t in mesh.trias for v in t}
    # (a ring's strips beside a poor element of the free field are meshed free again, SHMESH-40's check: here at most
    # one hole's)
    assert sum(1 for lp in holes if not (set(lp) & tri_nodes)) >= 3
    assert applied["quality"]["structure"]["mapped_faces"] >= 12


def test_round_and_square_holes_on_one_face_of_a_several_face_part(tmp_path):
    """SHMESH-45f/45g, a real column on 9e8fcbed: a face with round AND square holes was cut twice (its rings, then its
    square rings on the face the first cut had replaced - "Unknown OpenCASCADE entity of dimension 2"), and the guard
    kept the free mesh for every face. Now each face's washers are drawn in one cut, and a face's failure costs that
    face only."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
    from OCP.TopoDS import TopoDS_Compound

    def plate(z: float, squares: list, rounds: list):
        p = BRepPrimAPI_MakeBox(gp_Pnt(-175, -175, z), 350, 350, 19.05).Shape()
        for x, y in squares:
            poly = BRepBuilderAPI_MakePolygon()
            for j in range(4):
                a = math.radians(45 + 90 * j)
                poly.Add(gp_Pnt(x + 9.3 * math.sqrt(2) * math.cos(a), y + 9.3 * math.sqrt(2) * math.sin(a), z - 1))
            poly.Close()
            p = BRepAlgoAPI_Cut(p, BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(),
                                                         gp_Vec(0, 0, 21)).Shape()).Shape()
        for x, y in rounds:
            cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z - 1), gp_Dir(0, 0, 1)), 10.5, 21).Shape()
            p = BRepAlgoAPI_Cut(p, cyl).Shape()
        return p

    comp = TopoDS_Compound()
    bb = BRep_Builder()
    bb.MakeCompound(comp)
    bb.Add(comp, plate(0.0, [(140, -140), (-140, 140)], [(-140, -140), (140, 140)]))
    bb.Add(comp, plate(300.0, [(110, 110)], [(-110, -110), (110, -110)]))
    rec, surf = ms_extract(comp)
    brep = tmp_path / "two.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert "guard" not in s, s.get("guard")
    assert s["washers"] == 7 and s["no_washer"] == {}, s["hole_list"]
    assert sum(1 for h in s["hole_list"] if h.get("shape") == "square") == 3
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]


def test_a_top_plate_with_round_and_square_holes_is_gridded(tmp_path):
    """SHMESH-48h, a real column's top plates (19 mm, 6 round and 5 four-sided holes on one face): the square rings
    were drawn after the grid and left out of it, so the grid saw holes without a washer and left the plate free. Now
    each square ring takes a free box of the grid (its strips drawn in the box), and the plate is gridded with every
    line met."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec

    T_ = 19.05
    shape = BRepPrimAPI_MakeBox(gp_Pnt(-210, -160, 0), 420, 320, T_).Shape()
    for x, y in ((-150, -100), (0, -100), (150, -100), (-150, 100), (0, 100), (150, 100)):
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), 9.0,
                                                                T_ + 2).Shape()).Shape()
    for (x, y), turn in (((-75, -100), 0), ((75, -100), 0), ((-75, 100), 0), ((75, 100), 0), ((0, 0), 45)):
        poly = BRepBuilderAPI_MakePolygon()
        for j in range(4):
            a = math.radians(turn + 45 + 90 * j)
            poly.Add(gp_Pnt(x + 9.0 * math.sqrt(2) * math.cos(a), y + 9.0 * math.sqrt(2) * math.sin(a), -1))
        poly.Close()
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(),
                                                             gp_Vec(0, 0, T_ + 2)).Shape()).Shape()
    rec, surf = ms_extract(shape)
    brep = tmp_path / "top.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert "guard" not in s, s.get("guard")
    assert s["washers"] == 11 and s["no_washer"] == {}, s["hole_list"]
    assert "a hole without a washer in the face" not in (s.get("not_gridded") or {}), s.get("not_gridded")
    kinds = [h["kind"] for h in s["hole_list"]]
    assert kinds.count("square ring in a free box of a gridded face") == 5, kinds
    assert all(k.endswith("gridded face") for k in kinds), kinds
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
    assert all(r["met"] is not False for r in applied["requirements"]["rows"]), applied["requirements"]["rows"]
    again, _a = _mesh(brep, rec, _criteria())
    assert _sha(again) == _sha(mesh)


@pytest.mark.parametrize("limited", [False, True])
def test_holes_out_of_step_keep_their_grid(tmp_path, monkeypatch, limited):
    """SHMESH-48i, a real channel web: four 17.4 mm holes out of step, 5+ sizes from each other and the edges, whose
    box sides lie 5 mm apart in a chain (120, 125, 130, 135 mm along the web). Made one line at their mean, the last
    side moved 7.5 mm into its ring's room and the web was left free; now the chain is two lines, every ring keeps
    its room and the web is gridded with every line met."""
    monkeypatch.setattr(ST, "SNAP_LIMITS", limited)
    brep, rec = _plate_with_holes(tmp_path, [(97.3, 61.5, 17.4), (147.7, 140.0, 17.4), (152.7, 301.5, 17.4),
                                             (112.3, 220.0, 17.4)], size=(400.0, 363.0, 10.0))
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    too_near = "a ring too near an edge or another ring for its box" in (s.get("not_gridded") or {})
    assert too_near is not limited, s.get("not_gridded")
    if limited:
        assert s["washers"] == 4 and s["washers_ogrid"] + s["washers_boxed"] == 4, s
        assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
        assert all(r["met"] is not False for r in applied["requirements"]["rows"]), applied["requirements"]["rows"]


@pytest.mark.parametrize("join", [False, True])
def test_a_channels_web_and_flanges_are_gridded_across_their_joints(tmp_path, monkeypatch, join):
    """SHMESH-47c, a real column's channel (web and flanges with bolt holes, about 3.3 sizes from the flanges' free
    edges, joined through narrow strips): each face's lines ended on its joint and cut the strip on one side only
    (triangles there: the faces were left free), or a flange beside the web with holes was not gridded at all. Now the
    faces are cut across at the same places, the strips straight across, and all three are gridded with every line
    met."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    L, W, T_, F = 900.0, 363.0, 10.0, 100.0

    def box(x, y, z, dx, dy, dz):
        return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()

    shape = BRepAlgoAPI_Fuse(BRepAlgoAPI_Fuse(box(0, 0, 0, L, W, T_), box(0, -T_, 0, L, T_, F)).Shape(),
                             box(0, W, 0, L, T_, F)).Shape()
    for x, y in [(120.0 + 150 * i, 61.5) for i in range(5)] + [(208.0 + 150 * i, W - 61.5) for i in range(4)]:
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), 8.7,
                                                                T_ + 2).Shape()).Shape()
    for x, y0, d, z in [(200.0 + 320 * i, -T_ - 1, 17.4, F - 41.7) for i in range(3)] + \
                       [(300.0 + 450 * i, W - 1, 12.2, F - 42.1) for i in range(2)]:
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y0, z), gp_Dir(0, 1, 0)), d / 2,
                                                                T_ + 2).Shape()).Shape()
    rec, surf = ms_extract(shape)
    brep = tmp_path / "channel.brep"
    BRepTools.Write_s(surf, str(brep))
    monkeypatch.setattr(ST, "JOIN", join)
    monkeypatch.setattr(ST, "JOIN_STRIPS", join)      # (this channel's faces meet through narrow strips)
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert "guard" not in s, s.get("guard")
    if not join:
        # (as before: the faces' grids spoil the strips between them and are left out)
        assert s.get("not_gridded"), s
        return
    assert s["washers"] == 14 and s.get("not_gridded") in (None, {}), (s.get("not_gridded"), s.get("refused"))
    assert all(h["kind"].endswith("gridded face") for h in s["hole_list"]), s["hole_list"]
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
    assert all(r["met"] is not False for r in applied["requirements"]["rows"]), applied["requirements"]["rows"]
    again, _a = _mesh(brep, rec, _criteria())
    assert _sha(again) == _sha(mesh)


def ms_extract(shape):
    from meshpipeline.cad import midsurface as ms

    return ms.extract(shape, mm_per_unit=1.0)


@pytest.mark.parametrize("pins", [False, True])
def test_a_line_near_a_chamfers_end_does_not_cut_a_sliver(tmp_path, monkeypatch, pins):
    """SHMESH-48o, a real top plate: a hole's box line landed 0.6 mm past the end of a slanted outline piece and cut
    a sliver of the outline into a cell ("a cell edge 0.637 mm, under 4 mm"): the plate was left free. Here a corner
    cut off at 45 degrees ends 1.6 mm short of a 19.8 mm hole's box line; the chamfer's ends now hold the lines near
    them and the plate is gridded with every line met."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec

    monkeypatch.setattr(ST, "OUTLINE_PINS", pins)
    A, B, T_ = 457.0, 406.0, 19.0
    shape = BRepPrimAPI_MakeBox(gp_Pnt(-A / 2, -B / 2, 0), A, B, T_).Shape()
    for x in (-114.0, 0.0, 114.0):
        for y in (-171.1, 171.1):
            shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), 9.9,
                                                                    T_ + 2).Shape()).Shape()
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in ((-A / 2 - 1, -B / 2 - 1), (-A / 2 + 94.0, -B / 2 - 1), (-A / 2 - 1, -B / 2 + 94.0)):
        poly.Add(gp_Pnt(x, y, -1.0))
    poly.Close()
    shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(),
                                                         gp_Vec(0, 0, T_ + 2)).Shape()).Shape()
    rec, surf = ms_extract(shape)
    brep = tmp_path / "chamfer.brep"
    BRepTools.Write_s(surf, str(brep))
    _mesh_, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    small = [k for k in (s.get("not_gridded") or {}) if "a cell edge" in k]
    if not pins:
        assert small, s.get("not_gridded")
        return
    assert not s.get("not_gridded"), s.get("not_gridded")
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
    assert all(r["met"] is not False for r in applied["requirements"]["rows"]), applied["requirements"]["rows"]


def test_a_round_plate_with_a_bolt_circle_gets_rings_and_spokes(tmp_path):
    """SHMESH-49/49b: a round plate with its holes on one bolt circle is cut into a strip per hole (two lines parallel
    to its radial line, from the core to the rim) and rings round the bolt circle; each hole an O-grid in its strip,
    the core paved. Every washer is an O-grid, the strips are mapped, and the mesh keeps the user's edges."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 250.0, 20.0).Shape()
    for j in range(8):
        a = math.radians(22.5 + 45 * j)
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(190 * math.cos(a), 190 * math.sin(a), -1), gp_Dir(0, 0, 1)),
                                        11.0, 22.0).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    rec, surf = ms_extract(shape)
    brep = tmp_path / "round.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["washers"] == 8 and s["washers_ogrid"] == 8 and s["grids_freed"] == 0, s
    assert all(h["kind"] == "o-grid in a sector of a round plate" for h in s["hole_list"])
    assert s["mapped_faces"] >= 8 * 8
    q = applied["quality"]
    assert q["valid"] and q["within_nastran"]
    assert all(r["met"] is not False for r in applied["requirements"]["rows"])
    assert SS.measure(mesh)["holes_ringed"] >= 8      # (the measure counts the rim's own loop too)


def test_an_empty_curve_loop_costs_no_washer(web, monkeypatch):
    """SHMESH-45h, 8 of the 80 shell parts on 8b3ed13b: gmsh gives an empty curve loop for some faces, the hole record
    sampled it (numpy: "need at least one array to concatenate") and the part lost every washer to the free mesh."""
    import gmsh

    brep, rec = web
    _m, plain = _mesh(brep, rec, _criteria())
    real = gmsh.model.occ.getCurveLoops

    def with_empty(tag):
        tags, loops = real(tag)
        return list(tags) + [0], list(loops) + [[]]

    monkeypatch.setattr(gmsh.model.occ, "getCurveLoops", with_empty)
    _m2, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert "guard" not in s, s.get("guard")
    assert s["washers"] == plain["quality"]["structure"]["washers"] > 0


def test_a_base_plate_with_a_central_hole_keeps_rings_and_strips_inside_every_line(tmp_path):
    """SHMESH-49d..i, a real base plate (bolt holes about two sizes across round a central hole): square boxes with
    bowed sides (no 45 degree O-grid corner), the band inside the bolt circle paved, and the plate judged on its own
    elements after its holes are drawn again - rings and strips with every line met, or the free-face washers."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 250.0, 20.0).Shape()
    shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), 110.0,
                                                            22.0).Shape()).Shape()
    for j in range(8):
        a = math.radians(22.5 + 45 * j)
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(190 * math.cos(a), 190 * math.sin(a), -1), gp_Dir(0, 0, 1)),
                                        11.0, 22.0).Shape()
        shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    rec, surf = ms_extract(shape)
    brep = tmp_path / "base.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec, _criteria())
    s = applied["quality"]["structure"]
    assert s["washers_ogrid"] == 8 and s["grids_freed"] == 0, s
    assert len(mesh.trias) <= ST.POLAR_TRIAS
    assert applied["quality"]["valid"] and applied["quality"]["within_nastran"]
    assert all(r["met"] is not False and (r["met"] is True or r["field"] == "min_edge")
               for r in applied["requirements"]["rows"]), applied["requirements"]["rows"]
