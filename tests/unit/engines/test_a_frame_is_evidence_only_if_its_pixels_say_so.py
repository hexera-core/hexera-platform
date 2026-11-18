"""A cut frame is evidence only when its OWN PIXELS have been measured to hold the passages.

WHY THIS EXISTS. The zoom window is `plan_cell / CELL_PIXELS` and the reading side decides a
frame is countable by dividing that window by `quality.near_wall_cell_m`, which finalize.py
derives as `background_cell_m / 2**levels`. On job 0ff6f9e5 those are the same number by
construction - 0.0004025248 / 2**4 = 2.51578e-05 = fine_cell_pinned_m, exactly - so every zoom
frame declares itself countable whatever is drawn in it, and nothing looks at the ink.

MEASURED on that plate, through the shipped renderer, on a cut of its fluid/solid interface. All
three zoom frames were declared countable and introduced to the reviewer with the words "cells are
countable in this frame":

    frame                     what is in it                 ink      repeats   periodic at
    along_the_flow_zoom       the channel array              3.258%       79       0.987
    across_the_flow_zoom      three plenum rectangles        0.592%        3       0.304
    through_the_plate_zoom    one circular port bore         0.414%        0       0.657

Two of those three are a picture with no passage in it, offered as evidence of a channel count.

AND THE WORST CASE IS NOT IN THAT TABLE. Every frame measured on that plate so far, by four
agents, is of the INTERFACE SURFACE: its interior was pruned off the workspace before anyone read
it. A fluid-only VOLUME slice through a plenum comes back as a dense, healthy-looking grid of
cells with a high ink fraction, crisp edges and the right scale, and no channel in it. The test
below builds exactly that and requires it to be refused.
"""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines.cfmesh_conjugate import slices as S

W, H = 1280, 960
FILL, EDGE, BACKGROUND = 87, 0, 255


def _blank() -> np.ndarray:
    return np.full((H, W), BACKGROUND, dtype=np.uint8)


def _striped(period_px: int, duty: float = 0.5, *, rows=(0, H)) -> np.ndarray:
    """An array of filled passages: `period_px` apart, `duty` of each period inked."""
    a = _blank()
    x = np.arange(W)
    on = (x % period_px) < max(1, int(period_px * duty))
    a[rows[0]:rows[1], on] = FILL
    a[rows[0]:rows[1], on & ((x % period_px) == 0)] = EDGE      # a cell edge inside each passage
    return a


def _solid_grid(cell_px: int) -> np.ndarray:
    """A dense field of cells with no passage in it: the plenum a volume slice draws."""
    a = np.full((H, W), FILL, dtype=np.uint8)
    a[:, ::cell_px] = EDGE
    a[::cell_px, :] = EDGE
    return a


def test_an_array_of_passages_is_what_a_frame_has_to_hold():
    got = S.ink_structure(_striped(32))
    assert got["shows_structure"], got
    assert got["period_px"] == 32
    assert got["repeats"] >= S.MIN_REPEATS
    assert got["strength"] >= S.ACF_MIN
    assert got["scan"] == "horizontal"


def test_a_dense_field_of_cells_with_no_passage_in_it_is_refused():
    """THE DANGEROUS CASE, and no scale and no ink fraction can see it. This frame has 20% of its
    pixels inked, a cell edge every four pixels and exactly the window the plan asked for."""
    got = S.ink_structure(_solid_grid(4))
    assert got["ink_fraction"] > 0.2, "this frame is meant to LOOK healthy"
    # 24 grey levels is what sandbox/review_session already demands of a frame it will look at
    assert got["edge_contrast"] >= 24, "and its cells are meant to be crisp"
    assert not got["shows_structure"], got
    assert "repeat" in got["why"]


def test_one_port_bore_is_refused():
    a = _blank()
    yy, xx = np.mgrid[0:H, 0:W]
    bore = ((xx - 640) ** 2 + (yy - 480) ** 2) < 150 ** 2
    ring = bore & ~(((xx - 640) ** 2 + (yy - 480) ** 2) < 140 ** 2)
    a[ring] = EDGE
    got = S.ink_structure(a)
    assert not got["shows_structure"], got


def test_three_plenum_rectangles_are_refused():
    a = _blank()
    for x0 in (200, 600, 1000):
        a[300:700, x0:x0 + 60] = FILL
    got = S.ink_structure(a)
    assert not got["shows_structure"], got
    assert str(got["repeats"]) in got["why"]


