# Responsibility: Verify that a TRUE PICTURE of real passages with real metal between them
# discharges its region whatever its pitch, its duty cycle, the axis it repeats on, or how many
# cells wide its passages happen to be - and that the three pieces of arithmetic which were
# refusing such frames are fixed rather than re-thresholded.
# Boundaries: the home side only, and only what can be measured off a PNG plus the one plan
# figure. Nothing here renders and nothing here reads a manifest for a size.
#
# FOUR DEFECTS THIS IS WRITTEN FROM, all arithmetic, all SYNTHETIC and VOLUME-shaped at 1280x960
# with a claimed 4.00 px cell, all measured through the production ArtifactResolver and ResolvedCut
# unless a test says otherwise:
#
#   1. THE AUTOCORRELATION WAS BIASED AGAINST LONG PERIODS BY ITS OWN NORMALISATION. The numerator
#      summed over n - k samples and the denominator was the WHOLE profile's variance, so every
#      lag paid a factor of (1 - k/n) and lag 2 won outright once passage x wall exceeded about
#      2n. MEASURED: passages 8 cells wide with 24 cells of wall, 12/16, 12/20, 12/24, 16/12,
#      16/16, 16/20 and 16/24 all came back "the ink repeats every 2.0 px, under the 8 px a
#      passage and its wall need" and were refused. THE BOUNDARY MOVED WITH THE DUTY CYCLE, which
#      is the signature of the arithmetic and of nothing in any part, and it made the WHOLE
#      VERTICAL AXIS unreachable - 960 samples instead of 1280, so the bound bites sooner.
#
#   2. THE "DRAWN CELL" WAS THE MEDIAN OF GAPS THAT WERE MOSTLY NOT CELLS. An interior is counted
#      only when ink closes it on both sides, so the outermost interior of every passage is
#      dropped and the gap to the next passage is a JUMP ACROSS THE BARE WALL. MEASURED: a passage
#      two drawn cells wide yields ONE interior per passage, every gap is a wall jump, and the
#      drawn cell read 12 to 104 px against a claimed 4.00 - refused at every wall from 1 to 24
#      cells, while its cells really are 4 px. At two interiors per passage the gaps alternate
#      cell, wall, cell, wall and the median landed on whichever the band count made it: passage
#      3 / wall 3 gave 106 gaps and index 53 read 20.0 px and was REFUSED, and passage 3 / wall 2
#      gave 127 gaps and index 63 read 4.0 px and was ADMITTED.
#
#   3. THE SPAN WAS MEASURED OVER INK THAT IS NOT PERIODIC, so a plenum inflated the figure that
#      is supposed to disclose it: an honest array with featureless coolant either side of it read
#      0.7508 at an array share of 0.50, 0.6758 at 0.35 and 0.6258 at 0.25 - the figure rising as
#      the real array shrank.
#
#   4. THE SEPARATING MEASUREMENT WAS COMPUTED AND THROWN AWAY. The column scan ran only when the
#      repeat was vertical, so `cell_pitch_down_px` was 0.0 both for an honest frame with the
#      planner's own anisotropy (cells 8 px across by 4 px down) and for the magnification lie it
#      is refused with (cells 8 px square declared at a window that makes a cell 4 px). Their row
#      pitch is 8.0 in both; their column pitch is 4.0 and 8.0.
#
# AND ONE MORE, on the other side of the ledger: WALLS THAT ALTERNATE 16 px AND 1 px measured a
# median bare run of 8.5 px - 2.125 drawn cells - and PASSED, with half their walls at a quarter
# of a cell, which `picture_structure.WALL_CELLS_MIN` says must be refused with the 1-cell rib.
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED. Every assertion is on behaviour - whether a frame
# discharges its region, and what figure was measured off it - read through `getattr` where the
# field is new, so each test is COLLECTED against the pre-change tree and fails there on its own
# assertion rather than on an import.
from __future__ import annotations

from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_alternating_walls,
    write_anisotropic_passages,
    write_cell_passages,
    write_horizontal_passages,
    write_passages,
    write_plenum_either_side,
    write_plenum_with_ribs,
    write_truncated_plenum,
)

from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
CELL_M = 2.51578e-05              # quality.near_wall_cell_m, and fine_cell_pinned_m
FRAME_PX = (1280, 960)
#: The producer's own zoom scale: one planned cell four pixels across. 6.28945 um/px.
MPP = CELL_M / 4.0

