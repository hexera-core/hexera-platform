"""Merging coplanar faces must change the geometry by NOTHING.

`render.exact_surface` exists to draw a delivered boundary without approximating it, so the only
claim it makes is that the surface it emits covers exactly what the faces it replaced covered. That
claim is worth nothing unless it is held here, because every other number the module reports - the
collapse ratio, the polygon count, the byte count - is attractive in the wrong direction. A merge
that quietly spans a gap looks BETTER on all three.

THE REGRESSION THAT MADE THIS FILE NECESSARY is `test_a_row_is_not_joined_across_a_gap`. The first
version grouped rows by a tolerance of 1e-10 m while sorting on the exact value, so two faces 44.9
mm apart in u, whose v0 differed by 1e-11 m, landed adjacent in the sort, passed the tolerance row
test, and were merged across the 44.9 mm of nothing between them - one rectangle covering 111x the
area of the two faces it replaced. Area alone catches it, which is why area is checked on every
case here and not only on the happy one.

AREA ALONE IS NOT ENOUGH THOUGH: an overlap and a matching gap cancel in a sum. So the coverage
test rasterises a group and asserts every cell is covered exactly once.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.render.boundary_index import open_index, write_index
from meshpipeline.render.exact_surface import (RECT_TOL, exact_surface, merge_rectangles)
from tests.foam_fixtures import write_row_of_hexes


# --------------------------------------------------------------------- the merge, on its own
def _area(rect):
    return float(((rect[:, 1] - rect[:, 0]).astype(np.float64)
                  * (rect[:, 3] - rect[:, 2])).sum())


def _f32(rows):
    a = np.asarray(rows, dtype=np.float32)
    return a[:, 0].copy(), a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy()


def test_a_tiled_rectangle_becomes_one_rectangle():
    # a 3 x 2 grid of unit squares tiles a 3 x 2 rectangle, which is one polygon and not six
    rows = [[u, u + 1, v, v + 1] for v in (0, 1) for u in (0, 1, 2)]
    rect, of_face = merge_rectangles(*_f32(rows))
    assert len(rect) == 1
    assert rect[0].tolist() == [0.0, 3.0, 0.0, 2.0]
    assert _area(rect) == pytest.approx(6.0, abs=1e-12)
    # every input face is accounted for, and they all went into that one rectangle
    assert sorted(set(of_face.tolist())) == [0]
    assert len(of_face) == len(rows)


def test_a_row_with_a_gap_stays_two_rectangles():
    rows = [[0, 1, 0, 1], [1, 2, 0, 1], [5, 6, 0, 1]]
    rect, _ = merge_rectangles(*_f32(rows))
    assert len(rect) == 2
    assert _area(rect) == pytest.approx(3.0, abs=1e-12)
    spans = sorted((float(r[0]), float(r[1])) for r in rect)
    assert spans == [(0.0, 2.0), (5.0, 6.0)]


def test_a_row_is_not_joined_across_a_gap():
    """THE REGRESSION. Two faces far apart in u whose v0 differ by a float hair.

    The values are the real ones off the delivered coolant, where this was found: u 19.368 mm and
    u 64.048 mm, v0 1.930759936e-06 and 1.930769940e-06 - 1e-11 m apart, which any tolerance loose
    enough to be useful would swallow. They are different rows and must stay two rectangles.
    """
    rows = [[0.064048498868942, 0.064249701797962, 0.000001930759936, 0.000202705996344],
            [0.019368199631572, 0.019569499418139, 0.000001930769940, 0.000202705996344]]
    u0, u1, v0, v1 = _f32(rows)
    rect, _ = merge_rectangles(u0, u1, v0, v1)
    got = _area(rect)
    want = float(((u1 - u0).astype(np.float64) * (v1 - v0)).sum())
    assert len(rect) == 2, "two faces 44.9 mm apart were merged into one rectangle"
    assert got == pytest.approx(want, rel=1e-9)
    # and the shape of the failure, stated so it cannot come back wearing a different number
    assert got < 1e-7, "the merged area is the two faces', not the 44.9 mm span between them"


def test_an_l_shape_is_covered_exactly_and_only_once():
    """An L cannot be one rectangle. It must still be covered exactly, with no overlap."""
    rows = [[0, 1, 0, 1], [1, 2, 0, 1], [2, 3, 0, 1], [0, 1, 1, 2], [0, 1, 2, 3]]
    rect, _ = merge_rectangles(*_f32(rows))
    assert len(rect) >= 2
    assert _area(rect) == pytest.approx(5.0, abs=1e-12)
    # rasterise: every unit cell the faces covered is covered by exactly one rectangle
    grid = np.zeros((3, 3), dtype=int)
    for r in rect:
        for i in range(int(r[0]), int(r[1])):
            for j in range(int(r[2]), int(r[3])):
                grid[i, j] += 1
    want = np.zeros((3, 3), dtype=int)
    for u0, u1, v0, v1 in rows:
        want[u0:u1, v0:v1] += 1
    assert np.array_equal(grid, want), "the rectangles do not cover the L exactly once"


def test_no_overlap_on_a_staircase():
    """A staircase of rows of different lengths - the shape a channel wall actually is."""
    rows = []
    for v in range(6):
        for u in range(6 - v):
            rows.append([u, u + 1, v, v + 1])
    rect, _ = merge_rectangles(*_f32(rows))
    assert _area(rect) == pytest.approx(float(len(rows)), abs=1e-12)
    grid = np.zeros((6, 6), dtype=int)
    for r in rect:
        grid[int(r[0]):int(r[1]), int(r[2]):int(r[3])] += 1
    assert grid.max() <= 1, "a cell is covered by two rectangles"
    assert int(grid.sum()) == len(rows), "a cell the faces covered is not covered"


def test_one_face_is_one_rectangle():
    rect, of_face = merge_rectangles(*_f32([[0, 1, 0, 1]]))
    assert len(rect) == 1
    assert of_face.tolist() == [0]


def test_no_faces_is_no_rectangles():
    rect, of_face = merge_rectangles(*_f32(np.empty((0, 4))))
    assert len(rect) == 0
    assert len(of_face) == 0


# ------------------------------------------------------ the shape test, where the area test is blind
def _saddle(w, length, h):
    """A quad on the four corners of a w x length bbox with the two DIAGONAL corners lifted by h.

    The case no area tolerance can see. Newell's two off-axis components are
        A_u = (W/2)(h0 - h1 - h2 + h3)     A_v = (L/2)(h0 + h1 - h2 - h3)
    so with h0 == h2 and h1 == h3 both vanish, |Newell| is exactly the bounding box and the unit
    normal is exactly the axis - at ANY h. It is a real population on the delivered plate: 99,295
    faces of 36,490,208 pass the area test without lying in a plane.
    """
    return np.array([[0.0, 0.0, h], [w, 0.0, 0.0], [w, length, h], [0.0, length, 0.0]])


@pytest.mark.parametrize("h", [1e-12, 1e-9, 1e-7, 1e-6, 1e-4, 1e-2])
def test_the_area_test_alone_cannot_see_a_saddle(h):
    """Not a test of the module - a test of WHY it needs a second predicate.

    If this ever starts failing, the area test has become sufficient and FLATNESS_M could go. It
    will not: the cancellation is exact algebra, not a tolerance.
    """
    P = _saddle(201.2998e-6, 201.2998e-6, h)
    av = 0.5 * np.cross(P, np.roll(P, -1, axis=0)).sum(axis=0)
    mag = float(np.linalg.norm(av))
    box = 201.2998e-6 * 201.2998e-6
    assert abs(mag - box) <= RECT_TOL * box, "the area test was supposed to be fooled"
    assert abs(av[2] / mag) == pytest.approx(1.0, abs=1e-15), "and the normal to look axis-aligned"
    # meanwhile the triangles a renderer actually draws cover more and more
    tri = np.cross(P[1:-1] - P[:1], P[2:] - P[:1])
    fan = 0.5 * float(np.linalg.norm(tri, axis=1).sum())
    if h >= 1e-4:
        assert fan > box * 1.01, "the fan should blow up where the Newell area does not"


def test_a_saddle_face_is_kept_whole_not_flattened(tmp_path):
    """The guard itself, through the real module on a real mesh.

    `shear_last_y` tilts the last cell's side faces; this asserts the stronger property that holds
    whatever the fixture contains - every face that WAS merged is exactly flat, so the merge moved
    no vertex at all.
    """
    for kw in ({}, {"shear_last_y": 0.3}, {"last_cell_length": 2.0}):
        pm = _built(tmp_path / ("s%d" % abs(hash(tuple(sorted(kw.items()))))), **kw)
        _rows, prov = exact_surface(pm, with_quality=False)
        assert prov["max_vertex_shift_m"] == 0.0, (
            "a merged rectangle moved a corner by %.3e m with %s"
            % (prov["max_vertex_shift_m"], kw))
        assert prov["flatness_m"] == 0.0


# ------------------------------------------------------------- the whole thing, on a real mesh
def _built(tmp_path: Path, **kw):
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm, **kw)
    write_index(pm)
    return pm


def _delivered_area(pm):
    """The area of the mesh's own drawable faces, independently of the merge."""
    ix = open_index(pm)
    pts, sizes, conn, offs = ix.points, ix.sizes, ix.conn, ix.offs
    total = 0.0
    for f in range(ix.n_faces):
        k = int(sizes[f])
        if k < 3:
            continue
        v = np.asarray(pts[conn[offs[f]:offs[f] + k].astype(np.int64)], dtype=np.float64)
        total += float(np.linalg.norm(0.5 * np.cross(v, np.roll(v, -1, axis=0)).sum(axis=0)))
    return total


