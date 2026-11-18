# Responsibility: Verify that the sentence a reviewer reads about a frame that DISCHARGES its
# region claims only the three things this side established - a periodic structure at a measured
# pitch, cells countable across it, and a measured run of metal between two repeats - and does not
# call it a passage array.
# Boundaries: the words. Nothing here renders, nothing here decides coverage, and nothing here
# adds a bar: every frame below is admitted in both arms and must stay admitted.
#
# THE DEFECT THIS IS WRITTEN FROM. Round 6 deleted the band that held a frame's period to a figure
# derived from the plan. Round 7 measured that the ground for deleting it - "the wall floor keeps
# this plate's other regular features out" - is false, and softened the clause AFTER the figures
# while leaving the OPENING asserting the thing the clause denies.
#
# MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell, through the production ArtifactResolver,
# on both arms: a solid field of coolant with an 8 px (two-cell) BARE RIB every 32, 64 and 128 px
# reads 39, 19 and 9 repeats at 1.000 of a perfect array with a 2.00-cell wall, and DISCHARGES its
# region - and 64 px and 128 px are this plate's own 402.525 um and 805.050 um features, four and
# eight times its passage pitch. There is no bank of passages in any of the three. The sentence
# they got opened "A PASSAGE ARRAY WITH METAL BETWEEN ITS PASSAGES IS IN THIS FRAME".
#
# NO BAR IS ADDED FOR THIS, deliberately: `counts_cells` stays True on all three, because what
# this side measured - a pitch, a cell, a wall - is true of them, and a reviewer told it cannot
# count cells in a frame full of countable cells is being told something false instead.
from __future__ import annotations

from pathlib import Path

from _reviewer_part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_horizontal_passages,
    write_passages,
    write_plenum_with_ribs,
)

from meshpipeline.agents.reviewer.scene_text import CANNOT_COUNT, cut_frame_line
from meshpipeline.sandbox.picture_structure import PLAN_CELLS_ACROSS_A_WALL, WALL_CELLS_MIN
from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

CELL_M = 2.51578e-05              # quality.near_wall_cell_m on job 0ff6f9e5
FRAME_PX = (1280, 960)
MPP = CELL_M / 4.0                # the producer's own zoom scale, 6.28945 um/px


def _line(ws: Path, draw, **kw) -> str:
    rel = "review/cuts/across_the_flow_zoom.png"
    draw(ws / rel, *FRAME_PX, **kw)
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    man = {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
           "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                       "max_cell_m": 0.0006788786304584814},
           "inspection_regions": [{
               "name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
               "plane_axes": [1, 2], "label": "Square across the coolant's path",
               "why": "the cells across each passage, and the metal between them",
               "frames": [{"scope": FRAME_ZOOM, "image": rel,
                           "metres_per_pixel": MPP,
                           "window_m": [MPP * FRAME_PX[0], MPP * FRAME_PX[1]],
                           "centre_m": [0.0309152940, 0.0242716710],
                           "centre_source": "the densest window-sized patch of this cut"}]}]}
    cuts = ArtifactResolver(ws, man).resolve_cuts()["across_the_flow"]
    return cut_frame_line(cuts[0])


def test_a_field_of_coolant_with_a_rib_is_not_called_a_passage_array(tmp_path):
    """THE THREE FRAMES THAT REFUTE THE CLAIM, and two of them are this plate's own features.

    Each is a solid field of coolant - one connected body of it - with a bare two-cell rib every
    32, 64 and 128 px. Each discharges its region, which is the state round 7 measured and did not
    act on, and each was introduced as a passage array.
    """
    for period_px, um in ((32, 201.262), (64, 402.525), (128, 805.050)):
        line = _line(tmp_path / f"f{period_px}", write_plenum_with_ribs,
                     wall_px=8, period_px=period_px)
        assert CANNOT_COUNT not in line, (
            f"the {um} um frame still discharges, which is why the WORDS are what had to change")
        assert "PASSAGE ARRAY" not in line, (
            f"nothing measured here makes the {um} um feature a passage array, and the frame is a "
            f"field of coolant with one rib in it: {line}")
        assert ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME as "
                "measured here") in line, line
        assert "WHETHER THAT PITCH IS THIS PART'S PASSAGE PITCH WAS NOT ESTABLISHED HERE" in (
            line), line
        assert "402.525 um and 805.050 um" in line, line
        assert "a field of coolant with a two-cell rib in it and no passage at all" in line, line


