"""A reader who zooms in on a 98 um channel is shown merged cells, so there is a way to ask.

`render.polymesh_budget.fit_to_budget` clusters cells so a delivered mesh fits what a browser can
carry. The first customer cold plate delivers 13,022,400 boundary faces on the coolant alone and
the budget is 800,000 triangles, so about sixteen delivered faces become one drawn one. The payload
says so, and the viewer's zoom still lets a reader go in to a few cell widths - where the surface
under their cursor is a merged approximation of the cells rather than the cells. Every face count,
size and quality figure they are given is the mesh's own; the surface is not.

`detail_window.detail_at` is the answer: the same reader, on the same delivered polyMesh,
restricted to a ball around the point they asked about, with nothing merged. Measured on the
delivered coupon (fluid 349,656 boundary faces, solid 375,594):

    r = 0.3 mm   first ask        8.05 s   732 faces, one patch
    r = 0.3 mm   asked again      0.04 s   the same 732
    r = 1.0 mm                    0.10 s   23,223 over four patches
    r = 4.0 mm                    0.51 s   404,725 in range, 150,000 returned, truncated

and every patch's two buffers agreed with its declared face count, with no index past its own
points. fluid_to_solid and solid_to_fluid both came back 8,869 faces, which is the conjugate
interface showing up equal on both sides in the detail cut too.
"""
from __future__ import annotations

import struct

import pytest

from meshpipeline.render import detail_window as D
from tests.foam_fixtures import write_row_of_hexes

#: Three unit hexes along x: inlet at x=0 (1 face), outlet at x=3 (1 face), wall (12 faces).
#: A ball at the middle of the first cell reaches the inlet and four wall faces and not the outlet.
NEAR_INLET = (0.5, 0.5, 0.5)


@pytest.fixture
def one_region(tmp_path):
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    return tmp_path


@pytest.fixture
def two_regions(tmp_path):
    """The shape a conjugate delivery has: named regions AND the pre-split block still at the
    root, because splitMeshRegions leaves it there."""
    for region in ("fluid", "solid"):
        write_row_of_hexes(tmp_path / "constant" / region / "polyMesh")
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    return tmp_path


def test_a_single_region_mesh_is_read_from_the_root(one_region):
    assert [n for n, _p in D.delivered_regions(one_region)] == [""]


def test_the_presplit_block_is_not_one_of_the_regions(two_regions):
    """THE TRAP THE VIEWER REFUSES BY DESIGN. splitMeshRegions leaves the pre-split block at
    constant/polyMesh carrying every patch with the two regions' face counts ADDED TOGETHER -
    zmax is 532 at the root, the fluid's 128 plus the solid's 404 - so a reader shown it is shown
    a mesh that was never delivered and no split at all. The first version of this read it:
    measured on the delivered coupon, a 0.3 mm ball came back with `zmax` 732 faces beside
    `fluid/inlet` 732, the first being the pre-split surface standing in for the delivered one.
    """
    assert [n for n, _p in D.delivered_regions(two_regions)] == ["fluid", "solid"]


def test_nothing_delivered_is_said_and_not_guessed(tmp_path):
    with pytest.raises(D.DetailError, match="no mesh boundary"):
        D.detail_at(tmp_path, centre=NEAR_INLET, radius=1.0)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_radius_that_is_not_a_length_is_refused(one_region, bad):
    with pytest.raises(D.DetailError, match="positive radius"):
        D.detail_at(one_region, centre=NEAR_INLET, radius=bad)


def test_a_point_out_in_space_brings_back_nothing_rather_than_the_nearest_thing(one_region):
    """Returning the nearest faces would put a reader somewhere they did not ask to be."""
    out = D.detail_at(one_region, centre=(100.0, 100.0, 100.0), radius=1.0)
    assert out["faces_in_range"] == 0
    assert out["patches"] == []
    assert out["truncated"] is False


def test_only_the_faces_whose_centroid_is_in_range_come_back(one_region):
    """The test the viewer's own areas use, and it keeps each face whole rather than clipping it
    into something the mesher never wrote. The inlet's centroid is 0.5 from the ball's centre and
    the outlet's is 2.5."""
    out = D.detail_at(one_region, centre=NEAR_INLET, radius=0.6)
    names = {p["name"]: p["face_count"] for p in out["patches"]}
    assert "inlet" in names and names["inlet"] == 1
    assert "outlet" not in names
    assert out["faces_in_range"] == sum(names.values())


