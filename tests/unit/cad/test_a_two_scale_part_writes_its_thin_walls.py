"""A part with two scales of passage gets its thin walls written out, beside the coolant surface.

WHY THIS TEST EXISTS, AND IT IS THE GAP THAT MADE IT NECESSARY. `_scales_and_fine_surface` returns
early for a part with ONE scale, and every part in this project's corpus has one: of ten read on
2026-10-28, every one came back single-scaled and only the first real customer cold plate did not.
So the whole second half of that function - including the line that writes the file - was never
executed by any test, and it contained a NameError: it called Path() in a module that imports
pathlib inside each function that needs it, and that import was missing. It would have thrown on
the first two-scale part, which is the part the path exists for. mypy's ratchet caught it; no test
did, because no test reached the line.

The field here is SYNTHETIC and bimodal on purpose, so the test does not need a customer part to
reach that code.
"""
import numpy as np
import pytest

from meshpipeline.cad import fluid_volume as fv
from meshpipeline.cad import scales as SC


def _two_scale_field(fine_mm=0.1, broad_mm=4.0, n=600):
    """(points, triangles, radius) with two clearly separated passage widths.

    The widths are 2 x radius, so the two groups land forty times apart - well past the two octaves
    cad/scales needs between the modes, with an empty trough between them.
    """
    rng = np.random.default_rng(7)
    # two slabs of points, far enough apart that the fine triangles bound a small box
    fine_pts = rng.uniform([0.0, 0.0, 0.0], [0.02, 0.004, 0.02], size=(n, 3))
    broad_pts = rng.uniform([0.05, 0.0, 0.0], [0.10, 0.03, 0.03], size=(n, 3))
    pts = np.vstack([fine_pts, broad_pts])
    radius = np.concatenate([
        np.full(n, fine_mm / 2.0 / 1000.0) * rng.uniform(1.0, 1.05, n),
        np.full(n, broad_mm / 2.0 / 1000.0) * rng.uniform(1.0, 1.05, n),
    ])
    # triangles entirely within one group, so the all-three-vertices rule puts each on one side
    faces = np.vstack([
        np.column_stack([np.arange(0, n - 2), np.arange(1, n - 1), np.arange(2, n)]),
        np.column_stack([np.arange(n, 2 * n - 2), np.arange(n + 1, 2 * n - 1),
                         np.arange(n + 2, 2 * n)]),
    ])
    return pts, faces, radius


def test_the_synthetic_field_really_has_two_scales():
    """If this stops reading as two scales the rest of the file proves nothing."""
    got = SC.scales_of_field(*_two_scale_field())
    assert got is not None, "the fixture is supposed to be bimodal"
    assert got["fine"]["width_m"] < got["split_m"] < got["broad"]["width_m"]
    assert got["fine"]["area_m2"] > 0 and got["broad"]["area_m2"] > 0


def test_the_thin_walls_are_written_beside_the_coolant_surface(tmp_path):
    """THE LINE THAT WAS NEVER RUN. A NameError lived here and no test reached it."""
    out = tmp_path / "constant" / "triSurface" / "fluid.stl"
    out.parent.mkdir(parents=True, exist_ok=True)
    scales, fine = fv._scales_and_fine_surface(out, _two_scale_field())
    assert scales is not None, "a two-scale field has to produce a split"
    assert fine is not None, "a two-scale field has to produce a thin surface"
    written = out.parent / fv.FINE_SURFACE_NAME
    assert written.exists(), "the thin surface was not written at all"
    assert written.stat().st_size > 0
    # beside the coolant surface, because the plan names it by a path relative to the case
    assert written.parent == out.parent
    assert fine["triangles"] > 0
    assert fine["area_m2"] == pytest.approx(scales["fine"]["area_m2"], rel=1e-9), (
        "the written surface and the measured fine side have to be the same triangles")


def test_a_one_scale_part_writes_nothing_and_says_so(tmp_path):
    """The ordinary case. It must not leave a file behind for the plan to point at."""
    rng = np.random.default_rng(3)
    pts = rng.uniform(0, 0.05, size=(400, 3))
    faces = np.column_stack([np.arange(0, 398), np.arange(1, 399), np.arange(2, 400)])
    radius = np.full(400, 0.002) * rng.uniform(1.0, 1.1, 400)
    out = tmp_path / "fluid.stl"
    scales, fine = fv._scales_and_fine_surface(out, (pts, faces, radius))
    assert scales is None and fine is None
    assert not (tmp_path / fv.FINE_SURFACE_NAME).exists()


def test_no_field_is_not_a_failure(tmp_path):
    """The radius field reads through pyvista and scipy and can genuinely fail. One scale is the
    safe answer: it over-resolves the broad passages, where a wrong split under-resolves the narrow
    ones."""
    assert fv._scales_and_fine_surface(tmp_path / "fluid.stl", None) == (None, None)


def test_a_field_that_cannot_be_read_is_survived(tmp_path):
    """Never raises - the caller sizes the part the way it always has."""
    assert fv._scales_and_fine_surface(tmp_path / "fluid.stl", ("not", "a", "field")) == (None, None)


def test_the_name_is_the_one_the_plan_points_at():
    """cad writes it and cfmesh_conjugate's plan names it; if the two drift, the container meshes
    without the refinement and reports success."""
    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    assert PL.FLUID_FINE_STL.endswith(fv.FINE_SURFACE_NAME)
    assert PL.FLUID_FINE_STL.rsplit("/", 1)[0] == PL.FLUID_STL.rsplit("/", 1)[0], (
        "the thin surface has to sit in the same directory as the coolant surface")
