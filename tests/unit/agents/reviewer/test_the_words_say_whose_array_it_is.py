# Responsibility: Verify the sentence a reviewer reads about a prepared frame never calls a
# periodic structure "the passage array" unless its period was measured to be the part's.
# Boundaries: the words. Nothing here renders and nothing here decides coverage.
#
# THE DEFECT THIS IS WRITTEN FROM, measured by an independent review on 2026-11-17. A WIREFRAME
# LATTICE at 8 px per cell - 23.4375% ink, 159 repeats, period 8.0 px, 0.994 of a perfect array,
# and ZERO enclosed cell interiors anywhere in it - was refused as evidence on the interior count,
# correctly, and the sentence handed to the reviewer read:
#
#     "The PASSAGE ARRAY IS IN THIS FRAME as measured here: 159 repeats, every 8.0 px, at
#      0.994 of a perfect array"
#
# There is no passage in that picture at all. The 8.0 px period is the cell lattice. A reviewer is
# not shown the frame twice; it judges from what it is told, and a refusal in one clause does not
# unsay a false statement in another.
#
# SO THERE ARE FOUR ANSWERS TO "IS THE ARRAY IN THIS FRAME", not two, and the words have to tell
# them apart: nothing periodic here; something periodic with NOT ENOUGH METAL between its
# passages to be a picture of them; something periodic with no drawn cell to measure its period
# against, where the question cannot be asked; and an array with real metal between its passages.
#
# AND NONE OF THE FOUR SAYS WHOSE ARRAY IT IS, which is round 7. The grant branch opened "THIS
# PART'S PASSAGE ARRAY IS IN THIS FRAME" after round 6 deleted the period band, justified by the
# wall floor keeping this plate's other regular features out - and that justification is REFUTED
# by measurement: see `test_this_plates_other_features_discharge_the_region`, where both of them
# discharge their region when they are drawn with a two-cell rib.
from __future__ import annotations

from pathlib import Path

from _reviewer_part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_alternating_walls,
    write_passages,
    write_plenum_with_ribs,
    write_wireframe_lattice,
)

from meshpipeline.agents.reviewer.scene_text import CANNOT_COUNT, cut_frame_line
from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

CELL_M = 2.51578e-05              # quality.near_wall_cell_m on job 0ff6f9e5
THICKNESS_M = 0.0001006312        # conjugate_plan.fine_surfaces[0].thickness_m, the thin wall
FRAME_PX = (1280, 960)
MPP = CELL_M / 4.0                # the producer's own zoom scale, 6.28945 um/px

#: Restated rather than imported so every test here runs against the OLD tree and fails there on
#: its assertion rather than on its import.
PLAN_CELLS_ACROSS_A_WALL = 4.0

#: The opening of the grant branch, as round 8 leaves it. Restated here for the same reason.
NEW_OPEN = ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME as "
            "measured here")


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


def test_a_cell_lattice_is_not_introduced_as_the_passage_array(tmp_path):
    """THE EXACT SENTENCE THAT WAS SAID OVER A PICTURE WITH NO PASSAGE IN IT.

    The frame is a wireframe lattice at 8 px per cell: 159 repeats every 8.0 px at 0.994 of a
    perfect array, and not one enclosed cell interior. There is nothing to count the period in,
    so whether that 8.0 px is a passage pitch or the lattice's own spacing was never measured -
    and the line has to say that rather than assert the first.
    """
    line = _line(tmp_path, write_wireframe_lattice, cells_px=8)
    assert "PASSAGE ARRAY IS IN THIS FRAME" not in line, line
    assert "A PERIODIC STRUCTURE IS IN THIS FRAME as measured here" in line, line
    assert "WHETHER IT IS THIS PART'S PASSAGE ARRAY WAS NOT MEASURED" in line, line
    assert "no cell interior was resolved along the horizontal axis" in line, line
    assert "including the cell lattice" in line
    assert "Do not call it a passage" in line
    assert CANNOT_COUNT in line


def test_a_structure_that_is_not_the_parts_is_said_not_to_be(tmp_path):
    """A REGULAR ARRAY AT FOUR TIMES THE PART'S PITCH, with the cells plainly resolved.

    Job 0ff6f9e5 carries regular arrays at 402.525 um and 805.050 um beside its 100.631 um
    channel wall. This frame is the first of those: 19 repeats, period 64.0 px, 280 interiors on
    the best row over 720 rows, a 4.0 px drawn cell - so 16.00 cells per period against the four
    the plan puts across the thin wall. The line must say it is NOT this part's array and say the
    two numbers it compared.
    """
    line = _line(tmp_path, write_plenum_with_ribs, wall_px=2, period_px=64)
    assert "THERE IS NOT ENOUGH METAL BETWEEN ITS PASSAGES TO BE A PICTURE OF THEM" in line, line
    assert "PASSAGE ARRAY IS IN THIS FRAME" not in line, line
    assert "16.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert ("the metal between two passages measures 2.0 px at its narrowest - 0.50 cell(s)"
            in line), line
    assert f"the {PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan puts across its thin wall" in (
        line), line
    assert "a count taken off this would be a count of nothing" in line
    assert CANNOT_COUNT in line
    assert "the cells are in the picture and the metal between passages is not" in line, line


