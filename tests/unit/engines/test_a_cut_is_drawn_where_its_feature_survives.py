"""A cut is drawn in a window where the thing it was cut to show survives to a pixel.

WHY THIS EXISTS. `cfmesh_conjugate/slices.review_slices` says in its own text what the three cuts
are for: "how many cells are across each channel" and "how many cells are through the channel's
height". Both are counts of cells. Every cut has only ever been drawn ONCE, fitted to the whole
part, into the fixed 1280x960 of `sandbox/render_inputs.DEFAULT_SCREENSHOT_W/H`.

MEASURED, job 0ff6f9e5, the delivered Corintis cold plate, 283,410,955 cells. Its own
conjugate_report.review.bbox is 177.69 x 41.83 x 101.21 mm, and at 1280x960 that is:

    cut                 um/px     a 98.2422 um channel    a 25.1578 um cell
    across_the_flow     105.42    0.93 px                 0.24 px
    through_the_plate   138.82    0.71 px                 0.18 px
    along_the_flow      138.82    0.71 px                 0.18 px

The channel is SUB-PIXEL on all three. So the reviewer was being shown three pictures of nothing
and asked to count cells across a passage, and a verdict returned from them would be confident and
unfounded, which is strictly worse than the honest "evidence missing" it returns today.

AND THE OBVIOUS WINDOW DOES NOT WORK EITHER, which is the part that had to be measured rather than
reasoned about. Sizing the zoom so the plan's own feature THICKNESS is four pixels looks right and
is circular: on this part `fine_surfaces[0].thickness_m` / `fine_cell_pinned_m` is 4.0 EXACTLY,
because the planner pins the cell at the thickness over its own four-cell floor. "Four pixels per
thickness" is therefore identically "one pixel per cell", on every part the planner sizes this
way, and one pixel per cell cannot show an edge between two cells at any contrast.

RENDERED AND COUNTED, 2026-11-17, on this plate's own fluid/solid interface, with a 100-pixel
calibration segment drawn into each frame so the scale was measured back out of the image:

    window      um/px measured   channel   cell      dark px drawn   gaps found
    32.202 mm         25.1578     3.905 px  1.000 px           109           13
     8.051 mm          6.2895    15.620 px  4.000 px         1,200          411

At 8.051 mm the median gap between wall traces measured 15 px, or 94.34 um against the plan's
98.2422 um. At 32.202 mm the passages are gone from the picture.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import slices as S

# Job 0ff6f9e5's own numbers: conjugate_report.review.bbox and conjugate_plan.
PLATE_LO = (-0.061679501086473465, 0.020110199227929115, -0.008694685064256191)
PLATE_HI = (0.11601100116968155, 0.06193649768829346, 0.09251250326633453)
CELL_M = 2.51578e-05            # conjugate_plan.fine_cell_pinned_m
THICKNESS_M = 0.0001006312      # conjugate_plan.fine_surfaces[0].thickness_m
CHANNEL_M = 98.2422e-6          # the plan's sizing_basis: this part's real passages


def _cuts():
    return S.review_slices(PLATE_LO, PLATE_HI, cell_m=CELL_M)


def _frame(cut, scope):
    return next(f for f in cut["frames"] if f["scope"] == scope)


def test_the_whole_part_frame_is_the_scale_the_reviewer_has_been_getting():
    """The context frame is unchanged, and this pins it to the measured table above so a future
    change to the camera cannot move it without a test saying so."""
    got = {c["name"]: _frame(c, "whole")["um_per_pixel"] for c in _cuts()}
    assert got["across_the_flow"] == pytest.approx(105.42, abs=0.01)
    assert got["through_the_plate"] == pytest.approx(138.82, abs=0.01)
    assert got["along_the_flow"] == pytest.approx(138.82, abs=0.01)


def test_a_channel_is_under_one_pixel_on_the_whole_part_frame():
    """The defect itself, stated as a measurement rather than as a worry."""
    for cut in _cuts():
        mpp = _frame(cut, "whole")["metres_per_pixel"]
        assert CHANNEL_M / mpp < 1.0, (
            f"{cut['name']}: the channel is {CHANNEL_M / mpp:.2f} px, which is not the defect "
            "this test was written against")


def test_every_cut_is_also_drawn_zoomed():
    cuts = _cuts()
    assert cuts, "no cuts at all"
    for cut in cuts:
        scopes = [f["scope"] for f in cut["frames"]]
        assert scopes == ["whole", "zoom"], f"{cut['name']} has frames {scopes}"


def test_the_zoom_frame_puts_four_pixels_on_a_cell():
    """The bar, and it is per CELL. Counting cells is what the cut is for."""
    for cut in _cuts():
        mpp = _frame(cut, "zoom")["metres_per_pixel"]
        assert CELL_M / mpp == pytest.approx(S.CELL_PIXELS, abs=1e-9), cut["name"]


def test_the_zoom_frame_puts_fifteen_pixels_across_a_real_channel():
    """The number counted out of the rendered image: 15 px, median of 411 gaps measured."""
    for cut in _cuts():
        mpp = _frame(cut, "zoom")["metres_per_pixel"]
        assert CHANNEL_M / mpp == pytest.approx(15.62, abs=0.01), cut["name"]
        assert CHANNEL_M / mpp >= 4.0


def test_sizing_the_window_from_the_feature_thickness_gives_one_pixel_a_cell():
    """THE REFUTATION, kept as a test because the wrong window is the plausible one.

    If this ever stops holding, the planner has stopped pinning the cell at thickness/4 and the
    choice of input for the window is worth re-opening. Until then, a window sized from the
    thickness is a window at exactly one pixel per cell.
    """
    assert THICKNESS_M / CELL_M == pytest.approx(4.0, abs=1e-9)
    _, _, mpp = S.zoom_window(THICKNESS_M)
    assert CELL_M / mpp == pytest.approx(1.0, abs=1e-9), (
        "a thickness-sized window no longer gives one pixel per cell")
    assert CHANNEL_M / mpp < S.CELL_PIXELS, (
        "and the channel does not even reach the four pixels that window was aiming at")


def test_every_frame_publishes_the_window_it_was_drawn_in():
    """A cut whose window is not stated is not evidence: the claim cannot be checked against the
    picture it was made from."""
    for cut in _cuts():
        for f in cut["frames"]:
            w, h = f["window_m"]
            assert w > 0 and h > 0
            assert f["metres_per_pixel"] > 0
            # um_per_pixel is the same number rounded for a reader; metres_per_pixel is the one
            # the camera is set from, so the two must not be able to drift apart
            assert f["um_per_pixel"] == round(f["metres_per_pixel"] * 1e6, 4)
            # the window IS the frame at this scale, which is what makes it checkable
            assert w == pytest.approx(f["metres_per_pixel"] * S.FRAME_W, rel=1e-9)
            assert h == pytest.approx(f["metres_per_pixel"] * S.FRAME_H, rel=1e-9)


def test_a_plan_with_no_fine_cell_publishes_no_zoom():
    """A zoom computed from a guessed feature is a stated scale that is wrong, which is worse
    than one frame fewer."""
    for cut in S.review_slices(PLATE_LO, PLATE_HI):
        assert [f["scope"] for f in cut["frames"]] == ["whole"]
    assert S.zoom_window(0.0) == (0.0, 0.0, 0.0)
    assert S.zoom_window(-1.0) == (0.0, 0.0, 0.0)


def test_the_frame_is_the_one_the_reviewer_actually_renders_into():
    """The window is computed against 1280x960 because that is what the home side draws into. If
    those two ever differ, every published um/px is wrong by the ratio."""
    from meshpipeline.sandbox.render_inputs import (
        DEFAULT_SCREENSHOT_H,
        DEFAULT_SCREENSHOT_W,
    )
    assert (S.FRAME_W, S.FRAME_H) == (DEFAULT_SCREENSHOT_W, DEFAULT_SCREENSHOT_H)


def test_the_in_plane_axes_are_the_ones_the_scale_was_computed_from():
    """Get this mapping the wrong way round and every published scale is out by the frame's
    aspect ratio, silently and in the direction that flatters."""
    assert S.plane_axes(0) == (1, 2)
    assert S.plane_axes(1) == (0, 2)
    assert S.plane_axes(2) == (0, 1)
    # the binding direction is whichever runs out of pixels first
    assert S.fitted_metres_per_pixel(0.1012072, 0.0418263) == pytest.approx(0.1012072 / 1280)
    assert S.fitted_metres_per_pixel(0.0418263, 0.1012072) == pytest.approx(0.1012072 / 960)


def test_a_degenerate_block_still_promises_nothing():
    assert S.review_slices((0, 0, 0), (0, 1, 1), cell_m=CELL_M) == []
