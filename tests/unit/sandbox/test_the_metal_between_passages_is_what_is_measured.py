# Responsibility: Verify that the figure which decides whether a frame is a picture of passages
# measures METAL - samples with no coolant in them - on the AXIS THE STRUCTURE REPEATS ON, and
# that every true picture of passages with metal between them is admitted.
# Boundaries: the home side only, and only what can be measured off a PNG plus the one plan
# figure. Nothing here renders and nothing here reads a manifest for a size.
#
# THREE DEFECTS THIS IS WRITTEN FROM, all found by independent review, all SYNTHETIC and
# VOLUME-shaped, all at 1280x960 with a claimed 4.00 px cell, all through the production
# ArtifactResolver and ResolvedCut:
#
#   1. DENSITY WAS BEING READ AS OCCUPANCY. `_profile_period` cut its material mask at half the
#      profile's own 95th percentile, which is a per-column ink DENSITY contrast, so a field of
#      coolant whose section varies in depth read its shallow columns as metal. Measured: a frame
#      of 4 px coolant cells covering the whole frame with every other 16 px band truncated
#      partway down came back 39 repeats, period 32.0 px, 0.975, duty 0.5004, wall 3.9968 cells
#      and DISCHARGED its region - at truncation depths of 200, 400 and 470 px alike. It contains
#      no metal at all: zero columns with no ink, widest all-background column run 0 px, and all
#      915,200 inked pixels in ONE 4-connected body. It scored BETTER on the separating figure
#      than the honest frame, which read 3.9968 hard-edged and 3.7472 anti-aliased.
#
#   2. TWO AXES WERE MIXED INTO ONE RATIO. `ink_structure` reports the period on whichever axis
#      shows the repeat; `_measure_png` only ever measured the drawn cell along ROWS. Measured:
#      passages running across the frame at a 32 px VERTICAL period, drawn with cells 4 px wide,
#      read 8.00 drawn cells per period and a 3.996-cell wall and DISCHARGED, where the truth in
#      the direction the structure repeats is half a cell per period. Cell heights of 16, 32 and
#      64 px gave byte-for-byte identical frames and therefore identical figures.
#
#   3. THE HONEST WINDOW WAS 1.43x WIDE. Measured, passages 4 cells wide with walls of 1 to 20
#      cells: walls 1 and 2 refused, 3 to 6 admitted, 8, 12 and 20 refused; passages 8 cells with
#      a 4-cell wall and passages 12 cells with a 4-cell wall both refused. Every refused frame
#      there is a TRUE PICTURE of real passages with real metal between them, and a gate that
#      refuses one costs the user a run and sends back "evidence missing".
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED. Every assertion is on behaviour - whether a
# frame discharges its region, and what the measured wall is - read through `getattr` where the
# field is new, so each test is COLLECTED against the pre-change tree and fails there on its own
# assertion rather than on an import.
from __future__ import annotations

from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_anti_aliased_passages,
    write_cell_passages,
    write_half_array_half_plenum,
    write_horizontal_passages,
    write_passages,
    write_plenum_with_ribs,
    write_truncated_plenum,
)

from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
CELL_M = 2.51578e-05              # quality.near_wall_cell_m, and fine_cell_pinned_m
FRAME_PX = (1280, 960)
#: The producer's own zoom scale: one planned cell four pixels across. 6.28945 um/px.
MPP = CELL_M / 4.0

#: Restated rather than imported, so every test here runs against the OLD tree too.
WALL_CELLS_MIN = 2.0

REGION = {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
          "plane_axes": [1, 2], "label": "Square across the coolant's path",
          "why": "the cells across each passage, and the metal between them"}


def _manifest(ws: Path, frames: list[dict]) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    return {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
            "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                        "max_cell_m": 0.0006788786304584814},
            "inspection_regions": [dict(REGION, frames=frames)]}


def _cut(ws: Path, draw, *, mpp: float = MPP, **kw):
    """Draw one frame with `draw`, declare it, and resolve it exactly as production does."""
    rel = "review/cuts/across_the_flow_zoom.png"
    draw(ws / rel, *FRAME_PX, **kw)
    man = _manifest(ws, [{"scope": FRAME_ZOOM, "image": rel,
                          "window_m": [mpp * FRAME_PX[0], mpp * FRAME_PX[1]],
                          "metres_per_pixel": mpp,
                          "centre_m": [0.0309152940, 0.0242716710],
                          "centre_source": "the densest window-sized patch of this cut"}])
    got = ArtifactResolver(ws, man).resolve_cuts().get("across_the_flow", ())
    assert got, "the frame was not offered at all, so nothing below is being tested"
    return got[0]


 # 1. a field of coolant is not a bank of passages, however its section varies

