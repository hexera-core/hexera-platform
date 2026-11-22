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


def _web(tmp_path, width: float, pitch: float, across: list[float]):
    """A channel web's look-alike: a 2000 mm plate, 10 thick, four clusters of 17.4 mm bolt holes, each cluster three
    columns `pitch` apart by three rows across the width."""
    holes = [(cx + dx, y, 17.4) for cx in (250.0, 750.0, 1250.0, 1750.0) for dx in (-pitch, 0.0, pitch) for y in across]
    return _plates(tmp_path, [(2000.0, width, 10.0, 0.0, holes)])


def test_a_web_with_room_keeps_its_grid_and_two_rings_round_every_hole(tmp_path):
    """SHMESH-51b: the grid and the rings together - holes 80 mm apart take two rings inside the web's grid."""
    brep, rec = _web(tmp_path, 300.0, 80.0, [70.0, 150.0, 230.0])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert not s.get("not_gridded"), s.get("not_gridded")
    assert all(h["kind"].endswith("gridded face") for h in s["hole_list"]), s["hole_list"][:2]
    assert s["bolt_rings"] == {2: 36}, s["bolt_rings"]
    assert s["mapped_faces"] >= 100
    _check(applied)


def test_rows_of_bolt_holes_60_mm_apart_keep_the_grid_and_two_rings(tmp_path):
    """SHMESH-51c, a real column's channel web (rows of three bolt holes about 60 mm apart at 10 mm, edges 5 to 15 mm):
    two rings round each hole left the web's grid with 4 mm strips between the holes' boxes and box sides counted 6,
    no two-to-one band - the web was paved free (2277d1ec) or kept its grid with one ring (5ad1646e). Planned together,
    each box leaves a whole cell to its neighbour and its sides take the band's count up to the user's longest edge:
    the grid AND two rings round every hole, nothing under the shortest edge."""
    brep, rec = _web(tmp_path, 250.0, 60.0, [65.0, 125.0, 185.0])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert not s.get("not_gridded"), s.get("not_gridded")
    assert all(h["kind"] == "free box in a gridded face" and h.get("box_band") == "made" for h in s["hole_list"])
    assert s["bolt_rings"] == {2: 36}, s["bolt_rings"]
    assert s["mapped_faces"] >= 100 and len(mesh.trias) == 0
    rows = {r["field"]: r for r in applied["requirements"]["rows"]}
    assert int(rows["min_edge"].get("outside") or 0) == 0
    _check(applied)


def test_a_web_too_tight_for_two_rings_in_its_grid_keeps_the_grid_with_one(tmp_path):
    """SHMESH-51b: holes 50 mm apart have room for two rings in a free face but not inside the grid's boxes; the web
    keeps its grid (as with one ring before) and one ring a hole, and each hole's line says why."""
    brep, rec = _web(tmp_path, 250.0, 50.0, [75.0, 125.0, 175.0])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert all(h["kind"].endswith("gridded face") for h in s["hole_list"])
    assert all(h.get("one_ring") == "one ring so its face keeps its grid" for h in s["hole_list"])
    assert s["mapped_faces"] >= 40
    assert any("a second would break the plate's straight grid" in n for n in mesh.notes), mesh.notes
    _check(applied)


def _channel(tmp_path, length: float, web: float, holes: list[tuple[float, float, float]]):
    """A channel (10 mm plates, flanges 100 tall along both long edges of the web), holes through its web,
    extracted to a mid-surface: the web's mid-face is `web` wide between the flanges' mid-planes."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    from meshpipeline.cad import midsurface as ms

    def unify(s):
        u = ShapeUpgrade_UnifySameDomain(s, True, True, True)
        u.Build()
        return u.Shape()

    w_ = web - 10.0
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), length, w_, 10.0).Shape()
    for y0 in (-10.0, w_):
        shape = unify(BRepAlgoAPI_Fuse(shape, BRepPrimAPI_MakeBox(gp_Pnt(0, y0, 0), length, 10.0, 100.0).Shape()).Shape())
    for x, y, d in holes:
        cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y - 5.0, -1), gp_Dir(0, 0, 1)), d / 2, 12.0).Shape()
        shape = unify(BRepAlgoAPI_Cut(shape, cyl).Shape())
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "midsurface.brep"
    BRepTools.Write_s(surf, str(brep))
    return brep, rec


def test_a_channel_web_with_an_end_column_keeps_its_grid_and_two_rings_round_every_hole(tmp_path):
    """SHMESH-51e, a channel web's bolt rows (three rows about 115 mm apart across, columns in pairs, an end column of
    four holes at each end: two 38 mm either side of the middle row, two 2 mm inside the outer rows), joined to its
    flanges. The end columns' lines ran the length of the web and ended on the other holes' two-ring boxes, cutting
    their sides into pieces (6 segments where the band needs 4), and the web's free cells were judged on gmsh's
    paving that the bands replace: the web lost its grid (b6196e02, and main with one ring). Now no line ends on a
    two-ring box (each stops at the last line before it), the box's sides hold its band's count and the cells beside
    take it, each box no wider than keeps its corner kites under the longest edge: the grid and two rings round every
    hole, no triangle, nothing past the user's edges."""
    mid = 180.0
    cols = [165.0, 325.0, 1075.0, 1235.0]
    holes = [(x, mid + dy, 17.5) for x in cols for dy in (-115.0, 0.0, 115.0)]
    holes += [(x, mid + dy, 17.5) for x in (60.0, 1340.0) for dy in (-113.0, -38.0, 38.0, 113.0)]
    brep, rec = _channel(tmp_path, 1400.0, 2 * mid, holes)
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert not s.get("not_gridded"), s.get("not_gridded")
    assert s["bolt_rings"] == {2: len(holes)}, s.get("bolt_rings")
    assert all(h.get("box_band") == "made" for h in s["hole_list"] if h.get("bolt"))
    assert s["mapped_faces"] >= 60 and len(mesh.trias) == 0, (s["mapped_faces"], len(mesh.trias))
    rows = {r["field"]: r for r in applied["requirements"]["rows"]}
    assert int(rows["min_edge"].get("outside") or 0) == 0 and int(rows["max_edge"].get("outside") or 0) == 0
    _check(applied)