REGION = {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
          "plane_axes": [1, 2], "label": "Square across the coolant's path",
          "why": "the cells across each passage, and the metal between them"}


def _cut(ws: Path, draw, **kw):
    """Draw one frame with `draw`, declare it, and resolve it exactly as production does."""
    rel = "review/cuts/across_the_flow_zoom.png"
    draw(ws / rel, *FRAME_PX, **kw)
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    man = {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
           "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                       "max_cell_m": 0.0006788786304584814},
           "inspection_regions": [dict(REGION, frames=[
               {"scope": FRAME_ZOOM, "image": rel,
                "window_m": [MPP * FRAME_PX[0], MPP * FRAME_PX[1]],
                "metres_per_pixel": MPP,
                "centre_m": [0.0309152940, 0.0242716710],
                "centre_source": "the densest window-sized patch of this cut"}])]}
    got = ArtifactResolver(ws, man).resolve_cuts().get("across_the_flow", ())
    assert got, "the frame was not offered at all, so nothing below is being tested"
    return got[0]


 # 1. the period a frame repeats at is its own, however long it is

def test_a_passage_and_its_wall_wider_than_the_bias_allowed_still_reads_its_own_period(tmp_path):
    """THE EIGHT TRUE PICTURES THE NORMALISATION REFUSED, each with its measured period.

    These are passages of real coolant with real metal between them - from 8 cells of passage and
    24 of wall to 16 and 24 - and every one of them was refused for repeating "every 2.0 px". The
    product passage x wall is what decides it: at 4 px per cell all eight exceed 2n = 2560 px^2,
    and passage 4 / wall 4, which does not, always passed. The period measured here is the drawn
    pitch to the pixel and the strength is 1.000, because an exactly periodic profile correlates
    perfectly with itself at its own lag once the overlap is what it is normalised over.
    """
    want = {(8, 24): 128.0, (12, 16): 112.0, (12, 20): 128.0, (12, 24): 144.0,
            (16, 12): 112.0, (16, 16): 128.0, (16, 20): 144.0, (16, 24): 160.0}
    got, verdict = {}, {}
    for (passage, wall), period in want.items():
        cut = _cut(tmp_path / f"p{passage}w{wall}", write_cell_passages,
                   passage_cells=passage, wall_cells=wall)
        assert cut.ink is not None
        got[(passage, wall)] = cut.ink.period_px
        verdict[(passage, wall)] = bool(cut.counts_cells)
        assert cut.ink.strength >= 0.999, (
            f"passage {passage} / wall {wall} is an exactly periodic drawing and reads "
            f"{cut.ink.strength:.3f} of a perfect array")
    assert got == want, got
    assert all(verdict.values()), (
        "every one of these is a true picture of real passages with real metal between them, and "
        f"a refusal costs the user a run and returns evidence missing: {verdict}")


def test_the_boundary_does_not_move_with_the_duty_cycle(tmp_path):
    """A BOUNDARY THAT MOVES WITH THE DUTY IS ARITHMETIC AND NOT A PART.

    MEASURED, before: a 104 px pitch passed at duty 0.2 and 0.8 and was refused at 0.4, 0.5 and
    0.6; a 128 px pitch was refused at every duty from 0.2 to 0.8. Nothing about a plate makes a
    frame readable at one duty cycle, unreadable at the next, and readable again at the third -
    the product of the two halves of one period is what the bias was weighing.
    """
    verdict, periods = {}, {}
    for pitch in (104, 128):
        for duty in (0.2, 0.4, 0.5, 0.6, 0.8):
            cut = _cut(tmp_path / f"d{pitch}_{int(duty * 100)}", write_passages,
                       cells_px=4, period_px=pitch, duty=duty)
            assert cut.ink is not None
            verdict[(pitch, duty)] = bool(cut.counts_cells)
            periods[(pitch, duty)] = cut.ink.period_px
    assert all(verdict.values()), verdict
    assert set(periods.values()) == {104.0, 128.0}, periods
    assert {p for (p, _), v in periods.items() if v == float(p)} == {104, 128}, (
        "each frame must measure its own drawn pitch and not a submultiple of it")


