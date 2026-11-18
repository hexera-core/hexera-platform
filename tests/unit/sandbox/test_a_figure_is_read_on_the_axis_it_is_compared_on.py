# Responsibility: Verify that the drawn cell a frame's claim is checked against is the cell drawn
# on THE AXIS THE STRUCTURE REPEATS ON - the same axis the wall and the period are counted on - so
# that the two halves of the test are talking about one direction.
# Boundaries: the reading side only. Nothing here renders and nothing here reads a manifest for a
# size that is not declared in the fixture.
#
# THE DEFECT THIS IS WRITTEN FROM, measured by independent review on 2026-11-17 and reproduced
# here on 2026-11-18 against the pre-change tree, all SYNTHETIC, VOLUME, 1280x960, a claimed
# 4.00 px cell, through the production ArtifactResolver and ResolvedCut:
#
#   `ResolvedCut.cells_resolved` granted when EITHER axis's drawn cell fell inside
#   CELL_PITCH_TOLERANCE, while `wall_cells` and `period_cells` both divide by the cell drawn on
#   the axis the structure REPEATS on. A frame drawn with cells 2 px across by 4 px down, with
#   honest bare metal between its passages, was therefore ADMITTED - passage 8c/wall 2c, 4c/2c and
#   12c/2c - on a COLUMN reading of 4.0, while the row reading, which is the axis every published
#   figure is counted on, was 2.0 against a claimed 4.00. The record then carried the 8c/8c
#   frame's 16.0 px of metal as "8.00 cell(s)" and its period as "16.00 drawn cell(s)", both
#   exactly double what the declared scale makes them.
#
# AND THE SAME MIXING RAN THE OTHER WAY on the vertical axis: a frame of passages running across
# the frame, repeating vertically, drawn with cells 4 px wide and 16 px TALL, was admitted on its
# row reading of 4.0 while its wall - 32 px of metal, EIGHT claimed cells - was published as
# "2.00 cell(s)", counted in the 16 px cell drawn down its columns.
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED. Every assertion is on behaviour, and the fields
# this round did not add are read directly, so each test is COLLECTED against the pre-change tree
# and fails there on its own assertion.
from __future__ import annotations

from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_cell_passages,
    write_horizontal_passages,
    write_passages,
    write_row_coarse_col_fine,
)

from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
CELL_M = 2.51578e-05              # quality.near_wall_cell_m, and fine_cell_pinned_m
FRAME_PX = (1280, 960)
#: The producer's own zoom scale: one planned cell four pixels across. 6.28945 um/px.
MPP = CELL_M / 4.0

#: Restated rather than imported, so every test here runs against the OLD tree too.
CELL_PITCH_TOLERANCE = 1.5

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


def test_a_frame_drawn_at_half_the_scale_it_declares_does_not_discharge_a_region(tmp_path):
    """THE FAKE THE ANY-AXIS FORM ADMITTED, at the three shapes it was measured at.

    Cells 2 px across by 4 px down, honest bare metal between the passages, declared at a window
    that makes a planned cell 4.00 px. The row - the axis the structure repeats on, and the axis
    the wall and the period are divided by - reads 2.0 px, a factor of two under the claim, which
    is exactly the reading CELL_PITCH_TOLERANCE at 1.5 exists to refuse.
    """
    for passage, wall in ((8, 2), (4, 2), (12, 2), (16, 4), (4, 4), (12, 4)):
        cut = _cut(tmp_path / f"p{passage}w{wall}", write_row_coarse_col_fine,
                   cell_w=2, cell_h=4, passage_cells=passage, wall_cells=wall)
        assert cut.ink is not None
        assert cut.ink.cell_pitch_px == 2.0, cut.ink.cell_pitch_px
        assert cut.ink.cell_pitch_down_px == 4.0, cut.ink.cell_pitch_down_px
        assert round(cut.cell_px, 2) == 4.0, cut.cell_px
        assert cut.ink.repeat_axis == "horizontal", cut.ink.repeat_axis
        assert not cut.cells_resolved, (
            f"passage {passage}c / wall {wall}c is drawn at 2.0 px per cell on the axis its "
            "structure repeats on against a claimed 4.00 px, which is the factor of two this "
            "test exists to refuse, and the column agreeing is not that axis")
        assert not cut.counts_cells, (
            "and a frame that resolves no cell of the claimed size on the axis its figures are "
            "counted on cannot be offered as covering its region")


