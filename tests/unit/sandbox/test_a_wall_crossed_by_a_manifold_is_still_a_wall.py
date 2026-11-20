# Responsibility: Verify that the metal between two passages is measured WHERE A WALL IS, so a
# frame whose channels are crossed by a transverse manifold is read as the picture of passages it
# is - and that the field of coolant with no metal in it stays refused.
# Boundaries: the home side only, and only what can be measured off a PNG plus the one plan
# figure. Nothing here renders and nothing here reads a manifest for a size.
#
# THE DEFECT THIS IS WRITTEN FROM, and it is the first one on this gate found on a REAL MESH
# rather than on a drawing. Job 6dd6703c-c8d2-43cd-882c-a01c7375d014 put the Corintis cold plate
# through the product end to end and delivered 283,429,764 cells, and foamToVTK wrote an
# internal.vtu the cutter read 91,342,053 cells out of - matching checkMesh exactly. Six frames
# were drawn from real cells. The gate then REFUSED its through_the_plate zoom, which is a true
# picture of this part's channel array: 39 repeats every 32.0 px at 1.000 of a perfect array over
# 97.58% of the frame, 51.72% ink, 452 interiors on its best row over 727 rows. The clause it was
# refused on, verbatim from the production sentence:
#
#     "the metal between two passages measures 0.0 px at its narrowest"
#
# MEASURED, 2026-11-19, REAL PLATE, VOLUME, on that job's own PNG. The frame holds 640 metal
# columns in 41 runs whose median and maximum are both 16.0 px - at the producer's 6.28945 um/px
# that is 100.63 um, which is 1.0000 of the plan's own 100.6312 um wall and 4.00 planned cells,
# the exact figure `PLAN_CELLS_ACROSS_A_WALL` names. It read 0.0 because the plate has a
# TRANSVERSE MANIFOLD crossing every channel: every metal column carries exactly 33 inked pixels,
# all 33 in one contiguous band at rows 462..494, so no column of 960 rows is bare and the wall
# was being summed down the whole frame.
#
# AND IT IS NOT ANTI-ALIASING, which is what the refusal looked like and is the hypothesis these
# numbers refute. Measured on the same PNG: 179 distinct grey levels and NOT ONE PIXEL between
# 193 and 254 - the pixel counts under 193, 200, 210, 220, 230, 240, 248 and 254 are all exactly
# 635,520 - so nothing in the frame blends toward the background and no brightness threshold
# could have moved the answer. The ink in a metal column is DARK: that column's own histogram is
# {0: 3, 4: 2, 6: 1, 12: 1, 16: 1, 18: 1, 27: 1, 43: 1, 61: 1, 74: 1, 78: 1, 79: 17, 186: 2,
# 255: 167}.
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED to the product. Every assertion is on behaviour -
# whether a frame discharges its region, and what the measured wall is - so each test is
# COLLECTED against the pre-change tree and fails there on its own assertion rather than on an
# import.
from __future__ import annotations

from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_passages,
    write_passages_with_a_transverse_join,
    write_truncated_plenum,
)

from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with - and job
#: 6dd6703c ran at the same 6.28945 um/px, which its report states as `um_per_pixel: 6.2895`.
CELL_M = 2.51578e-05
FRAME_PX = (1280, 960)
MPP = CELL_M / 4.0

#: Restated rather than imported, so every test here runs against the OLD tree too.
WALL_CELLS_MIN = 2.0
PLAN_CELLS_ACROSS_A_WALL = 4.0

REGION = {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
          "plane_axes": [1, 2], "label": "Square across the coolant's path",
          "why": "the cells across each passage, and the metal between them"}


def _manifest(ws: Path, frames: list[dict]) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    return {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
            "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.788779615505995e-06,
                        "max_cell_m": 0.0006000499958339121},
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


# 1. THE DEFECT ITSELF: the real plate's shape, which used to read a 0.0 px wall

def test_a_manifold_crossing_the_channels_does_not_erase_the_metal_between_them(tmp_path):
    """THE REAL PLATE'S OWN FRAME, drawn to its measured geometry: 40 passages 16 px wide on a
    32 px pitch, crossed by one 33-row band of coolant running the full width.

    This is job 6dd6703c's through_the_plate zoom. Its metal is 16.0 px - 100.63 um at the
    producer's 6.28945 um/px, 1.0000 of the plan's 100.6312 um wall, 4.00 planned cells - and the
    whole-frame column sum read it as 0.0 px because 33 rows of 960 put ink into all 1280 columns.

    THE WALL IS WHERE A WALL IS. Measured at BOTH ENDS of the structure's cross extent, this
    frame's metal is 16.0 px and the plenum's is 0.0, and the two can be told apart. Measured
    through the production ArtifactResolver, SYNTHETIC at the real plate's own geometry:

        frame                                   whole-frame sum   at the ends of the extent
        passages crossed by a 33-row manifold          0.00 px                    16.00 px
        truncated_plenum(deep_px=200)                  0.00 px                     0.00 px
        honest write_passages()                       16.00 px                    16.00 px
    """
    cut = _cut(tmp_path, write_passages_with_a_transverse_join, join_px=33, join_at="middle")
    assert cut.ink is not None
    # the frame really is the array it claims to be, on every bar that existed before this round
    assert cut.ink.shows_structure, "a periodic structure really is in it"
    assert cut.ink.repeats == 39 and cut.ink.period_px == 32.0, (
        f"{cut.ink.repeats} repeats every {cut.ink.period_px} px")
    assert cut.ink.strength >= 0.97 and cut.ink.extent_fraction >= 0.97
    assert cut.cell_px == 4.0

    # THE FIGURE THAT MOVED. Pre-change this is 0.0 and the sentence a reviewer reads says
    # "the metal between two passages measures 0.0 px at its narrowest".
    assert cut.ink.bare_run_min_px == 16.0, (
        "the metal between two passages is 16 px wide in this frame and a manifold crossing it "
        f"does not make it narrower; measured {cut.ink.bare_run_min_px} px")
    assert cut.wall_cells == PLAN_CELLS_ACROSS_A_WALL, (
        f"16 px at a 4.00 px drawn cell is 4.00 cells of wall, not {cut.wall_cells}")

    # THE VERDICT, which is the behaviour this exists for
    assert cut.shows_metal_between_passages, (
        "a true picture of this part's channel array, whose metal measures exactly the wall the "
        "planner asked for, must not be refused for showing 0.0 px of metal")


