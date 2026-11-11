"""A window draws the parts the reader has on screen, and not the ones they turned off.

A box knows nothing about a parts panel, so a window asked for a box came back with every region
in it. On a conjugate mesh that is the metal drawn over the coolant: isolate the coolant, zoom
past the handover, and the plate's outer wall arrives in front of the channels. It gets worse
rather than settling, because the box is centred on whatever is under the middle of the screen -
so the next window centres on the metal that was just drawn in front, and the one after that
walks further out again. Measured on the delivered plate before this: `solid/solid_to_air`, 400
faces, no coolant at all, at a zoom where the coolant had tens of thousands.

TWO NAMES FOR THE SAME PATCH. A window serves a patch as `<region>/<patch>`, because both halves
of a conjugate mesh carry a `..._to_...` side and `fluid_to_solid` and `solid_to_fluid` are
different surfaces. The boundary file it reads calls that same patch just `<patch>`. A list built
from what is on a reader's screen therefore arrives qualified, and the first version of this
matched it against the bare name - which skips nothing, silently.

AND WHY IT IS A KEEP LIST. A viewer can only name patches it was handed, and a conjugate payload
is not handed all of them: this plate's drawn set leaves out `solid/solid_to_fluid` and
`solid/solid_to_chip`, the metal's side of two interfaces already drawn from the other side. A
skip list cannot name what it was never told about, so those two came back regardless - two
coincident sheets of 16,619,777 faces each, which a renderer cannot order and draws as a speckle
of two materials over every channel wall. Naming what IS on screen closes that without the client
ever having to know a patch it was not given.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.render.boundary_index import write_index
from meshpipeline.render.detail_window import detail_box
from tests.foam_fixtures import write_row_of_hexes

ALL = ((-9.0, -9.0, -9.0), (9.0, 9.0, 9.0))


def _ws(tmp_path: Path, regions=("fluid", "solid")) -> Path:
    ws = tmp_path / "ws"
    for r in regions:
        pm = ws / "constant" / r / "polyMesh"
        write_row_of_hexes(pm)
        write_index(pm)
    return ws


def _names(out) -> set[str]:
    return {p["name"] for p in out["patches"]}


def _box(ws, **kw):
    return detail_box(ws, lo=ALL[0], hi=ALL[1], **kw)


# ------------------------------------------------------------------ the unfiltered answer
def test_without_a_list_every_region_in_the_box_comes_back(tmp_path):
    """The behaviour the fix has to preserve: asked for a box and nothing else, a window is still
    everything in that box. Only a reader's own list narrows it."""
    out = _box(_ws(tmp_path))
    assert _names(out) == {"fluid/inlet", "fluid/outlet", "fluid/wall",
                           "solid/inlet", "solid/outlet", "solid/wall"}
    assert out["fits"] is True


# ------------------------------------------------------------------ the keep list
def test_a_keep_list_leaves_out_everything_it_does_not_name(tmp_path):
    out = _box(_ws(tmp_path), keep_names=("fluid/wall",))
    assert _names(out) == {"fluid/wall"}


def test_a_keep_list_can_name_a_patch_in_each_region(tmp_path):
    out = _box(_ws(tmp_path), keep_names=("fluid/wall", "solid/inlet"))
    assert _names(out) == {"fluid/wall", "solid/inlet"}


def test_a_keep_list_that_names_nothing_in_a_region_drops_that_region_whole(tmp_path):
    """Not an error and not an empty patch: the region is simply not read. A reader who isolated
    the coolant is not asking the metal's index a question."""
    out = _box(_ws(tmp_path), keep_names=("fluid/inlet", "fluid/outlet", "fluid/wall"))
    assert {n.split("/")[0] for n in _names(out)} == {"fluid"}


def test_an_absent_keep_list_is_not_an_empty_one(tmp_path):
    """`()` means "the reader did not narrow this", which is every patch. A caller that means
    "the reader can see nothing" must not ask for a window at all - there is nothing to draw and
    an empty answer and a full one would otherwise be written the same way."""
    assert _names(_box(_ws(tmp_path), keep_names=())) == _names(_box(_ws(tmp_path)))


# ------------------------------------------------------------------ the two spellings
def test_a_qualified_name_matches_the_patch_the_reader_means(tmp_path):
    """THE BUG THAT STARTED THIS. The reader holds `fluid/wall`; the boundary file says `wall`.
    Matching only the bare name skipped nothing at all, and said so nowhere."""
    out = _box(_ws(tmp_path), skip_names=("fluid/wall",))
    assert "fluid/wall" not in _names(out)
    assert "solid/wall" in _names(out), "the other region's wall is a different surface"


