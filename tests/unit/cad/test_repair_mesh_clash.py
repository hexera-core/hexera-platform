"""mesh_clash: which gmsh surfaces' triangles cross each other - pure numpy, no gmsh."""
from __future__ import annotations

import numpy as np

from meshpipeline.cad.repair.mesh_clash import clashing_surfaces


def _square(z: float, *, base: int, size: float = 1.0, shift=(0.0, 0.0)):
    """Two triangles covering a square at height z; node ids start at `base`."""
    x0, y0 = shift
    nodes = np.array([[x0, y0, z], [x0 + size, y0, z], [x0 + size, y0 + size, z],
                      [x0, y0 + size, z]])
    tris = np.array([[0, 1, 2], [0, 2, 3]]) + base
    return nodes, tris


def _wall(x: float, *, base: int):
    """A vertical square in the plane x = const spanning z in [-0.5, 0.5]."""
    nodes = np.array([[x, 0.2, -0.5], [x, 0.8, -0.5], [x, 0.8, 0.5], [x, 0.2, 0.5]])
    tris = np.array([[0, 1, 2], [0, 2, 3]]) + base
    return nodes, tris


def test_a_wall_through_a_floor_is_one_clashing_pair():
    floor_nodes, floor_tris = _square(0.0, base=0)
    wall_nodes, wall_tris = _wall(0.5, base=4)
    coords = np.vstack([floor_nodes, wall_nodes])
    tris = np.vstack([floor_tris, wall_tris])
    tags = np.array([1, 1, 2, 2])

    found = clashing_surfaces(coords, tris, tags)

    assert [c.surfaces for c in found] == [(1, 2)]
    assert found[0].hits > 0
    assert found[0].kind == "crossing"
    x, _y, z = found[0].point
    assert abs(x - 0.5) < 1e-9 and abs(z) < 1e-9


def test_neighbours_sharing_an_edge_do_not_clash():
    # Two squares meeting along x = 1, sharing the two nodes there: a conformal seam.
    coords = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0], [2, 1, 0]],
                      dtype=float)
    tris = np.array([[0, 1, 2], [0, 2, 3], [1, 4, 5], [1, 5, 2]])
    tags = np.array([1, 1, 2, 2])

    assert clashing_surfaces(coords, tris, tags) == ()


def test_coincident_faces_of_two_bodies_are_a_coplanar_clash():
    a_nodes, a_tris = _square(0.0, base=0)
    b_nodes, b_tris = _square(0.0, base=4, shift=(0.3, 0.3))
    coords = np.vstack([a_nodes, b_nodes])
    tris = np.vstack([a_tris, b_tris])
    tags = np.array([7, 7, 9, 9])

    found = clashing_surfaces(coords, tris, tags)

    assert [(c.surfaces, c.kind) for c in found] == [((7, 9), "coplanar")]


def test_parallel_faces_apart_do_not_clash():
    a_nodes, a_tris = _square(0.0, base=0)
    b_nodes, b_tris = _square(0.01, base=4)
    coords = np.vstack([a_nodes, b_nodes])
    tris = np.vstack([a_tris, b_tris])

    assert clashing_surfaces(coords, tris, np.array([1, 1, 2, 2])) == ()


def test_a_surface_folding_through_itself_is_not_reported_as_a_pair():
    # Same tag on both: gmsh's 2D stage owns that, and a self pair names no second face.
    floor_nodes, floor_tris = _square(0.0, base=0)
    wall_nodes, wall_tris = _wall(0.5, base=4)
    coords = np.vstack([floor_nodes, wall_nodes])
    tris = np.vstack([floor_tris, wall_tris])

    assert clashing_surfaces(coords, tris, np.array([3, 3, 3, 3])) == ()


def test_pairs_are_ranked_by_hits_and_capped():
    pieces = [_square(0.0, base=0)]
    tags = [1, 1]
    for n, x in enumerate((0.25, 0.5, 0.75)):
        pieces.append(_wall(x, base=4 * (n + 1)))
        tags += [10 + n, 10 + n]
    coords = np.vstack([p[0] for p in pieces])
    tris = np.vstack([p[1] for p in pieces])

    found = clashing_surfaces(coords, tris, np.array(tags), max_pairs=2)

    assert len(found) == 2
    assert all(c.surfaces[0] == 1 for c in found)


def test_empty_mesh_is_no_clash():
    assert clashing_surfaces(np.zeros((0, 3)), np.zeros((0, 3), dtype=int),
                             np.zeros((0,), dtype=int)) == ()


def test_surfaces_in_different_volume_components_are_never_compared():
    # gmsh tetrahedralises each connected group of volumes alone, so two bodies that share no face
    # may interpenetrate without failing the mesh: their crossing is not the cause.
    floor_nodes, floor_tris = _square(0.0, base=0)
    wall_nodes, wall_tris = _wall(0.5, base=4)
    coords = np.vstack([floor_nodes, wall_nodes])
    tris = np.vstack([floor_tris, wall_tris])
    tags = np.array([1, 1, 2, 2])

    apart = clashing_surfaces(coords, tris, tags, groups=np.array([0, 0, 1, 1]))
    together = clashing_surfaces(coords, tris, tags, groups=np.array([4, 4, 4, 4]))
    unowned = clashing_surfaces(coords, tris, tags, groups=np.array([-1, -1, -1, -1]))

    assert apart == () and unowned == ()
    assert [c.surfaces for c in together] == [(1, 2)]


