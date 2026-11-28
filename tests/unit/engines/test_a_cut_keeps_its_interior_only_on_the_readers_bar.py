# Responsibility: an interior is deleted at a plane only when that plane's picture is real evidence.
# Boundaries: `native._zoom_discharges_its_region` and `native._refused_planes`.
# Collaborates with: sandbox/picture_structure (the shared rule) and render_adapter (the reader).
"""The producer and the reader were asking two different questions about the same picture.

`_refused_planes` decided which planes keep an interior by reading `shows_structure`. What
actually discharges a region is `review_session.ResolvedCut.counts_cells`, which is
`cell_px >= COUNTABLE_CELL_PX and shows_cells`, and `shows_cells` ends at
`wall_in_cells(ink.bare_run_min_px, repeat_pitch_px) >= WALL_CELLS_MIN`. The second is strictly
stronger: a frame can show a perfectly regular structure and contain no metal at all.

MEASURED, job 40e4e578, from its own mesh_manifest.json:

    frame                      shows_structure   bare_run_min_px   counts_cells   wall_cells
    across_the_flow   zoom     True              0.0               False          0.00
    through_the_plate zoom     True              30.0              True           7.50
    along_the_flow    zoom     False             300.0             False          75.00

The reader's own words for across_the_flow: "the NARROWEST metal between two passages is 0.0 px -
0.00 cell(s) ... No column of this frame along that axis is bare, so there is no metal in it at
all - this is a field of coolant, whatever its section does."

WHAT THE TWO BARS COST TOGETHER. `expand_region_targets` offers a region only when the volume is
available or one of its frames `counts_cells`, and this engine OBLIGES `region:across_the_flow`.
So the reader needed an interior at precisely the plane whose zoom `shows_structure` told the
producer to delete it at. Four jobs - 40e4e578, 606af55e, b8f09ae9, 110daca9 - delivered a mesh, a
bundle and a viewer with `review_execution: failed_to_complete`, refused before the model was
asked anything.

`slices.py` imports these constants from `sandbox/picture_structure` with the note that they moved
there "verbatim, so the side that DRAWS a frame and the side" that reads it cannot drift apart.
This is that, for the interior.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate.native import (
    _refused_planes, _zoom_discharges_its_region)
from meshpipeline.sandbox.picture_structure import (
    PLAN_CELLS_ACROSS_A_WALL, WALL_CELLS_MIN, wall_in_cells)

CELL_M = 2.51578e-05
CELL_PX = 4

#: (shows_structure, bare_run_min_px) exactly as job 40e4e578 published them.
REAL = {
    "across_the_flow": (True, 0.0),
    "through_the_plate": (True, 30.0),
    "along_the_flow": (False, 300.0),
}
#: And what the READER measured on the same three, through ArtifactResolver.
READER_COUNTS_CELLS = {
    "across_the_flow": False,
    "through_the_plate": True,
    "along_the_flow": False,
}


def zoom(shows: bool, bare: float, *, cell_px=CELL_PX, cell_m=CELL_M) -> dict:
    return {"scope": "zoom", "feature_m": cell_m, "feature_pixels": cell_px,
            "window_m": [0.0041802, 0.0031351],
            "content": {"shows_structure": shows, "bare_run_min_px": bare,
                        "bare_run_px": bare, "repeats": 9 if shows else 1,
                        "period_px": 123.0 if shows else 0.0}}


def whole() -> dict:
    return {"scope": "whole", "window_m": [0.17751, 0.13313],
            "centre_m": [0.02717348, 0.04102818],
            "content": {"shows_structure": False, "bare_run_min_px": 0.0}}


def cut(name: str, shows: bool, bare: float, **kw) -> dict:
    return {"name": name, "normal": [0.0, 1.0, 0.0], "normal_axis": 1, "plane_axes": [0, 2],
            "origin": [0.0229212, 0.0227133, 0.0418994],
            "frames": [whole(), zoom(shows, bare, **kw)]}


# ------------------------------------------------------------------ the shared bar

def test_the_constants_are_the_readers_own():
    assert PLAN_CELLS_ACROSS_A_WALL == 4.0
    assert WALL_CELLS_MIN == 2.0


@pytest.mark.parametrize("name", sorted(REAL))
def test_the_producer_now_agrees_with_the_reader_on_every_real_frame(name):
    """THE REGRESSION, and the whole point: these two columns used to disagree on
    across_the_flow, which is the one the engine obliges."""
    shows, bare = REAL[name]
    assert _zoom_discharges_its_region(zoom(shows, bare)) is READER_COUNTS_CELLS[name]


def test_a_structured_frame_with_no_metal_does_not_buy_the_deletion():
    """across_the_flow, exactly. shows_structure True, and not evidence."""
    assert _zoom_discharges_its_region(zoom(True, 0.0)) is False


def test_a_structured_frame_with_metal_does():
    """through_the_plate, exactly: 30 px of bare run over a 4 px cell is 7.5 cells of wall."""
    assert wall_in_cells(30.0, CELL_PX) == 7.5
    assert _zoom_discharges_its_region(zoom(True, 30.0)) is True


def test_an_unstructured_frame_never_buys_the_deletion_however_much_metal_it_has():
    """along_the_flow has 75 cells of wall and still shows nothing periodic - its window is too
    narrow for the pitch. Metal alone is not a picture of passages."""
    assert wall_in_cells(300.0, CELL_PX) == 75.0
    assert _zoom_discharges_its_region(zoom(False, 300.0)) is False


def test_the_floor_is_inclusive_at_exactly_two_cells():
    """`wall_in_cells` exists because the old form drifted: an honest 2-cell wall read 1.9986 and
    fell under its own floor. This form reads 2.00, so the boundary case is decided by the rule."""
    assert wall_in_cells(2.0 * CELL_PX, CELL_PX) == 2.0
    assert _zoom_discharges_its_region(zoom(True, 2.0 * CELL_PX)) is True
    assert _zoom_discharges_its_region(zoom(True, 2.0 * CELL_PX - 0.1)) is False


# ------------------------------------------------------------------ silence is not evidence

@pytest.mark.parametrize("bad", [None, 0, 0.0])
def test_an_unmeasured_wall_is_not_evidence(bad):
    assert _zoom_discharges_its_region(zoom(True, bad)) is False


@pytest.mark.parametrize("cell", [None, 0, 0.0])
def test_an_unmeasured_cell_is_not_evidence(cell):
    """Without the drawn cell there is nothing to express the wall in, and `wall_in_cells` returns
    0.0 for it - "silence and not a denial". An interior is kept on silence."""
    assert _zoom_discharges_its_region(zoom(True, 30.0, cell_px=cell)) is False


def test_a_frame_with_no_content_block_is_not_evidence():
    assert _zoom_discharges_its_region({"scope": "zoom", "feature_pixels": CELL_PX}) is False
    assert _zoom_discharges_its_region(
        {"scope": "zoom", "feature_pixels": CELL_PX, "content": None}) is False
    assert _zoom_discharges_its_region({}) is False


# ------------------------------------------------------------------ which planes get a slab

def test_the_obliged_plane_now_gets_a_slab():
    """BEFORE THE FIX THIS PLANE GOT NOTHING, because its zoom shows structure. It is the plane
    `expand_region_targets` cannot discharge from its picture, so it is exactly the one the
    interior had to be kept at."""
    cuts = [cut(n, *REAL[n]) for n in ("across_the_flow", "through_the_plate", "along_the_flow")]
    got = {c["name"] for c, _w, _cell in _refused_planes(cuts)}
    assert "across_the_flow" in got
    assert "along_the_flow" in got
    assert "through_the_plate" not in got, \
        "a plane whose picture IS evidence must still buy the deletion honestly"


def test_a_cut_with_no_zoom_is_skipped():
    """There is nothing to judge and nothing to centre a slab on."""
    assert _refused_planes([{"name": "x", "frames": [whole()]}]) == []


def test_the_planned_cell_and_the_whole_frame_come_back_with_the_cut():
    """`_reduce_to_review_slabs` needs the in-plane window and a thickness; a slab with no
    thickness clips to nothing."""
    got = _refused_planes([cut("across_the_flow", True, 0.0)])
    assert len(got) == 1
    _c, w, cell = got[0]
    assert w["scope"] == "whole"
    assert cell == pytest.approx(CELL_M)


def test_no_cuts_is_no_planes():
    assert _refused_planes(None) == []
    assert _refused_planes([]) == []


def test_a_zoom_with_no_cell_still_yields_a_thickness_from_the_window():
    """SLAB_FALLBACK_WINDOW_FRACTION: a plane with no planned cell still needs a thickness, or
    the clip takes nothing and the reviewer is handed an empty file instead of a slab."""
    from meshpipeline.engines.cfmesh_conjugate.native import SLAB_FALLBACK_WINDOW_FRACTION

    c = cut("across_the_flow", True, 0.0, cell_m=0.0)
    got = _refused_planes([c])
    assert len(got) == 1
    _c, w, cell = got[0]
    assert cell == pytest.approx(min(*w["window_m"]) / SLAB_FALLBACK_WINDOW_FRACTION)
    assert cell > 0.0
