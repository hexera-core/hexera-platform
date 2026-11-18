# Responsibility: Hold the producer's own half of the reviewer fix - the yardstick every cut is
# matched to must be bounded by something outside the frame it came from, and the gate that
# decides whether to start the pass must price the renders the pass actually plans.
# Boundaries: cfmesh_conjugate/native._render_cuts and its pricing. No manifest, no reviewer.
"""THE YARDSTICK WAS SELF-CERTIFIED AND NOTHING BOUNDED IT.

Three things combined to leave exactly one frame in a run held to no period at all:

  * `_measure_frame` applies `period_matches` only when `expect_period_m` is already non-zero;
  * `_rematch` skips the frame the yardstick came from, "the yardstick came from this one";
  * `_place_by_what_is_drawn` stops at the FIRST placement that looks periodic.

So the aiming cut's zoom chose itself, and every other cut in the run was then matched to it with
PERIOD_MATCH's factor of 2.5 of slack either way.

THAT IS NOT A THEORETICAL RISK ON THIS PART. Round 3's own `cuts.json` records
`across_the_flow`'s zoom measuring `period_px 128.0` at `metres_per_pixel 6.28945e-06` - 805.05 um
- with 39 repeats and an autocorrelation peak of 0.582. 805.05 / 100.631 is 8.0 exactly: it is a
bank of coarse fins, and `sandbox/picture_structure.PERIOD_MATCH`'s own comment lists arrays at
1x, 2x, 4x and 8x the passage pitch on this one plate. Whichever of those a search reached first
became the run's yardstick.

MEASURED ON THE SHIPPED CODE, 2026-11-17: handed a synthetic array drawn at exactly 8x the pitch
its own cut set was sized for, `_render_cuts` came back with `repeat_period_m` 201.262 um against
a 25.1578 um cell - a ratio of 8.000 to the 100.631 um pitch, OUTSIDE PERIOD_MATCH's 2.5 - with
the aiming zoom reporting `shows_structure: true` and `part_period_m: None`.

THE CUT SET CARRIES THE BOUND AND IT COSTS NOTHING. The zoom window is sized so the part's finest
cell is `CELL_PIXELS` pixels across, and `slices.CELL_PIXELS` records why that makes the product
the feature: on job 0ff6f9e5 `fine_surfaces[0].thickness_m` is 0.0001006312 m and
`fine_cell_pinned_m` is 2.51578e-05 m, "and the first divided by the second is 4.0 EXACTLY,
because the planner pins the cell AT the thickness over its four-cell floor". So the aiming cut is
held to `feature_m x CELL_PIXELS`, and when no placement of it produces a period that figure
stands in as the yardstick rather than 0.0 - at 0.0 `period_matches` returns True for everything,
which is the state that let an 805 um array answer a question about a 100.631 um passage. It is
ONE source: the plan's `thickness_m` is the same quantity, and taking it as well would give one
number two readers that can disagree.

AND THE GATE THAT DECIDES WHETHER TO START ANY OF THIS WAS PRICING THE WRONG WORK.
`CUT_SECONDS_PER_MCELL` was 0.22 and its comment described "slice it, then draw it twice". The
code takes up to POSITION_TRIES slices and POSITION_TRIES x (WINDOW_TRIES + 1) = 20 zoom renders
per cut, measuring every one. Measured 2026-11-17 through this file's own functions on a SYNTHETIC
171^3 = 5,000,211-cell grid (the real plate's interior was pruned off its workspace, so there is
no interior of the part left to time): slice 0.3753 s, clip 0.0438 s, zoom draw 0.1997 s, measure
0.0250 s, whole draw 0.7747 s. The minimal path the comment described is 1.350 s and the worst
case the code can take is 8.047 s, against 1.100 s priced.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.engines.cfmesh_conjugate.slices import CELL_PIXELS, review_slices

from test_the_mesher_draws_its_own_cuts import (  # noqa: F401
    CELL_M,
    STRIPE_CELLS,
    _block,
    _by_name,
    _cuts,
    _frame,
    _grid,
    _replan,
    _save,
    _striped,
)

pv = pytest.importorskip("pyvista")

#: THE PITCH THE CUT SET DECLARES, which is what bounds the yardstick: the window is sized so the
#: part's finest cell is CELL_PIXELS across, so feature_m x CELL_PIXELS is the feature. On job
#: 0ff6f9e5 that is 2.51578e-05 x 4 = 100.631 um, and `fine_surfaces[0].thickness_m` on the same
#: plan is 0.0001006312 m - the same number to six figures.
DECLARED_PITCH_M = CELL_M * CELL_PIXELS            # 100.631 um
#: An array drawn at EIGHT TIMES that, which is the real plate's own relationship: round 3's
#: cuts.json measured 805.05 um on a part whose passages are 100.631 um.
COARSE_STRIPE_CELLS = 16
COARSE_PITCH_M = 2 * COARSE_STRIPE_CELLS * CELL_M  # 805.05 um
#: ...and `_striped`'s own stripe, which is 2x the declared pitch and INSIDE PERIOD_MATCH's 2.5.
FINE_PITCH_M = 2 * STRIPE_CELLS * CELL_M           # 201.262 um


def _coarse(tmp_path: Path, axis: int = 0):
    """A BANK OF COARSE FINS: narrow runs of coolant repeating EIGHT TIMES the pitch the cut set
    declares. Same renderer, same window, same scale - a regular, crisp, high-contrast array of
    the wrong feature, which is what round 3's `across_the_flow` zoom actually drew (128.0 px at
    6.28945 um/px, 39 repeats, autocorrelation 0.582, on a part whose passages are 100.631 um).

    NARROW RUNS AND NOT HALF-ON-HALF-OFF, deliberately: 16 contiguous cells of coolant put a
    4-pixel cell-edge repeat inside each run, and `ink_structure` then reads the CELLS at 2 px
    rather than the feature at 128 - measured, and it is why this fixture is shaped like a fin
    bank. The feature has to be the coarsest thing in the frame for the frame to be about it.
    """
    nx, ny, nz = 1320, 250, 3
    g = _grid(nx, ny, nz)
    k = np.floor(np.asarray(g.cell_centers().points)[:, axis] / CELL_M).astype(int)
    keep = (k % (2 * COARSE_STRIPE_CELLS)) < 2
    sub = g.extract_cells(np.flatnonzero(keep))
    return _save(sub, tmp_path), [0.0, 0.0, 0.0], [nx * CELL_M, ny * CELL_M, nz * CELL_M]


# ------------------------------------------------------------------ the yardstick is bounded

def test_an_array_eight_times_the_declared_pitch_cannot_become_the_yardstick(tmp_path):
    """The aiming cut is the one frame that chose itself. Held to the pitch its own window was
    sized for, a drawn array eight times coarser is refused there exactly as it is refused
    everywhere else."""
    vol, lo, hi = _coarse(tmp_path, axis=0)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000, replan=_replan(lo, hi))
    assert out["why"] == "", out["why"]
    assert not out.get("repeat_period_m"), (
        "the aiming cut adopted an array eight times the pitch its window was sized for as the "
        "run's yardstick, and every other cut is then matched to it with PERIOD_MATCH's 2.5x of "
        f"slack: repeat_period_m={out.get('repeat_period_m')}")
    assert "showed no repeating array" in str(out.get("aim") or ""), out.get("aim")
    content = _frame(_by_name(cuts)[N.AIM_FROM_CUT], "zoom").get("content") or {}
    assert content.get("shows_structure") is False, content
    assert content.get("part_period_m") == pytest.approx(DECLARED_PITCH_M), (
        "the aiming frame records no period it was held to, so it was held to none: "
        f"part_period_m={content.get('part_period_m')}")
    assert "different feature of the same part" in str(content.get("why") or ""), content


def test_an_array_within_the_tolerance_still_becomes_the_yardstick(tmp_path):
    """THE CONTROL ARM, and it matters: nothing here refuses an array for being an array. The same
    array does not measure the same from every direction - a plane normal to it sees the WALLS
    every half pitch and a plane across it sees the PASSAGES every full pitch - which is why
    PERIOD_MATCH is as loose as 2.5. `_striped`'s stripe is 2x the declared pitch and stands."""
    vol, lo, hi = _striped(tmp_path, axis=0)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000, replan=_replan(lo, hi))
    assert out["why"] == "", out["why"]
    assert out.get("repeat_period_m") == pytest.approx(FINE_PITCH_M, rel=0.02), out
    assert out.get("repeat_world_axis") == 0, out
    assert out.get("yardstick_m", FINE_PITCH_M) == pytest.approx(FINE_PITCH_M, rel=0.02)


