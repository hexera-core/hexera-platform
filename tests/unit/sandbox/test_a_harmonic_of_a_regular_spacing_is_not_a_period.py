# Responsibility: Verify that a correlation peak landing on a whole multiple of a REGULAR run
# spacing is published as the spacing, not as the multiple - and that an IRREGULAR spacing is still
# not believed over the lag.
# Boundaries: the reading side only. Nothing here renders and nothing here meshes.
#
# THE DEFECT, measured on job 606af55e (2026-11-27) from the frame's own published content:
#
#     run_spacing_px    62.0     21 spacings, spread 0.016
#     strength_lag_px  185       strength 0.996
#     period_px        185.0  -> period_um 603.923
#     part_period_m      0.0001006312
#     shows_structure  False
#     why: "this array repeats every 603.92 um and this part's passages were measured at
#           100.63 um, so it is a different feature of the same part"
#
# 62 px at that frame's 3.2645 um/px is 202.4 um, which IS the plate's passage pitch; 185 is almost
# exactly three times it. The ratio test then saw 603.92 / 100.631 = 6.0 against PERIOD_MATCH 2.5
# and refused a clean picture of 21 passages. All three zoom frames of that run were refused this
# way, and the reviewer was asked for one region target and got none.
#
# WHY IT APPEARED WHEN IT DID: the zoom window is now sized from the POST-LAYER cell rather than
# the plan's (`slices.drawn_cell_m`), which is what made a group-of-three super-structure
# resolvable in the frame at all. The mistake is not new; the magnification that exposes it is.
#
# THE MIRROR CASE, WHICH MUST NOT MOVE, is already in the code's own words: "THE PERIOD IS THE LAG,
# not the median spacing between runs, and this was measured the hard way". Job 0ff6f9e5's z-normal
# cut holds fins in PAIRS, and the doublets put enough short spacings in the list that the median
# came out 24.0 px against a true 128 px pitch. So the median can be fooled DOWNWARD by
# sub-structure, and the lag can be fooled UPWARD by super-structure, and the two are told apart by
# `spread` - a figure that was already on the frame.
#
# MEASURED HERE, through `ink_structure` on the synthetic frames, with the rule on and off:
#
#   frame                     spacing  spread   lag   period, rule off   period, rule on
#   groups of three (62 px)      62.0   0.000   186        186.0               62.0
#   doublet fins (16/16/1)       24.5   0.612    49         49.0               49.0
#   plain passages (62 px)       62.0   0.000    62         62.0               62.0
from __future__ import annotations

import numpy as np
import pytest
from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_alternating_walls,
    write_passages,
    write_passages_in_groups_of_three,
)
from PIL import Image

import meshpipeline.sandbox.picture_structure as PS

#: Job 606af55e's own through_the_plate zoom scale, so the micron figures below are that frame's.
MPP_UM = 3.2645
#: What that run's plan measured this plate's passages at, which is what a period is compared to.
PART_PITCH_UM = 100.631