def test_a_plenum_with_a_rib_says_the_wall_is_what_is_missing(tmp_path):
    """The period here IS the part's and the metal is not there, so the words must name the wall.

    MEASURED: this frame and the honest one report the same 39 repeats, the same 32.0 px period,
    the same 0.975, the same 4.0 px drawn cell and the same 720 rows. A reader told only the
    period could not tell them apart, because on that figure they are the same picture.
    """
    line = _line(tmp_path, write_plenum_with_ribs, wall_px=2, period_px=32)
    assert "NOT ENOUGH METAL BETWEEN ITS PASSAGES" in line, line
    assert "8.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert "measures 2.0 px at its narrowest - 0.50 cell(s)" in line, line
    assert "2.0 cells of wall a picture of this part has to show" in line, line
    assert CANNOT_COUNT in line


def test_the_honest_frame_is_reported_without_a_claim_about_whose_array_it_is(tmp_path):
    """THE CLAIM THIS ROUND DELETES, and it was deleted because its ground is refuted.

    Round 6 removed the band that held a frame's period to a figure derived from the plan, and
    opened the grant branch with "THIS PART'S PASSAGE ARRAY IS IN THIS FRAME" on the ground that
    the wall floor keeps this plate's other regular features out.

    IT DOES NOT. MEASURED, 2026-11-17, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell, through the
    production ArtifactResolver: drawn as a field of coolant with an 8 px bare rib - two drawn
    cells, exactly the floor - the 402.525 um feature discharges at 87.50% ink with a 2.00-cell
    wall and the 805.050 um one discharges at 93.75% ink with a 2.00-cell wall. Both are four and
    eight times this part's passage pitch. `test_this_plates_other_features_discharge_the_region`
    below is that measurement.

    So the sentence says what was measured - an array, its pitch, its wall - and says plainly
    that WHICH FEATURE it is was not. A claim no surviving measurement supports is worse than no
    claim: it is the one a reviewer would rely on.
    """
    line = _line(tmp_path, write_passages, period_px=32, duty=0.5)
    assert "THIS PART'S PASSAGE ARRAY IS IN THIS FRAME" not in line, line
    assert ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS "
            "FRAME as measured here") in line, line
    assert "8.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert ("the narrowest metal between two of its repeats is 16.0 px - 4.00 cell(s)"
            in line), line
    assert f"against the {PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan puts across this part's "\
           "thin wall" in line, line
    assert ("WHETHER THAT PITCH IS THIS PART'S PASSAGE PITCH WAS NOT ESTABLISHED HERE"
            in line), line
    assert "402.525 um and 805.050 um" in line, line
    assert "cells are countable in this frame" in line
    assert CANNOT_COUNT not in line


def test_this_plates_other_features_discharge_the_region(tmp_path):
    """THE MEASUREMENT THAT REFUTES THE GROUND THE DELETED CLAIM STOOD ON.

    Both of job 0ff6f9e5's other regular features, drawn with a two-cell rib, clear every bar on
    this side and discharge their region - so nothing here holds the measured pitch to the part,
    and the words must not say it does.
    """
    for period_px, um in ((64, 402.525), (128, 805.050)):
        line = _line(tmp_path / f"f{period_px}", write_plenum_with_ribs,
                     wall_px=8, period_px=period_px)
        assert NEW_OPEN in line, (um, line)
        assert "THIS PART'S PASSAGE ARRAY IS IN THIS FRAME" not in line, (um, line)
        assert CANNOT_COUNT not in line, (
            f"the {um} um feature discharges its region on a two-cell rib, which is why the "
            "sentence cannot call what it holds this part's passage array")