def test_a_field_of_coolant_with_no_metal_in_it_does_not_discharge_its_region(tmp_path):
    """THE WHOLE FRAME IS COOLANT. Every other 16 px band stops 470 px down, and that is all.

    MEASURED, SYNTHETIC, VOLUME: 74.4792% ink, 39 repeats, period 32.0 px, 0.975 of a perfect
    array, extent 0.9758, 319 enclosed interiors on its best row over 720 rows, a 4.0 px drawn
    cell - every bar of rounds 1 to 5 cleared, and a duty of 0.5004 that made the wall read
    3.9968 cells, BETTER than the honest frame's. There is no metal in it: zero columns with no
    ink, widest all-background column run 0 px, one connected body of 915,200 pixels.

    The picture is of a plenum whose depth varies. A wall measured as the metal between passages -
    samples carrying no ink at all - reads 0.00 cells in it, and that is why it now declines.
    """
    cut = _cut(tmp_path, write_truncated_plenum, deep_px=470)
    assert cut.ink is not None
    # every gate that existed before this round passes on this frame
    assert cut.ink.shows_structure, "a periodic structure really is in it"
    assert cut.ink.repeats == 39 and cut.ink.period_px == 32.0
    assert cut.ink.strength >= 0.975 and cut.ink.extent_fraction >= 0.97
    assert cut.ink.cells_on_best_row == 319 and cut.ink.cell_rows == 720
    assert cut.ink.cell_pitch_px == 4.0 and cut.cell_px == 4.0
    assert abs(cut.ink.ink_fraction - 0.744792) < 1e-5, cut.ink.ink_fraction
    # THE VERDICT FIRST, because that is the behaviour that changed
    assert not cut.counts_cells, (
        "a field of coolant with no metal anywhere in it must not discharge a region, whatever "
        "its section does")
    # and the separating figure says what the picture actually holds
    assert cut.wall_cells == 0.0, (
        "no sample along the repeat axis is bare, so the metal between these 'passages' is "
        f"0.00 cells and not {cut.wall_cells:.4f}")


def test_the_depth_it_is_truncated_at_changes_nothing(tmp_path):
    """200, 400 and 470 px all gave duty 0.5004 and a 4-cell wall and all discharged.

    The density contrast fires as soon as the shallow columns fall under half the deep ones, so
    the defect is not a boundary case of one depth - it is every plenum whose section varies by
    more than about 2x. All three must decline, and the ink fraction proves they are three
    different pictures.
    """
    inks = []
    for deep in (200, 400, 470):
        cut = _cut(tmp_path / f"d{deep}", write_truncated_plenum, deep_px=deep)
        assert cut.ink is not None and cut.ink.shows_structure
        inks.append(round(cut.ink.ink_fraction, 4))
        assert not cut.counts_cells, (
            f"a plenum truncated at {deep} px is still a plenum")
    assert inks == [0.6042, 0.7083, 0.7448], inks


def test_the_honest_frame_measures_its_wall_exactly(tmp_path):
    """And the honest frame is unharmed: 4 cells of passage, 4 of bare metal, wall 4.00 EXACTLY.

    THE PUBLISHED FIGURE IS BEHAVIOUR, because it is the number the reviewer is handed. The duty
    form read 3.9968 for this same drawing - the arithmetic drifting rather than the picture
    changing - and the drift was not harmless: a wall drawn exactly two cells thick read 1.9986
    and fell under a floor of 2.0 that it sits ON, so a frame that satisfied the floor by
    construction was refused by rounding.
    """
    four = _cut(tmp_path / "a", write_cell_passages, passage_cells=4, wall_cells=4)
    two = _cut(tmp_path / "b", write_cell_passages, passage_cells=4, wall_cells=2)
    assert four.ink is not None and four.ink.ink_fraction == 0.5
    assert two.counts_cells, (
        "a wall drawn exactly WALL_CELLS_MIN cells thick must not be refused by rounding")
    assert four.counts_cells
    assert (four.wall_cells, two.wall_cells) == (4.0, 2.0), (four.wall_cells, two.wall_cells)