def _read(tmp_path, draw, **kw) -> dict:
    """One frame's structure, straight off the PNG - no manifest, no resolver, no zoom arithmetic."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = draw(tmp_path / "frame.png", 1280, 960, **kw)
    return PS.ink_structure(np.asarray(Image.open(p).convert("L")))


def test_the_frame_that_was_refused_publishes_the_pitch_and_not_its_third_harmonic(tmp_path):
    """THE DEFECT. A group-of-three super-structure over a 62 px pitch.

    The peak really does land on the harmonic - that is asserted, so this test is known to drive
    the branch rather than to pass by never reaching it - and the published period is the pitch.
    """
    got = _read(tmp_path, write_passages_in_groups_of_three, period_px=62)
    assert got["run_spacing_px"] == 62.0, got
    assert got["strength_lag_px"] == 186, (
        "the peak has to be ON the harmonic for this test to be testing anything: "
        f"{got['strength_lag_px']}")
    assert got["spread"] <= PS.SPACING_REGULAR, got["spread"]
    assert got["period_px"] == 62.0, (
        f"published {got['period_px']} px, which at {MPP_UM} um/px is "
        f"{got['period_px'] * MPP_UM:.2f} um of a part whose passages are {PART_PITCH_UM} um")


def test_the_refused_frame_now_clears_the_bar_it_was_refused_on(tmp_path):
    """And the consequence, in the units the refusal was written in: 202.40 um against 100.631 um
    is a factor of 2.011, inside PERIOD_MATCH; 603.92 um was a factor of 6.0 and outside it."""
    got = _read(tmp_path, write_passages_in_groups_of_three, period_px=62)
    period_m = got["period_px"] * MPP_UM * 1e-6
    assert PS.period_matches(period_m, PART_PITCH_UM * 1e-6), (
        f"{period_m * 1e6:.2f} um against {PART_PITCH_UM} um is still refused")
    harmonic_m = got["strength_lag_px"] * MPP_UM * 1e-6
    assert not PS.period_matches(harmonic_m, PART_PITCH_UM * 1e-6), (
        "the harmonic is what was refused, and it has to still be refusable - otherwise this "
        "change admitted a frame rather than measured one")


def test_the_doublet_fins_still_keep_their_lag(tmp_path):
    """THE HALF THAT MUST NOT MOVE, and the case the original rule was measured the hard way from.

    Fins in pairs: alternating 16 px and 1 px walls, so the spacings alternate short and long and
    their median is a width neither wall in the picture has. Note their lag is a whole multiple of
    that median TOO - exactly 2.00 - so the multiple test alone would have believed it. `spread` is
    the whole of what refuses it, at 0.612 against a bar of 0.25.
    """
    got = _read(tmp_path, write_alternating_walls, passage_px=16, wall_a_px=16, wall_b_px=1)
    assert got["run_spacing_px"] == 24.5, got
    assert got["strength_lag_px"] == 49, got
    assert round(49 / 24.5, 6) == 2.0, "their lag IS a whole multiple of their median"
    assert got["spread"] > PS.SPACING_REGULAR, (
        f"the spacings have to read as irregular or the median gets believed: {got['spread']}")
    assert got["period_px"] == 49.0, (
        f"the lag is the period on this frame and it published {got['period_px']}")


def test_a_frame_whose_peak_is_its_own_pitch_is_untouched(tmp_path):
    """The ordinary case, which is most of them: lag and spacing agree, so there is nothing to
    choose between and the rule may not change the answer."""
    got = _read(tmp_path, write_passages, cells_px=4, period_px=62)
    assert got["run_spacing_px"] == 62.0 and got["strength_lag_px"] == 62
    assert got["period_px"] == 62.0, got


def test_the_regularity_bar_is_what_does_the_work(tmp_path, monkeypatch):
    """Both measured frames, read with the regularity bar moved, to show which half decides.

    Drop it below the synthetic frame's own spread and the defect comes back exactly - 186.0 px,
    which is the pre-change behaviour reproduced rather than asserted from memory. Raise it above
    the doublets' 0.612 and their median gets believed, which is the failure the original rule was
    written from. The shipped 0.25 sits between 0.000 and 0.612, two orders of magnitude apart.
    """
    monkeypatch.setattr(PS, "SPACING_REGULAR", -1.0)
    broken = _read(tmp_path / "a", write_passages_in_groups_of_three, period_px=62)
    assert broken["period_px"] == 186.0, (
        "the rule off has to reproduce the defect, or it is not the rule being tested: "
        f"{broken['period_px']}")
    monkeypatch.setattr(PS, "SPACING_REGULAR", 1.0)
    loose = _read(tmp_path / "b", write_alternating_walls,
                  passage_px=16, wall_a_px=16, wall_b_px=1)
    assert loose["period_px"] == 24.5, (
        "a regularity bar above the doublets' spread un-fixes the case the original rule was "
        f"measured from: {loose['period_px']}")


@pytest.mark.parametrize(("lag", "spacing", "spread", "want"), [
    (186.0, 62.0, 0.000, True),     # the synthetic frame: exactly 3.00, perfectly regular
    (185.0, 62.0, 0.016, True),     # job 606af55e's real frame: 2.984, which is why there is room
    (49.0, 24.5, 0.612, False),     # the doublet fins: a whole multiple, but irregular
    (62.0, 62.0, 0.000, False),     # the fundamental is not its own harmonic
    (155.0, 62.0, 0.000, False),    # 2.5x is not a whole multiple of anything
    (211.0, 62.0, 0.000, False),    # 3.403x either
    (124.0, 62.0, 0.250, True),     # exactly at the regularity bar, which is inclusive
    (124.0, 62.0, 0.251, False),    # and a hair over it is not
    (124.0, 0.0, 0.000, False),     # no spacing to divide by
    (0.0, 62.0, 0.000, False),      # and no lag to divide
])
def test_both_bars_are_load_bearing(lag, spacing, spread, want):
    """The arithmetic on its own, including the two real measurements and the boundary.

    `HARMONIC_TOLERANCE` is a tenth of a period because the real frame read 2.984 and the
    synthetic one 3.000; 2.5x and 3.4x are the near misses it must not admit.
    """
    assert PS.is_harmonic(lag, spacing, spread) is want