def test_three_hexes_in_a_row_merge_to_their_six_sides(tmp_path):
    """Three unit cubes in a row have fourteen boundary faces and six flat sides.

    The wall patch is four coplanar 3 x 1 strips of three unit squares each, so a correct merge
    takes it from twelve faces to four rectangles; the inlet and outlet are one face each and
    cannot merge with anything. Anything other than six polygons means the merge is either joining
    faces that are not coplanar or failing to join ones that are.
    """
    pm = _built(tmp_path)
    rows, prov = exact_surface(pm, with_quality=False)
    by = {r["name"]: r for r in rows}
    assert set(by) == {"inlet", "outlet", "wall"}
    assert by["wall"]["face_count"] == 12
    assert by["wall"]["poly_count"] == 4
    assert by["inlet"]["poly_count"] == 1
    assert by["outlet"]["poly_count"] == 1
    assert prov["drawn_polygons"] == 6
    assert prov["delivered_faces"] == 14
    assert prov["area_drawn_m2"] == pytest.approx(14.0, abs=1e-12)
    assert prov["area_error"] < 1e-12


def test_the_area_is_the_mesh_s_own(tmp_path):
    """Whatever it merges, the area it draws is the area the mesh has."""
    for kw in ({}, {"last_cell_length": 2.0}, {"shear_last_y": 0.3},
               {"shear_last_y": 0.3, "last_cell_length": 2.0}, {"degenerate_wall_face": True}):
        pm = _built(tmp_path / ("c%d" % abs(hash(tuple(sorted(kw.items()))))), **kw)
        rows, prov = exact_surface(pm, with_quality=False)
        want = _delivered_area(pm)
        assert prov["area_drawn_m2"] == pytest.approx(want, rel=1e-12), kw
        assert prov["drawn_polygons"] <= prov["delivered_faces"], kw


