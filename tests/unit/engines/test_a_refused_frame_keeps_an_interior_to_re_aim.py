# Responsibility: the interior a review still needs is reduced, never deleted outright.
# Boundaries: `engines/cfmesh_conjugate/native` only; no rendering and no engine run.
# Collaborates with: the real pyvista through `sandbox/backend`, because the clip is the subject.
"""Job 40e4e578 delivered 279,281,786 cells and the reviewer said
`region 'across_the_flow' was not produced`.

WHAT ACTUALLY HAPPENED, which no unit test could have caught before the run:
`_render_cuts` reached `if out.get("usable")` with 2 of 6 frames usable, which it had never
done before (the harmonic fix is what made any frame usable), `_drop_cut_volume` found the
fluid interior at 2,312,113,596 B over a 1,200,000,000 B ceiling, and unlinked it. The two
regions the reviewer asked about were the two whose own frames had been REFUSED, so the
interior was their only remaining evidence and it had just been deleted on the strength of
two frames belonging to other regions.

The ceiling is right - 2.15 GiB x VOLUME_READ_RSS_RATIO is 20.4 GiB in a 16 GiB worker - so
these tests are not about keeping the big file. They are about the third option: a slab at the
unanswered plane, which is the same evidence at a size the home side can open.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import native as N

CELL = 1e-4
DIM = 41
SPAN = (DIM - 1) * CELL


@pytest.fixture
def grid():
    """A real unstructured grid, because the clip is what is under test."""
    from meshpipeline.sandbox.backend import pv

    return pv.ImageData(dimensions=(DIM, DIM, DIM),
                        spacing=(CELL, CELL, CELL)).cast_to_unstructured_grid()


#: THE WALL A PICTURE HAS TO SHOW, in pixels, over this fixture's 4 px drawn cell.
#: `picture_structure.WALL_CELLS_MIN` is 2.0 cells, and 30 px is what job 40e4e578's
#: through_the_plate zoom actually measured - 7.5 cells of metal between its passages.
WALL_PX = 30.0


def _cut(name: str, *, shows: bool, axis: int = 0, wall_px: float | None = None) -> dict:
    """One cut across `axis`, with a whole frame and a zoom that did or did not ANSWER.

    "ANSWERED" IS NOT `shows_structure`, AND THIS FIXTURE USED TO SAY IT WAS. What discharges a
    region is `review_session.ResolvedCut.counts_cells`, and that ends at
    `wall_in_cells(bare_run_min_px, drawn_cell_px) >= WALL_CELLS_MIN` - so a frame can show a
    perfectly regular structure and contain no metal at all, which is precisely what job
    40e4e578's across_the_flow zoom did: shows_structure True, bare_run_min_px 0.0. Pass
    `wall_px=0.0` with `shows=True` for that case.
    """
    mid = SPAN / 2.0
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    ah, av = (k for k in range(3) if k != axis)
    return {
        "name": name,
        "normal": normal,
        "origin": [mid, mid, mid],
        "plane_axes": [ah, av],
        "frames": [
            {"scope": "whole", "window_m": [SPAN, SPAN], "centre_m": [mid, mid]},
            {"scope": "zoom", "window_m": [CELL * 8, CELL * 8], "centre_m": [mid, mid],
             "feature_m": CELL, "feature_pixels": 4,
             "content": {"shows_structure": shows,
                         "bare_run_min_px": (WALL_PX if wall_px is None and shows
                                             else float(wall_px or 0.0))}},
        ],
    }


def _volume(grid, tmp_path):
    p = tmp_path / "internal.vtu"
    grid.save(str(p))
    return p


# --------------------------------------------------------------------------- the defect itself

def test_a_region_whose_frame_was_refused_still_has_an_interior(grid, tmp_path):
    """THE REGRESSION. Before the fix this file was unlinked and the region had nothing."""
    src = _volume(grid, tmp_path)
    whole = src.stat().st_size
    out: dict = {"rendered": 2, "usable": 1}
    N._drop_cut_volume(src, out, ceiling=whole // 2, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=False)])
    assert src.is_file(), "the interior of a region with no usable frame must not be deleted"
    assert src.stat().st_size <= whole // 2, "what is kept has to be inside the ceiling"
    assert "volume_reduced" in out


def test_the_kept_slab_is_whole_cells_and_not_clip_fragments(grid, tmp_path):
    """The region's question is how many cells lie across the passage. Fragments answer it wrongly."""
    src = _volume(grid, tmp_path)
    out: dict = {"usable": 1}
    N._drop_cut_volume(src, out, ceiling=src.stat().st_size // 2, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=False)])
    from meshpipeline.sandbox.backend import pv

    kept = pv.read(str(src))
    assert 0 < kept.n_cells < grid.n_cells
    # a crinkled slab holds whole cells of the original, so its count divides the original's
    # layer count exactly; a tetrahedralising clip comes back with MORE cells than it kept
    assert kept.n_cells <= grid.n_cells // 4
    assert out["volume_reduced_cells"] == kept.n_cells


def test_a_slab_says_it_is_a_slab(grid, tmp_path):
    """A verdict may not rest on a completeness the file does not have."""
    src = _volume(grid, tmp_path)
    out: dict = {"usable": 1}
    N._drop_cut_volume(src, out, ceiling=src.stat().st_size // 2, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=False)])
    said = out["volume_reduced"]
    assert "slab" in said and "not the whole interior" in said
    assert "across_the_flow" in said, "the record names the plane(s) it holds"


# ----------------------------------------------------------- what must NOT change behaviour

def test_a_region_whose_frame_answered_still_carries_a_slab(grid, tmp_path):
    """IT USED TO GIVE THE INTERIOR UP ENTIRELY HERE, and that was the remaining hole.

    The reasoning for deleting it was sound as far as it went: the picture IS the evidence when a
    frame answered, so the interior is dead weight. What it missed is that the picture only has to
    convince the HOME side, and the home side is not the one that decided the region was
    showable. `spec._cuts_that_can_be_shown` names a region unless the PRODUCER vetoed it and says
    so outright - "a cut it did NOT refuse may still be refused by the home side's own measurement
    of its own copy of the PNG; that stays where it is, and this does not try to predict it" - and
    the home side then applies five further floors. `review_requires_the_interior` is True for this
    engine, so such an obligation is never waived, and `expand_region_targets` has nothing to fall
    back on once the file is gone: the review ends [EXPECTED_TARGET_MISSING] before the model is
    asked anything, which is what happened to four delivered meshes.

    So a dropped interior now always leaves a slab at every declared plane. The whole interior
    still does not travel - that is the point of the ceiling - but something at every plane does.
    It is bounded by `cap` like everything else here, and `_reduce_to_review_slabs` still refuses
    and lets the caller unlink when even the slabs will not fit; `test_a_slab_that_still_cannot_
    be_opened_is_refused_and_said` pins that.
    """
    src = _volume(grid, tmp_path)
    before = src.stat().st_size
    out: dict = {"rendered": 2, "usable": 1}
    N._drop_cut_volume(src, out, ceiling=before // 2, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=True)])
    assert src.is_file(), "the answered plane was left with nothing to cut"
    assert src.stat().st_size < before, "the whole interior must still not travel"
    assert "volume_reduced" in out and "volume_dropped" not in out
    assert "across_the_flow" in out["volume_reduced"]


def test_an_interior_inside_the_ceiling_is_untouched(grid, tmp_path):
    """Nothing is reduced when the whole thing can travel."""
    src = _volume(grid, tmp_path)
    before = src.stat().st_size
    out: dict = {"usable": 1}
    N._drop_cut_volume(src, out, ceiling=before * 4, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=False)])
    assert src.stat().st_size == before
    assert "volume_kept" in out and "volume_reduced" not in out


def test_a_slab_that_still_cannot_be_opened_is_refused_and_said(grid, tmp_path):
    """The ceiling is the point. A slab over it buys nothing and the old behaviour stands."""
    src = _volume(grid, tmp_path)
    out: dict = {"usable": 1}
    N._drop_cut_volume(src, out, ceiling=2048, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=False)])
    assert not src.exists()
    assert "volume_slab_refused" in out
    assert "over the" in out["volume_slab_refused"]


