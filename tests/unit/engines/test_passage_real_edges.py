# Responsibility: Verify the passage measure counts a hex wall's cells by the polygons' own edges, never a fan's diagonals.
# Three cfMesh elbows with 12.7, 10.8 and 20.7 cells across their narrowest wall read 11.2, 9.5 and
# 18.3 (2026-10-04): the polyMesh boundary was fanned into triangles, and each quad's diagonal was
# counted as a cell edge. The first was refused for "11.2 cells across, under 12".
from __future__ import annotations

import numpy as np

import meshpipeline.engines.passage as P
import meshpipeline.engines.passage_flow as PF


def _square_duct_quads(n_across: int, n_along: int, h: float = 0.01):
    """The four walls of a square duct n_across cells wide, as h x h quads wound outward."""
    w = n_across * h
    pts: list[tuple[float, float, float]] = []
    index: dict[tuple[int, int, int], int] = {}

    def pid(x, y, z):
        key = (round(x / h), round(y / h), round(z / h))
        if key not in index:
            index[key] = len(pts)
            pts.append((x, y, z))
        return index[key]

    quads = []
    for i in range(n_along):
        x0, x1 = i * h, (i + 1) * h
        for j in range(n_across):
            a, b = j * h, (j + 1) * h
            quads.append([pid(x0, a, 0), pid(x1, a, 0), pid(x1, b, 0), pid(x0, b, 0)])       # z=0
            quads.append([pid(x0, a, w), pid(x0, b, w), pid(x1, b, w), pid(x1, a, w)])       # z=w
            quads.append([pid(x0, 0, a), pid(x0, 0, b), pid(x1, 0, b), pid(x1, 0, a)])       # y=0
            quads.append([pid(x0, w, a), pid(x1, w, a), pid(x1, w, b), pid(x0, w, b)])       # y=w
    return np.asarray(pts, dtype=float), quads


def _fan(quads):
    q = np.asarray(quads, dtype=np.int64)
    return np.concatenate([q[:, [0, 1, 2]], q[:, [0, 2, 3]]])


def test_a_hex_wall_is_counted_by_its_own_edges():
    pts, quads = _square_duct_quads(n_across=13, n_along=20)
    tris = _fan(quads)
    r = np.full(len(pts), 13 * 0.01 / 2.0)          # half the duct width at every wall point
    real = P.measure_passage(pts, tris, r, edges=P.polygon_edges(quads))
    assert real["median"] == 13.0 and real["min"] == 13.0, real
    # the fan's diagonals read the same wall coarser than it is - the defect this replaces
    fanned = P.measure_passage(pts, tris, r)
    assert fanned["p05"] < 12.0 < real["p05"], (fanned, real)


def test_a_triangulated_wall_is_measured_as_before():
    pts, quads = _square_duct_quads(n_across=10, n_along=10)
    tris = _fan(quads)
    r = np.full(len(pts), 0.05)
    assert P.measure_passage(pts, tris, r) == P.measure_passage(pts, tris, r,
                                                                 edges=P.triangle_edges(tris))


def test_the_polymesh_boundary_brings_its_real_edges(tmp_path):
    from tests.foam_fixtures import write_row_of_hexes
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    pts, tris, edges = P.boundary_of_polymesh(pm)
    assert len(tris) == 2 * 14 and len(edges) == 4 * 14
    # unit hexes: every real edge is 1 long, while a fan's diagonal is sqrt(2)
    assert np.allclose(P.mean_edge(pts, edges), 1.0)
    assert P.mean_edge(pts, P.triangle_edges(tris)).max() > 1.05
    wpts, wtris, wedges = P.boundary_of_polymesh(pm, wall_only=True)
    assert len(wedges) == 4 * 12 and wedges.max() < len(wpts)
    # the measure beside the mesh takes them: a one-cell duct reads 1 cell (radius ~ half a cell)
    out = P.passage_of_polymesh(tmp_path)
    assert out["passage_cells_across_local"]["median"] <= 1.5, out


def test_the_flow_reading_takes_the_wall_polygons_edges(tmp_path):
    from tests.foam_fixtures import write_row_of_hexes
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    pts, tris, owner, patches, edges, edge_owner = PF.boundary_by_patch_with_edges(pm)
    p2, t2, o2, pa2 = PF.boundary_by_patch(pm)
    assert np.array_equal(pts, p2) and np.array_equal(tris, t2) and np.array_equal(owner, o2)
    assert len(edges) == len(edge_owner) == 4 * 14
    wall = [k for k, p in enumerate(patches) if p["type"] == "wall"]
    assert np.isin(edge_owner, wall).sum() == 4 * 12
    assert np.allclose(P.mean_edge(pts, edges[np.isin(edge_owner, wall)])[np.unique(tris)], 1.0)