def test_a_web_with_unequal_pitches_keeps_its_grid_and_two_rings_on_most_holes(tmp_path):
    """SHMESH-51d, a real channel web: three rows of bolt holes about 115 mm apart across, columns in pairs 160 mm apart
    along, the pairs 410 mm apart, an end column of four holes. Its boxes are not square in counts (sides of 4 and 6
    segments), so most two-to-one bands were refused and the whole web went back to one ring. Now a band takes sides
    of unequal counts, and only a hole whose band still cannot be made goes back to one ring: the grid stays and at
    least half the holes keep two (the centre row's boxes, cut by the end column's lines, still cannot)."""
    xs = [170.0 + 572.0 * p + d for p in range(4) for d in (0.0, 159.0)]
    holes = [(x, 181.5 + dy, 17.4) for x in xs for dy in (-116.0, 0.0, 116.0)]
    holes += [(x, 181.5 + dy, 17.4) for x in (67.0, xs[-1] + 103.0) for dy in (-114.0, -38.0, 38.0, 114.0)]
    brep, rec = _plates(tmp_path, [(xs[-1] + 170.0, 363.0, 10.0, 0.0, holes)])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert not s.get("not_gridded"), s.get("not_gridded")
    assert all(h["kind"].endswith("gridded face") for h in s["hole_list"])
    two = [h for h in s["hole_list"] if h["rings"] >= 2]
    assert len(two) >= len(s["hole_list"]) / 2, s.get("bolt_rings")      # (2277d1ec..11d4ccc3: none)
    assert all(h.get("one_ring") for h in s["hole_list"] if h["rings"] < 2)
    _check(applied)


def test_a_fault_after_meshing_is_put_on_the_two_ring_holes_whose_boxes_hold_it():
    """SHMESH-51g: elements past the limits in or beside two-ring bolt boxes name those holes (and only those); a
    fault away from every box names none, so the face is judged whole as before (SHMESH-51b)."""
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh import shell_adapter as SA

    def hole(x: float) -> SimpleNamespace:
        c = np.array([x, 0.0, 0.0])
        return SimpleNamespace(centre=c, box_corners=[c + np.array(d) for d in ((-28, -28, 0), (28, -28, 0),
                                                                                (28, 28, 0), (-28, 28, 0))])

    ws = [hole(0.0), hole(200.0), hole(400.0)]
    inside, beside = np.array([[10.0, 5.0, 0.0]]), np.array([[200.0, 70.0, 0.0]])
    assert SA._boxes_at(np.vstack([inside, beside]), ws, 1.0) == ws[:2]
    assert SA._boxes_at(np.vstack([inside, [[300.0, 0.0, 0.0]]]), ws, 1.0) is None      # (72 mm past a side: none)
    assert SA._boxes_at(np.array([[200.0, 120.0, 0.0]]), ws, 1.0) is None              # (two box widths out)
    assert SA._boxes_at(np.zeros((0, 3)), ws, 1.0) is None


def test_bolt_hole_rounds_are_counted_per_face_and_refused_keys_name_their_face():
    """SHMESH-51f: one face's rounds of one ring do not use up another's; a refused key names its sewn face."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    assert SA._rounds({3: 2}, 3) == 2 and SA._rounds({3: 2}, 4) == 0 and SA._rounds(1, 9) == 1
    assert SA._face_of_key(5) == 5 and SA._face_of_key(-6) == 5
    assert SA._face_of_key(-(5 + SA.BOLT_GRID_KEY) - 1) == 5


def test_a_freed_cell_is_found_again_by_its_key_when_the_tags_change():
    """SHMESH-51f: a cell freed on one pass is named by its sewn face, centre and area, not by gmsh's tag: drawn
    again in another order (other tags) it is the same cell; a cell cut otherwise is none."""
    import gmsh

    from meshpipeline.engines.gmsh import shell_adapter as SA

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)

        def draw(order: list[int]) -> dict[int, int]:
            gmsh.clear()
            rects = {0: (0.0, 0.0, 50.0, 30.0), 1: (60.0, 0.0, 50.0, 30.0), 2: (120.0, 0.0, 50.0, 30.0)}
            tag_of = {gmsh.model.occ.addRectangle(rects[k][0], rects[k][1], 0.0, rects[k][2], rects[k][3]): 7
                      for k in order}
            gmsh.model.occ.synchronize()
            return tag_of

        tag_of = draw([0, 1, 2])
        keys = SA._cell_keys(gmsh, tag_of, [2], 1.0)                 # (the middle one)
        tag_of = draw([2, 1, 0])
        assert SA._cells_of_keys(gmsh, tag_of, keys, 1.0) == {2}      # (now tag 2 again, by its place: the middle)
        tag_of = draw([1, 0, 2])
        assert SA._cells_of_keys(gmsh, tag_of, keys, 1.0) == {1}
        tag_of = draw([1, 0, 2])
        assert SA._cells_of_keys(gmsh, dict.fromkeys(tag_of, 8), keys, 1.0) == set()   # (another sewn face)
        assert SA._cells_of_keys(gmsh, tag_of, [], 1.0) == set()
    finally:
        gmsh.finalize()


def test_a_freed_cell_is_let_go_when_a_face_its_freeing_came_from_changes():
    """SHMESH-51f: a cell freed beside another face's free face is kept across a change to a third face, and let go
    when its own face or that face has its holes drawn again or its rings changed."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    k = (4, 0.0, 0.0, 0.0, 100.0, (4, 7))
    assert SA._keys_kept({k}, {9}) == {k}
    assert SA._keys_kept({k}, {7}) == set() and SA._keys_kept({k}, {4}) == set()
    body = {4: "a", 7: "a", 9: "a", 12: "b"}.get
    assert SA._keys_kept({k}, {9}, body) == set() and SA._keys_kept({k}, {12}, body) == {k}   # (a ring changed in its body)


