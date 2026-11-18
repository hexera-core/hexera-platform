"""The mesher draws its own cuts, where the mesh is, and says what is IN each one.

WHY THE PICTURE TRAVELS AND THE INTERIOR DOES NOT. `mesh_paths.volume` is the fluid interior and
the review was meant to cut it at home. On job 0ff6f9e5's plate that file is 10,366,611,525 bytes
and the worker that would open it is capped at 16 GiB, with `sandbox/backend._load_volume` reading
it whole - so fixing the archive ceiling alone would turn an honest "evidence missing" into an OOM
kill of the Celery worker. Measured 2026-11-17 over six reads of a .vtu, warm and cold, peak RSS
is 9.48x to 9.50x the file's bytes.

So the cut is made on the mesh VM, where the 512 GiB is, and the pictures go home instead.

AND A PICTURE IS EVIDENCE ONLY IF ITS OWN PIXELS SAY SO. The window is `plan_cell/CELL_PIXELS` and
the reading side divides by the same plan cell, so a zoom frame is "countable" by arithmetic
whatever is drawn in it. Measured on that plate: of the three zoom frames the last round
produced, one held the channel array and the other two held three plenum rectangles and one
circular port bore, and all three were introduced to the reviewer as frames cells can be counted
in. Every frame here is measured through `slices.ink_structure` and a zoom frame that does not
hold an array is published as a picture and NOT as an inspection.

MEASURED END TO END on the real plate's own fluid/solid interface, 2026-11-17, through
`_render_cuts` itself: six frames in 36.6 s of which 6.4 s was the read; the y-normal cut found
the channel array at y = 22.713 mm with 79 repeats at 100.631 um and a periodicity of 0.987, and
the other two were measured to hold a feature at 805.05 um and scattered traces at 0.18, and were
withheld.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.engines.cfmesh_conjugate.slices import CELL_PIXELS, review_slices

pv = pytest.importorskip("pyvista")

CELL_M = 2.51578e-05
#: four cells of coolant, four of metal: 201.262 um, which is this plate's own pitch
STRIPE_CELLS = 4


def _grid(nx, ny, nz, *, origin=(0.0, 0.0, 0.0), spacing=CELL_M):
    return pv.ImageData(dimensions=(nx + 1, ny + 1, nz + 1), spacing=(spacing,) * 3,
                        origin=origin).cast_to_unstructured_grid()


def _save(grid, tmp_path: Path) -> Path:
    p = tmp_path / "VTK" / "fluid" / "case_0" / "internal.vtu"
    p.parent.mkdir(parents=True, exist_ok=True)
    grid.save(str(p))
    return p


def _block(tmp_path: Path):
    """A solid block of cells with no passage in it: the plenum a volume slice draws."""
    nx, ny, nz = 330, 250, 3
    g = _grid(nx, ny, nz)
    return _save(g, tmp_path), [0.0, 0.0, 0.0], [nx * CELL_M, ny * CELL_M, nz * CELL_M]


def _striped(tmp_path: Path, axis: int = 0):
    """Coolant passages: stripes of cells four wide, four apart, repeating along `axis`."""
    nx, ny, nz = 330, 250, 3
    g = _grid(nx, ny, nz)
    k = np.floor(np.asarray(g.cell_centers().points)[:, axis] / CELL_M).astype(int)
    keep = (k // STRIPE_CELLS) % 2 == 0
    sub = g.extract_cells(np.flatnonzero(keep))
    return _save(sub, tmp_path), [0.0, 0.0, 0.0], [nx * CELL_M, ny * CELL_M, nz * CELL_M]


def _cuts(lo, hi):
    return review_slices(lo, hi, cell_m=CELL_M)


def _replan(lo, hi):
    """What `_review_aids` hands the renderer: the same cut set, re-placed from a MEASURED axis."""
    return lambda axis=None: review_slices(lo, hi, cell_m=CELL_M, repeat_axis=axis)


def _frame(cut, scope):
    return next(f for f in cut["frames"] if f["scope"] == scope)


def _by_name(cuts):
    return {c["name"]: c for c in cuts}


# --------------------------------------------------------------------------- nothing to cut

def test_a_cut_with_no_volume_draws_nothing_and_says_so(tmp_path):
    """With no interior there is nothing to cut, and a cut promised from nothing is the defect
    the whole aid exists to stop."""
    out = N._render_cuts(tmp_path, _cuts((0, 0, 0), (0.002, 0.001, 0.0015)), volume=None)
    assert out["rendered"] == 0
    assert "nothing to cut" in out["why"]


def test_a_volume_that_was_pruned_is_not_cut(tmp_path):
    gone = tmp_path / "VTK" / "fluid" / "case_0" / "internal.vtu"
    gone.parent.mkdir(parents=True)
    out = N._render_cuts(tmp_path, _cuts((0, 0, 0), (0.002, 0.001, 0.0015)), volume=str(gone))
    assert out["rendered"] == 0 and "nothing to cut" in out["why"]


def test_cuts_too_expensive_for_what_is_left_are_not_started(tmp_path):
    """A pass killed with the machine leaves no picture and costs the rest of the run."""
    vol, lo, hi = _block(tmp_path)
    out = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=283_410_955,
                         budget_left_s=0.001)
    assert out["rendered"] == 0
    assert "not started" in out["why"] and "budget" in out["why"]
    assert out["priced_seconds"] > 0
    assert vol.is_file(), "a pass that never ran may not cost the artifact"


# --------------------------------------------------------------- what the picture is measured to hold

def test_a_dense_block_of_cells_is_refused_with_its_picture_published(tmp_path):
    """THE CASE NO SCALE CAN SEE. A fluid-only slice through a plenum is a dense, healthy-looking
    grid of cells at exactly the right magnification with no channel in it. Before this, every one
    of its zoom frames was published as a frame cells can be counted in.

    THE REFUSAL IS A CONTENT BLOCK, NOT A MISSING FILENAME. The frame keeps `image`, so the
    reviewer is shown WHAT was drawn instead of a passage, with `shows_structure: false` and the
    figure that refused it beside it. Withholding the path put the frame under `image_context`,
    which `sandbox/review_session._resolve_cut` never looks at - it takes the path from
    `_PATH_KEYS = ("image", "path")` and returns None before it reads `content` - so the reviewer
    was told the region had no zoom frame at all, which is exactly the "an empty frame and a frame
    of fine metal look identical to a reader" this aid exists to stop. The home side refuses it
    either way: on its own copy of the PNG (`ResolvedCut.shows_cells`, which measured 1 and 0
    cells on the best row of job 0ff6f9e5's two no-passage frames against a floor of 4) and on
    this very claim ("a claim from the producer may veto and may not grant").
    """
    vol, lo, hi = _block(tmp_path)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000)
    assert out["why"] == "", out["why"]
    for cut in cuts:
        zoom = _frame(cut, "zoom")
        content = zoom.get("content") or {}
        assert not content.get("shows_structure"), (cut["name"], content)
        assert zoom.get("image"), (
            cut["name"], "the pixels that were drawn and measured do not reach the reviewer, so "
                         "it cannot learn that a frame WAS drawn and what was in it")
        assert (tmp_path / zoom["image"]).is_file(), "the picture is kept, not destroyed"
        assert "refuses it as evidence" in zoom.get("frame_why", ""), zoom.get("frame_why")
        assert content.get("why"), "the figure that refused it travels in the content block"
        assert "image_context" not in zoom, (
            "image_context has no reader anywhere in src/ and manifest._published_cuts does not "
            "check it for existence, so a frame published under it is a dangling name")
    # NOTHING USABLE WAS DRAWN, so the interior is not thrown away on the strength of six frames
    # that cannot answer the question any of the cuts exists to ask.
    assert out["usable"] == 0, out
    assert out["rendered"] > 0, "the frames DID draw; they are just not evidence"
    assert vol.is_file(), "no frame holds a passage, so the interior is the only artifact left"
    assert "none of them holds the periodic structure" in out.get("volume_kept", "")
    # the one whose plane the block fills: the frame is DENSE and crisp and still not evidence
    full = _frame(_by_name(cuts)["through_the_plate"], "zoom")["content"]
    assert full["ink_fraction"] > 0.1, full
    assert full["edge_contrast"] >= 20, full


def test_passages_in_the_picture_are_what_makes_a_frame_an_inspection(tmp_path):
    """The same renderer, the same window, the same scale - and coolant passages in the frame."""
    vol, lo, hi = _striped(tmp_path, axis=0)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000)
    assert out["why"] == "", out["why"]
    # OFFERED IS NOW THE CONTENT BLOCK, not the presence of a filename: every drawn frame carries
    # `image`, and `shows_structure` is what says whether it is an inspection of its region.
    offered = [c["name"] for c in cuts
               if (_frame(c, "zoom").get("content") or {}).get("shows_structure")]
    assert offered, {c["name"]: _frame(c, "zoom").get("frame_why") for c in cuts}
    assert out.get("usable", len(offered)) == len(offered), out
    for name in offered:
        zoom = _frame(_by_name(cuts)[name], "zoom")
        content = zoom["content"]
        assert zoom.get("image"), name
        assert "frame_why" not in zoom, "a frame that holds the array carries no refusal"
        assert content["period_m"] == pytest.approx(2 * STRIPE_CELLS * CELL_M, rel=0.02), content
        assert content["measured"] == "the drawn PNG's own pixels"


def test_the_cut_that_runs_along_the_passages_is_marked_not_counted(tmp_path):
    """A plane normal to the axis the passages repeat along runs ALONG them, and no window and no
    origin can make it show the array. It is said, with the number, rather than published as a
    frame cells can be counted in - AND the picture of what it did draw goes with the saying."""
    vol, lo, hi = _striped(tmp_path, axis=0)
    cuts = _cuts(lo, hi)
    N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000, replan=_replan(lo, hi))
    along = _by_name(cuts).get("along_the_flow")
    assert along is not None and along["normal_axis"] == 0, along
    zoom = _frame(along, "zoom")
    assert not zoom["content"]["shows_structure"], (
        "a cut along the passages discharged its region")
    assert zoom["content"]["measured"]
    assert zoom.get("image") and (tmp_path / zoom["image"]).is_file(), (
        "the reviewer is told this region has no zoom frame, where the truth is that one was "
        "drawn and measured and holds no array")


def test_the_repeat_axis_is_measured_off_a_drawn_frame_not_guessed(tmp_path):
    """The plan cannot say which in-plane axis the passages repeat along: on job 0ff6f9e5 the fine
    zone is 60.2 x 12.0 x 52.1 mm, the passages repeat along x and run along z, and neither the
    spans nor the stretch factor nor the feature thickness picks x out. The longest span - the old
    answer - is x, which put `across_the_flow` ALONG the passages."""
    vol, lo, hi = _striped(tmp_path, axis=0)
    cuts = _cuts(lo, hi)
    out = N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000, replan=_replan(lo, hi))
    assert out.get("repeat_world_axis") == 0, out.get("aim")
    assert out["repeat_period_m"] == pytest.approx(2 * STRIPE_CELLS * CELL_M, rel=0.02)
    assert "repeat along x" in out["aim"]
    # across the flow is now normal to an axis that CROSSES the passages
    assert _by_name(cuts)["across_the_flow"]["normal_axis"] != 0
    assert "measured from a drawn cut" in _by_name(cuts)["across_the_flow"]["axis_source"]


def test_a_frame_that_shows_nothing_still_publishes_what_was_measured(tmp_path):
    """A reader handed "no" and no number cannot check it. Every frame carries its own figures."""
    vol, lo, hi = _block(tmp_path)
    cuts = _cuts(lo, hi)
    N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000)
    for cut in cuts:
        for frame in cut["frames"]:
            content = frame.get("content") or {}
            for key in ("ink_fraction", "edge_contrast", "repeats", "strength", "measured"):
                assert key in content, (cut["name"], frame["scope"], key)


# ------------------------------------------------------------------------------- where it is

def test_the_plane_is_moved_inside_the_search_band_until_it_finds_the_feature(tmp_path):
    """MEASURED on job 0ff6f9e5, sweeping the y-normal cut across its fine zone: the channels are
    in the bottom tenth of it (79 repeats at y = 22.1 and 22.7 mm, nothing at 23.9, scattered
    traces at 25.1 and 27.5). The middle of the zone - the obvious place - is empty."""
    nx, ny, nz = 330, 250, 20
    g = _grid(nx, ny, nz)
    c = np.asarray(g.cell_centers().points)
    # the passages are in the bottom fifth of the band, as this plate's are: its fine zone is
    # 21.51 to 33.55 mm and every channel is between 21.6 and 24.5 mm
    band = c[:, 2] < 4 * CELL_M
    k = np.floor(c[:, 0] / CELL_M).astype(int)
    keep = band & (((k // STRIPE_CELLS) % 2) == 0)
    vol = _save(g.extract_cells(np.flatnonzero(keep)), tmp_path)
    lo, hi = [0.0, 0.0, 0.0], [nx * CELL_M, ny * CELL_M, nz * CELL_M]
    cuts = _cuts(lo, hi)
    through = _by_name(cuts)["through_the_plate"]
    planned = list(through["origin"])
    N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000)
    moved = _by_name(cuts)["through_the_plate"]["origin"]
    assert moved[through["normal_axis"]] < planned[through["normal_axis"]], (
        "the plane stayed in the middle of a band whose features are at its floor")
    assert _frame(_by_name(cuts)["through_the_plate"], "zoom").get("image")
    assert "drawn and measured" in _by_name(cuts)["through_the_plate"]["origin_source"]


def test_the_window_and_the_scale_travel_with_every_picture(tmp_path):
    """A claim about a cut that cannot be checked against the scale AND THE PLACE of the picture
    it was made from is a guess with a picture next to it."""
    vol, lo, hi = _striped(tmp_path)
    cuts = _cuts(lo, hi)
    N._render_cuts(tmp_path, cuts, volume=str(vol), cells=25_000)
    for cut in cuts:
        assert cut["normal"] and cut["origin"]
        for frame in cut["frames"]:
            assert frame["window_m"][0] > 0 and frame["window_m"][1] > 0
            assert frame["um_per_pixel"] > 0
            assert frame["centre_m"] and len(frame["centre_m"]) == 2
            assert frame["centre_source"], (cut["name"], frame["scope"])
    zoom = [f for c in cuts for f in c["frames"] if f["scope"] == "zoom"]
    assert zoom and all(f["feature_pixels"] == CELL_PIXELS for f in zoom)
    assert all(f["um_per_pixel"] == pytest.approx(6.2895, abs=1e-4) for f in zoom)


def test_a_cell_edge_survives_a_camera_in_phase_with_the_lattice(tmp_path):
    """THE REFUTATION, and it is kept as a test because the claim is plausible and was acted on.

    It was reported that 3 of 16 sub-pixel camera phases render a 4-cell channel as ONE uniform
    bar with no interior boundary, and that a 2-pixel edge was the cure. Swept again over the
    WHOLE FRAME rather than one scan row, through this renderer, at 4 and 8 pixels per cell: 0 of
    16 phases do that, and the weaker of the frame's two directions never falls below 53 grey
    levels. What IS true is narrower - at the one aligned phase, HALF the scan rows carry no
    variation, the middle row among them, because a cell edge blends across the whole row.

    A reader that samples one row at that phase sees a flat bar. `ink_structure` sums a whole
    profile, so it never does.
    """
    from PIL import Image

    from meshpipeline.engines.cfmesh_conjugate.slices import ink_structure, worst_contrast

    mpp = CELL_M / CELL_PIXELS
    nx, ny = 330, 250
    sl = _grid(nx, ny, 2).slice(normal=(0, 0, 1), origin=(0, 0, CELL_M * 0.5))
    aligned = [nx * CELL_M / 2.0, ny * CELL_M / 2.0]
    worst, flat_rows, mid_flat = {}, {}, {}
    for tag, centre in (("in phase", aligned),
                        ("half a pixel off", [aligned[0] + mpp / 2, aligned[1]])):
        path = tmp_path / f"{tag.replace(' ', '_')}.png"
        N._render_frame(pv, sl, path, axis=2, ah=0, av=1, origin=[0, 0, CELL_M * 0.5],
                        centre=centre, mpp=mpp, width_px=1280, height_px=960)
        with Image.open(path) as im:
            a = np.asarray(im.convert("L")).astype(int)
        worst[tag] = worst_contrast(ink_structure(a))
        ink = a < 250
        swing = np.where(ink, a, -1).max(axis=1) - np.where(ink, a, 256).min(axis=1)
        flat_rows[tag] = int((swing == 0).sum())
        mid_flat[tag] = bool(a[480].min() == a[480].max())

    assert worst["in phase"] >= 50, worst
    assert worst["half a pixel off"] >= 50, worst
    # ...and this is the part of the report that holds, and the reason it looked total
    assert flat_rows["in phase"] == pytest.approx(a.shape[0] // 2, rel=0.05), flat_rows
    assert flat_rows["half a pixel off"] < flat_rows["in phase"], flat_rows
    assert mid_flat["in phase"], "the row the original claim was measured on"
    assert not mid_flat["half a pixel off"], mid_flat


def test_the_bbox_is_the_volume_s_own_and_is_recorded_as_such(tmp_path):
    """`_boundary_indexes` runs AFTER this, so the boundary index does not exist yet and anything
    reading it here would get an empty box on every run and a correct one on none."""
    vol, lo, hi = _block(tmp_path)
    out = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=25_000)
    assert out["bbox_source"] == "the exported volume's own bounds"
    assert len(out["bbox"]) == 6


# --------------------------------------------------------------------- what goes home with it

def test_a_small_interior_still_travels(tmp_path):
    """A volume the home side can open is a RE-AIMABLE view of the mesh, which is better evidence
    than any fixed frame: the reviewer picks where to look instead of being handed six places."""
    vol, lo, hi = _striped(tmp_path)
    out = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=25_000)
    assert out["rendered"] > 0
    assert vol.is_file()
    assert "travels home" in out["volume_kept"]


def test_an_interior_too_big_to_read_is_dropped_once_the_pictures_exist(tmp_path):
    """It is dead weight the moment the cuts are drawn: 10,366,611,525 bytes on job 0ff6f9e5,
    against 43,228 bytes of PNG, and reading it at home would take about 92 GiB of RSS in a
    worker capped at 16."""
    vol, lo, hi = _striped(tmp_path)
    out = N._render_cuts(tmp_path, _cuts(lo, hi), volume=str(vol), cells=25_000,
                         ceiling=1)
    assert out["rendered"] > 0
    assert not vol.is_file(), "the interior went home for nobody to open"
    assert out["volume_dropped_bytes"] > 0
    assert "drawn, and then dropped" in out["volume_dropped"]


def test_an_interior_is_never_dropped_when_no_picture_was_drawn(tmp_path):
    """The order is the whole safety of it. A run whose cuts failed keeps the interior exactly as
    it found it, because then it is still the only artifact a review could reach a verdict from."""
    vol, lo, hi = _striped(tmp_path)
    out = N._render_cuts(tmp_path, [], volume=str(vol), cells=25_000, ceiling=1)
    assert out["rendered"] == 0
    assert vol.is_file(), "the volume was consumed by a pass that drew nothing"