def test_a_rib_in_a_plenum_is_still_refused(tmp_path):
    """The round-5 frames are not let back in. A bare 2 px rib every 32 px measures half a cell.

    AND A ONE-CELL RIB IS REFUSED WITH IT, which is measured and is why the floor cannot be
    lower: a plenum with a 1-cell rib (28 px of coolant, 4 px bare) and an honest array of
    passages 7 cells wide with a 1-cell wall (28 px of coolant, 4 px bare) are the same drawing
    four pixels apart, and every figure measured off either one is identical - 87.5000% ink, 39
    repeats, period 32.0 px, 0.975, a 4.0 px drawn cell, 240 interiors over 720 rows, a 4.0 px
    bare run. No floor admits the second and refuses the first.
    """
    thin = _cut(tmp_path / "a", write_plenum_with_ribs, wall_px=2, period_px=32)
    thick = _cut(tmp_path / "b", write_plenum_with_ribs, wall_px=4, period_px=32)
    coarse = _cut(tmp_path / "c", write_plenum_with_ribs, wall_px=2, period_px=64)
    assert not thin.counts_cells and not thick.counts_cells and not coarse.counts_cells
    assert abs(thin.wall_cells - 0.5) < 0.01 and abs(thick.wall_cells - 1.0) < 0.01
    assert abs(coarse.wall_cells - 0.5) < 0.01
    same = _cut(tmp_path / "d", write_cell_passages, passage_cells=7, wall_cells=1)
    assert not same.counts_cells
    assert abs(same.wall_cells - thick.wall_cells) < 0.01, (
        "the one-cell rib and the one-cell wall measure the same thing because they ARE the same "
        "drawing")


 # 2. the period and the cell are read on the same axis

def test_a_vertical_period_is_not_divided_by_a_horizontal_cell(tmp_path):
    """PASSAGES ACROSS THE FRAME: a 32 px VERTICAL period, drawn with cells 4 px wide.

    MEASURED, SYNTHETIC, VOLUME: 50.0000% ink, 29 repeats at a 32.0 px period on the VERTICAL
    axis, 0.967 of a perfect array, extent 0.9677, 319 enclosed interiors on the best row over
    450 rows. The drawn cell was measured along ROWS at 4.0 px, so the ratio came out 8.00 drawn
    cells per period with a 3.996-cell wall - the honest frame's figures exactly - and the frame
    discharged its region. In the direction the structure repeats, a 64 px cell gives half a cell
    per period.

    Anisotropic near-wall cells are ordinary on this part: the planner pins the fine cell AT the
    thin wall's thickness and lets it run long the other two ways. This is not a contrived frame.
    """
    cut = _cut(tmp_path, write_horizontal_passages, cell_w=4, cell_h=64, band_px=16, period_px=32)
    assert cut.ink is not None
    assert cut.ink.shows_structure and cut.ink.repeats == 29
    assert cut.ink.period_px == 32.0 and cut.ink.cell_pitch_px == 4.0
    assert cut.ink.cells_on_best_row == 319 and cut.ink.cell_rows == 450
    assert cut.ink.strength >= 0.967 and cut.ink.extent_fraction >= 0.96
    assert not cut.counts_cells, (
        "a vertical period divided by a horizontal cell is not a measurement of anything, and "
        "must not discharge a region")
    assert cut.period_cells == 0.0, (
        "no cell interior is resolved down any column of this frame, so there is no drawn cell to "
        f"count a vertical period in - and it reported {cut.period_cells:.2f}")
    assert getattr(cut.ink, "repeat_axis", "") == "vertical", (
        "the repeat really is on the frame's vertical axis")