def test_rounds_of_one_ring_go_on_while_they_send_holes_and_blame_every_hole_at_once():
    """SHMESH-51h: with faults gathered a face is not stopped after BOLT_HOLE_ROUNDS rounds (a real web had 41 made
    bands thrown away for 5 holes when its fourth round was refused), only after HOLE_ROUNDS_MAX; and a round puts the
    faults it can on their holes even when others lie on no box (those are judged on the next pass)."""
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh import shell_adapter as SA

    assert SA._round_left({3: ST.BOLT_HOLE_ROUNDS}, 3) and not SA._round_left({3: SA.HOLE_ROUNDS_MAX}, 3)
    assert not SA._round_left(ST.BOLT_HOLE_ROUNDS, 3)                   # (rounds for the whole model: the fixed cap)
    c = np.zeros(3)
    w = SimpleNamespace(centre=c, box_corners=[c + np.array(d) for d in ((-28, -28, 0), (28, -28, 0), (28, 28, 0),
                                                                         (-28, 28, 0))])
    pts = np.array([[5.0, 5.0, 0.0], [500.0, 0.0, 0.0]])
    assert SA._boxes_at(pts, [w], 1.0) is None and SA._boxes_at(pts, [w], 1.0, partial=True) == [w]


def test_a_line_held_back_from_a_bolt_box_ends_on_a_node_of_its_side(tmp_path):
    """SHMESH-51i: a web whose middle row of bolt holes sits 25 mm along from its outer rows, with an end column of
    four holes whose lines run between the rows: each middle box side line runs into an outer box column, is held back
    and ended in a T on the line across there, cutting it 25 + 31 mm where the box side holds 4 segments. The cell
    between could not map, gmsh paved it, and every round of one ring put the faults on the next holes until none kept
    two (f22b2a3c: 0 of 41). Now the T moves on to a node of the box side: the grid maps and every hole keeps two."""
    holes = [(140.0 + 160.0 * k + (25.4 if y == 180.0 else 0.0), y, 17.4) for k in range(11) for y in (65.0, 180.0, 295.0)]
    holes += [(x, y, 17.4) for x in (40.0, 1900.0) for y in (66.8, 141.9, 218.1, 293.2)]
    brep, rec = _plates(tmp_path, [(2000.0, 361.9, 10.0, 0.0, holes)])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert s["bolt_rings"] == {2: len(holes)}, s.get("bolt_rings")
    assert len(mesh.trias) == 0 and applied["quality"]["passes"] == ["made"], applied["quality"]["passes"]
    _check(applied)


def test_one_ring_in_a_two_ring_box_is_one_row_out_to_the_two_rings_outer_circle():
    """SHMESH-51j: a bolt hole sent to one ring for a fault in its rings keeps its two-ring box, its count and its outer
    circle; it takes one row of quads from the hole out to that circle when that row is inside the solver's limits, the
    quad lines and the user's edges (a 17.4 mm hole, 8 round, out to 19 mm at 5 to 15 mm: 6.8 / 14.9 / 10.3 mm quads,
    corners 67.5 and 112.5 degrees). A row too wide for the longest edge, or a hole edge under the shortest, is not
    taken: that hole falls back as before."""
    assert ST._one_row_fits(8.7, 8, 19.0, 5.0, 15.0)
    assert not ST._one_row_fits(8.7, 8, 26.1, 5.0, 15.0)           # (its outer edge 20.5 mm, over 15)
    assert not ST._one_row_fits(8.7, 12, 19.0, 5.0, 15.0)          # (its hole edge 4.55 mm, under 5)
    w = ST.Washer(face=1, owner=0, hole=[1], centre=np.zeros(3), normal=np.array([0.0, 0.0, 1.0]),
                  xaxis=np.array([1.0, 0.0, 0.0]), r=8.7, R=19.0, n=8, rows=1, out_mm=14.9, kept=True,
                  plain=(8, 1, 1.55))
    assert w.boxed2 and not w.radii
    # (SHMESH-51b takes the whole face: a kept hole goes back to its plan by size too)
    assert ST.one_ring(w, 1.0, "one ring so its face keeps its grid") and not w.kept and not w.boxed2
    assert w.n == 8 and abs(w.R - 1.55 * 8.7) < 1e-9


