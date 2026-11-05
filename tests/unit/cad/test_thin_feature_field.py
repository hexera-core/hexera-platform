# Responsibility: Verify the thin-feature probe measures known fixture geometry truthfully.
# Boundaries: measurement only - what the snappy engine DOES with a classification is
# tests/unit/engines/test_thin_layer_policy.py's subject.
from __future__ import annotations

import numpy as np

from meshpipeline.cad.thin_features import (
    CLASS_NORMAL,
    CLASS_RAZOR,
    CLASS_THIN,
    class_area_fractions,
    classify_faces,
    measure_from_triangles,
)

# -- procedural fixtures: geometry whose thickness is KNOWN by construction -------------------


def _sheet(z_of, nx, ny, L, W, flip):
    tris = []
    for i in range(nx):
        for j in range(ny):
            x0, x1 = L * i / nx, L * (i + 1) / nx
            y0, y1 = W * j / ny, W * (j + 1) / ny
            a = [x0, y0, z_of(x0)]
            b = [x1, y0, z_of(x1)]
            c = [x1, y1, z_of(x1)]
            d = [x0, y1, z_of(x0)]
            if flip:
                tris.append((a, c, b))
                tris.append((a, d, c))
            else:
                tris.append((a, b, c))
                tris.append((a, c, d))
    return tris


def razor_plate(t=1e-4, nx=20, ny=8, L=1.0, W=0.4):
    """Two parallel sheets a hair apart - every face's opposing surface is at distance t."""
    return (_sheet(lambda x: 0.0, nx, ny, L, W, False)
            + _sheet(lambda x: t, nx, ny, L, W, True))


def wedge(t_min=0.0005, t_max=0.08, nx=40, ny=8, L=1.0, W=0.4):
    """Two sheets converging linearly: local thickness h(x) = t_min + (t_max - t_min) x / L,
    razor-sharp at x=0 and chunky at x=L - a trailing edge in miniature."""
    def h(x):
        return t_min + (t_max - t_min) * x / L
    return (_sheet(lambda x: +0.5 * h(x), nx, ny, L, W, False)
            + _sheet(lambda x: -0.5 * h(x), nx, ny, L, W, True))


def chunky_box(s=1.0):
    from meshpipeline.cad.stl_io import _box_triangles
    return _box_triangles([0.0, 0.0, 0.0], [s, s, s])


# -- the probe --------------------------------------------------------------------------------


def test_razor_plate_measures_its_own_thickness():
    f = measure_from_triangles(razor_plate(t=1e-4))
    assert f.measured
    finite = np.isfinite(f.thickness_m)
    assert finite.all(), "every plate face sees the opposing sheet"
    assert abs(float(np.median(f.thickness_m)) - 1e-4) < 2e-5


def test_wedge_thickness_tracks_the_analytic_gradient():
    tris = wedge()
    f = measure_from_triangles(tris)
    assert f.measured
    T = np.asarray(tris, dtype=float)
    x = T.mean(axis=1)[:, 0]
    h = 0.0005 + (0.08 - 0.0005) * x
    band = (x > 0.05) & (x < 0.5) & np.isfinite(f.thickness_m)
    assert band.any()
    rel = np.abs(f.thickness_m[band] - h[band]) / h[band]
    assert float(np.median(rel)) < 0.5, "measured thickness must track the built-in gradient"


def test_chunky_box_reads_as_chunky():
    f = measure_from_triangles(chunky_box())
    assert f.measured
    # the opposing wall is a full box-length away wherever the probe finds one at all
    finite = np.isfinite(f.thickness_m)
    assert (f.thickness_m[finite] > 0.9).all()
    assert not f.sharp.any(), "a 90-degree box edge is not a sharp fold"


def test_degenerate_input_degrades_to_unmeasured_never_raises():
    f = measure_from_triangles([])
    assert not f.measured and f.n_triangles == 0
    z = [([0, 0, 0], [0, 0, 0], [0, 0, 0])]  # zero-area only
    f2 = measure_from_triangles(z)
    assert not f2.measured
    labels = classify_faces(f2, thin_below_m=1.0, razor_below_m=0.5)
    assert (labels == CLASS_NORMAL).all(), "unmeasured means NO thin features, never a guess"


# -- classification against known thresholds --------------------------------------------------


def test_razor_plate_classifies_razor_wall_to_wall():
    f = measure_from_triangles(razor_plate(t=1e-4))
    labels = classify_faces(f, thin_below_m=0.02, razor_below_m=0.01)
    fr = class_area_fractions(labels, f.area_m2)
    assert fr["razor"] == 1.0 and fr["normal"] == 0.0


def test_wedge_partitions_razor_then_thin_then_normal_along_the_gradient():
    tris = wedge()
    f = measure_from_triangles(tris)
    labels = classify_faces(f, thin_below_m=0.02, razor_below_m=0.005)
    fr = class_area_fractions(labels, f.area_m2)
    assert fr["razor"] > 0.0 and fr["thin"] > 0.0 and fr["normal"] > 0.5
    x = np.asarray(tris, dtype=float).mean(axis=1)[:, 0]
    assert x[labels == CLASS_RAZOR].max() < x[labels == CLASS_NORMAL].min(), (
        "the razor band must sit at the sharp end, the normal class at the thick end")
    assert x[labels == CLASS_THIN].mean() < x[labels == CLASS_NORMAL].mean()


def test_chunky_box_classifies_wholly_normal():
    f = measure_from_triangles(chunky_box())
    labels = classify_faces(f, thin_below_m=0.02, razor_below_m=0.005)
    assert class_area_fractions(labels, f.area_m2) == {"normal": 1.0, "thin": 0.0, "razor": 0.0}


def test_sharp_extension_pulls_the_wedge_band_into_razor():
    # a face that is SHARP and within the extension factor of the razor bar fails like the
    # edge it walks into, so it must be classed with it
    f = measure_from_triangles(wedge())
    strict = classify_faces(f, thin_below_m=0.02, razor_below_m=0.005, sharp_razor_factor=1.0)
    extended = classify_faces(f, thin_below_m=0.02, razor_below_m=0.005, sharp_razor_factor=3.0)
    a_strict = float(f.area_m2[strict == CLASS_RAZOR].sum())
    a_ext = float(f.area_m2[extended == CLASS_RAZOR].sum())
    assert a_ext > a_strict


def test_area_fractions_are_a_partition():
    f = measure_from_triangles(wedge())
    labels = classify_faces(f, thin_below_m=0.02, razor_below_m=0.005)
    fr = class_area_fractions(labels, f.area_m2)
    assert abs(sum(fr.values()) - 1.0) < 1e-9
