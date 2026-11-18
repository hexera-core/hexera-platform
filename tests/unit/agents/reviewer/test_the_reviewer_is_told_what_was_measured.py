# Responsibility: Verify the sentence a reviewer reads about a prepared frame states only things
# that were measured about THAT frame, and says where the frame was taken.
# Boundaries: the words. Nothing here renders, and nothing here decides coverage.
#
# WHY THE SENTENCE IS THE PRODUCT. A reviewer cannot look at a picture twice; it reads what it is
# told about one and judges from that. Round 2 introduced all three of job 0ff6f9e5's zoom frames
# with "cells are countable in this frame" on the strength of arithmetic that cannot come out any
# other way, and two of those pictures contain no passage at all - one is a single circular port
# bore, one is three plenum rectangles. If a reader of these lines cannot tell a grid of cells
# from a port bore, this file is the thing that failed.
from __future__ import annotations

from pathlib import Path

from _reviewer_passage_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_cut_picture,
)

from meshpipeline.agents.reviewer.scene_text import (
    CANNOT_COUNT,
    cut_frame_line,
    prepared_cuts_block,
)
from meshpipeline.sandbox.review_session import (
    COUNTABLE_CELL_PX,
    FRAME_WHOLE,
    FRAME_ZOOM,
    ArtifactResolver,
)

CELL_M = 2.51578e-05                 # quality.near_wall_cell_m on job 0ff6f9e5
MIN_CELL_M = 7.787400214457237e-06   # quality.min_cell_m - a port-rim sliver
MAX_CELL_M = 0.0006788786304584814   # quality.max_cell_m - a factor of 87 away
FRAME_PX = (1280, 960)
CENTRE = [0.0672742836, 0.0751898789]
CENTRE_WHY = "the densest window-sized patch of this cut, which is where its cells are finest"
GOOD_CONTENT = {"shows_structure": True, "repeats": 40, "period_px": 32.0, "spread": 0.04,
                "ink_fraction": 0.251}
NO_CONTENT = {"shows_structure": False, "ink_fraction": 0.0041,
              "why": "the ink forms 1 repeat(s) across the frame, under the 6 a periodic array "
                     "has to show"}


def _cut(ws: Path, *, cells_px: int, scope: str = FRAME_ZOOM, centre=True, content=GOOD_CONTENT,
         period_px: int = 32, patch_px=None):
    rel = f"review/cuts/through_the_plate_{scope}.png"
    write_cut_picture(ws / rel, *FRAME_PX, cells_px=cells_px, period_px=period_px,
                      patch_px=patch_px)
    mpp = CELL_M / COUNTABLE_CELL_PX
    frame = {"scope": scope, "image": rel, "metres_per_pixel": mpp,
             "window_m": [mpp * FRAME_PX[0], mpp * FRAME_PX[1]],
             "why_frame": "sized so one cell is four pixels across"}
    if centre:
        frame["centre_m"] = list(CENTRE)
        frame["centre_source"] = CENTRE_WHY
    if content is not None:
        frame["content"] = dict(content)
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    manifest = {
        "mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
        "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": MIN_CELL_M,
                    "max_cell_m": MAX_CELL_M},
        "inspection_regions": [{
            "name": "through_the_plate", "kind": "slice", "normal": [0.0, 1.0, 0.0],
            "origin": [0.02717348, 0.041070016, 0.0419095995], "plane_axes": [0, 2],
            "label": "Through the part at mid-thickness",
            "why": "where the coolant reaches and where the metal is",
            "frames": [frame]}]}
    return ArtifactResolver(ws, manifest).resolve_cuts()["through_the_plate"][0]


def test_the_line_says_what_was_measured_in_the_picture_itself(tmp_path):
    """Not what the window implies. The measured clause is the only one that can disagree."""
    line = cut_frame_line(_cut(tmp_path, cells_px=0))
    assert "MEASURED IN THE PICTURE ITSELF" in line
    assert "and 0 cell(s) are resolved on its best row" in line
    assert "% of the frame has anything drawn in it at all" in line


def test_a_frame_with_no_cells_is_not_introduced_as_countable(tmp_path):
    """The exact sentence that was said over a picture of a port bore."""
    line = cut_frame_line(_cut(tmp_path, cells_px=0))
    assert "cells are countable in this frame" not in line
    assert CANNOT_COUNT in line
    # and the reason names WHICH test failed, because the repairs are different ones
    assert "the window is sized correctly and the cells are not in the picture" in line


def test_a_frame_with_cells_is_still_introduced_as_countable(tmp_path):
    """So the words are a report, not a refusal with extra steps."""
    line = cut_frame_line(_cut(tmp_path, cells_px=4))
    assert "cells are countable in this frame" in line
    assert CANNOT_COUNT not in line