def test_a_band_moved_past_the_longest_edge_goes_back_to_where_it_was_made(tmp_path, monkeypatch):
    """SHMESH-51k/51l: a two-ring box's band is checked where it is made (solver's limits, quad lines, the user's
    longest edge: a 56 mm box at 4 segments a side has 14.4 mm kite edges and 14.7 mm ring chords at 5 to 15 mm). Its
    box's side nodes lie on cut lines and the relaxing after it may move them: on the real column a kite came out
    16.02 mm (a side node 1.6 mm out) and its hole was sent to one ring. Held fast (51k), they could not even out the
    cells beside the box and a 12-hole plate lost half its bands. Now they move as before (still movable here), and a
    band whose quad a move puts past the solver's limits or over the longest edge takes its nodes back."""
    from meshpipeline.engines import shell_quality as SQ
    from meshpipeline.engines.gmsh import shell_adapter as SA

    holes = [(100.0 + 100.0 * k, 100.0, 17.4) for k in range(9)]
    brep, rec = _plates(tmp_path, [(1000.0, 200.0, 10.0, 0.0, holes)])
    seen: list = []
    real = SQ.relax_past

    def push(xyz, quads, trias, movable, **kw):
        # (a relaxing that moves the movable nodes on the boxes' sides 1.6 mm outward, square to the side)
        xyz, n = real(xyz, quads, trias, movable, **kw)
        if not seen:
            seen.append(np.array(movable))
            xyz = np.array(xyz)
            for x, y, _d in holes:
                rel = xyz[:, :2] - (x, y)
                side = movable & (np.abs(np.abs(rel).max(axis=1) - 28.0) < 0.5)
                ax = np.argmax(np.abs(rel), axis=1)
                for i in np.nonzero(side)[0]:
                    xyz[i, ax[i]] += 1.6 * np.sign(rel[i, ax[i]])
        return xyz, n

    monkeypatch.setattr(SQ, "relax_past", push)
    monkeypatch.setattr(SA, "MESH_DEADLINE_S", 0)       # (meshed here, not in a forked child)
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert seen and seen[0].any()
    xyz, Q = mesh.xyz, mesh.quads
    cen = xyz[Q].mean(axis=1)
    band = np.zeros(len(Q), dtype=bool)
    for x, y, _d in holes:
        rel = cen[:, :2] - (x, y)
        band |= (np.abs(rel).max(axis=1) <= 30.0) & (np.linalg.norm(rel, axis=1) > 19.5)
    assert band.sum() >= 12 * len(holes)            # (each band: 8 side quads and 4 corner kites)
    ed = np.linalg.norm(xyz[Q] - xyz[np.roll(Q, -1, axis=1)], axis=2)
    assert float(ed[band].max()) <= 15.0, float(ed[band].max())
    assert s["bolt_rings"] == {2: len(holes)}, s.get("bolt_rings")


def _channel_both(tmp_path, length: float, web: float, web_holes: list[tuple[float, float, float]],
                  flange_holes: list[tuple[float, float, float]]):
    """A channel as _channel (10 mm plates, flanges 100 tall along both long edges of the web), with holes through
    its web (x, y from the first flange's mid-plane, d) and through its first flange (x, z up from the web's
    mid-plane, d)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    from meshpipeline.cad import midsurface as ms

    def unify(s):
        u = ShapeUpgrade_UnifySameDomain(s, True, True, True)
        u.Build()
        return u.Shape()

    w_ = web - 10.0
    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), length, w_, 10.0).Shape()
    for y0 in (-10.0, w_):
        shape = unify(BRepAlgoAPI_Fuse(shape, BRepPrimAPI_MakeBox(gp_Pnt(0, y0, 0), length, 10.0, 100.0).Shape()).Shape())
    for x, y, d in web_holes:
        cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y - 5.0, -1), gp_Dir(0, 0, 1)), d / 2, 12.0).Shape()
        shape = unify(BRepAlgoAPI_Cut(shape, cyl).Shape())
    for x, z, d in flange_holes:
        cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, -11.0, z + 5.0), gp_Dir(0, 1, 0)), d / 2, 12.0).Shape()
        shape = unify(BRepAlgoAPI_Cut(shape, cyl).Shape())
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "midsurface.brep"
    BRepTools.Write_s(surf, str(brep))
    return brep, rec


def test_a_joint_cut_inside_a_bolt_box_column_lands_on_its_nodes(tmp_path):
    """SHMESH-51m/51n, the real column's long plate: a web's row of bolt holes 57.2 mm from its joint with a flange
    (a 29.2 mm cell between each 56 mm two-ring box and the joint), the flange's own bolt holes 38.1 mm either side of
    each web hole's column. The flange's box sides cut the joint inside the web's box column (pieces of about 13 + 31
    + 13 mm), which 4 segments under 15 mm cannot cover: the web's box side was given 6 and its band refused ("sides
    [4, 4, 6, 4]: a quad past the solver's own limits"), and the flange's cells beside the joint were left with
    opposite sides of 4 and 6 - 6b492f51: one ring round all 15 holes after 8 passes. Now each box side that would land
    inside the other face's box column lands on a node of that box's side (its last piece slanted): one pass, two
    rings round every hole, no triangle, nothing outside the user's edges."""
    xs = [150.0 + 250.0 * k for k in range(5)]
    brep, rec = _channel_both(tmp_path, 1300.0, 300.0, [(x, 57.2, 17.4) for x in xs],
                              [(x + s, 50.0, 17.4) for x in xs for s in (-38.1, 38.1)])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert s["bolt_rings"] == {2: 15}, s.get("bolt_rings")
    assert applied["quality"]["passes"] == ["made"], applied["quality"]["passes"]
    assert all(h.get("box_band") == "made" for h in s["hole_list"] if h.get("bolt"))
    assert len(mesh.trias) == 0
    rows = {r["field"]: r for r in applied["requirements"]["rows"]}
    assert int(rows["min_edge"].get("outside") or 0) == 0 and int(rows["max_edge"].get("outside") or 0) == 0
    _check(applied)


def test_a_strip_between_a_bolt_box_and_a_joint_takes_the_boxs_count(tmp_path):
    """SHMESH-51m/51n, the real column's 12-hole plate: a flange's row of bolt holes 45.1 mm up from its joint with
    the web (a 56.0 x 17.1 mm strip between each two-ring box and the joint), the web's own bolt holes 38.1 mm either
    side of each column. The strip's joint side was a chain the box's 4 could not cover, so the box's side took 6
    (the strip mapped [6, 2, 6, 2]) and its band was refused - 6b492f51: one ring round 14 of the 15 holes, 12
    triangles. Now the chain lands on the box's nodes and its pieces take their count from the box: one pass, every
    band made, no triangle."""
    xs = [150.0 + 250.0 * k for k in range(5)]
    brep, rec = _channel_both(tmp_path, 1300.0, 300.0, [(x + s, 57.2, 17.4) for x in xs for s in (-38.1, 38.1)],
                              [(x, 45.1, 17.4) for x in xs])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert s["bolt_rings"] == {2: 15}, s.get("bolt_rings")
    assert applied["quality"]["passes"] == ["made"], applied["quality"]["passes"]
    assert all(h.get("box_band") == "made" for h in s["hole_list"] if h.get("bolt"))
    assert len(mesh.trias) == 0
    _check(applied)