def test_the_vertical_axis_can_be_reached_at_all(tmp_path):
    """ROUND 6 ADDED A CELL-PITCH MEASUREMENT DOWN COLUMNS AND NOTHING COULD REACH IT.

    A vertical profile is 960 samples where a horizontal one is 1280, so the bias bit sooner on
    the axis the new measurement was for: MEASURED, before round 7, vertical pitches of 64, 96 and
    128 px at cell heights of 8, 16 and 32 px ALL read a 2.0 px period and declined as holding no
    structure. The frames are passages running across the frame, which is half of what a cut of a
    channel array looks like depending on which way the plane was taken.

    WHAT THIS TEST ASKED FOR AND WHAT IT ASKS FOR NOW. It was written over cell heights of 8 and
    16 px against a claimed 4.00 px cell and required all six frames to discharge, which they did
    - on their ROW reading of 4.0 px, while their walls were counted in the 8 px and 16 px cell
    drawn down their columns. ONE PICTURE, STATED THREE WAYS: measured 2026-11-18, SYNTHETIC,
    VOLUME, 1280x960, the 64 px frame's 32 px of metal was published as 8.00 cells at cell_h=4,
    4.00 at cell_h=8 and 2.00 at cell_h=16 - and 32 px at the declared 4.00 px cell is 8.00 cells
    whatever the cells are drawn at. Round 8 reads the claim against the cell on the axis the wall
    and the period are counted on, so the figure and the check are the same one; the vertical axis
    is still reachable, which is what this test is for, and what reaches it is a frame drawn at
    the cell it declares. See `test_a_figure_is_read_on_the_axis_it_is_compared_on`.
    """
    verdict, periods, walls = {}, {}, {}
    for pitch in (64, 96, 128):
        for cell_h in (4, 8, 16):
            cut = _cut(tmp_path / f"v{pitch}_{cell_h}", write_horizontal_passages,
                       cell_w=4, cell_h=cell_h, band_px=pitch // 2, period_px=pitch)
            assert cut.ink is not None
            assert cut.ink.repeat_axis == "vertical", cut.ink.repeat_axis
            verdict[(pitch, cell_h)] = bool(cut.counts_cells)
            periods[(pitch, cell_h)] = cut.ink.period_px
            walls[(pitch, cell_h)] = round(cut.wall_cells, 2)
    assert periods == {(p, c): float(p) for p in (64, 96, 128) for c in (4, 8, 16)}, periods
    assert all(verdict[(p, 4)] for p in (64, 96, 128)), (
        f"the vertical axis is unreachable, so the column measurement round 6 added can never "
        f"decide anything: {verdict}")
    assert not any(verdict[(p, c)] for p in (64, 96, 128) for c in (8, 16)), (
        f"a vertical frame whose cells are twice or four times the cell its window declares is a "
        f"scale disagreement on the axis its own wall is counted on: {verdict}")
    assert (walls[(64, 4)], walls[(64, 8)], walls[(64, 16)]) == (8.0, 4.0, 2.0), (
        f"one 32 px wall, stated three ways by the three divisors: {walls}")


 # 2. the drawn cell is a cell

def test_the_drawn_cell_is_a_cell_and_not_a_jump_across_a_wall(tmp_path):
    """A PASSAGE TWO DRAWN CELLS WIDE LEAVES ONE ENCLOSED INTERIOR, so every gap was a wall jump.

    Its row bytes are [0, 176, 176, 176, 0, 176, 176, 176, 255 x8, ...]: the second interior runs
    into bare background, so only the first is closed on both sides and the spacing between
    consecutive closed interiors is the PITCH. MEASURED, before: the drawn cell read 12.0, 16.0,
    20.0, 24.0, 32.0, 40.0, 56.0, 72.0, 88.0 and 104.0 px for walls of 1 to 24 cells, against a
    claimed 4.00, and every one of them was refused as a magnification lie - while the cells in
    them are 4 px, exactly as declared.
    """
    pitches, verdict = {}, {}
    for wall in (1, 2, 3, 4, 6, 8, 12, 16, 20, 24):
        cut = _cut(tmp_path / f"w{wall}", write_cell_passages, passage_cells=2, wall_cells=wall)
        assert cut.ink is not None
        pitches[wall] = cut.ink.cell_pitch_px
        verdict[wall] = bool(cut.counts_cells)
    assert pitches == dict.fromkeys(pitches, 4.0), (
        f"the cells in these frames are 4 px across and the reading says {pitches}")
    assert verdict == {1: False, 2: True, 3: True, 4: True, 6: True, 8: True,
                       12: True, 16: True, 20: True, 24: True}, verdict


def test_the_drawn_cell_does_not_turn_on_the_parity_of_the_band_count(tmp_path):
    """TWO INTERIORS PER PASSAGE PUT A CELL AND A WALL JUMP ALTERNATELY IN THE GAP LIST.

    MEASURED, before: passage 3 cells / wall 3 cells has 106 gaps and gaps[53] read 20.0 px and
    the frame was REFUSED; passage 3 / wall 2 has 127 gaps and gaps[63] read 4.0 px and the frame
    was ADMITTED. The two drawings differ by one cell of wall and a run was lost or kept on
    whether the band count came out even.
    """
    three = _cut(tmp_path / "a", write_cell_passages, passage_cells=3, wall_cells=3)
    two = _cut(tmp_path / "b", write_cell_passages, passage_cells=3, wall_cells=2)
    assert three.ink is not None and two.ink is not None
    assert (three.ink.cell_pitch_px, two.ink.cell_pitch_px) == (4.0, 4.0), (
        f"both frames are drawn at 4 px per cell and they read "
        f"{three.ink.cell_pitch_px} and {two.ink.cell_pitch_px}")
    assert three.counts_cells and two.counts_cells, (
        "three cells of passage with three cells of metal between them is a true picture of this "
        "part, and it was refused where the same drawing with two cells of metal was admitted")


 # 3. the span is the span of the part that repeats

def test_the_extent_falls_as_the_real_array_shrinks(tmp_path):
    """THE FIGURE THAT IS SUPPOSED TO DISCLOSE A PLENUM WAS BEING SUPPLIED BY IT.

    `repeat_span` ran from the first rising edge of the material mask to the last, and a
    featureless field either side of an honest array supplies both ends. MEASURED, before:
    0.6258, 0.6758 and 0.7508 for array shares of 0.25, 0.35 and 0.50 - rising as the array
    shrank, and the sentence a reviewer reads said "over 62.58% of the frame" about a frame three
    quarters of which has no passage in it.
    """
    extents = {}
    for share in (0.25, 0.35, 0.5, 0.75):
        cut = _cut(tmp_path / f"s{int(share * 100)}", write_plenum_either_side,
                   array_share=share)
        assert cut.ink is not None
        extents[share] = round(cut.ink.extent_fraction, 4)
    assert extents == {0.25: 0.2258, 0.35: 0.3258, 0.5: 0.4758, 0.75: 0.7258}, extents
    assert sorted(extents.values()) == list(extents.values()), (
        f"the extent has to rise with the array's share of the frame and it went {extents}")
    for share, extent in extents.items():
        assert extent <= share, (
            f"an array over {share:.0%} of the width cannot cover {extent:.2%} of the frame")


 # 4. the measurement that separates anisotropy from a lie is taken

def test_the_column_cell_is_measured_on_a_horizontally_repeating_frame(tmp_path):
    """THE MEASUREMENT ROUND 7 ADDED, AND WHAT IT TURNED OUT NOT TO SETTLE.

    The planner pins the fine cell at the thin wall and lets it run long the other two ways, so a
    cell 8 px across by 4 px down in a window sized for a 4 px cell is a shape this part draws.
    The frame it has to be told from is a cell 8 px SQUARE declared at the same window, which is a
    factor of two in magnification. MEASURED, before round 7: both read 8.0 px along their rows
    and 0.0 down their columns, because the column scan ran only when the repeat was vertical -
    and the repeat in both is horizontal. The scan now runs on every frame and separates them:
    4.0 down one and 8.0 down the other. That measurement is still taken and still published, and
    this test holds it.

    WHAT IT MUST NOT DO IS GRANT. Round 7 let EITHER axis satisfy the claim, and the frame that
    walked through the opening was a third one: cells 2 px across by 4 px DOWN, honest metal
    between its passages, declared at a window that makes a cell 4.00 px - drawn at HALF the
    magnification it declares, admitted on its column, and published with a wall of 8.00 cells
    where the declared scale makes it 4.00 (MEASURED, SYNTHETIC, VOLUME, 1280x960; see
    `test_a_figure_is_read_on_the_axis_it_is_compared_on`). The wall and the period are counted on
    the axis the structure repeats on, so that is the axis the claim is checked on - and on that
    axis these two frames read the SAME 8.0 px. They are refused together, and the honest one is
    refused on its drawn cell rather than on its wall.
    """
    honest = _cut(tmp_path / "a", write_anisotropic_passages, cell_w=8, cell_h=4,
                  passage_cells=4, wall_cells=4)
    lie = _cut(tmp_path / "b", write_passages, cells_px=8, period_px=64, duty=0.5)
    assert honest.ink is not None and lie.ink is not None
    assert honest.ink.cell_pitch_px == lie.ink.cell_pitch_px == 8.0, (
        "on the axis both frames repeat on they are the same picture, which is the whole of why "
        "no measurement on that axis can tell them apart")
    assert (getattr(honest.ink, "cell_pitch_down_px", -1.0),
            getattr(lie.ink, "cell_pitch_down_px", -1.0)) == (4.0, 8.0), (
        "the column scan is the measurement that distinguishes them and it has to be taken on a "
        "frame whose repeat is horizontal")
    assert not lie.counts_cells, (
        "a cell 8 px square declared as 4 px is a factor of two in magnification")
    assert not honest.counts_cells, (
        "and the honestly anisotropic frame is refused with it, on the 8.0 px cell drawn along "
        "the axis its wall and its period are counted on - admitting it off the column is what "
        "admitted a frame drawn at half the magnification it declares")


 # 5. the wall the floor is applied to is the narrowest one in the frame

def test_the_narrowest_wall_is_what_the_floor_is_applied_to(tmp_path):
    """WALLS OF TWO WIDTHS HAVE NO SINGLE WIDTH, and the median of them is neither.

    MEASURED, before: walls alternating 16 px and 1 px read a median bare run of 8.5 px - 2.125
    drawn cells - and discharged their region over a floor of 2.00 that NEITHER wall in the
    picture clears. Half of them are the 1-cell rib `WALL_CELLS_MIN` says cannot be told from a
    1-cell wall and is refused with it. This part's own z-normal cut holds fins THAT COME IN
    PAIRS, so this is its shape and not a contrived one.

    AND A FRAME WHOSE NARROWEST WALL CLEARS THE FLOOR IS STILL ADMITTED: walls of 16 px and 8 px
    are 4 cells and 2, and 2 is the floor.
    """
    pairs = _cut(tmp_path / "a", write_alternating_walls, wall_a_px=16, wall_b_px=1)
    ok = _cut(tmp_path / "b", write_alternating_walls, wall_a_px=16, wall_b_px=8)
    assert pairs.ink is not None and ok.ink is not None
    assert pairs.ink.bare_run_px == 8.5, (
        "the median bare run is the figure that passed, and it is a width no wall here has")
    assert not pairs.counts_cells, (
        "a frame half of whose walls are a one-cell rib must not discharge a region, because a "
        "count taken across that rib is a count of nothing")
    assert ok.counts_cells, (
        "and a frame whose narrowest wall is two drawn cells is admitted on the same reading")
    assert getattr(pairs.ink, "bare_run_min_px", -1.0) == 1.0, (
        "the narrowest wall in this picture is one pixel, a quarter of a drawn cell")
    assert getattr(ok.ink, "bare_run_min_px", -1.0) == 8.0
    assert round(pairs.wall_cells, 2) == 0.25 and round(ok.wall_cells, 2) == 2.0


 # the guard: none of the above may let a frame with no metal in it through

def test_a_frame_with_no_metal_in_it_is_still_refused(tmp_path):
    """THE INVARIANT THIS ROUND MUST NOT WEAKEN, checked after all four fixes.

    A column with no ink in it cuts the ink into two pieces and depth variation can never produce
    one, so every picture with no metal in it measures a 0.00-cell wall and declines - and none of
    the arithmetic above touches that, because it is an occupancy test and they are all density
    and spacing. The ribs are here with them: their metal is real and it is thinner than a
    picture of this part's wall.
    """
    verdict, walls = {}, {}
    for deep in (200, 400, 470):
        cut = _cut(tmp_path / f"t{deep}", write_truncated_plenum, deep_px=deep)
        verdict[f"coolant truncated at {deep} px"] = bool(cut.counts_cells)
        walls[f"coolant truncated at {deep} px"] = cut.wall_cells
    for wall_px, period_px in ((2, 32), (4, 32), (2, 64)):
        cut = _cut(tmp_path / f"r{wall_px}_{period_px}", write_plenum_with_ribs,
                   wall_px=wall_px, period_px=period_px)
        key = f"plenum, {wall_px} px rib every {period_px} px"
        verdict[key] = bool(cut.counts_cells)
        walls[key] = cut.wall_cells
    assert not any(verdict.values()), verdict
    assert walls == {"coolant truncated at 200 px": 0.0, "coolant truncated at 400 px": 0.0,
                     "coolant truncated at 470 px": 0.0,
                     "plenum, 2 px rib every 32 px": 0.5,
                     "plenum, 4 px rib every 32 px": 1.0,
                     "plenum, 2 px rib every 64 px": 0.5}, walls