def test_the_honest_frame_gets_the_same_sentence_and_no_more(tmp_path):
    """AND THE TRUE PICTURE IS NOT FLATTERED EITHER. The honest frame's figures are the part's
    own - 8.00 drawn cells per period, 4.00 cells of metal - and the words still stop at what was
    measured, because the two frames are indistinguishable on every figure in the sentence except
    the ink.
    """
    line = _line(tmp_path, write_passages, period_px=32, duty=0.5)
    assert CANNOT_COUNT not in line, line
    assert "PASSAGE ARRAY" not in line, line
    assert "8.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert "the narrowest metal between two of its repeats is 16.0 px - 4.00 cell(s)" in line, (
        line)
    assert "cells are countable in this frame" in line, line


def test_the_two_frames_cannot_be_told_apart_by_the_claim_either_makes(tmp_path):
    """THE POINT OF THE CHANGE, stated as the test the brief asks for: the honest frame and the
    plenum with a rib get the SAME opening, because this side measured the same things in both.

    The figures differ - 4.00 cells of wall against 2.00, 50.00% ink against 75.00% - and they are
    all in the sentence. What is gone is the word that told the reader which of the two it was
    looking at on no measurement at all.
    """
    honest = _line(tmp_path / "a", write_passages, period_px=32, duty=0.5)
    rib = _line(tmp_path / "b", write_plenum_with_ribs, wall_px=8, period_px=32)
    opening = ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME "
               "as measured here")
    assert opening in honest and opening in rib
    assert honest != rib, "the figures in them differ and the reader is shown all of them"
    assert "4.00 cell(s)" in honest and "2.00 cell(s)" in rib


# ---------------------------------------------------------------------------------------------
# ROUND 10. THE TWO PLACES THE SAME DEFECT CLASS WAS STILL LIVE.
#
# Round 8 moved the gate to the cell drawn on the axis the structure repeats on. Round 9 followed
# it in the refusal log and in the GRANT branch of `_measured_clause`. Two places did not follow
# it, and both of them speak to the reviewer in CELLS off a figure the verdict was not taken on.
# ---------------------------------------------------------------------------------------------

#: The frames that reach the NOT-ENOUGH-METAL branch with `cells_resolved` False, which a
#: 322-frame sweep hit ZERO times - which is why round 9 missed the branch and why these are
#: written down by hand. Each has a ONE-CELL wall in the cell actually drawn (so the branch is
#: reached) and a drawn cell on the repeat axis that is twice or four times the 4.00 px the window
#: declares (so `cells_resolved` is False). The first four repeat VERTICALLY and the last two
#: HORIZONTALLY, so neither axis is the thing being tested.
_NO_CELL_AND_NO_METAL = (
    ("vertical p2c w1c, 8 px cell", write_horizontal_passages,
     {"cell_w": 4, "cell_h": 8, "band_px": 16, "period_px": 24}),
    ("vertical p4c w1c, 8 px cell", write_horizontal_passages,
     {"cell_w": 4, "cell_h": 8, "band_px": 32, "period_px": 40}),
    ("vertical p8c w1c, 8 px cell", write_horizontal_passages,
     {"cell_w": 4, "cell_h": 8, "band_px": 64, "period_px": 72}),
    ("vertical p4c w1c, 16 px cell", write_horizontal_passages,
     {"cell_w": 4, "cell_h": 16, "band_px": 64, "period_px": 80}),
    ("horizontal rib, 8 px cell, 64 px pitch", write_plenum_with_ribs,
     {"cells_px": 8, "wall_px": 8, "period_px": 64}),
    ("horizontal rib, 8 px cell, 128 px pitch", write_plenum_with_ribs,
     {"cells_px": 8, "wall_px": 8, "period_px": 128}),
)