def test_a_bare_name_still_means_that_patch_in_every_region(tmp_path):
    """Back-compat, and the single-region spelling. A bare name is ambiguous across regions by
    construction, so it means all of them - which is what every caller that predates the
    qualified form intended."""
    out = _box(_ws(tmp_path), skip_names=("wall",))
    assert not {n for n in _names(out) if n.endswith("/wall")}


def test_bare_and_qualified_work_the_same_way_in_a_keep_list(tmp_path):
    bare = _box(_ws(tmp_path), keep_names=("inlet",))
    assert _names(bare) == {"fluid/inlet", "solid/inlet"}
    qual = _box(_ws(tmp_path), keep_names=("fluid/inlet",))
    assert _names(qual) == {"fluid/inlet"}


def test_on_a_single_region_mesh_the_two_spellings_are_one(tmp_path):
    """`delivered_regions` calls the only region "", so a window names its patches bare and a
    reader's list is bare too. Neither form may fall through."""
    ws = tmp_path / "ws"
    pm = ws / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    write_index(pm)
    assert _names(_box(ws)) == {"inlet", "outlet", "wall"}
    assert _names(_box(ws, keep_names=("wall",))) == {"wall"}
    assert _names(_box(ws, skip_names=("wall",))) == {"inlet", "outlet"}


# ------------------------------------------------------------------ keep and skip together
def test_skip_narrows_a_keep_list_further(tmp_path):
    out = _box(_ws(tmp_path), keep_names=("fluid/wall", "fluid/inlet"),
               skip_names=("fluid/inlet",))
    assert _names(out) == {"fluid/wall"}


# ------------------------------------------------------------------ the numbers that travel
def test_the_count_in_the_box_is_the_count_of_what_was_kept(tmp_path):
    """The client learns this view's density from `faces_in_box` and predicts the next question
    with it. If the count included patches the answer will not carry, every prediction is wrong
    by whatever the reader has turned off - which on this plate is most of the mesh."""
    ws = _ws(tmp_path)
    whole = _box(ws)
    part = _box(ws, keep_names=("fluid/wall",))
    assert part["faces_in_box"] == part["faces_returned"] < whole["faces_in_box"]
    assert part["faces_in_box"] == sum(p["face_count"] for p in part["patches"])


def test_the_ceiling_travels_with_the_answer(tmp_path):
    """The client cannot predict against a cap it was never told. Both answers carry it, and the
    refusal especially: a refusal is the case the next prediction has to get right."""
    ws = _ws(tmp_path)
    assert _box(ws)["max_faces"] == 1_500_000
    tiny = detail_box(ws, lo=ALL[0], hi=ALL[1], max_faces=1)
    assert tiny["fits"] is False
    assert tiny["max_faces"] == 1
    assert tiny["faces_in_box"] > 1 and tiny["patches"] == []


def test_a_kept_patch_comes_back_exactly_as_it_would_unfiltered(tmp_path):
    """Narrowing decides WHICH patches are read, never what a patch is. Same points, same
    polygons, same count - or the handover from the drawn surface would move geometry."""
    ws = _ws(tmp_path)
    whole = {p["name"]: p for p in _box(ws)["patches"]}
    one = {p["name"]: p for p in _box(ws, keep_names=("fluid/wall",))["patches"]}
    assert set(one) == {"fluid/wall"}
    a, b = whole["fluid/wall"], one["fluid/wall"]
    assert a["face_count"] == b["face_count"]
    assert bytes(a["points"]) == bytes(b["points"])
    assert bytes(a["polys"]) == bytes(b["polys"])


# ------------------------------------------------------------------ the refusal still refuses
@pytest.mark.parametrize("keep", [(), ("fluid/wall",)])
def test_a_window_that_does_not_fit_still_carries_nothing(tmp_path, keep):
    """Narrowing must not turn the refusal into a sample. A window carries the whole view or
    none of it whatever list it was given."""
    out = detail_box(_ws(tmp_path), lo=ALL[0], hi=ALL[1], keep_names=keep, max_faces=1)
    assert out["fits"] is False
    assert out["patches"] == []
    assert out["faces_returned"] == 0