def test_no_mesh_in_hand_behaves_exactly_as_before(grid, tmp_path):
    """A caller that cannot supply the mesh must not be made worse off."""
    src = _volume(grid, tmp_path)
    out: dict = {"usable": 1}
    N._drop_cut_volume(src, out, ceiling=src.stat().st_size // 2, mesh=None,
                       cuts=[_cut("across_the_flow", shows=False)])
    assert not src.exists()
    assert "volume_dropped" in out


# --------------------------------------------------------------------------- which planes

def test_only_the_unanswered_planes_are_kept(grid, tmp_path):
    """A cut that answered needs no cells; carrying them would spend the ceiling on nothing.

    This is `_refused_planes` alone. Note that `_reduce_to_review_slabs` now slabs every DECLARED
    plane after these, for the reason in `_all_declared_planes` - the obligation is built by a
    weaker test than the one that discharges it. This test is about which planes are provably
    unanswered, which is still the set that gets a slab first.
    """
    answered = _cut("along_the_flow", shows=True, axis=1)
    refused = _cut("across_the_flow", shows=False, axis=0)
    planes = N._refused_planes([answered, refused])
    assert [c["name"] for c, _f, _cell in planes] == ["across_the_flow"]


def test_a_frame_that_shows_structure_with_no_metal_is_not_answered(grid, tmp_path):
    """THE CASE THIS FIXTURE COULD NOT EXPRESS, and the one that cost four deliveries. Job
    40e4e578's across_the_flow zoom: shows_structure True, bare_run_min_px 0.0, and the reader's
    own `counts_cells` False. A picture with no wall in it is not a picture of a wall."""
    no_metal = _cut("across_the_flow", shows=True, wall_px=0.0, axis=0)
    assert [c["name"] for c, _f, _cell in N._refused_planes([no_metal])] == ["across_the_flow"]