def test_the_cell_height_changes_the_figures_now(tmp_path):
    """Cell heights of 16, 32 and 64 px gave BYTE-FOR-BYTE IDENTICAL figures before this.

    At band_px=16 the three drawings are themselves byte-identical, because a 16 px band has no
    room for a second horizontal cell line - so the reading being the same proved nothing. Drawn
    in a band tall enough to HOLD its own cell height, the vertical cell is measurable, and the
    figures separate: 16 px gives a 16.0 px cell down a column and 32 px gives 32.0 px. Before
    the column scan existed, both read a 0.500-cell period and a 0.2497-cell wall off the
    horizontal pitch.

    THE 32 PX FIGURE IS CORRECTED IN ROUND 7, from 128.0 px, and the correction is why this frame
    now discharges. A 64 px band holds exactly TWO 32 px cells, and the lower one is cut off by
    the band's own edge rather than closed by a drawn line - so only ONE enclosed interior exists
    per band, every gap between consecutive interiors was a JUMP ACROSS THE BARE WALL, and the
    "drawn cell" read 128.0 px, which is the passage pitch. The cell is now measured between the
    two ink runs that bound one interior, so it reads 32.0, and the 64 px of metal between these
    passages is 2.00 cells - which is a true picture of a two-cell wall and is admitted. See
    `review_session._cell_runs`.
    """
    walls, down, admitted = [], [], []
    for cell_h in (16, 32):
        cut = _cut(tmp_path / f"h{cell_h}", write_horizontal_passages,
                   cell_w=4, cell_h=cell_h, band_px=64, period_px=128)
        assert cut.ink is not None and cut.ink.cell_pitch_px == 4.0, (
            "the HORIZONTAL cell is 4 px in both, which is why the old reading could not tell "
            "them apart")
        walls.append(round(cut.wall_cells, 4))
        down.append(getattr(cut.ink, "cell_pitch_down_px", -1.0))
        admitted.append(bool(cut.counts_cells))
    assert walls[0] != walls[1], (
        f"both cell heights still measure the same {walls[0]} cells of wall off the horizontal "
        "pitch, so the reading cannot see the cell it is dividing by")
    assert walls == [4.0, 2.0], walls
    assert down == [16.0, 32.0], down
    # AND NEITHER OF THEM IS OFFERED AS EVIDENCE, which is round 8's correction to this test.
    # The two readings above are the whole reason: ONE 64 px wall, published as 4.00 cells and as
    # 2.00 cells, while the cell this frame's window declares makes it 16.00. A frame whose cells
    # on the axis its wall is counted on are four and eight times the cell it declares has not
    # resolved that cell, and a reviewer reading either figure against the plan's four-cell wall
    # would be reading a number taken at a scale the frame does not have. Cells 4 px tall in the
    # same drawing are admitted - see
    # `test_a_figure_is_read_on_the_axis_it_is_compared_on.py::
    # test_a_vertical_frame_is_judged_on_the_cell_drawn_down_its_columns`.
    assert admitted == [False, False], (
        "one 64 px wall cannot be 4.00 cells and 2.00 cells and 16.00 claimed cells and be "
        f"offered as a count of this part's wall in all three: {walls} against a 4.00 px claim")


 # 3. the window: any pitch, any passage, two drawn cells of metal

def test_every_true_picture_of_passages_with_metal_between_them_is_admitted(tmp_path):
    """PASSAGES 4 CELLS WIDE WITH WALLS OF 1 TO 20 CELLS, which is the whole measured window.

    Round 5 admitted walls of 3, 4, 5 and 6 only - a pitch of 7 to 10 drawn cells, a factor of
    1.43 on one quantity - and refused walls of 1, 2, 8, 12 and 20. Every one of those is a true
    picture of real passages. The window now is ANY PITCH with at least WALL_CELLS_MIN drawn
    cells of bare metal between passages, so only the 1-cell wall is refused, and that one
    because it is the same drawing as a 1-cell rib in a plenum.
    """
    verdict, measured = {}, {}
    for wall in (1, 2, 3, 4, 5, 6, 8, 12, 20):
        cut = _cut(tmp_path / f"w{wall}", write_cell_passages,
                   passage_cells=4, wall_cells=wall)
        assert cut.ink is not None and cut.ink.shows_structure
        verdict[wall] = bool(cut.counts_cells)
        measured[wall] = cut.wall_cells
    assert verdict == {1: False, 2: True, 3: True, 4: True, 5: True, 6: True,
                       8: True, 12: True, 20: True}, verdict
    assert measured == {w: float(w) for w in verdict}, measured