def test_when_the_stated_scale_is_what_disagrees_the_line_says_so(tmp_path):
    """A COARSER ARRAY WITH THE RIGHT SHAPE: 8 px cells on a 64 px period, honest window.

    Measured: 8.00 cells per period and 4.00 cells of metal between passages - the part's own
    shape exactly - while the cell the stated window implies puts the same period at 16.00. A
    reader shown only the drawn figure would read "8.00 against the plan's 4.0" as agreement.

    AND SO THE LINE PUBLISHES NEITHER OF THOSE CELL FIGURES ANY MORE, which is round 9's
    narrowing and is what this test now holds. ROUND 8 MOVED THE GATE TO THE REPEAT AXIS AND
    THESE WORDS DID NOT FOLLOW IT: this frame's drawn cell is 8.0 px against the 4.00 px its
    window declares, so `cells_resolved` is False and the frame is REFUSED - and the same line
    still opened "A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME",
    published "8.00 drawn cell(s) per period" and "32.0 px - 4.00 cell(s)", held that wall
    against "the 4.0 cells the plan puts across this part's thin wall", and then closed with
    "CELLS ARE NOT COUNTABLE IN THIS FRAME - do not state a cell count". A cell count divided by
    a drawn cell the same line says is wrong is not a measurement. Every figure about this frame
    is now a PIXEL figure, and the disagreement that refuses it is stated in pixels too.

    NOTHING ABOUT WHICH FRAMES DISCHARGE MOVED. This frame was refused before and is refused
    now, on the same measurement; what changed is what the words claim about it.
    """
    line = _line(tmp_path, write_passages, cells_px=8, period_px=64, duty=0.5)
    assert "A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME" in line, line
    assert "the narrowest metal between two of its repeats is 32.0 px." in line, line
    assert "EVERY FIGURE IN THIS SENTENCE IS IN PIXELS AND NONE OF THEM IS IN CELLS" in line, line
    assert "drawn cell(s) per period" not in line, line
    assert "4.00 cell(s)" not in line and "cell(s), against the" not in line, (
        "neither the period nor the metal may be published in drawn cells: " + line)
    assert (f"against the {PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan puts across this part's "
            "thin wall") not in line, (
        "and the wall may not be held against the plan's cell count either - the line may only "
        "say that the comparison cannot be made: " + line)
    assert ("what was resolved along the horizontal axis is drawn 8.0 px apart while the stated "
            "window makes a planned cell 4.00 px") in line, line
    assert "nothing in the picture says which" in line, line
    assert ("it is not cells - it is something larger or smaller that happens to repeat"
            not in line), (
        "that tail is refuted: on a frame with this part's own anisotropy what was resolved IS "
        "cells, and the measurement establishes only that the two readings disagree: " + line)
    assert CANNOT_COUNT in line


def test_the_three_answers_do_not_read_the_same(tmp_path):
    """The brief's own test. If a reader cannot tell these three apart from the words, this failed.

    Nothing periodic at all; something periodic that is a different feature of the part; and the
    part's own array. The first used to read "NO PASSAGE ARRAY IS IN THIS FRAME" and the other
    two both read "The PASSAGE ARRAY IS IN THIS FRAME".
    """
    nothing = _line(tmp_path / "a", write_passages, period_px=0 or 10 ** 9, duty=1.0)
    wrong = _line(tmp_path / "b", write_plenum_with_ribs, wall_px=2, period_px=64)
    right = _line(tmp_path / "c", write_passages, period_px=32, duty=0.5)
    assert nothing != wrong != right
    assert "NO PERIODIC STRUCTURE IS IN THIS FRAME as measured here" in nothing, nothing
    assert "PERIODIC STRUCTURE" in wrong and "NOT ENOUGH METAL BETWEEN ITS PASSAGES" in wrong
    assert NEW_OPEN not in wrong
    assert NEW_OPEN in right
    # and only one of the three says an array with metal between its passages is in the frame
    claims = [t for t in (nothing, wrong, right) if NEW_OPEN in t]
    assert claims == [right], "exactly one of these three frames holds an array with a wall"
    # AND NONE OF THE THREE SAYS WHOSE ARRAY IT IS, which is round 7's deletion
    assert not [t for t in (nothing, wrong, right)
                if "THIS PART'S PASSAGE ARRAY IS IN THIS FRAME" in t]


def test_the_line_says_how_much_of_the_frame_the_structure_covered(tmp_path):
    """The extent is measured, so it is reported - a reader given a repeat count and no footprint
    cannot tell a frame full of passages from a speck of them."""
    line = _line(tmp_path, write_passages, period_px=32, duty=0.5)
    assert "39 repeats along the horizontal axis of this frame" in line, line
    assert "over 97.58% of the frame" in line, line


def test_the_plan_figure_quoted_is_the_planners_own_four_cell_floor():
    """What the words compare against is job 0ff6f9e5's own plan arithmetic, not a chosen number."""
    assert THICKNESS_M / CELL_M == PLAN_CELLS_ACROSS_A_WALL


def test_walls_of_two_widths_are_both_given_in_pixels_when_no_cell_was_counted(tmp_path):
    """THE ONE CLAUSE ONLY A BIMODAL FRAME REACHES, on a frame that resolved no declared cell.

    Walls of 32 px and 16 px, passages 32 px, cells drawn 8 px across against a window that makes
    a planned cell 4.00 px. The narrowest wall is 16.0 px and a typical one is 24.0 px, so the
    "not all one width" clause fires - and this frame's drawn cell is twice the one it declares,
    so neither figure can be put in cells. MEASURED, SYNTHETIC, VOLUME, 1280x960, through the
    production ArtifactResolver: both are published in pixels and the frame is refused, in this
    arm and in the one before it.
    """
    line = _line(tmp_path, write_alternating_walls, cells_px=8, passage_px=32,
                 wall_a_px=32, wall_b_px=16)
    assert NEW_OPEN in line, line
    assert "the narrowest metal between two of its repeats is 16.0 px" in line, line
    assert ("though the walls here are not all one width and a typical one is 24.0 px"
            in line), line
    assert "cell(s)" not in line.split("as measured here")[1].split("EVERY FIGURE")[0], (
        "the bimodal clause may not publish either wall in drawn cells on a frame that resolved "
        "no cell of the size it declares: " + line)
    assert CANNOT_COUNT in line