def test_every_declared_plane_gets_a_slab_when_the_interior_is_dropped(grid, tmp_path):
    """Even the plane whose picture answered. `spec._cuts_that_can_be_shown` names a region
    unless the PRODUCER vetoed it and says outright that "a cut it did NOT refuse may still be
    refused by the home side's own measurement"; the home side then applies five more floors. So
    a plane this engine thinks is fine can still be an obligation nothing can discharge, and
    `review_requires_the_interior` is True so the obligation is never waived."""
    src = _volume(grid, tmp_path)
    out: dict = {"usable": 2}
    N._drop_cut_volume(src, out, ceiling=src.stat().st_size // 2, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=True, axis=0),
                             _cut("along_the_flow", shows=True, axis=1)])
    assert src.is_file(), "a dropped interior left nothing at either declared plane"
    said = out["volume_reduced"]
    assert "across_the_flow" in said and "along_the_flow" in said


def test_both_unanswered_planes_travel(grid, tmp_path):
    """Two regions with no frame need two slabs, and one file carries them."""
    src = _volume(grid, tmp_path)
    out: dict = {"usable": 1}
    N._drop_cut_volume(src, out, ceiling=src.stat().st_size // 2, mesh=grid,
                       cuts=[_cut("across_the_flow", shows=False, axis=0),
                             _cut("along_the_flow", shows=False, axis=1)])
    assert src.is_file()
    said = out["volume_reduced"]
    assert "across_the_flow" in said and "along_the_flow" in said


def test_a_cut_with_no_zoom_frame_asks_for_nothing(grid):
    """Only a zoom frame carries the question a slab exists to answer."""
    cut = _cut("across_the_flow", shows=False)
    cut["frames"] = [f for f in cut["frames"] if f["scope"] != "zoom"]
    assert N._refused_planes([cut]) == []


# --------------------------------------------------------------------------- the box itself

def test_the_slab_is_thin_across_the_plane_and_whole_within_it():
    cut = _cut("across_the_flow", shows=False, axis=0)
    whole = cut["frames"][0]
    box = N._slab_box(cut, whole, thickness_m=CELL * N.REVIEW_SLAB_CELLS)
    assert box is not None
    mid = SPAN / 2.0
    half = CELL * N.REVIEW_SLAB_CELLS / 2.0
    assert box[0] == pytest.approx(mid - half) and box[1] == pytest.approx(mid + half)
    # and the two in-plane axes hold the whole frame
    assert box[3] - box[2] == pytest.approx(SPAN)
    assert box[5] - box[4] == pytest.approx(SPAN)


def test_a_zero_thickness_slab_is_not_attempted():
    """It would clip to nothing, and 'nothing' must not read as a slab that fit."""
    cut = _cut("across_the_flow", shows=False)
    assert N._slab_box(cut, cut["frames"][0], thickness_m=0.0) is None


def test_a_cut_missing_its_geometry_is_skipped_not_guessed():
    cut = _cut("across_the_flow", shows=False)
    cut["normal"] = [1.0, 0.0]
    assert N._slab_box(cut, cut["frames"][0], thickness_m=CELL) is None


def test_a_zoom_with_no_planned_cell_still_gets_a_thickness():
    """`feature_m` is absent on a cut the planner could not size; a slab is still better than none."""
    cut = _cut("across_the_flow", shows=False)
    zoom = next(f for f in cut["frames"] if f["scope"] == "zoom")
    zoom.pop("feature_m")
    planes = N._refused_planes([cut])
    assert len(planes) == 1
    assert planes[0][2] == pytest.approx(SPAN / N.SLAB_FALLBACK_WINDOW_FRACTION)


# --------------------------------------------------------------------------- the wiring

def test_the_drop_is_reached_with_the_mesh_and_the_cuts():
    """THE LESSON FROM THE LAST TIME. Every test above passes with the call site unchanged, so
    the call site is asserted on directly: a fix nothing calls is not a fix."""
    import inspect

    src = inspect.getsource(N._render_cuts)
    assert "_drop_cut_volume(src, out, ceiling=ceiling, mesh=mesh, cuts=cuts)" in src
    drop = inspect.getsource(N._drop_cut_volume)
    assert "_reduce_to_review_slabs(src, out, cap=cap, mesh=mesh, cuts=cuts)" in drop
    # and the reducer runs BEFORE the unlink, or it can never save anything
    assert drop.index("_reduce_to_review_slabs") < drop.index("src.unlink()")


def test_the_mesh_is_bound_even_if_the_read_raised():
    """The drop runs after the except; an unbound `mesh` there would be a NameError on a path
    that is already handling a failure."""
    import inspect

    src = inspect.getsource(N._render_cuts)
    assert src.index("mesh = None") < src.index("mesh = pv.read(")
