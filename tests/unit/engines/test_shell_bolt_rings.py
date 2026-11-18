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


def test_a_bands_nodes_stay_where_the_band_put_them(tmp_path, monkeypatch):
    """SHMESH-51k: a two-ring box's band is checked where it is made (solver's limits, quad lines, the user's longest
    edge: a 56 mm box at 4 segments a side has 14.4 mm kite edges and 14.7 mm ring chords at 5 to 15 mm). Its box's
    side nodes lie on cut lines, free to move in the face's plane, and the relaxing after it moved them: on the real
    column a kite came out 16.02 mm and its hole was sent to one ring. Now no node of a made band is movable."""
    from meshpipeline.engines import shell_quality as SQ

    holes = [(100.0 + 100.0 * k, 100.0, 17.4) for k in range(9)]
    brep, rec = _plates(tmp_path, [(1000.0, 200.0, 10.0, 0.0, holes)])
    seen: list = []
    real = SQ.relax_past

    def spy(xyz, quads, trias, movable, **kw):
        seen.append((np.array(xyz), np.array(quads), np.array(movable)))
        return real(xyz, quads, trias, movable, **kw)

    from meshpipeline.engines.gmsh import shell_adapter as SA

    monkeypatch.setattr(SQ, "relax_past", spy)
    monkeypatch.setattr(SA, "MESH_DEADLINE_S", 0)       # (meshed here, not in a forked child: the spy sees it)
    mesh, applied = _mesh(brep, rec)
    s = applied["quality"]["structure"]
    assert s["bolt_rings"] == {2: len(holes)}, s.get("bolt_rings")
    assert seen
    xyz, Q, movable = seen[-1]
    cen = xyz[Q].mean(axis=1)
    band = np.zeros(len(Q), dtype=bool)
    for x, y, _d in holes:
        rel = cen[:, :2] - (x, y)
        band |= (np.abs(rel).max(axis=1) <= 28.0) & (np.linalg.norm(rel, axis=1) > 19.5)
    assert band.sum() >= 12 * len(holes)            # (each band: 8 side quads and 4 corner kites)
    assert not movable[np.unique(Q[band])].any()
    ed = np.linalg.norm(mesh.xyz[mesh.quads] - mesh.xyz[np.roll(mesh.quads, -1, axis=1)], axis=2)
    assert float(ed.max()) <= 15.0
    _check(applied)