def test_two_rings_never_cost_triangles_one_ring_would_not_have(monkeypatch):
    """SHMESH-51o: the faces tried with one ring are those whose mid-face holds two-ring bolt holes and triangles of
    its own (then every such face, when triangles are left anywhere); none without triangles. The mesh with one
    ring is kept only when it is no worse on the solver's limits and the user's lines. (Where the triangles lie -
    SHMESH-51o5's near its rings - is tested on its own below.)"""
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh import shell_adapter as SA

    monkeypatch.setattr(SA, "GUARD_NEAR", 0.0)

    applied = {"quality": {"structure": {"hole_list": [
        {"face": "Mid_1", "bolt": "one of 4 alike holes in a row", "rings": 2},
        {"face": "Mid_2", "bolt": "one of 4 alike holes in a row", "rings": 1},
        {"face": "Mid_3", "rings": 2}]}}}
    owners = ["Mid_1", "Mid_2", "Mid_3", "Mid_1", "Mid_4"]
    tri = SimpleNamespace(quads=np.zeros((2, 4)), trias=np.zeros((2, 3)), mid=["Mid_1", "Mid_2", "Mid_1", "Mid_4"])
    assert SA._tria_faces(tri, applied, owners) == [0, 3]
    off = SimpleNamespace(quads=np.zeros((2, 4)), trias=np.zeros((1, 3)), mid=["Mid_1", "Mid_2", "Mid_4"])
    assert SA._tria_faces(off, applied, owners) == [] and SA._tria_faces(off, applied, owners, True) == [0, 3]
    none = SimpleNamespace(quads=np.zeros((2, 4)), trias=np.zeros((0, 3)), mid=["Mid_1", "Mid_2"])
    assert SA._tria_faces(none, applied, owners, True) == []

    def rec(past: int, beyond: int, met: int) -> dict:
        return {"quality": {"valid": True, "nastran_past": {"skew": past}},
                "requirements": {"rows": [{"field": "limit:max_edge", "id": "e", "beyond": beyond, "met": met > 0}]}}

    assert SA._not_worse(rec(0, 0, 1), 0, rec(0, 2, 0), 0)
    assert not SA._not_worse(rec(0, 2, 0), 0, rec(0, 0, 1), 0)
    assert not SA._not_worse(rec(1, 0, 1), 0, rec(0, 0, 1), 0)


def test_lines_nearer_than_a_sliver_are_one_line_in_a_face_with_bolt_holes():
    """SHMESH-51p: a box side whose limits stop 0.005 mm short of a straight edge stood apart from it and left a
    0.005 mm cell edge (a face of a real column was refused its grid for it). With `tiny` (faces with bolt holes) the
    side is drawn on to the edge; without it (every other face) it stays where it was, as before."""
    vals = [(0.0, "edge"), (0.005, "0"), (56.0, "1")]
    lim = {"0": (0.005, 3.0), "1": (53.0, 59.0)}
    apart = ST._cluster(vals, 7.5, lim, 4.4)
    assert [round(at, 3) for at, _e, _s in apart] == [0.0, 0.005, 56.0]
    one = ST._cluster(vals, 7.5, lim, 4.4, tiny=1.0)
    assert [(round(at, 3), e, sides) for at, e, sides in one] == [(0.0, True, [0]), (56.0, False, [1])]
    assert ST._tiny([SimpleWasher({"bolt": "a row"})], 10.0) == 1.0 and ST._tiny([SimpleWasher({})], 10.0) == 0.0


def test_boxes_out_of_step_across_a_joint_meet_at_a_corner(tmp_path):
    """SHMESH-51r: the flange's bolt holes 44 mm either side of each web hole's column: their 56 mm boxes overlap the
    web's by 12 mm across the joint. Either side landing on the other box's nodes left one box's chain past what its
    4 segments cover (the real column's 12-hole plate: a 59.7 mm cell, [17.7, 42.0]); 47b9b675 sent 5 of the 15 holes
    to one ring. Now a side first lands on the other box's own corner (the two lines meet at one point of the joint,
    both chains whole): one pass, two rings round all 15, no triangle, nothing under the shortest edge."""
    xs = [150.0 + 250.0 * k for k in range(5)]
    brep, rec = _channel_both(tmp_path, 1300.0, 300.0, [(x, 57.2, 17.4) for x in xs],
                              [(x + s, 50.0, 17.4) for x in xs for s in (-44.0, 44.0)])
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert s["bolt_rings"] == {2: 15}, s.get("bolt_rings")
    assert applied["quality"]["passes"] == ["made"], applied["quality"]["passes"]
    assert len(mesh.trias) == 0
    rows = {r["field"]: r for r in applied["requirements"]["rows"]}
    assert int(rows["min_edge"].get("outside") or 0) == 0
    _check(applied)