def test_with_no_measured_period_the_declared_pitch_is_the_yardstick(tmp_path):
    """A plenum shows no array anywhere, so no frame produces a period. The run then held every
    remaining cut to 0.0, where `period_matches` returns True for anything resolved at all."""
    vol, lo, hi = _block(tmp_path)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000, replan=_replan(lo, hi))
    assert out["why"] == "", out["why"]
    assert out.get("yardstick_m") == pytest.approx(DECLARED_PITCH_M), (
        "with no measured period the run falls back to no yardstick at all, and at 0.0 "
        f"period_matches returns True for every pitch: yardstick_m={out.get('yardstick_m')}")
    assert "the part's own feature pitch" in str(out.get("yardstick_source") or ""), out


def test_a_cut_set_with_no_feature_size_says_it_has_no_yardstick(tmp_path):
    """A legal plan carries no fine cell, so `review_slices` publishes no zoom frame at all. The
    fallback is then honestly nothing, and that is stated rather than left as a 0.0 nobody can
    tell from a measurement."""
    vol, lo, hi = _block(tmp_path)
    cuts = review_slices(lo, hi, cell_m=None)
    assert all(not any(f["scope"] == "zoom" for f in c["frames"]) for c in cuts), cuts
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000)
    assert out.get("yardstick_m") == 0.0, (
        "the run records no yardstick at all, so nothing downstream can tell a cut that was held "
        f"to a period from one that was held to nothing: {out.get('yardstick_m')!r}")
    assert "the cuts carry no feature size" in str(out.get("yardstick_source") or ""), out


