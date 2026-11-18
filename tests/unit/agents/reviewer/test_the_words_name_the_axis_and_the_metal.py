# Responsibility: Verify the sentence a reviewer reads says WHICH AXIS of the frame every figure
# was read along, and reports the metal between passages as something measured in the picture
# rather than inferred from a density.
# Boundaries: the words. Nothing here renders and nothing here decides coverage.
#
# THE DEFECT THIS IS WRITTEN FROM, measured by an independent review on 2026-11-17, SYNTHETIC and
# VOLUME-shaped, at 1280x960 with a claimed 4.00 px cell, through the production ArtifactResolver:
#
#     "THIS PART'S PASSAGE ARRAY IS IN THIS FRAME as measured here: 29 repeats, every 32.0 px,
#      at 0.967 of a perfect array ... which is 8.00 drawn cell(s) per period with 4.00 cell(s)
#      of metal between passages"
#
# said over a frame of passages running ACROSS it. The 32.0 px period is VERTICAL and the 4.00 px
# cell it was divided by was measured along a ROW, so the ratio is of two different directions,
# and in the direction the structure repeats there is half a cell per period. THE SENTENCE NAMED
# NEITHER AXIS, so nothing in it was false and nothing in it could be checked. Cell heights of 16,
# 32 and 64 px gave byte-for-byte identical frames and therefore identical words.
#
# AND THE SAME SENTENCE, over a field of coolant with no metal in it anywhere, read "4.00 cell(s)
# of metal between passages" - a figure derived from a per-column ink density, in a picture whose
# every column carries ink. The words now report the bare run in PIXELS beside the figure in
# cells, so a reader can see that the pixels say zero.
from __future__ import annotations

from pathlib import Path

from _reviewer_part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_cell_passages,
    write_horizontal_passages,
    write_truncated_plenum,
)

from meshpipeline.agents.reviewer.scene_text import CANNOT_COUNT, cut_frame_line
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


def test_the_axis_the_repeat_was_read_on_is_named(tmp_path):
    """A frame of horizontal passages must not be introduced with an unnamed 32.0 px period.

    The period is on the frame's VERTICAL axis and the drawn cell the reader is told about is on
    its horizontal one. Naming them is what makes the division checkable at all.
    """
    line = _line(tmp_path, write_horizontal_passages, cell_w=4, cell_h=64,
                 band_px=16, period_px=32)
    assert "29 repeats along the vertical axis of this frame" in line, line
    assert "PASSAGE ARRAY IS IN THIS FRAME" not in line, line
    assert "WHETHER IT IS THIS PART'S PASSAGE ARRAY WAS NOT MEASURED" in line, line
    assert "no cell interior was resolved along the vertical axis" in line, line
    assert "Do not call it a passage" in line
    assert CANNOT_COUNT in line
    # AND THE REASON GIVEN FOR REFUSING IT NAMES THE SAME THING, which it did not when this
    # change was first written: the refusal read "this is a field of coolant and not a bank of
    # passages" over a frame that holds 16.0 px of real metal between real passages and simply
    # has no cell drawn in the direction the structure repeats on.
    assert "there is no cell drawn in that direction to count it in" in line, line
    assert "field of coolant" not in line, line


def test_an_honest_frame_names_its_axis_too(tmp_path):
    """And the naming is not reserved for the bad case: the honest frame says horizontal.

    A reader who only ever sees the axis named when something is wrong learns to read its
    presence as the warning rather than reading the axis.
    """
    line = _line(tmp_path, write_cell_passages, passage_cells=4, wall_cells=4)
    assert "39 repeats along the horizontal axis of this frame" in line, line
    assert "8.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert CANNOT_COUNT not in line


def test_the_metal_is_reported_in_pixels_as_well_as_cells(tmp_path):
    """A FIELD OF COOLANT WITH NO METAL IN IT read "4.00 cell(s) of metal between passages".

    The words now carry the measured bare run in pixels, which in this frame is 0.0 - and they
    say outright that nothing along that axis is bare, because a reader handed only a figure in
    cells has no way to tell a derived number from a measured one.
    """
    line = _line(tmp_path, write_truncated_plenum, deep_px=470)
    assert "NOT ENOUGH METAL BETWEEN ITS PASSAGES TO BE A PICTURE OF THEM" in line, line
    assert ("the metal between two passages measures 0.0 px at its narrowest - 0.00 cell(s)"
            in line), line
    assert "no sample along the horizontal axis is bare at all" in line, line
    assert "it is not a wall" in line, line
    assert CANNOT_COUNT in line


def test_the_honest_frame_quotes_the_bare_run_it_measured(tmp_path):
    """The same figure on the honest frame, so the two can be compared by a reader.

    16.0 px of bare metal at a 4.0 px cell is 4.00 cells, which is the plan's own figure for this
    part's thin wall - and the sentence puts the two side by side rather than asserting agreement.
    """
    line = _line(tmp_path, write_cell_passages, passage_cells=4, wall_cells=4)
    assert ("the narrowest metal between two of its repeats is 16.0 px - 4.00 cell(s)"
            in line), line
    assert "against the 4.0 cells the plan puts across this part's thin wall" in line, line
    assert "The pitch, the cell and the wall are reported" in line, line
    # AND THE WALLS HERE ARE ALL ONE WIDTH, so the typical one is not said twice.
    assert "not all one width" not in line, line


def test_a_wide_passage_is_reported_rather_than_refused(tmp_path):
    """PASSAGES 12 CELLS WIDE WITH THE PLAN'S OWN 4-CELL WALL used to be refused for their pitch.

    The words now put a 16.00-cell period in front of the reviewer with the wall beside it and
    say the pitch is not held to anything, which is the division of labour the deleted band had
    wrong: the gate admits true pictures of passages and the reviewer judges whether the feature
    in them is the one it was asked about.
    """
    line = _line(tmp_path, write_cell_passages, passage_cells=12, wall_cells=4)
    assert ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS "
            "FRAME as measured here") in line, line
    assert "16.00 drawn cell(s) per period at the 4.0 px cell drawn on that axis" in line, line
    assert ("the narrowest metal between two of its repeats is 16.0 px - 4.00 cell(s)"
            in line), line
    assert ("whether they are the passages you were asked about is yours to judge from "
            "them and the plan") in line, line
    assert "cells are countable in this frame" in line, line
    assert CANNOT_COUNT not in line