def test_a_cad_piece_of_a_hole_is_no_cell_edge_at_planning(tmp_path, monkeypatch):
    """SHMESH-51q, three plates of the real column: a bolt hole's circle came with a CAD piece of 0.005 mm (0.17 mm on
    another), the planning counted it as a cell edge under 4 mm and the plate was never gridded ("a cell would come out
    too small ... a cell edge 0.005 mm") - so its ring never asked for the hole to be drawn again as one circle. Now a
    piece of a hole's own circle is no planning reason: the plate is cut into its grid and the hole is drawn again."""
    import math

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec

    from meshpipeline.cad import midsurface as ms

    def hole(x, y, d, cuts):
        circ = gp_Circ(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)), d / 2)
        angs = [math.radians(a) for a in cuts] + [math.radians(cuts[0]) + 2 * math.pi]
        mw = BRepBuilderAPI_MakeWire()
        for a, b in zip(angs[:-1], angs[1:], strict=True):
            mw.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(circ, a, b, True).Value()).Edge())
        return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(mw.Wire()).Face(), gp_Vec(0, 0, 12)).Shape()

    shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 560.0, 200.0, 10.0).Shape()
    for k in range(6):
        shape = BRepAlgoAPI_Cut(shape, hole(80.0 + 80.0 * k, 100.0, 19.8,
                                            [81.5, 81.53, 260.0] if k == 2 else [0.0, 180.0])).Shape()
    rec, surf = ms.extract(shape, mm_per_unit=1.0)
    brep = tmp_path / "split.brep"
    BRepTools.Write_s(surf, str(brep))
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert not s.get("not_gridded"), s.get("not_gridded")
    assert s["bolt_rings"] == {2: 6} and len(mesh.trias) == 0, (s.get("bolt_rings"), len(mesh.trias))
    _check(applied)
    # (the hole never drawn again: still no planning refusal for its piece - the face is judged on its mesh)
    monkeypatch.setattr(ST, "RESEAT", False)
    _m2, applied2 = _mesh(brep, rec)
    assert not any("a cell edge" in str(k) for k in (applied2["quality"]["structure"].get("not_gridded") or {}))


def test_a_grid_with_no_triangle_comes_back_against_a_free_mesh_with_triangles():
    """SHMESH-48x3 (the user's choice, 18 Nov): a grid refused for its share of elements under the user's shortest edge
    (a real column's plate: 2,974 elements, 0 triangles, 10.5% short) came back only when that share was within 1.5
    times its free mesh's, whatever triangles the free mesh had. Now a grid with no triangle comes back when its free
    mesh has triangles and the shortest-edge line is no worse with it (the free mesh has elements under it too, or the
    grid none); the words say how many are under it. A grid with triangles, or a free mesh with none, is judged as
    before."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    grid = {"elements": 2974, "triangles": 0, "short": 314}
    ok, why = SA._grid_back(grid, {"elements": 3100, "triangles": 40, "short": 120})
    assert ok and "314 of its 2974 elements under your shortest edge" in why
    assert SA._grid_back(grid, {"elements": 3100, "triangles": 0, "short": 120}) == (False, "")
    assert SA._grid_back({**grid, "triangles": 30}, {"elements": 3100, "triangles": 40, "short": 120})[0] is False
    assert SA._grid_back(grid, {"elements": 3100, "triangles": 40, "short": 0})[0] is False   # (the line met free)
    assert SA._grid_back({"elements": 1000, "triangles": 0, "short": 20}, {"elements": 1000, "triangles": 0,
                                                                          "short": 20}) == (True, "")


def test_the_one_ring_trial_is_judged_face_by_face_and_keeps_the_grids():
    """SHMESH-51o3: the one-ring trial may not lose grids (no fewer than GUARD_MAPPED of the mapped faces): on a lab
    channel one ring took 4 triangles off a flange and lost the web's grid (448 mapped faces to 119, 2 elements under
    the shortest edge to 500)."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    def rec(mapped: int) -> dict:
        return {"quality": {"valid": True, "nastran_past": {}, "structure": {"mapped_faces": mapped}},
                "requirements": {"rows": []}}

    assert SA._not_worse(rec(448), 0, rec(448), 0) and not SA._not_worse(rec(119), 0, rec(448), 0)
    assert SA.GUARD_PER_FACE


def test_the_first_one_ring_trial_is_the_face_with_the_most_triangles_alone():
    """SHMESH-51o4, the real column: the guard's faces were the long plate (18 triangles of its own with two rings),
    the 12-hole plate (12) and another plate (10). One ring on all three, then face by face, cost two restarts (642 s
    of a 900 s limit) and ended on the long plate alone (the 12-hole plate's triangles were the long plate's doing).
    Now the first trial is the face with the most triangles of its own, alone; then, while time allows, every face."""
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh import shell_adapter as SA

    owners = ["Mid_A", "Mid_B", "Mid_C", "Mid_C", "Mid_D"]
    mid = ["Mid_A"] * 3 + ["Mid_A"] * 12 + ["Mid_B"] * 10 + ["Mid_C"] * 18
    mesh = SimpleNamespace(quads=np.zeros((3, 4)), trias=np.zeros((40, 3)), mid=mid)
    assert SA._first_trial(mesh, owners, [0, 1, 2, 3]) == [2, 3]          # (Mid_C: both its sewn faces)
    assert SA._first_trial(mesh, owners, [0, 1]) == [0]                   # (Mid_A 12 against Mid_B 10)
    none = SimpleNamespace(quads=np.zeros((3, 4)), trias=np.zeros((0, 3)), mid=["Mid_A"] * 3)
    assert SA._first_trial(none, owners, [0, 4]) == [0, 4]                # (no face has its own: all of them)
    # (SHMESH-51o5: a trial starts from the CAD's holes, as SHMESH-51b starts again)
    assert not SA.GUARD_CARRY and not SA.GUARD_CARRY_GRIDS


