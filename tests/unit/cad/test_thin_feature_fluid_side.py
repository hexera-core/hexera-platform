# Responsibility: Verify the thin-feature probe reads thinness on the FLUID side when it knows
# which side that is - a solid plate keeps its layers, a slot does not - and skips the faces the
# fluid never touches; and that it measures the triangles the snappy staging actually writes.
# Boundaries: the probe and the classifier; the layer policy's counts are tested elsewhere.
from __future__ import annotations

import numpy as np

from meshpipeline.cad import thin_features as TF


def _sheet(z, n_up, size=0.02, k=6):
    """A k x k grid of triangle pairs on the plane z, normals +z (n_up) or -z."""
    tris = []
    xs = np.linspace(0.0, size, k + 1)
    for i in range(k):
        for j in range(k):
            a = (xs[i], xs[j], z)
            b = (xs[i + 1], xs[j], z)
            c = (xs[i + 1], xs[j + 1], z)
            d = (xs[i], xs[j + 1], z)
            if n_up:
                tris += [(a, b, c), (a, c, d)]
            else:
                tris += [(a, c, b), (a, d, c)]
    return tris


def _two_faces(gap, *, facing):
    """Two parallel sheets `gap` apart. facing=False: a solid plate (normals point away from each
    other, out of the material); facing=True: a slot (normals point at each other, into it)."""
    lower = _sheet(0.0, n_up=facing)
    upper = _sheet(gap, n_up=not facing)
    return np.asarray(lower + upper, float)


CELL = 0.002          # razor below one cell; thin below two stacks / two cells
THIN, RAZOR = 0.006, CELL


def _classes(tris, wet):
    f = TF.measure_from_triangles(tris, wet_sides=wet)
    return TF.classify_faces(f, thin_below_m=THIN, razor_below_m=RAZOR), f


def test_a_solid_plate_a_few_cells_thick_keeps_full_layers_when_the_fluid_side_is_known():
    tris = _two_faces(0.004, facing=False)      # 4 mm plate, wetted on both outer faces
    n = len(tris)
    wet = np.ones(n, dtype=np.int8)             # each face's fluid is on its normal side
    labels, f = _classes(tris, wet)
    assert np.isfinite(f.thickness_m).all(), "the plate's own thickness is still read"
    assert np.isinf(f.fluid_gap_m).all(), "no opposing face across the fluid"
    assert (labels == TF.CLASS_NORMAL).all(), (
        "prism stacks grow out of the plate on both faces - they never meet")
    # without the sides, the same plate reads thin (the old, orientation-blind reading)
    old = TF.classify_faces(TF.measure_from_triangles(tris), thin_below_m=THIN, razor_below_m=RAZOR)
    assert (old == TF.CLASS_THIN).all()


def test_a_slot_of_the_same_width_is_thin_on_the_fluid_side():
    tris = _two_faces(0.004, facing=True)       # 4 mm of fluid between two facing walls
    wet = np.ones(len(tris), dtype=np.int8)
    labels, f = _classes(tris, wet)
    assert np.allclose(f.fluid_gap_m, 0.004)
    assert (labels == TF.CLASS_THIN).all(), "the two stacks grow into the slot and collide"


def test_a_plate_thinner_than_a_cell_is_still_razor():
    tris = _two_faces(0.001, facing=False)
    labels, _ = _classes(tris, np.ones(len(tris), dtype=np.int8))
    assert (labels == TF.CLASS_RAZOR).all(), "a plate under one cell cannot be castellated"


def test_faces_the_fluid_never_touches_are_neither_measured_nor_measured_against():
    # a hollow shell's skins 1 mm apart, the inner one facing a sealed space (wet 0): the outer
    # skin is as thick as the space behind it, not razor-thin
    tris = _two_faces(0.001, facing=False)
    n = len(tris)
    wet = np.ones(n, dtype=np.int8)
    wet[n // 2:] = 0                            # the upper sheet: the sealed side's skin
    labels, f = _classes(tris, wet)
    assert (labels[: n // 2] == TF.CLASS_NORMAL).all()
    assert np.isinf(f.thickness_m[n // 2:]).all() and (f.area_m2[n // 2:] == 0.0).all()


def test_wet_sides_must_match_the_triangles():
    tris = _two_faces(0.004, facing=False)
    try:
        TF.measure_from_triangles(tris, wet_sides=np.ones(3, dtype=np.int8))
    except ValueError:
        return
    raise AssertionError("a reading of another surface must not be applied silently")


def test_the_probe_reads_the_triangles_the_staging_writes(tmp_path):
    # a degenerate facet (two equal corners) is dropped by the snappy surface prep; the field must
    # drop it too, or the per-triangle labels shift and the whole layer policy switches off
    from meshpipeline.cad.stl_io import _write_solid
    good = [tuple(map(tuple, t)) for t in _two_faces(0.004, facing=False)]
    bad = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    path = tmp_path / "s.stl"
    with path.open("w") as fh:
        _write_solid(fh, "body", good[:5] + [bad] + good[5:])
    staged = TF.staged_triangles(path)
    assert len(staged) == len(good)