def test_a_wide_passage_with_the_parts_own_wall_is_admitted(tmp_path):
    """PASSAGES 8 AND 12 CELLS WIDE WITH THE PLAN'S OWN 4-CELL WALL, both refused by round 5.

    Their pitch is 12 and 16 drawn cells, outside the band round 5 held the pitch to - and the
    metal between their passages is exactly the four cells the plan puts across this part's thin
    wall. There is nothing wrong with either picture. The plan pins the fine cell AT the wall's
    thickness, so the WALL is the quantity it fixes; the passage width is the part's, and the
    pitch is passage plus wall.
    """
    for passage in (8, 12):
        cut = _cut(tmp_path / f"p{passage}", write_cell_passages,
                   passage_cells=passage, wall_cells=4)
        assert cut.ink is not None
        assert cut.counts_cells, (
            f"passages {passage} cells wide with the plan's own four-cell wall between them is a "
            "true picture of this part, and refusing it costs the user a run")
        assert cut.period_cells == float(passage + 4), cut.period_cells
        assert cut.wall_cells == 4.0


def test_anti_aliasing_costs_about_a_quarter_of_a_cell_and_is_stated(tmp_path):
    """WHERE THE EDGE OF THE WINDOW REALLY SITS once a rasteriser has been over the frame.

    A box filter over an 8x supersampled drawing - which is what anti-aliasing applies - paints a
    trace of ink into the column straddling each edge, so a bare run reads about 1 px narrower
    than drawn. MEASURED, SYNTHETIC, VOLUME, at a sub-pixel offset with the fill shaded: the
    honest 4-cell wall reads 16.0 px and 4.00 cells hard-edged and 15.0 px and 3.75 cells
    anti-aliased, clearing WALL_CELLS_MIN by 2.00x and 1.87x. A wall drawn exactly two cells
    thick reads 2.00 and is admitted, and 1.75 and is not - so two cells is the EDGE of the
    window and three cells is inside it.

    AND THE WALL IS WHAT IS HELD, NOT THE PITCH, so a table of these readings has to say which
    quantity it varied. The two frames below hold the PASSAGE at 16 px and grow the pitch, so the
    pitch names the wall: pitch 24 is a two-cell wall and pitch 28 a three-cell one. AT THE
    FIXTURE'S OWN DEFAULT DUTY OF 0.5 the same pitches mean different walls, and the whole table
    shifts one step - re-measured 2026-11-18, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell,
    through the production ArtifactResolver:

        drawn pitch, duty 0.5      16      20      24      28      32
        wall, hard-edged         2.00    2.50    3.00    3.50    4.00   all admitted
        wall, anti-aliased       1.75    2.25    2.75    3.25    3.75   16 px refused

    so at duty 0.5 the refused edge is a 16 px pitch at 1.75 cells, and pitch 24 reads 2.75 and is
    ADMITTED - which is the row a reader of this file would otherwise take from the assertions
    below, where the same figures belong to pitches 24 and 28.
    """
    soft = _cut(tmp_path / "b", write_anti_aliased_passages, period_px=32, duty=0.5,
                offset_px=0.375, edge_px=1.0, lit=90)
    assert soft.ink is not None and soft.counts_cells
    # THE EDGE OF THE WINDOW: the same two-cell wall, drawn hard and drawn soft
    hard_two = _cut(tmp_path / "c", write_cell_passages, passage_cells=4, wall_cells=2)
    soft_two = _cut(tmp_path / "d", write_anti_aliased_passages, period_px=24,
                    duty=16.0 / 24.0, offset_px=0.375, edge_px=1.0, lit=90)
    soft_three = _cut(tmp_path / "e", write_anti_aliased_passages, period_px=28,
                      duty=16.0 / 28.0, offset_px=0.375, edge_px=1.0, lit=90)
    assert hard_two.counts_cells, (
        "a wall drawn exactly two cells thick sits ON the floor and is admitted")
    assert not soft_two.counts_cells, (
        "and the same wall anti-aliased reads a quarter of a cell narrower and is not")
    assert soft_three.counts_cells, "three cells of wall is inside the window either way"
    assert (soft.wall_cells, hard_two.wall_cells, soft_two.wall_cells,
            soft_three.wall_cells) == (3.75, 2.0, 1.75, 2.75)