def test_a_face_takes_one_ring_only_where_its_own_triangles_drop():
    """SHMESH-51o5, the real column on a36f42e7: the second trial (one ring round the other bolt faces) took 8
    triangles off the run and none off its own faces (A 0 to 0, B 10 to 10) - kept, it cost the 12-hole plate its two
    rings. A trial is kept only when every face it switches has fewer triangles of its own; a face is a candidate only
    with triangles of its own near its two-ring holes."""
    from collections import Counter
    from types import SimpleNamespace

    from meshpipeline.engines.gmsh import shell_adapter as SA

    assert SA._own_gain(Counter({"A": 0, "B": 10}), Counter({"A": 0, "B": 10}), ["A", "B"]) == ([], False)
    assert SA._own_gain(Counter({"C": 18}), Counter(), ["C"]) == (["C"], True)
    assert SA._own_gain(Counter({"A": 12, "B": 10}), Counter({"B": 10}), ["A", "B"]) == (["A"], False)
    applied = {"quality": {"structure": {"hole_list": [
        {"face": "Mid_1", "bolt": "a row", "rings": 2, "at_mm": [0.0, 0.0, 0.0], "diameter_mm": 17.4,
         "planned_width_mm": 10.5}]}}}
    xyz = np.array([[30.0, 0.0, 0.0], [31.0, 0.0, 0.0], [30.0, 1.0, 0.0],
                    [300.0, 0.0, 0.0], [301.0, 0.0, 0.0], [300.0, 1.0, 0.0]])
    mesh = SimpleNamespace(quads=np.zeros((0, 4), dtype=int), trias=np.array([[0, 1, 2], [3, 4, 5]]), xyz=xyz,
                           mid=["Mid_1", "Mid_1"])
    assert SA._near_trias(mesh, applied) == Counter({"Mid_1": 1})      # (30 mm from the hole: in reach; 300: not)
    far = SimpleNamespace(quads=mesh.quads, trias=mesh.trias[1:], xyz=xyz, mid=["Mid_1"])
    assert SA._tria_faces(far, applied, ["Mid_1"]) == [] and SA._tria_faces(mesh, applied, ["Mid_1"]) == [0]


def _plates_sq(tmp_path, plates):
    """Plates [(L, W, T, z, holes)] as one compound: a hole (x, y, d) is round, (x, y, a, b) an a x b rectangle."""
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
    shape = None
    for L, W, T, z, holes in plates:
        shape = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, z), L, W, T).Shape()
        for h in holes:
            if len(h) == 3:
                tool = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(h[0], h[1], z - 1), gp_Dir(0, 0, 1)), h[2] / 2,
                                                T + 2).Shape()
            else:
                tool = BRepPrimAPI_MakeBox(gp_Pnt(h[0] - h[2] / 2, h[1] - h[3] / 2, z - 1), h[2], h[3], T + 2).Shape()
            shape = BRepAlgoAPI_Cut(shape, tool).Shape()
        bb.Add(comp, shape)
    rec, surf = ms.extract(comp if len(plates) > 1 else shape, mm_per_unit=1.0)
    brep = tmp_path / "midsurface.brep"
    BRepTools.Write_s(surf, str(brep))
    return brep, rec


def _squares(applied) -> list[dict]:
    return [h for h in applied["quality"]["structure"]["hole_list"] if h.get("shape") == "square"]


def test_a_square_hole_lined_up_with_one_in_another_plate_is_a_bolt_hole(tmp_path):
    """SHMESH-51o7 (a): 18 mm square holes (a carriage bolt's) through two plates line up: read as bolt holes, their
    square rings take a second row of near-square cells where it fits."""
    hs = [(80.0 + 90.0 * k, 100.0, 18.0, 18.0) for k in range(4)]
    brep, rec = _plates_sq(tmp_path, [(420.0, 200.0, 10.0, 0.0, hs), (420.0, 200.0, 10.0, 40.0, hs)])
    _mesh_, applied = _mesh(brep, rec)
    sq = _squares(applied)
    assert len(sq) == 8 and all(h.get("bolt", "").startswith("it lines up with a hole in another plate") for h in sq)
    assert all(h["planned_rows"] == 2 and h["rings"] == 2 for h in sq), [(h["planned_rows"], h.get("rings")) for h in sq]
    _check(applied)


def test_a_square_hole_of_bolt_size_beside_round_bolt_holes_is_a_bolt_hole(tmp_path):
    """SHMESH-51o7 (b): a plate with a row of 19.8 mm round bolt holes and 18 mm square holes (inscribed circle within
    15% of the round ones): the squares are read as bolt holes, in words that say so."""
    rd = [(60.0 + 70.0 * k, 70.0, 19.8) for k in range(5)]
    sq_ = [(90.0 + 90.0 * k, 200.0, 18.0, 18.0) for k in range(3)]
    brep, rec = _plates_sq(tmp_path, [(420.0, 280.0, 10.0, 0.0, rd + sq_)])
    _mesh_, applied = _mesh(brep, rec)
    sq = _squares(applied)
    assert len(sq) == 3 and all(h.get("bolt") == "it is a square hole of bolt size on a plate with bolt holes"
                                for h in sq), [h.get("bolt") for h in sq]
    _check(applied)


def test_a_lone_square_hole_and_a_slot_stay_as_they_were(tmp_path):
    """SHMESH-51o7: a square hole with no bolt hole beside it and nothing lined up stays as before (not a bolt hole,
    one row); a 2.5 to 1 slot beside round bolt holes is no square the square plan takes, and is no bolt hole."""
    brep, rec = _plates_sq(tmp_path, [(300.0, 200.0, 10.0, 0.0, [(150.0, 100.0, 18.0, 18.0)])])
    _mesh_, applied = _mesh(brep, rec)
    sq = _squares(applied)
    assert len(sq) == 1 and not sq[0].get("bolt") and sq[0]["planned_rows"] == 1
    rd = [(60.0 + 70.0 * k, 70.0, 19.8) for k in range(5)]
    brep2, rec2 = _plates_sq(tmp_path, [(420.0, 280.0, 10.0, 0.0, rd + [(200.0, 200.0, 45.0, 18.0)])])
    _mesh2, applied2 = _mesh(brep2, rec2)
    slot = [h for h in applied2["quality"]["structure"]["hole_list"] if h["diameter_mm"] > 30.0]
    assert len(slot) == 1 and not slot[0].get("bolt"), slot