def test_a_wider_ball_reaches_further_and_a_wide_enough_one_reaches_everything(one_region):
    near = D.detail_at(one_region, centre=NEAR_INLET, radius=0.6)
    far = D.detail_at(one_region, centre=NEAR_INLET, radius=10.0)
    assert far["faces_in_range"] > near["faces_in_range"]
    assert {p["name"] for p in far["patches"]} == {"inlet", "outlet", "wall"}


def test_both_regions_answer_and_wear_their_own_names(two_regions):
    out = D.detail_at(two_regions, centre=NEAR_INLET, radius=0.6)
    names = {p["name"] for p in out["patches"]}
    assert any(n.startswith("fluid/") for n in names)
    assert any(n.startswith("solid/") for n in names)
    assert not any("/" not in n for n in names), "an unprefixed patch is the pre-split block"


def test_the_two_buffers_are_the_pair_the_main_view_packs(one_region):
    """float32 xyz and [n, i0..i(n-1)] uint32 per face, so the client draws this with the code it
    already has (viewer_pack.polymesh_response base64s them unchanged)."""
    out = D.detail_at(one_region, centre=NEAR_INLET, radius=10.0)
    assert out["patches"]
    for p in out["patches"]:
        n_pts = len(p["points"]) // 12
        assert len(p["points"]) % 12 == 0
        polys = struct.unpack(f"<{len(p['polys']) // 4}I", p["polys"])
        i = seen = 0
        while i < len(polys):
            k = polys[i]
            assert k >= 3, f"a face of {k} corners"
            assert all(v < n_pts for v in polys[i + 1:i + 1 + k]), "an index past the points"
            i += k + 1
            seen += 1
        assert seen == p["face_count"]


def test_a_named_patch_can_be_left_out(one_region):
    out = D.detail_at(one_region, centre=NEAR_INLET, radius=10.0, skip_names=("wall",))
    assert "wall" not in {p["name"] for p in out["patches"]}
    assert {"inlet", "outlet"} <= {p["name"] for p in out["patches"]}


def test_more_than_the_return_budget_is_truncated_and_says_so(one_region, monkeypatch):
    """A reader given 150,000 of 404,725 faces with no word about it would read the gap as mesh
    that is not there."""
    monkeypatch.setattr(D, "MAX_RETURN_FACES", 2)
    out = D.detail_at(one_region, centre=NEAR_INLET, radius=10.0)
    assert out["faces_returned"] == 2
    assert out["faces_in_range"] == 14, "three hexes in a row have 14 boundary faces"
    assert out["truncated"] is True


def test_a_boundary_too_big_to_read_is_refused_by_name(one_region, monkeypatch):
    """The read is the whole boundary, because a face's position is not known until its points
    are, so there is no way to seek to a neighbourhood. The customer plate's coolant side is
    13,022,400 faces and this is the case that refuses rather than reading 400 MB in an API
    process - and it names the region, because a reader who asked for the coolant and was given
    the metal would read the metal as the coolant."""
    monkeypatch.setattr(D, "MAX_SOURCE_FACES", 3)
    with pytest.raises(D.DetailError, match="14 faces and this reads at most 3"):
        D.detail_at(one_region, centre=NEAR_INLET, radius=1.0)


def test_the_parse_is_kept_so_the_second_ask_is_not_the_first_again(one_region):
    """Measured on the coupon: 8.05 s to answer the first ball and 0.04 s the next, because the
    parse is the whole cost. A reader opens several details of ONE mesh."""
    D._parsed.cache_clear()
    D.detail_at(one_region, centre=NEAR_INLET, radius=0.6)
    before = D._parsed.cache_info()
    D.detail_at(one_region, centre=(2.5, 0.5, 0.5), radius=0.6)
    after = D._parsed.cache_info()
    assert after.hits == before.hits + 1, "the same boundary was parsed twice"
    assert after.misses == before.misses


def test_a_rebuilt_workspace_is_read_again_and_not_answered_from_the_old_one(one_region):
    """The cache key carries the owner file's size and mtime, so a mesh replaced under the same
    path is not served from the one that is gone."""
    D._parsed.cache_clear()
    D.detail_at(one_region, centre=NEAR_INLET, radius=0.6)
    first = D._parsed.cache_info().misses
    owner = one_region / "constant" / "polyMesh" / "owner"
    import os
    import time
    os.utime(owner, (time.time() + 10, time.time() + 10))
    D.detail_at(one_region, centre=NEAR_INLET, radius=0.6)
    assert D._parsed.cache_info().misses == first + 1