def test_the_figures_the_record_carries_are_not_double_the_declared_scale(tmp_path):
    """WHAT THE REVIEWER WAS TOLD ABOUT THAT FRAME, and what the refusal did and did not remove.

    MEASURED, before: the 8c/8c frame's narrowest metal is 16.0 px, which at the declared 4.00 px
    cell is 4.00 cells - and it was published as 8.00, with 16.00 drawn cells per period against
    the 8.00 the declared scale makes it. Both figures are counted in the 2.0 px row cell, so both
    are exactly twice the truth at the scale the frame declares.

    AND ROUND 8 DID NOT REMOVE THEM, which is a claim of round 8's own that is measured wrong and
    is corrected here. It was written up as removing a false published figure and not only a
    verdict. IT REMOVED ONLY THE VERDICT. `wall_cells` and `period_cells` divide by
    `repeat_pitch_px`, which round 8 did not touch - it changed `cells_resolved` alone - so on
    every frame round 8 moved the published wall is BYTE-IDENTICAL before and after.
    RE-MEASURED 2026-11-18, SYNTHETIC, VOLUME, 1280x960, a 217-frame set driven through the
    production ArtifactResolver in both arms: round 8 moved 27 frames, all of them from admitted
    to refused, and not one of their `wall_cells` figures changed by so much as a digit. This
    frame is one of them and the assertion below is that measurement: it still publishes 8.00.
    What stopped the false CELL figure reaching the reviewer was round 9's narrowing of
    `scene_text._measured_clause`, which gives a frame that resolved no cell of the declared size
    its period and its metal in PIXELS - and until that landed, the line a reviewer reads carried
    "8.00 cell(s)" and "CELLS ARE NOT COUNTABLE IN THIS FRAME" in the same sentence.
    """
    cut = _cut(tmp_path, write_row_coarse_col_fine, cell_w=2, cell_h=4,
               passage_cells=8, wall_cells=8)
    assert cut.ink is not None
    assert cut.ink.bare_run_min_px == 16.0, cut.ink.bare_run_min_px
    assert round(cut.wall_cells, 2) == 8.0, (
        "the wall as this frame publishes it, counted in its own 2.0 px row cell")
    assert round(cut.ink.bare_run_min_px / cut.cell_px, 2) == 4.0, (
        "and the same 16.0 px of metal counted in the cell the stated window implies")
    assert round(cut.period_cells, 2) == 16.0 and round(cut.period_cells_claimed, 2) == 8.0
    assert not cut.counts_cells, (
        "a frame whose two readings of its own scale differ by a factor of two must not be "
        "offered as evidence, whichever of them the reviewer would have believed")


def test_whatever_discharges_was_checked_in_the_cell_its_wall_is_counted_in(tmp_path):
    """THE PROPERTY, stated over every shape measured this round: if a frame is offered at all,
    the drawn cell its wall and its period are counted in is the drawn cell its claim was checked
    against, within CELL_PITCH_TOLERANCE.

    MEASURED, before: the fake above discharges with a ratio of 0.50 and the anisotropic frames
    discharge with 2.00 and 4.00, because the grant came off the other axis.
    """
    cases = {
        "honest 4x4, passage 4c wall 4c":
            (write_cell_passages, {"cells_px": 4, "passage_cells": 4, "wall_cells": 4}),
        "honest 4x4, passage 2c wall 2c":
            (write_cell_passages, {"cells_px": 4, "passage_cells": 2, "wall_cells": 2}),
        "honest pitch 32 duty 0.5": (write_passages, {"cells_px": 4, "period_px": 32}),
        "fake 2 across by 4 down":
            (write_row_coarse_col_fine,
             {"cell_w": 2, "cell_h": 4, "passage_cells": 8, "wall_cells": 8}),
        "vertical repeat, cells 4 wide by 4 tall":
            (write_horizontal_passages,
             {"cell_w": 4, "cell_h": 4, "band_px": 32, "period_px": 64}),
        "vertical repeat, cells 4 wide by 16 tall":
            (write_horizontal_passages,
             {"cell_w": 4, "cell_h": 16, "band_px": 32, "period_px": 64}),
    }
    offered = []
    for i, (name, (draw, kw)) in enumerate(cases.items()):
        cut = _cut(tmp_path / f"c{i}", draw, **kw)
        assert cut.ink is not None
        if not cut.counts_cells:
            continue
        offered.append(name)
        ratio = cut.repeat_pitch_px / cut.cell_px
        assert 1.0 / CELL_PITCH_TOLERANCE <= ratio <= CELL_PITCH_TOLERANCE, (
            f"{name} was offered as evidence while the cell its wall and period are counted in "
            f"({cut.repeat_pitch_px:.1f} px on the {cut.ink.repeat_axis} axis) is {ratio:.2f} "
            f"times the {cut.cell_px:.2f} px cell its window claims")
    assert offered, "no frame was offered at all, so the property above tested nothing"


def test_a_vertical_frame_is_judged_on_the_cell_drawn_down_its_columns(tmp_path):
    """PASSAGES RUNNING ACROSS THE FRAME ARE COUNTED DOWN IT, so that is the cell that is checked.

    MEASURED, SYNTHETIC, VOLUME, 1280x960, a claimed 4.00 px cell, vertical pitches of 64, 96 and
    128 px with the band half the pitch: cell heights of 4, 8 and 16 px ALL discharged before,
    because the row reading is 4.0 px in all three. Their walls were published as 8.00, 4.00 and
    2.00 cells for pitch 64 - one measurement of one picture, stated three ways, because the
    divisor was a cell nothing had checked against the claim.
    """
    verdict, walls = {}, {}
    for pitch in (64, 96, 128):
        for cell_h in (4, 8, 16):
            cut = _cut(tmp_path / f"v{pitch}_{cell_h}", write_horizontal_passages,
                       cell_w=4, cell_h=cell_h, band_px=pitch // 2, period_px=pitch)
            assert cut.ink is not None
            assert cut.ink.repeat_axis == "vertical", cut.ink.repeat_axis
            assert cut.ink.period_px == float(pitch), cut.ink.period_px
            assert cut.ink.cell_pitch_px == 4.0, (
                "the row reading is the claimed cell in every one of these frames, which is why "
                "it must not be what decides them")
            assert cut.ink.cell_pitch_down_px == float(cell_h), cut.ink.cell_pitch_down_px
            verdict[(pitch, cell_h)] = bool(cut.counts_cells)
            walls[(pitch, cell_h)] = round(cut.wall_cells, 2)
    assert verdict == {(p, c): (c == 4) for p in (64, 96, 128) for c in (4, 8, 16)}, verdict
    assert walls[(64, 4)] == 8.0 and walls[(64, 8)] == 4.0 and walls[(64, 16)] == 2.0, walls
