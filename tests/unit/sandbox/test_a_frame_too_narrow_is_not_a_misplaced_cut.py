# A frame too narrow to test the pitch drawn in it was reported as a cut that landed on the wrong
# thing.
#
# MEASURED, job 22ac7054, the real Corintis plate. The session told the review, of two of three
# regions:
#
#   inspection_regions.along_the_flow.frames.zoom is sized correctly but its own pixels hold no
#   passage array (the ink forms 1 repeat(s) across the frame, under the 6 a periodic array has to
#   show, and no period is published for this frame at all: its ink runs stand 394.0 px apart along
#   the horizontal axis while the strongest repeat measurable in it is at 2 px, and the longest
#   repeat a frame this size can test is 212 px - so the structure in this frame is coarser than
#   this frame can measure) - so whatever the cut landed on, it is not the passages region
#   'along_the_flow' exists to show, and this frame cannot satisfy it.
#
# The two halves of that sentence contradict each other. The detector's own words say the frame
# could not measure the period; the conclusion says the cut is in the wrong place. Only one of them
# is in the pixels, and it is the first.
#
# REPRODUCED HERE EXACTLY, through the production reader, on a drawn picture at a 394 px pitch:
# 3 repeats, no period published, run spacing 394.0 px against the 212 px longest repeat a
# 1280 px profile can test, 0.981 of a perfect array. A picture of real passages, at a pitch this
# window cannot hold six of.
#
# THE FRAME STILL DOES NOT SATISFY THE REGION. Six periods is what proves an array and this one
# cannot show them. What these tests pin is that the repair named is the WINDOW and not the
# placement, so nobody is sent to move a cut that is already on the right thing.
from __future__ import annotations

import logging

import pytest
from _passage_pictures import write_cut_picture  # noqa: E402  (this dir is on sys.path)

from meshpipeline.sandbox.picture_structure import MIN_REPEATS
from meshpipeline.sandbox.review_session import COUNTABLE_CELL_PX, ArtifactResolver

#: Job 0ff6f9e5/22ac7054's pinned thin cell, which is what the reading side divides by.
CELL_M = 2.51578e-05
#: Eight pixels on that cell, which is twice the floor it is countable at - so every frame below
#: clears the scale branch and is judged on what is DRAWN in it, which is the point.
CELL_PX = 8
M_PER_PX = CELL_M / CELL_PX
WINDOW = (1280 * M_PER_PX, 960 * M_PER_PX)

COARSE_PITCH_PX = 394       # the real plate's own reading
LONGEST_TESTABLE_PX = 212   # about a sixth of a 1280 px profile

MISPLACED = "whatever the cut landed on"
TOO_NARROW = "TOO NARROW FOR THE PITCH"


def _manifest(ws, pitch_px: int, *, cells_px: int = CELL_PX, claim=None) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    rel = "review/cuts/along_the_flow_zoom.png"
    write_cut_picture(ws / rel, 1280, 960, cells_px=cells_px, period_px=pitch_px)
    frame = {"scope": "zoom", "image": rel, "window_m": [WINDOW[0], WINDOW[1]],
             "metres_per_pixel": M_PER_PX}
    if claim is not None:
        frame["content"] = {"shows_structure": claim}
    return {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
            "quality": {"near_wall_cell_m": CELL_M},
            "inspection_regions": [{"name": "along_the_flow", "kind": "slice",
                                    "normal": [0.0, 1.0, 0.0], "origin": [0.0, 0.0, 0.0],
                                    "label": "Along the coolant's path",
                                    "why": "the cells across each passage", "frames": [frame]}]}


def _resolve(ws, caplog, pitch_px: int, **kw) -> str:
    man = _manifest(ws, pitch_px, **kw)
    with caplog.at_level(logging.INFO, logger="meshpipeline.sandbox.review_session"):
        ArtifactResolver(ws, man).resolve_cuts()
    return "\n".join(r.getMessage() for r in caplog.records)


def test_eight_pixels_on_the_cell_really_does_clear_the_scale_branch():
    """The premise of every test below: these frames are refused on their CONTENT, not their
    scale, so the branch under test is the one that runs."""
    assert CELL_PX >= COUNTABLE_CELL_PX


def test_a_frame_too_narrow_for_its_pitch_is_not_called_a_misplaced_cut(tmp_path, caplog):
    log = _resolve(tmp_path, caplog, COARSE_PITCH_PX)
    assert TOO_NARROW in log, log
    assert MISPLACED not in log, log