class SimpleWasher:
    def __init__(self, info: dict) -> None:
        self.info = info


def test_a_small_bolt_hole_keeps_two_rings_under_a_flexible_shortest_edge():
    """SHMESH-51o52: a 12.24 mm bolt hole with a 5 mm shortest edge has no two rings inside it (nor inside its 4 mm
    band); with the edge flexible the rings are planned under it, every quad within BOLT_ASPECT."""
    from meshpipeline.engines.gmsh import shell_structured as ST

    r = 12.24 / 2
    assert ST._count_bolt(r, 3.2, 10.0, 5.0, 15.0) == ST.NO_TWO_EDGES
    assert ST._count_bolt(r, 3.2, 10.0, 5.0 * (1 - ST.LOW_BAND), 15.0) == ST.NO_TWO_EDGES
    got = ST._count_bolt(r, 3.2, 10.0, None, 15.0)
    assert isinstance(got, tuple) and got[1] == 2
    radii = [1.0, *got[3], got[2]]
    widths = [(b - a) * r for a, b in zip(radii, radii[1:])]
    assert min(widths) > 2.0 and max(widths) < 15.0


def test_rows_go_even_where_a_row_in_ratio_would_come_under_the_shortest_edge():
    """SHMESH-51o75: a ring sent back to its plan by size alone is drawn with even rows where a row in ratio would be
    under the user's shortest edge (the real column's 17.42 mm holes: a 10.47 mm band in ratio gave a 4.22 mm inner
    row under 5 mm; even, 5.24 mm each), in ratio where both rows clear it, and in ratio where even rows would leave a
    quad past BOLT_ASPECT."""
    r, R = 8.71, 8.71 + 10.47
    ratio = ST._row_radii(r, R, 2, 8, 0.0)
    assert ratio[1] - ratio[0] == pytest.approx(4.22, abs=0.01)
    even = ST._row_radii(r, R, 2, 8, 5.0)
    assert [round(b - a, 3) for a, b in zip(even, even[1:], strict=False)] == [5.235, 5.235]
    assert ST._row_radii(r, R, 2, 8, 4.0) == ratio                      # both rows in ratio clear 4 mm
    assert ST._row_radii(r, R, 1, 8, 5.0) == [r, R]                     # one row: nothing to spread
    assert ST._row_radii(1.0, 30.0, 2, 8, 20.0) == ST._row_radii(1.0, 30.0, 2, 8, 0.0)   # even rows past BOLT_ASPECT: ratio



def test_a_band_quad_moved_under_the_shortest_edge_is_sent_back():
    """SHMESH-51o79: a band quad made with every edge at or over the user's shortest edge (5 mm) is sent back to where
    it was made once a move brings an edge of its under it - the row from a ring to its box, 9 mm wide made, 4 mm
    after its box side node moved in (on the real column 196 such edges sat at 3.0 to 5.0 mm). A quad made under the
    edge is not (the guard keeps what was made), and with the rule off only the solver's limits and the longest edge
    count, as before."""
    from meshpipeline.engines.gmsh import shell_adapter as SA

    made = np.array([[0.0, 0, 0], [9, 0, 0], [9, 9, 0], [0, 9, 0], [20, 0, 0], [24, 0, 0], [24, 9, 0], [20, 9, 0]])
    quads = np.array([[0, 1, 2, 3], [4, 5, 6, 7]])
    now = made.copy()
    now[1, 0] = now[2, 0] = 4.0                     # (the first quad pulled to 4 mm wide)
    now[5, 0] = now[6, 0] = 23.5                    # (the second, made 4 mm wide, a little narrower)
    assert SA._band_bad(now, quads, made, 15.0, 5.0).tolist() == [True, False]
    assert SA._band_bad(made, quads, made, 15.0, 5.0).tolist() == [False, False]
    old = SA.BAND_LOW_GUARD
    try:
        SA.BAND_LOW_GUARD = False
        assert SA._band_bad(now, quads, made, 15.0, 5.0).tolist() == [False, False]
    finally:
        SA.BAND_LOW_GUARD = old
    assert SA._band_bad(now, quads, made, 15.0, None).tolist() == [False, False]


def test_rings_planned_under_the_band_still_go_even_where_even_rows_clear_the_shortest_edge():
    """SHMESH-51o75 on the flexible band's plan (SHMESH-51o52): a 12.24 mm hole whose rim edges are under the user's
    5 mm is planned with the band let down; its rows are still judged against the 5 mm - even where even rows clear it
    (the real column's took 4.49 / 7.78 mm in ratio where 6.13 each fit)."""
    plain = ST._count_bolt(6.12, 4.8, 10.0, None, 15.0)
    even = ST._count_bolt(6.12, 4.8, 10.0, None, 15.0, even_mm=5.0)
    assert isinstance(plain, tuple) and isinstance(even, tuple)
    rows = [(b - a) * 6.12 for a, b in zip([1.0, *even[3], even[2]], [*even[3], even[2]], strict=False)]
    assert min(rows) >= 5.0 and abs(rows[0] - rows[1]) < 1e-6
    rows_p = [(b - a) * 6.12 for a, b in zip([1.0, *plain[3], plain[2]], [*plain[3], plain[2]], strict=False)]
    assert min(rows_p) < 5.0                          # (in ratio, the inner row was under the edge)
    assert even[:3] == plain[:3]                      # (same count, rows and outer circle: only the rows move)