def test_a_blank_frame_says_so():
    got = S.ink_structure(_blank())
    assert not got["shows_structure"]
    assert got["ink_fraction"] == 0.0
    assert "nothing was drawn" in got["why"]


def test_scattered_marks_are_not_an_array():
    """The whole-part frames hold traces of everything the plane crossed. They clear the repeat
    count easily - 23 and 38 measured - and it is the autocorrelation that sees through them."""
    rng = np.random.default_rng(7)
    a = _blank()
    for x in rng.integers(0, W - 2, size=40):
        a[rng.integers(0, H // 2):rng.integers(H // 2, H), x:x + 2] = FILL
    got = S.ink_structure(a)
    assert got["repeats"] >= S.MIN_REPEATS, "the point of this frame is that it passes that bar"
    assert got["strength"] < S.ACF_MIN
    assert not got["shows_structure"], got


def test_a_period_of_two_cells_is_the_floor():
    """A period under two cells is as likely to be the cell grid, or the rasteriser's own line
    spacing, as anything in the part."""
    assert S.MIN_PERIOD_PX == 2 * S.CELL_PIXELS
    got = S.ink_structure(_striped(4))
    assert not got["shows_structure"], got


def test_the_frame_publishes_what_it_measured_not_just_a_verdict():
    """A reader who is handed "no" and no number cannot check it or argue with it."""
    got = S.ink_structure(_striped(32))
    for key in ("ink_fraction", "edge_contrast", "repeats", "period_px", "run_spacing_px",
                "strength", "strength_lag_px", "scan", "horizontal", "vertical", "pixels"):
        assert key in got, key
    assert got["pixels"] == [W, H]


def test_the_part_s_own_period_rules_a_different_feature_out():
    """MEASURED on job 0ff6f9e5: the y-normal cut shows the channel walls every 100.631 um, and
    two other windows on the same plate hold regular arrays at 402.525 and 805.05 um, four and
    eight times that. A reviewer asked how many cells are across a passage would have answered
    from one of them about the wrong passage."""
    assert S.period_matches(100.631e-6, 100.631e-6)
    assert S.period_matches(201.262e-6, 100.631e-6), (
        "a plane ACROSS the array sees passages at the full pitch while a plane normal to it sees "
        "walls at half: the same array measures two ways and both are it")
    assert not S.period_matches(402.525e-6, 100.631e-6)
    assert not S.period_matches(805.05e-6, 100.631e-6)
    assert S.period_matches(805.05e-6, 0.0), "with no yardstick there is nothing to disagree with"


def test_ink_is_anything_that_is_not_the_background():
    """NOT A DARKNESS THRESHOLD, and that is measured. A cut of the interface SURFACE is lines,
    and the six frames of job 0ff6f9e5 hold five grey levels - 255, 241, 227, 211, 193 - because
    the line colour is lightsteelblue blended toward white. A threshold of 128 finds zero ink in
    all six and calls every one of them blank."""
    a = _blank()
    a[:, ::32] = 193
    a[:, 1::32] = 227
    assert S.ink_structure(a)["ink_fraction"] > 0.0
    assert (np.asarray(a) < 128).sum() == 0, "nothing here is dark by any ordinary threshold"


def test_a_frame_that_is_not_a_picture_is_an_answer_not_a_crash():
    assert not S.ink_structure(np.zeros((0, 0)))["shows_structure"]
    assert not S.ink_structure(np.zeros((5,)))["shows_structure"]


@pytest.mark.parametrize("period", [16, 24, 32, 64])
def test_the_published_period_is_the_lag_the_profile_repeats_at(period):
    """NOT the median spacing of the marks, and this was measured the hard way: the z-normal cut
    of job 0ff6f9e5 holds fins in PAIRS, truly 128 px apart, and the median spacing came out 24 px
    - close enough to this plate's real 100.631 um passages to be accepted as a picture of them."""
    a = _blank()
    x = np.arange(W)
    # a PAIR of marks per period, as the real fins come: this is what fools a median spacing
    a[:, ((x % period) < 3) | (((x % period) >= 6) & ((x % period) < 9))] = FILL
    got = S.ink_structure(a)
    assert got["period_px"] == period, got
    assert got["run_spacing_px"] < period, "the median spacing is the figure that is fooled"