def test_it_says_the_window_and_not_the_placement_is_the_repair(tmp_path, caplog):
    log = _resolve(tmp_path, caplog, COARSE_PITCH_PX)
    assert "the WINDOW and not the placement" in log, log
    assert "may be on the passages and unable to prove it" in log, log


def test_it_names_the_window_that_would_hold_an_array(tmp_path, caplog):
    """The figure an operator acts on. Six of the frame's own run spacing, in millimetres: this
    frame is 4.03 mm across and 394 px x 6 of it is 7.43 mm."""
    log = _resolve(tmp_path, caplog, COARSE_PITCH_PX)
    want_mm = COARSE_PITCH_PX * MIN_REPEATS * M_PER_PX * 1e3
    assert f"A window of {want_mm:.2f} mm" in log, (want_mm, log)
    assert want_mm > WINDOW[0] * 1e3, "a repair that is not wider than the frame is not a repair"


def test_it_carries_the_three_measured_figures_the_refusal_rests_on(tmp_path, caplog):
    log = _resolve(tmp_path, caplog, COARSE_PITCH_PX)
    assert f"{float(COARSE_PITCH_PX):.1f} px apart" in log, log
    assert f"past the {LONGEST_TESTABLE_PX} px" in log, log
    assert f"the {MIN_REPEATS} a periodic array has to show" in log, log


def test_the_region_is_still_not_satisfied_by_it(tmp_path, caplog):
    """THE HALF THAT MUST NOT MOVE. A frame that cannot show six periods has not proved an array,
    whatever the reason - this change renames the defect, it does not discharge the region."""
    log = _resolve(tmp_path, caplog, COARSE_PITCH_PX)
    assert "is not satisfied by this frame" in log, log


def test_a_frame_that_really_holds_nothing_is_still_called_a_misplaced_cut(tmp_path, caplog):
    """THE BRANCH THAT WAS ALREADY RIGHT. One solid block of cells - the plenum shape - publishes
    0 repeats, run spacing 0.0 px and `period_unmeasurable` False: the frame COULD have measured a
    period and there was none to measure. That is a cut in the wrong place and is still said so."""
    log = _resolve(tmp_path, caplog, 0)
    assert MISPLACED in log, log
    assert TOO_NARROW not in log, log


def test_a_frame_that_holds_the_array_is_refused_on_neither(tmp_path, caplog):
    """A 32 px pitch is 39 repeats at 1.000 of a perfect array - the honest frame."""
    log = _resolve(tmp_path, caplog, 32)
    assert MISPLACED not in log, log
    assert TOO_NARROW not in log, log


@pytest.mark.parametrize("pitch", [320, 394, 400])
def test_every_pitch_the_frame_cannot_test_reads_the_same_way(tmp_path, caplog, pitch):
    log = _resolve(tmp_path, caplog, pitch)
    assert TOO_NARROW in log, log
    assert MISPLACED not in log, log


def test_a_producer_veto_still_wins_over_both(tmp_path, caplog):
    """ORDER MATTERS, and this is the one case that proves the new branch did not jump the queue
    it was inserted into: the producer's `shows_structure: false` is a veto, and a frame this side
    measured no array in is refused for the measurement regardless. The veto branch is below both,
    so a frame with a coarse pitch AND a veto must read as the coarse pitch - this side's own
    measurement, which is the authority the module's header gives it."""
    log = _resolve(tmp_path, caplog, COARSE_PITCH_PX, claim=False)
    assert TOO_NARROW in log, log
    assert "the side that drew it refused the frame" not in log, log


def test_the_run_spacing_is_measured_from_the_file_and_not_the_manifest(tmp_path, caplog):
    """The figure the repair is computed from. A manifest cannot state it: `FrameInk` is built
    only from the PNG's own pixels, and the manifest below claims a healthy array."""
    man = _manifest(tmp_path, COARSE_PITCH_PX, claim=True)
    cuts = ArtifactResolver(tmp_path, man).resolve_cuts()
    ink = cuts["along_the_flow"][0].ink
    assert ink.run_spacing_px == pytest.approx(float(COARSE_PITCH_PX), abs=1.0)
    assert ink.period_unmeasurable is True
    assert ink.max_lag_px == LONGEST_TESTABLE_PX
    assert ink.shows_structure is False, "the manifest's claim may not grant one"
