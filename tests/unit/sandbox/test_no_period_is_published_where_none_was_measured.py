# Responsibility: Verify that a frame whose structure is coarser than the frame can measure
# publishes NO period at all, and says so, rather than publishing the shortest lag its own search
# happens to start at.
# Boundaries: the reading side only, and only what can be measured off a PNG. Nothing here renders.
#
# THE DEFECT THIS IS WRITTEN FROM, measured by independent review on 2026-11-17 and reproduced
# here on 2026-11-18 against the pre-change tree. The lag search runs from 2 to about n/6, so in a
# 1280 px frame no pitch over about 212 px can be found at all - and the peak then lands on the
# shortest lag, which any smooth profile correlates with. MEASURED, SYNTHETIC, VOLUME, 1280x960 at
# 4 px per cell, through the production ArtifactResolver:
#
#     drawn pitch     published period     strength     runs stand apart
#     216 px              2.0 px            0.966            216.0 px
#     240 px              2.0 px            0.969            240.0 px
#     256 px              2.0 px            0.972            256.0 px
#     320 px              2.0 px            0.978            320.0 px
#     400 px              2.0 px            0.981            400.0 px
#     240 px VERTICAL     2.0 px            0.971            240.0 px
#
# NO FRAME WAS WRONGLY ADMITTED: every one of them is refused, and refused on the right reason -
# five repeats across the frame against the six a periodic array has to show. What was wrong is
# the FIGURE THAT WENT INTO THE RECORD, and the same 2.0 px was published for an honest 32 px
# array over a fifth of a frame of featureless coolant (runs 32.0 px apart, 0.861) and for a speck
# of array with one stripe beside it (32.0 px apart, 0.878) - two frames whose structure is well
# inside the searchable band.
#
# A PERIOD HOLDS AT LEAST ONE RISING EDGE BY DEFINITION, so the median spacing of a profile's own
# rising edges cannot exceed its period: that is the bound these tests are written on, and it is
# arithmetic rather than a tolerance.
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED - the new field is read through `getattr` - so
# every test here is COLLECTED against the pre-change tree and fails there on its own assertion.
from __future__ import annotations

from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_horizontal_passages,
    write_passages,
    write_plenum_either_side,
    write_plenum_with_ribs,
)

from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

CELL_M = 2.51578e-05              # quality.near_wall_cell_m on job 0ff6f9e5
FRAME_PX = (1280, 960)
MPP = CELL_M / 4.0                # the producer's own zoom scale, 6.28945 um/px

REGION = {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
          "plane_axes": [1, 2], "label": "Square across the coolant's path",
          "why": "the cells across each passage, and the metal between them"}


def _cut(ws: Path, draw, **kw):
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


def test_a_pitch_coarser_than_the_frame_can_measure_publishes_no_period(tmp_path):
    """THE FIVE PITCHES THAT PUBLISHED 2.0 px AT 0.97 OF A PERFECT ARRAY.

    Each one is honest passages at half duty, so the ink's own runs stand one drawn pitch apart -
    216, 240, 256, 320 and 400 px - while the longest lag a 1280 px profile can test is 212 px.
    """
    published, strengths = {}, {}
    for pitch in (216, 240, 256, 320, 400):
        cut = _cut(tmp_path / f"p{pitch}", write_passages, cells_px=4, period_px=pitch)
        assert cut.ink is not None
        published[pitch] = cut.ink.period_px
        strengths[pitch] = cut.ink.strength
        assert getattr(cut.ink, "period_unmeasurable", False) is True, (
            f"a {pitch} px pitch cannot be found by a search that stops at 212 px, and the frame "
            "has to record that rather than publish the lag the search starts at")
        assert cut.period_cells == 0.0, cut.period_cells
        assert not cut.shows_passages, "and none of them is offered as holding an array"
    assert published == dict.fromkeys(published, 0.0), (
        f"these frames published a period they could not have measured: {published}")
    assert all(s >= 0.96 for s in strengths.values()), (
        f"the strength is what made the false figure look like a measurement: {strengths}")