def test_a_frame_with_no_countable_cell_is_told_its_wall_in_pixels_not_in_cells(tmp_path):
    """THE BRANCH ROUND 9's FIX DID NOT REACH: not enough metal, and no cell to count it in.

    `shows_metal_between_passages` is measured in `repeat_pitch_px` - the cell ACTUALLY DRAWN on
    the repeat axis - and says nothing about whether that cell is the size the window declares.
    `cells_resolved` is the test that asks THAT. So this branch could be reached by a frame the
    gate refuses, and on all six frames below it published "N.NN drawn cell(s) per period", a
    "- N.NN cell(s)" wall suffix and TWO cell-count comparisons - against the two-cell wall floor
    and against the plan's four cells across the thin wall - on a line ending "CELLS ARE NOT
    COUNTABLE IN THIS FRAME - do not state a cell count". Three false cell figures per frame.

    MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the production
    ArtifactResolver, on the pre-change tree: all six carry all three figures. The first of them
    read "9.00 drawn cell(s) per period at the 8.0 px cell drawn on that axis ... 8.0 px at its
    narrowest - 1.00 cell(s) ... against the 2.0 cells of wall a picture of this part has to show
    and the 4.0 cells the plan puts across its thin wall".

    NO VERDICT MOVES. Every frame here is refused in both arms, on the same two measurements.
    """
    for name, draw, kw in _NO_CELL_AND_NO_METAL:
        line = _line(tmp_path / name.replace(" ", "_").replace(",", ""), draw, **kw)
        assert "THERE IS NOT ENOUGH METAL BETWEEN ITS PASSAGES TO BE A PICTURE OF THEM" in line, (
            f"the premise: {name} must still reach this branch - the gate is unchanged:\n{line}")
        assert CANNOT_COUNT in line, (
            f"the premise: {name} must still be refused as uncountable:\n{line}")
        assert "drawn cell(s) per period" not in line, (
            f"{name} publishes a period in cells divided by a drawn cell the same line says is "
            f"not the one the window declares:\n{line}")
        assert "cell(s)" not in line.split("THERE IS NOT ENOUGH METAL")[1].split(
            "EVERY FIGURE IN THIS SENTENCE")[0], (
            f"{name} still states a cell count between the verdict and the pixels-only "
            f"disclaimer:\n{line}")
        assert f"against the {WALL_CELLS_MIN:.1f} cells of wall" not in line, (
            f"{name} holds its wall against the two-cell floor as a cell count:\n{line}")
        assert f"{PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan puts" not in line, (
            f"{name} holds its wall against the plan's cell count:\n{line}")
        assert "EVERY FIGURE IN THIS SENTENCE IS IN PIXELS AND NONE OF THEM IS IN CELLS" in (
            line), f"{name} drops the figures without saying why:\n{line}"
        assert "px at its narrowest" in line, (
            f"{name} must still publish the metal it measured, in pixels:\n{line}")


def test_the_same_branch_still_speaks_in_cells_when_a_cell_was_counted(tmp_path):
    """AND THE SPLIT TOOK NOTHING FROM THE FRAMES THAT DO RESOLVE A CELL.

    A rib drawn at the cell the window declares reaches the same branch with `cells_resolved`
    True, and its figures are cell counts a reader can hold against a plan - so they stay.
    """
    line = _line(tmp_path, write_plenum_with_ribs, cells_px=4, wall_px=2, period_px=32)
    assert "THERE IS NOT ENOUGH METAL BETWEEN ITS PASSAGES TO BE A PICTURE OF THEM" in line, line
    assert "8.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert "measures 2.0 px at its narrowest - 0.50 cell(s)" in line, line
    assert "2.0 cells of wall a picture of this part has to show" in line, line
    assert "EVERY FIGURE IN THIS SENTENCE IS IN PIXELS" not in line, line


