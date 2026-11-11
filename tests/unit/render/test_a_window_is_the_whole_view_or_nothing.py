"""A window carries every face in the box or none of them, and never a sample.

`detail_at` may sample, and should: a cut is a ball a reader opened ON something, so a sample of it
still answers the question they asked, and the budget is shared out so every patch keeps some of
itself rather than one patch eating the lot.

`detail_box` is the opposite case and the opposite rule. It answers "draw what I am LOOKING at",
which is the whole screen - and a sample of the whole screen is a screen with holes in it. Nobody
can tell a hole in the sample from a hole in the mesh, so a reader would be looking at a picture
of a mesh with gaps in it and have no way to know. It refuses instead, and the client keeps
drawing the surface it already has.

That handover is only invisible because the surface it falls back to is the EXACT one - same
geometry, coarser subdivision - which is why this file sits next to the exact-surface tests rather
than next to the cut's.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.render.boundary_index import open_index, write_index
from meshpipeline.render.detail_window import DetailError, detail_box
from tests.foam_fixtures import write_row_of_hexes


def _ws(tmp_path: Path, regions=("fluid",), **kw) -> Path:
    ws = tmp_path / "ws"
    for r in regions:
        pm = ws / "constant" / r / "polyMesh"
        write_row_of_hexes(pm, **kw)
        write_index(pm)
    return ws


# ------------------------------------------------------------------ the box query itself
def test_the_box_query_agrees_with_a_scan(tmp_path):
    """The index's answer must be the set a whole-boundary scan returns, or it is drawing
    something other than what is in view."""
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    write_index(pm)
    ix = open_index(pm)
    cen = np.asarray(ix.centroids, dtype=np.float64)
    sizes = np.asarray(ix.sizes)
    for lo, hi in (((-9, -9, -9), (9, 9, 9)),          # everything
                   ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),  # one cell's corner
                   ((1.2, 0.0, 0.0), (2.8, 1.0, 1.0)),  # the middle cell
                   ((50.0, 50.0, 50.0), (51.0, 51.0, 51.0))):   # nothing
        got = ix.faces_in_box(lo, hi)
        want = np.flatnonzero(
            np.all((cen >= np.array(lo)) & (cen <= np.array(hi)), axis=1) & (sizes >= 3))
        assert np.array_equal(np.sort(got), np.sort(want)), (lo, hi)
        assert ix.count_in_box(lo, hi) == len(want)


def test_the_corners_of_the_box_are_not_lost(tmp_path):
    """A box test written as a distance would clip the corners, which is the whole reason this is
    not a ball: on a 16:9 frustum a ball drawn round the box over-reaches by about 2.2x and one
    inscribed in it misses the corners entirely."""
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    write_index(pm)
    ix = open_index(pm)
    cen = np.asarray(ix.centroids, dtype=np.float64)
    lo, hi = np.array([-1.0, -1.0, -1.0]), np.array([4.0, 2.0, 2.0])
    got = set(ix.faces_in_box(lo, hi).tolist())
    # every face whose centroid is in the box, corner-most included
    for f in range(ix.n_faces):
        if int(ix.sizes[f]) < 3:
            continue
        if np.all((cen[f] >= lo) & (cen[f] <= hi)):
            assert f in got, "face %d is in the box and was not returned" % f


# ------------------------------------------------------------------ the window's own rule
def test_a_window_that_fits_returns_every_face_in_it(tmp_path):
    got = detail_box(_ws(tmp_path), lo=(-9, -9, -9), hi=(9, 9, 9))
    assert got["fits"] is True
    assert got["faces_returned"] == got["faces_in_box"] > 0, (
        "a window that fits must carry ALL of it, or the screen has holes in it")
    assert got["truncated"] is False
    n = 0
    for p in got["patches"]:
        pol = np.frombuffer(p["polys"], dtype=np.uint32)
        at = 0
        while at < pol.size:
            at += int(pol[at]) + 1
            n += 1
    assert n == got["faces_returned"]


def test_a_window_over_the_budget_sends_nothing_and_says_how_many(tmp_path):
    """NOT a sample, and not an error either - this is the routine answer at every zoom above the
    threshold, and the client simply keeps the surface up."""
    got = detail_box(_ws(tmp_path), lo=(-9, -9, -9), hi=(9, 9, 9), max_faces=3)
    assert got["fits"] is False
    assert got["patches"] == []
    assert got["faces_returned"] == 0
    assert got["faces_in_box"] > 3
    assert "3" in got["why"] and "faces" in got["why"]


def test_an_empty_window_fits_and_carries_nothing(tmp_path):
    got = detail_box(_ws(tmp_path), lo=(80.0, 80.0, 80.0), hi=(81.0, 81.0, 81.0))
    assert got["fits"] is True
    assert got["faces_in_box"] == 0
    assert got["patches"] == []


def test_both_regions_of_a_conjugate_window_come_back(tmp_path):
    """One side of a conjugate interface without the other is the mistake `detail_at`'s budget
    rule exists to prevent, and a window must not reintroduce it."""
    got = detail_box(_ws(tmp_path, regions=("fluid", "solid")), lo=(-9, -9, -9), hi=(9, 9, 9))
    assert got["fits"]
    regions = {str(p["name"]).split("/")[0] for p in got["patches"]}
    assert regions == {"fluid", "solid"}, regions


def test_it_packs_the_same_shape_the_browser_already_decodes(tmp_path):
    """`detail_response` is a named shape the viewer is tested against; a window reuses it rather
    than growing a second decoder, so it has to carry those keys too."""
    got = detail_box(_ws(tmp_path), lo=(-9, -9, -9), hi=(9, 9, 9))
    for k in ("centre", "radius", "faces_in_range", "faces_returned", "truncated", "patches"):
        assert k in got, k
    assert len(got["centre"]) == 3
    assert got["radius"] > 0
    for p in got["patches"]:
        for k in ("name", "type", "face_count", "faces_in_range", "points", "polys"):
            assert k in p, (p.get("name"), k)


def test_a_degenerate_box_is_refused_in_words(tmp_path):
    ws = _ws(tmp_path)
    for lo, hi in (((0, 0, 0), (0, 1, 1)), ((0, 0, 0), (1, 1, 0))):
        with pytest.raises(DetailError):
            detail_box(ws, lo=lo, hi=hi)
    with pytest.raises(DetailError):
        detail_box(ws, lo=(0, 0), hi=(1, 1, 1))


def test_the_corners_may_arrive_either_way_round(tmp_path):
    """A camera at an angle produces whichever corner it produces; a box is a box."""
    a = detail_box(_ws(tmp_path), lo=(-9, -9, -9), hi=(9, 9, 9))
    b = detail_box(_ws(tmp_path), lo=(9, 9, 9), hi=(-9, -9, -9))
    assert a["faces_in_box"] == b["faces_in_box"] > 0


def test_no_index_is_refused_rather_than_parsed(tmp_path):
    """`detail_at` falls back to parsing the whole boundary. A window must not: it is asked again
    every time the camera settles, and that parse is minutes on a delivered plate."""
    ws = _ws(tmp_path)
    import shutil
    shutil.rmtree(ws / "constant" / "fluid" / "polyMesh" / "boundaryIndex")
    with pytest.raises(DetailError) as exc:
        detail_box(ws, lo=(-9, -9, -9), hi=(9, 9, 9))
    assert "index" in str(exc.value).lower()