def test_the_route_reads_the_workspace_and_says_why_when_it_cannot():
    """Unlike /surface, which is built from the stored payload. The bound and the refusal are the
    price of reading a delivered mesh inside an API process."""
    import inspect

    from meshpipeline.api.v1 import simulation as S

    src = inspect.getsource(S.get_detail)
    assert "final_attempt_workspace" in src
    assert "DetailError" in src and "409" in src
    assert "points_b64" in src and "polys_b64" in src


#   -- the budget is shared out, not eaten -------------------------------------------------------

def test_a_truncated_window_still_shows_every_region(two_regions, monkeypatch):
    """MEASURED ON THE DELIVERED COUPON, 2026-10-29, before this existed. A 4 mm window held
    684,660 faces in range across both regions; the 150,000-face cap was spent walking the patches
    in the order they came, and the answer was 150,000 faces of `fluid/fluid_to_solid` with the
    metal side missing altogether:

        faces_in_range 684660 returned 150000 truncated True
          fluid/fluid_to_solid   mappedWall   faces 150000
          (and nothing else)

    `truncated: true` said something was dropped and not that a whole REGION was, so a reader
    looking at a conjugate interface was shown one side of it with no way to tell. Same mistake as
    publishing half a conjugate review surface, in a different file.
    """
    monkeypatch.setattr(D, "MAX_RETURN_FACES", 8)
    out = D.detail_at(two_regions, centre=NEAR_INLET, radius=10.0)
    names = {p["name"] for p in out["patches"]}
    assert any(n.startswith("fluid/") for n in names), "the coolant went missing"
    assert any(n.startswith("solid/") for n in names), "the metal went missing"
    assert out["truncated"] is True
    assert out["faces_returned"] <= 8


@pytest.mark.parametrize("budget", [1, 2, 3, 5, 6, 7, 8, 13, 27, 28, 29])
def test_no_patch_with_a_face_in_range_is_ever_dropped_in_silence(two_regions, monkeypatch,
                                                                  budget):
    """The property, across every budget from one face to more than the window holds: a patch is
    either in the answer or it had nothing there. Six patches, 28 faces in range."""
    monkeypatch.setattr(D, "MAX_RETURN_FACES", budget)
    out = D.detail_at(two_regions, centre=NEAR_INLET, radius=10.0)
    assert out["faces_returned"] <= budget, "the cap is a cap"
    assert out["faces_returned"] >= min(budget, 1)
    if budget >= 6:
        assert len(out["patches"]) == 6, "every patch keeps a face once there is room for one each"
    for q in out["patches"]:
        assert q["face_count"] >= 1
        assert q["face_count"] <= q["faces_in_range"], "more faces than the patch has there"


def test_each_patch_says_how_much_of_itself_is_being_shown(two_regions, monkeypatch):
    """So a client can say "1 face in 5 of the coolant" rather than leaving a reader to read a
    sample as the whole thing."""
    monkeypatch.setattr(D, "MAX_RETURN_FACES", 8)
    out = D.detail_at(two_regions, centre=NEAR_INLET, radius=10.0)
    assert sum(q["faces_in_range"] for q in out["patches"]) == out["faces_in_range"]
    assert any(q["face_count"] < q["faces_in_range"] for q in out["patches"]), "nothing was cut"


def test_the_faces_kept_are_spread_across_the_patch_and_not_its_first_few(one_region, monkeypatch):
    """A contiguous prefix is whatever order the mesher wrote, which puts the whole sample in one
    corner of the window. Three unit hexes span x=0..3 and the wall has 12 faces along them: two
    faces taken off the front sit inside the first cell, two taken evenly sit at either end."""
    import struct

    monkeypatch.setattr(D, "MAX_RETURN_FACES", 4)         # 1 inlet + 1 outlet + 2 of the 12 wall
    out = D.detail_at(one_region, centre=(1.5, 0.5, 0.5), radius=10.0)
    wall = [q for q in out["patches"] if q["name"] == "wall"]
    assert wall and wall[0]["face_count"] == 2, [q["name"] for q in out["patches"]]
    xs = struct.unpack(f"<{len(wall[0]['points']) // 4}f", wall[0]["points"])[0::3]
    assert max(xs) - min(xs) > 1.5, (
        "both kept faces are in the same cell, so this is still a prefix: %r" % (sorted(xs),))