def test_a_sheared_cell_is_not_merged_into_a_flat_one(tmp_path):
    """Shearing the last cell tilts its side faces off axis. Those must be kept whole.

    This is the test that fails if the axis threshold is loosened until it starts flattening real
    geometry: a sheared face drawn as an axis-aligned rectangle is in the wrong place.
    """
    flat = exact_surface(_built(tmp_path / "flat"), with_quality=False)[1]
    bent = exact_surface(_built(tmp_path / "bent", shear_last_y=0.3), with_quality=False)[1]
    assert flat["drawn_polygons"] == 6
    assert bent["drawn_polygons"] > flat["drawn_polygons"], \
        "a sheared cell merged as if it were flat"
    assert bent["area_error"] < 1e-12


def test_a_degenerate_face_is_dropped_not_drawn(tmp_path):
    """A face with two vertices is kept in the index's NUMBERING and must not be drawn.

    Same convention `index_surface` holds to: a face id is the boundary file's own, so the index
    keeps it and whoever draws decides. If this leaked, `face_count` would mean two different
    things on the two routes.
    """
    pm = _built(tmp_path, degenerate_wall_face=True)
    rows, prov = exact_surface(pm, with_quality=False)
    ix = open_index(pm)
    assert prov["delivered_faces"] < ix.n_faces
    assert prov["area_error"] < 1e-12


def test_the_buffers_parse_and_say_what_they_claim(tmp_path):
    """points and polys are raw buffers a browser reinterprets; a wrong count is a wrong picture."""
    rows, _ = exact_surface(_built(tmp_path), with_quality=False)
    for r in rows:
        pts = np.frombuffer(r["points"], dtype=np.float32).reshape(-1, 3)
        pol = np.frombuffer(r["polys"], dtype=np.uint32)
        at, n = 0, 0
        while at < pol.size:
            k = int(pol[at])
            assert k >= 3, r["name"]
            assert int(pol[at + 1:at + 1 + k].max()) < len(pts), r["name"]
            at += k + 1
            n += 1
        assert n == r["poly_count"], r["name"]


def test_a_merged_rectangle_faces_the_way_its_faces_did(tmp_path):
    """Winding. A rectangle wound the wrong way lights the inside of the part and leaves the
    outside black, and nothing about the area or the counts would notice."""
    rows, _ = exact_surface(_built(tmp_path), with_quality=False)
    ix = open_index(_built(tmp_path))
    for r in rows:
        pts = np.frombuffer(r["points"], dtype=np.float32).reshape(-1, 3)
        pol = np.frombuffer(r["polys"], dtype=np.uint32)
        at = 0
        while at < pol.size:
            k = int(pol[at])
            v = pts[pol[at + 1:at + 1 + k].astype(np.int64)].astype(np.float64)
            nrm = 0.5 * np.cross(v, np.roll(v, -1, axis=0)).sum(axis=0)
            centre = v.mean(axis=0)
            # these three cubes are convex and centred on (1.5, 0.5, 0.5), so an outward normal
            # has a positive component along the vector from the centre of the body to the face
            out = centre - np.array([1.5, 0.5, 0.5])
            assert float(np.dot(nrm, out)) > 0, "%s: a face is wound inward" % r["name"]
            at += k + 1