def test_the_drawn_cell_a_frame_publishes_is_the_one_its_verdict_was_taken_on(tmp_path):
    """THE ONE THAT MATTERS: an ADMITTED frame published a cell off the wrong axis, unnamed.

    MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the production
    ArtifactResolver: cells 8 px ACROSS by 4 px DOWN repeating VERTICALLY are ADMITTED at all
    nine shapes of passage 4/8/12 cells by wall 2/4/8 cells - the cell on the repeat axis is
    4.0 px and agrees with the window - and the line opened "what was resolved is drawn 8.0 px
    apart, which is 50.32 um", the ROW reading, naming no axis. That is exactly twice the planned
    cell, while the figure `cells_resolved` compared, and the figure the wall and the period on
    the same line are counted in, is 4.0 px and 25.16 um. A reader asking whether the drawn cell
    matches the window was told NO on a frame the gate admitted as YES.

    The anisotropy is not contrived: the planner pins the fine cell AT the thin wall's thickness
    and lets it run long in the other two directions, so this is the part's ordinary shape.
    """
    for passage, wall in ((4, 2), (4, 4), (4, 8), (8, 2), (8, 4), (8, 8), (12, 2), (12, 4),
                          (12, 8)):
        line = _line(tmp_path / f"p{passage}w{wall}", write_horizontal_passages,
                     cell_w=8, cell_h=4, band_px=passage * 4,
                     period_px=(passage + wall) * 4)
        assert "cells are countable in this frame" in line, (
            f"the premise: passage {passage}c / wall {wall}c is ADMITTED in both arms and the "
            f"gate is unchanged:\n{line}")
        assert ("what was resolved is drawn 4.0 px apart along the vertical axis the structure "
                "repeats on, which is 25.16 um") in line, (
            "the published drawn cell must be the one on the axis the verdict was taken on, and "
            f"it must name that axis:\n{line}")
        assert "what was resolved is drawn 8.0 px apart, which is 50.32 um" not in line, (
            "the ROW reading may not be published as THIS FRAME'S cell with no axis on it, "
            f"which is the sentence this frame used to get:\n{line}")
        assert ("along its ROWS the same ink is drawn 8.0 px apart, which is 50.32 um" in line), (
            "the row reading is a real measurement of the picture and is kept - NAMED as the "
            f"row, so a reader can see the cells here are not square:\n{line}")
        assert "the cells here are not square" in line, line


def test_an_isotropic_frame_says_its_cell_once(tmp_path):
    """AND NOTHING IS ADDED WHERE THERE IS NOTHING TO ADD. On a frame of square cells the two
    readings are the same number, so the row clause is silent - a line that printed one figure
    twice in different words is how a reader starts believing two facts agree."""
    line = _line(tmp_path, write_passages, cells_px=4, period_px=32, duty=0.5)
    assert ("what was resolved is drawn 4.0 px apart along the horizontal axis the structure "
            "repeats on, which is 25.16 um") in line, line
    assert "along its ROWS" not in line, line
    assert "not square" not in line, line


def test_a_frame_with_no_cell_on_its_repeat_axis_says_the_figure_is_the_row(tmp_path):
    """THE THIRD FORM: a frame whose structure repeats vertically and which resolves no interior
    down any column has no cell on its repeat axis at all.

    There is nothing to publish on that axis, so the row reading is published AS the row reading
    and the line says plainly that it is not a figure this frame's period or wall could be
    counted in. The old sentence published the same number with no axis on it, which on this
    frame is the only reading there is - and reads as the frame's cell.
    """
    line = _line(tmp_path, write_horizontal_passages, cell_w=4, cell_h=64, band_px=16,
                 period_px=32)
    assert "drawn 4.0 px apart ALONG ITS ROWS" in line, line
    assert ("no cell was resolved along the vertical axis the structure repeats on, so this "
            "figure is not one this frame's period or wall could be counted in") in line, line
    assert CANNOT_COUNT in line, line