def test_a_channel_array_and_a_port_bore_do_not_read_the_same(tmp_path):
    """The brief's own test: if the reviewer cannot tell these apart from the words, we failed."""
    bore = cut_frame_line(_cut(tmp_path, cells_px=0, period_px=0, patch_px=(120, 120)))
    cells = cut_frame_line(_cut(tmp_path, cells_px=4))
    assert bore != cells
    assert "and 0 cell(s) are resolved on its best row" in bore
    # RE-WORDED IN ROUND 5. "NO PASSAGE ARRAY" was said of a frame that may hold a perfectly
    # regular structure which is simply not the part's, so the three answers are named apart.
    assert "NO PERIODIC STRUCTURE IS IN THIS FRAME as measured here" in bore, bore
    assert "and 0 cell(s) are resolved on its best row" not in cells
    # RE-WORDED IN ROUND 7: the opener no longer says the array is this part's, because
    # nothing on this side holds the measured pitch to the part. See
    # `test_the_words_say_whose_array_it_is.py` for the two features of this plate that discharge.
    assert ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME as "
            "measured here") in cells, cells
    assert "drawn" in cells and "apart" in cells, "the passing line says the spacing it measured"


def test_the_passage_reading_is_this_sides_own_and_is_said_so(tmp_path):
    """The figure the reviewer reads came out of the PNG THIS side holds, not out of the manifest.

    It used to be quoted as "the side that drew this frame measured N repeats", read straight from
    `content`, and that boolean was what discharged the region. The manifest below states 40
    repeats every 32.0 px and the picture holds 39 every 32.0 px: the line must carry the
    measured pair, because that is the one a reader could check.
    """
    line = cut_frame_line(_cut(tmp_path, cells_px=4))
    assert ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME as "
            "measured here: 39 repeats along the "
            "horizontal axis of this frame, every 32.0 px") in line, line
    assert "The side that drew this frame measured" not in line, (
        "an agreeing claim from the untrusted side adds nothing and must not be recited")
    assert "40 repeats" not in line, "40 is the manifest's number, and 39 is the picture's"


def test_a_frame_the_other_side_refused_says_so_and_cannot_count(tmp_path):
    """A present False still vetoes, and the words name whose refusal it was.

    Measured on this part: two of three planes cannot cross the microchannels at all, and the side
    that drew the frame knows the plan and the whole picture while this side holds one window.
    """
    line = cut_frame_line(_cut(tmp_path, cells_px=4, content=NO_CONTENT))
    assert "The side that drew this frame REFUSED it as evidence" in line, line
    assert NO_CONTENT["why"] in line
    assert CANNOT_COUNT in line
    assert "its refusal is taken" in line


def test_a_frame_with_no_published_claim_is_described_and_still_counts(tmp_path):
    """An ABSENT claim is neutral, and the line says that rather than implying a denial.

    A manifest written before `content` existed, or replayed out of an archive, carries no such
    field, and the reader no longer needs one.
    """
    line = cut_frame_line(_cut(tmp_path, cells_px=4, content=None))
    assert "published no measurement of its own" in line, line
    assert "neither adds to nor subtracts from" in line
    assert "cells are countable in this frame" in line
    assert CANNOT_COUNT not in line


 # item 3: where the window sits

def test_the_line_says_where_the_window_sits_and_who_chose_it(tmp_path):
    """A reader of a 4.5% window who is not told WHICH 4.5% cannot check anything in it.

    The producer re-aims the zoom at the densest patch of the cut AFTER the plan is written, so
    `centre_m` is the only record of where the camera ended up.
    """
    line = cut_frame_line(_cut(tmp_path, cells_px=4))
    assert "centred at x = 67.27 mm, z = 75.19 mm" in line, line
    assert CENTRE_WHY in line


def test_a_window_with_no_published_centre_says_that_out_loud(tmp_path):
    """Silence about the place reads as "the middle", which is where it is NOT."""
    line = cut_frame_line(_cut(tmp_path, cells_px=4, centre=False))
    assert "WHERE IN THE PLANE THIS WINDOW SITS WAS NOT PUBLISHED" in line


 # item 8: say what the resolution figure IS

def test_the_resolution_figure_is_named_as_the_plan_s_and_not_the_mesh_s(tmp_path):
    """`quality.near_wall_cell_m` is derived from the plan. This mesh's cells span a factor of 87."""
    line = cut_frame_line(_cut(tmp_path, cells_px=4))
    assert "a cell of the size the PLAN pinned" in line
    assert "the mesh's own" not in line, (
        "that phrase claimed a measurement of the mesh for a number the plan chose")
    assert "whose cells run 7.787 to 678.9 um" in line, line


 # the opening block

def test_the_opening_block_sends_the_reviewer_to_the_frame_that_can_discharge(tmp_path):
    """With no scope the reviewer gets the whole plane, so the block has to name the other one."""
    block = prepared_cuts_block([_cut(tmp_path, cells_px=4, scope=FRAME_WHOLE),
                                 _cut(tmp_path, cells_px=4, scope=FRAME_ZOOM)])
    assert 'scope="zoom"' in block
    assert "the only one an inspection can be judged on" in block
    assert "MEASURED in the picture itself" in block