# ---------------------------------------------------------------- the gate prices what it runs

def test_the_gate_prices_every_render_the_pass_can_plan(tmp_path):
    """MEASURED: 8.047 s worst case for ONE cut on 5,000,211 cells, through this file's own
    `_slice_at`, `_render_frame` and `_measure_frame`. The gate decides whether to START a pass
    it cannot abandon, so its figure must not come in under what the pass can take."""
    vol, lo, hi = _block(tmp_path)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=5_000_211, budget_left_s=0.001)
    read = vol.stat().st_size / N.VOLUME_READ_BYTES_PER_S
    drawing = out["priced_seconds"] - read
    assert drawing >= len(cuts) * 8.047, (
        f"{len(cuts)} cuts on 5,000,211 cells are priced at {drawing:.3f} s of work against the "
        f"{len(cuts) * 8.047:.3f} s they were measured to take at worst, so the gate can start a "
        "pass the machine will be deleted under")
    # the rate that was here, for the size of the miss: 0.22 x 5.000211 = 1.100 s for the lot
    assert 5.000211 * 0.22 < 8.047 / 2.0


def test_the_price_counts_the_renders_and_not_only_the_cells(tmp_path):
    """The drawing is per RENDER and only the slice is per cell, so a rate that is purely
    per-Mcell charges nothing at all for twenty screenshots a cut."""
    vol, lo, hi = _block(tmp_path)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=0, budget_left_s=0.001)
    read = vol.stat().st_size / N.VOLUME_READ_BYTES_PER_S
    drawing = out["priced_seconds"] - read
    planned = len(cuts) * N.POSITION_TRIES * (N.WINDOW_TRIES + 1)
    # 0.2685 s MEASURED for one placement render: clip the window, draw it, read the PNG back.
    # Written out rather than read off the constant, so a constant that drifts does not take the
    # measurement with it.
    assert drawing >= planned * 0.2685, (
        f"{len(cuts)} cuts on a mesh of no cells are priced at {drawing:.3f} s, so all {planned} "
        "placement screenshots are free")


def test_the_price_still_grows_with_the_mesh(tmp_path):
    """The slice IS per cell - 0.0750 s per Mcell measured - so a bigger mesh still costs more.
    A regression guard: this held before the re-pricing and has to go on holding."""
    vol, lo, hi = _block(tmp_path)
    small = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=5_000_000,
                           budget_left_s=0.001)["priced_seconds"]
    big = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=283_410_955,
                         budget_left_s=0.001)["priced_seconds"]
    assert big > small * 5, (small, big)


def test_a_pass_priced_over_the_budget_says_how_many_renders_it_priced(tmp_path):
    vol, lo, hi = _block(tmp_path)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000, budget_left_s=0.001)
    assert out["rendered"] == 0 and "not started" in out["why"]
    assert out.get("priced_renders") == len(cuts) * (N.POSITION_TRIES * (N.WINDOW_TRIES + 1) + 1), (
        "the gate does not say how many renders it priced, so its figure cannot be checked "
        f"against what the pass would actually do: {out.get('priced_renders')!r}")
    assert vol.is_file(), "a pass that never ran may not cost the artifact"


# --------------------------------------------------- the interior needs a USABLE picture to go

def test_frames_that_hold_nothing_do_not_buy_the_interiors_deletion(tmp_path):
    """`rendered` counts whole frames and now counts every drawn frame whatever its pixels hold,
    so it cannot be what licenses deleting the only artifact a review could use."""
    vol, lo, hi = _block(tmp_path)
    out = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=25_000, ceiling=1)
    assert out["rendered"] > 0, "the frames drew"
    assert vol.is_file(), (
        "the fluid interior was deleted although not one frame holds the structure its cut "
        "exists to show, so the review is left with pictures it cannot count in and nothing else")
    assert out.get("usable") == 0, out
    assert "none of them holds the periodic structure" in str(out.get("volume_kept") or ""), out


def test_a_usable_picture_does_buy_it(tmp_path):
    """The control arm: once a frame holds the array, the interior is dead weight at this size."""
    vol, lo, hi = _striped(tmp_path, axis=0)
    out = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=25_000, ceiling=1)
    assert not vol.is_file(), "a cut that can answer the question replaces the interior"
    assert out.get("volume_dropped_bytes", 0) > 0, out
    assert out.get("usable", 1), out
