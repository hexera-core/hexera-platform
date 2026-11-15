"""One point deep inside each passage of a staged fluid surface (cad/fluid_passages.interior_seeds).

WHY THERE ARE SEEDS AT ALL. A mesher that cuts the coolant out of a solid block asks, cell by cell,
whether a cell centre is inside the coolant surface. OpenFOAM's `topoSet surfaceToCell
useSurfaceOrientation` answers out of an octree whose leaves can be larger than the feature they
straddle, and on job 6ddb58aa - ColdPlateAssembly_CLEAN.step, 283,410,955 cells, 3.94 h on
n2-highmem-64 - it tagged 247.998 mm3 of bulk METAL as coolant: 6,669 cells in four lumps, three of
them a single background cell, one of them a block 40.0018 x 40.0065 background cells square in
plan reaching 4.2 mm into solid metal, every one with a generalised winding number of 0.0000
against BOTH shells of the surface. The delivered coolant was in six pieces where the part has two,
and the mesh was refused.

Handed to `regionToCell`, a seed per passage says "the coolant is the cells connected to HERE", so
a lump with no face path to one is not coolant. Measured in OpenFOAM v2412 on the engine's own
rendered dictionary: a fluid set of 9,064 cells over three shells, two of them seeded, came out at
9,000 with the 64-cell shell moved to the metal, and fluid_to_solid / solid_to_fluid stayed equal
at 3,000 each - serial and under 4-rank MPI alike.

WHAT THESE TESTS ARE ABOUT. A wrong seed is worse than no seed: the same OpenFOAM run measured that
a seed in a cell the query did not tag empties the coolant set and still exits 0, and a seed outside
the mesh is a FOAM FATAL ERROR. So the contract is "a point whose own CELL the mesher will agree is
coolant, or nothing at all", and every test here is about one of the two halves of that.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.cad.fluid_passages import (
    MAX_PIECES,
    SEED_CLEARANCE_CELLS,
    _point_tri_distance,
    interior_seeds,
)
from meshpipeline.cad.stl_io import _box_triangles

#: 1 mm, so every clearance below reads as millimetres and as cells at once.
BG = 1.0e-3


def _cube(lo_mm: float, hi_mm: float, at=(0.0, 0.0, 0.0)) -> list:
    lo = tuple(at[k] + lo_mm * 1e-3 for k in range(3))
    hi = tuple(at[k] + hi_mm * 1e-3 for k in range(3))
    return _box_triangles(lo, hi)


def test_a_surface_in_two_pieces_gets_one_seed_each_at_the_middle_of_each():
    tris = _cube(10.0, 30.0) + _cube(40.0, 50.0)
    seeds = interior_seeds(tris, background_m=BG)
    assert len(seeds) == 2
    # Largest passage first, as `read_passages` orders them.
    assert seeds[0]["volume_m3"] == pytest.approx(20e-3 ** 3)
    assert seeds[1]["volume_m3"] == pytest.approx(10e-3 ** 3)
    assert np.allclose(seeds[0]["point_m"], (0.020, 0.020, 0.020))
    assert np.allclose(seeds[1]["point_m"], (0.045, 0.045, 0.045))
    # The deepest point of a 20 mm cube is 10 mm from its wall, and of a 10 mm cube 5 mm.
    assert seeds[0]["clearance_m"] == pytest.approx(0.010)
    assert seeds[1]["clearance_m"] == pytest.approx(0.005)


def test_a_passage_too_narrow_to_clear_the_bar_gives_no_seeds_at_all():
    """NOT A PARTIAL ANSWER. One passage without a trustworthy point means the surface is not one
    this understands well enough to name points in, and a `regionToCell` run missing one passage's
    seed deletes that passage from the coolant - which is worse than the islands it was removing."""
    narrow = 2.0 * SEED_CLEARANCE_CELLS * BG * 1e3 - 0.4      # just under 4 cells across
    tris = _cube(10.0, 30.0) + _cube(40.0, 40.0 + narrow)
    assert interior_seeds(tris, background_m=BG) == []
    # ...and the same surface at a cell small enough for that passage does get both.
    assert len(interior_seeds(tris, background_m=BG / 4.0)) == 2


def test_the_bar_is_half_the_planners_four_cell_floor():
    """A passage exactly four cells across is the narrowest the planner will size, and its deepest
    point is exactly two cells from the wall. That is the bar, so such a passage passes."""
    across = 2.0 * SEED_CLEARANCE_CELLS * BG * 1e3            # 4 cells across, in mm
    seeds = interior_seeds(_cube(10.0, 10.0 + across), background_m=BG)
    assert len(seeds) == 1
    assert seeds[0]["clearance_m"] == pytest.approx(SEED_CLEARANCE_CELLS * BG)


def test_a_piece_standing_inside_another_is_part_of_its_wall_and_not_a_passage():
    """The same rule `read_passages` uses, because `expect_fluid_pieces` is counted by it and the
    caller only uses the seeds when the two counts agree. A rod up the middle of a passage is one
    passage with a hole in it, so it gets ONE seed - and that seed must not be inside the rod."""
    tris = _cube(0.0, 40.0) + _cube(18.0, 22.0)
    seeds = interior_seeds(tris, background_m=BG)
    assert len(seeds) == 1
    assert seeds[0]["volume_m3"] == pytest.approx(40e-3 ** 3 - 4e-3 ** 3)
    p = np.asarray(seeds[0]["point_m"])
    assert not np.all((p > 0.018) & (p < 0.022)), "the seed is inside the rod"
    assert seeds[0]["clearance_m"] >= SEED_CLEARANCE_CELLS * BG


def test_an_inside_out_surface_is_read_rather_than_assumed():
    """Stepping the wrong way off the wall puts every seed in the metal. The direction comes from
    the piece's own signed volume, so a surface wound inside-out - a real failure this package has
    measured, 95.16% wrong by volume while checkMesh said Mesh OK - still seeds correctly."""
    flipped = [[t[0], t[2], t[1]] for t in _cube(10.0, 30.0)]
    seeds = interior_seeds(flipped, background_m=BG)
    assert len(seeds) == 1
    assert np.allclose(seeds[0]["point_m"], (0.020, 0.020, 0.020))


def test_nothing_is_named_on_a_surface_with_more_pieces_than_the_reader_will_read():
    tris = [t for k in range(MAX_PIECES + 1)
            for t in _cube(0.0, 2.0, at=(k * 0.005, 0.0, 0.0))]
    assert interior_seeds(tris, background_m=BG) == []


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_cell_size_that_is_not_a_size_names_no_points(bad):
    assert interior_seeds(_cube(10.0, 30.0), background_m=bad) == []


def test_an_empty_surface_names_no_points():
    assert interior_seeds([], background_m=BG) == []


def test_the_clearance_is_measured_to_the_triangles_and_not_to_their_corners():
    """`_closest_approach` above may use vertex distance because it reports a number for a human.
    This one decides whether a seed is trusted, and vertex distance OVER-states clearance: on one
    big triangle the nearest corner is far away while the surface is right there."""
    tri = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]])
    p = np.array([0.3, 0.3, 0.01])
    assert _point_tri_distance(p, tri)[0] == pytest.approx(0.01)
    assert float(np.linalg.norm(tri.reshape(-1, 3) - p, axis=1).min()) > 0.4