def test_a_vertical_pitch_coarser_than_the_frame_publishes_no_period(tmp_path):
    """THE SAME ARITHMETIC ON THE SHORTER AXIS, where it bites 1.33x sooner: a 960 px profile can
    test no lag over 159 px, so a 240 px vertical pitch published 2.0 px at 0.971.

    AND A TRUE PERIOD ONE PIXEL UNDER THE CEILING IS STILL PUBLISHED, which is why the bound is on
    the profile's own runs and not on the ceiling of the search: a 160 px vertical pitch reads
    lag 159 against runs 160.0 px apart and keeps its figure.
    """
    coarse = _cut(tmp_path / "a", write_horizontal_passages,
                  cell_w=4, cell_h=4, band_px=120, period_px=240)
    edge = _cut(tmp_path / "b", write_horizontal_passages,
                cell_w=4, cell_h=4, band_px=80, period_px=160)
    assert coarse.ink is not None and edge.ink is not None
    assert coarse.ink.repeat_axis == "vertical" and edge.ink.repeat_axis == "vertical"
    assert coarse.ink.period_px == 0.0, coarse.ink.period_px
    assert getattr(coarse.ink, "period_unmeasurable", False) is True
    assert edge.ink.period_px == 159.0, (
        "a period one pixel under the longest lag this frame can test is a measurement, not "
        f"silence, and it reads {edge.ink.period_px}")
    assert getattr(edge.ink, "period_unmeasurable", False) is False


def test_the_record_says_the_structure_was_coarser_than_the_frame_could_measure(tmp_path):
    """SILENCE HAS TO BE SAID, or a reader cannot tell it from a frame with nothing in it.

    Both measured figures are in the sentence: how far apart the ink's own runs stand, and the
    longest repeat the frame was able to test.
    """
    cut = _cut(tmp_path, write_passages, cells_px=4, period_px=320)
    assert cut.ink is not None
    why = cut.ink.structure_why
    assert "coarser than this frame can measure" in why, why
    assert "no period is published for this frame at all" in why, why
    assert "320.0 px apart" in why, why
    assert "212 px" in why, why
    assert "the ink forms 3 repeat(s) across the frame" in why, (
        "and the refusal it is given is still the right one - too few repeats - with the period "
        f"note beside it rather than instead of it: {why}")


def test_an_honest_array_over_a_fifth_of_a_frame_publishes_no_period(tmp_path):
    """THE CASE A CEILING TEST CANNOT CATCH: the structure is a 32 px array, well inside the
    searchable band, and the featureless coolant either side of it takes the peak to lag 2.

    MEASURED, before: period 2.0 px at 0.861 over 8 repeats - and because the repeat count clears
    the bar here, the refusal a reader was given was "the ink repeats every 2.0 px, under the 8 px
    a passage and its wall need", which is a sentence about a figure nobody measured.
    """
    cut = _cut(tmp_path, write_plenum_either_side, cells_px=4, period_px=32, array_share=0.20)
    assert cut.ink is not None
    assert cut.ink.repeats >= 6, cut.ink.repeats
    assert cut.ink.period_px == 0.0, cut.ink.period_px
    assert getattr(cut.ink, "period_unmeasurable", False) is True
    assert "32.0 px apart" in cut.ink.structure_why, cut.ink.structure_why
    assert "coarser than this frame can measure" in cut.ink.structure_why, cut.ink.structure_why
    assert not cut.counts_cells


def test_every_period_that_was_measured_is_still_published(tmp_path):
    """THE GUARD. Silencing a period must not silence the ones a frame really did measure, and
    every one of these is a frame whose own runs stand one published period apart.
    """
    for pitch in (16, 32, 64, 128, 212):
        cut = _cut(tmp_path / f"ok{pitch}", write_passages, cells_px=4, period_px=pitch)
        assert cut.ink is not None
        assert cut.ink.period_px == float(pitch), (pitch, cut.ink.period_px)
        assert getattr(cut.ink, "period_unmeasurable", False) is False
        assert cut.counts_cells, (
            f"a {pitch} px pitch is inside the window this frame can measure and it has to stay "
            "admitted")
    # and the rib field whose pitch IS this plate's 402.525 um feature keeps its 64 px figure
    rib = _cut(tmp_path / "rib", write_plenum_with_ribs, cells_px=4, period_px=64, wall_px=8)
    assert rib.ink is not None and rib.ink.period_px == 64.0