def test_a_frame_drawn_coarser_than_it_claims_is_still_refused(tmp_path):
    """THE ONE FRAME THE DELETED PERIOD BAND WAS CATCHING, and it must still be caught.

    An array drawn at 8 px cells on a 64 px period and DECLARED at the honest 6.28945 um/px
    window reports 8.00 drawn cells per period and a 4.00-cell wall - the honest frame's figures
    exactly - while its period in metres is 402.52 um, a different feature of this plate. What
    refuses it is the drawn cell against the claimed one: 8.0 px drawn against 4.00 px claimed is
    a factor of two, and `CELL_PITCH_TOLERANCE` is now narrow enough to say so. It used to admit
    exactly 2.00 at its own boundary and leave the work to a band on the period.
    """
    coarse = _cut(tmp_path / "a", write_passages, cells_px=8, period_px=64, duty=0.5)
    honest = _cut(tmp_path / "b", write_passages, cells_px=4, period_px=32, duty=0.5)
    assert coarse.ink is not None and honest.ink is not None
    assert coarse.ink.cell_pitch_px == 8.0 and honest.ink.cell_pitch_px == 4.0
    assert coarse.cell_px == honest.cell_px == 4.0
    assert abs(coarse.wall_cells - 4.0) < 0.01, (
        "the picture really does have the part's own shape in it, which is why the pixels alone "
        "cannot refuse it")
    assert abs(coarse.ink.period_px * coarse.m_per_px - 4.02525e-4) < 1e-7
    assert not coarse.counts_cells, (
        "a frame drawn at twice the magnification it claims must not discharge a region")
    assert not coarse.cells_resolved, (
        "and THIS is now the test that refuses it: a drawn cell twice the claimed one is not a "
        "cell of this mesh, where the old tolerance admitted exactly a factor of two and left "
        "the work to a band on the period")
    assert honest.counts_cells and honest.cells_resolved


 # the one this does NOT cover, stated rather than patched

def test_half_an_array_and_half_a_plenum_still_discharges_and_that_is_said(tmp_path):
    """A FRAME HALF HONEST ARRAY AND HALF FEATURELESS COOLANT PASSES IN BOTH ARMS.

    MEASURED, SYNTHETIC, VOLUME: 75.0000% ink, 20 repeats, period 32.0 px, extent 0.5008, and a
    4-cell wall. The occupancy fix does not reach it and this test says so rather than a fifth
    bar being added for it: the repeat span runs from the first rising edge to the LAST, which is
    the plenum's single run, so the wall is measured on the array half - where the wall really is
    four cells - and the plenum half contributes no bare run to the median at all.

    IT IS ALSO NOT THE SAME KIND OF WRONG as the three defects above. Half of this frame IS a
    true picture of the passages, drawn at the right scale, with the right wall; a reviewer
    counting cells across a passage in it would get the right answer. What is missing is any word
    about the other half. The extent published with it, 50.08%, is the only figure that says so,
    and it is in the sentence the reviewer reads.
    """
    cut = _cut(tmp_path, write_half_array_half_plenum)
    assert cut.ink is not None
    assert cut.ink.ink_fraction == 0.75 and cut.ink.repeats == 20
    assert abs(cut.ink.extent_fraction - 0.5008) < 1e-4, cut.ink.extent_fraction
    assert abs(cut.wall_cells - 4.0) < 0.01
    assert cut.counts_cells, (
        "this is the measured state of the code and not an endorsement: half this frame is a "
        "plenum and nothing refuses it")


 # the pinning test: the literal above is the production constant

def test_the_wall_floor_is_the_production_one_and_the_period_band_is_gone():
    """The one test here that imports what this round changed, for the reason the round-5 file
    states: a test that cannot be collected against the old tree proves only that a name moved."""
    import meshpipeline.sandbox.picture_structure as P
    from meshpipeline.sandbox.review_session import CELL_PITCH_TOLERANCE

    assert P.WALL_CELLS_MIN == WALL_CELLS_MIN == P.PLAN_CELLS_ACROSS_A_WALL / 2.0
    assert not hasattr(P, "period_is_the_parts"), (
        "the band that held a period in drawn cells to the plan's wall figure is deleted, and "
        "with it the 1.43x window on pitch")
    assert CELL_PITCH_TOLERANCE == 1.5, (
        "narrowed from 2.0, which admitted a factor-of-two magnification lie on its own boundary")
    assert P.PERIOD_MATCH == 2.5, (
        "still 2.5, and still the producer's: `period_matches` holds one cut's measured period "
        "as the yardstick for the other cuts of the same mesh")