def test_the_manifold_may_cross_anywhere_in_the_middle_of_the_window(tmp_path):
    """The join's WIDTH is not what decides it, up to half the frame.

    MEASURED, SYNTHETIC, VOLUME, honest passages with a full-width coolant band across the middle:
    joins of 0, 33, 100, 240, 400 and 479 rows of 960 all measure 16.00 px of metal; 480 and above
    measure 0.00. The transition is at exactly half, and it belongs to the material threshold that
    decides whether there is a repeat at all - a column inked on more than half the lines is
    MATERIAL, so it supplies no rising edge and the frame is refused on its repeat count first.
    """
    for join in (0, 33, 100, 240, 400):
        cut = _cut(tmp_path, write_passages_with_a_transverse_join, join_px=join)
        assert cut.ink.bare_run_min_px == 16.0, (
            f"a {join}-row manifold across the middle leaves the 16 px wall intact; "
            f"measured {cut.ink.bare_run_min_px} px")
        assert cut.shows_metal_between_passages, f"join {join} rows"


# 2. WHAT MUST NOT MOVE: the field of coolant with no metal in it

def test_a_field_of_coolant_joined_at_the_edge_of_the_window_is_still_refused(tmp_path):
    """THE SAME BAND AGAINST THE TOP EDGE, which is what `write_truncated_plenum` draws and what
    a wall is not.

    A wall runs the length of the passages and leaves the window at BOTH sides; a pool that stops
    partway is bounded by coolant at one of them. THE TWO FRAMES DIFFER IN NOTHING BUT WHERE THE
    BAND SITS - same passages, same pitch, same cells, same band - so this is the measurement that
    separates them and nothing else is doing it.

    MEASURED, SYNTHETIC, VOLUME: a 33-row join in the MIDDLE reads 16.00 px and discharges; the
    SAME 33-row join against the top edge reads 0.00 px and declines. So does 240 and so does 480.
    """
    for join in (33, 240, 480):
        cut = _cut(tmp_path, write_passages_with_a_transverse_join,
                   join_px=join, join_at="top")
        assert cut.ink.bare_run_min_px == 0.0, (
            f"a {join}-row band against the edge of the window is not a wall at that end; "
            f"measured {cut.ink.bare_run_min_px} px")
        assert not cut.shows_metal_between_passages, f"top join {join} rows"
        assert not cut.counts_cells, f"top join {join} rows must not discharge its region"


def test_the_truncated_plenum_family_is_still_refused_at_every_depth(tmp_path):
    """THE ELEVEN METAL-FREE PICTURES ARE THE WHOLE REASON THIS TEST EXISTS, and the truncated
    plenum is the one a per-line reading would have let through.

    MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell, the metal mask built four ways:

        frame                                  whole   ends only   majority only   ends AND maj
        truncated_plenum(deep_px=200)         0.00 px     0.00 px        16.00 px        0.00 px

    79.2% of that frame's lines show its 16 px gaps, so a reduction that only counts lines reads a
    FOUR-CELL WALL in a picture with no metal in it. It is the ends that refuse it.
    """
    for deep in (200, 400, 470):
        cut = _cut(tmp_path, write_truncated_plenum, deep_px=deep)
        assert cut.ink.bare_run_min_px == 0.0, (
            f"deep_px={deep} contains no metal at all; measured {cut.ink.bare_run_min_px} px")
        assert cut.wall_cells == 0.0, f"deep_px={deep}"
        assert not cut.counts_cells, (
            f"a field of coolant with no metal anywhere in it must not discharge a region; "
            f"deep_px={deep}")


def test_the_honest_frame_is_unmoved(tmp_path):
    """The frame with no join in it reads exactly what it always read: 16.0 px, 4.00 cells."""
    cut = _cut(tmp_path, write_passages)
    assert cut.ink.bare_run_min_px == 16.0 and cut.ink.bare_run_px == 16.0
    assert cut.wall_cells == PLAN_CELLS_ACROSS_A_WALL
    assert cut.shows_metal_between_passages