def test_surface_components_join_volumes_that_share_a_surface():
    from meshpipeline.cad.repair.mesh_clash import surface_components

    owners = {1: [1], 2: [1, 2], 3: [2], 4: [3], 5: []}   # vols 1+2 share surface 2; vol 3 alone
    groups = surface_components(owners)

    assert groups[1] == groups[2] == groups[3]
    assert groups[4] != groups[1] and groups[4] >= 0
    assert groups[5] == -1


def test_coincident_faces_far_from_the_origin_still_clash():
    # Greptile #495: the 2D tolerance scaled with absolute coordinates, so the same overlap
    # translated to x = 1e6 was missed.
    a_nodes, a_tris = _square(0.0, base=0)
    b_nodes, b_tris = _square(0.0, base=4, shift=(0.3, 0.3))
    coords = np.vstack([a_nodes, b_nodes]) + np.array([1_000_000.0, 0.0, 0.0])
    tris = np.vstack([a_tris, b_tris])

    found = clashing_surfaces(coords, tris, np.array([7, 7, 9, 9]))

    assert [(c.surfaces, c.kind) for c in found] == [((7, 9), "coplanar")]


def test_a_dense_cell_of_one_surface_builds_no_candidate_pairs():
    # Greptile #495: every pair in a cell was built before the surface/component filter, so one
    # crowded cell could exhaust memory. Pairs of one surface, or of two components, are never built.
    from meshpipeline.cad.repair.mesh_clash import _candidate_pairs

    count = 3000
    lo = np.zeros((count, 3))
    hi = np.full((count, 3), 1e-3)
    same_tag = _candidate_pairs(lo, hi, tags=np.ones(count, dtype=np.int64),
                                groups=np.zeros(count, dtype=np.int64))
    split = _candidate_pairs(lo, hi, tags=np.arange(count, dtype=np.int64),
                             groups=np.arange(count, dtype=np.int64))
    assert len(same_tag) == 0 and len(split) == 0


def test_the_candidate_pairs_are_capped():
    from meshpipeline.cad.repair.mesh_clash import _candidate_pairs

    count = 400
    lo = np.zeros((count, 3))
    hi = np.full((count, 3), 1e-3)
    pairs = _candidate_pairs(lo, hi, tags=np.arange(count, dtype=np.int64),
                             groups=np.zeros(count, dtype=np.int64), max_pairs=1000)
    assert len(pairs) <= 1000


def test_a_spent_deadline_returns_promptly_with_no_clash():
    import time

    floor_nodes, floor_tris = _square(0.0, base=0)
    wall_nodes, wall_tris = _wall(0.5, base=4)
    coords = np.vstack([floor_nodes, wall_nodes])
    tris = np.vstack([floor_tris, wall_tris])

    found = clashing_surfaces(coords, tris, np.array([1, 1, 2, 2]),
                              deadline=time.monotonic() - 1.0)
    assert found == ()


def test_two_surfaces_folded_flat_across_a_shared_edge_are_a_fold():
    # Measured on abc/0059_00595955 ("overlapping facets on surface 370 surface 376"): triangles of
    # two surfaces share an edge and lie folded onto each other - dihedral 0. Sharing nodes, they
    # were never compared; gmsh's boundary check calls them overlapping facets.
    coords = np.array([[0, 0, 0], [1, 0, 0], [0.5, 1, 0], [0.4, 0.8, 0]], dtype=float)
    tris = np.array([[0, 1, 2], [0, 1, 3]])          # shared edge 0-1, both apexes on +y
    found = clashing_surfaces(coords, tris, np.array([370, 376]))
    assert [(c.surfaces, c.kind) for c in found] == [((370, 376), "fold")]


def test_a_sharp_but_open_edge_is_not_a_fold():
    # 5 degrees between the two triangles across their edge: sharp, not folded.
    import math

    ang = math.radians(5.0)
    coords = np.array([[0, 0, 0], [1, 0, 0], [0.5, 1, 0],
                       [0.5, math.cos(ang), math.sin(ang)]], dtype=float)
    tris = np.array([[0, 1, 2], [0, 1, 3]])
    assert clashing_surfaces(coords, tris, np.array([1, 2])) == ()


def test_one_facet_meshed_on_two_surfaces_is_a_duplicate():
    # Measured on abc/0059_00595955 (370/376): the "overlapping facets" were three triangles that
    # appear on BOTH surfaces - all three nodes shared. Never compared, because they share nodes.
    coords = np.array([[0, 0, 0], [1, 0, 0], [0.5, 1, 0], [3, 3, 3]], dtype=float)
    tris = np.array([[0, 1, 2], [2, 1, 0], [0, 1, 3]])
    found = clashing_surfaces(coords, tris, np.array([370, 376, 376]))
    assert [(c.surfaces, c.kind) for c in found] == [((370, 376), "duplicate")]
