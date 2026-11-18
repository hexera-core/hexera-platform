# Responsibility: Verify a prepared frame discharges a region only when the PICTURE was measured
# to contain what the cut exists to show, not when the arithmetic about the plan says it should.
# Boundaries: the home side only - what is measured off the PNG, what the producer's own
# measurement is allowed to do, and what that lets a frame do.
#
# THE DEFECT THIS IS WRITTEN FROM. Round 2 gated a frame on `cell_px >= COUNTABLE_CELL_PX`, where
# cell_px is `quality.near_wall_cell_m` divided by the window the producer sized as
# `plan_cell / COUNTABLE_CELL_PX * width_px`. Those are the same number by construction:
# near_wall_cell_m is background_cell_m / 2**levels, which is what the planner pinned the fine
# cell to. Measured on job 0ff6f9e5, 0.0004025248 / 2**4 == 2.51578e-05 == fine_cell_pinned_m
# EXACTLY, so every zoom frame ever drawn came out at exactly 4.00 px and was declared countable.
#
# What the three frames actually contained, measured through this same code at 1280x960:
#   along_the_flow_zoom     3.2585% inked, 0 cells resolved   channel walls, no cells
#   across_the_flow_zoom    0.5921% inked, 1 cell resolved    three plenum rectangles
#   through_the_plate_zoom  0.4142% inked, 0 cells resolved   one circular port bore
# All three were introduced to the reviewer with "cells are countable in this frame".
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED, except the one test that exists to pin the two
# floors to the production constants. That is on purpose: a test that cannot be COLLECTED against
# the old tree proves only that a constant is new, and the claim being made here is about
# behaviour.
from __future__ import annotations

from pathlib import Path

from _passage_pictures import write_cut_picture  # noqa: E402  (this dir is on sys.path)

from meshpipeline.sandbox.render_adapter import (
    VOLUME_READ_CEILING_BYTES,
    expand_region_targets,
)
from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
from meshpipeline.sandbox.review_session import (
    COUNTABLE_CELL_PX,
    FRAME_WHOLE,
    FRAME_ZOOM,
    ArtifactResolver,
)

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
BACKGROUND_M = 0.0004025248          # conjugate_plan.background_cell_m
LEVELS = 4                           # conjugate_plan.fine_surfaces[0].levels
CELL_M = 2.51578e-05                 # quality.near_wall_cell_m, and fine_cell_pinned_m
FRAME_PX = (1280, 960)
#: The plan block's extents, so the whole-plane frames below are the ones really drawn.
SPAN = (0.17750004, 0.041836, 0.101000001)

#: Pinned to the production floors by `test_the_floors_are_the_production_ones`. Restated here
#: rather than imported so that every other test in this file runs against the OLD tree too, and
#: fails there on its assertion rather than on its import.
CELLS_ON_A_ROW = 4
#: NARROWED FROM 2.0, and the narrowing is what let two period bands be deleted. At 2.0 this band
#: admitted a drawn cell exactly twice the claimed one on its own boundary, which is the signature
#: of a frame drawn at twice the magnification it declares, and
#: `ResolvedCut.period_cells_claimed`'s band was carrying that case - while also refusing true
#: pictures of real passages for their pitch. Measured, SYNTHETIC, VOLUME: every honest frame
#: reads a drawn-to-claimed ratio of exactly 1.00 by construction; the two bad cases read exactly
#: 2.00 and 8.00; nothing measured lands between 1.00 and 2.00.
PITCH_TOLERANCE = 1.5
#: The two floors this round adds, restated for the same reason as the two above.
CELL_ROWS = 12
INK_FLOOR = 1e-3

REGIONS = [
    {"name": "through_the_plate", "kind": "slice", "normal": [0.0, 1.0, 0.0],
     "origin": [0.02717348, 0.041070016, 0.0419095995], "plane_axes": [0, 2],
     "label": "Through the part at mid-thickness",
     "why": "where the coolant reaches and where the metal is"},
]


def _zoom_window() -> tuple[float, float]:
    """The window the producer publishes: sized so one planned cell is COUNTABLE_CELL_PX across."""
    mpp = CELL_M / COUNTABLE_CELL_PX
    return (mpp * FRAME_PX[0], mpp * FRAME_PX[1])


def _whole_window() -> tuple[float, float]:
    """The whole cut plane, fitted to the frame, as `slices.fitted_metres_per_pixel` does it."""
    mpp = max(SPAN[0] / FRAME_PX[0], SPAN[2] / FRAME_PX[1])
    return (mpp * FRAME_PX[0], mpp * FRAME_PX[1])


def _content(ok: bool = True) -> dict:
    """`frames[].content` as cfmesh_conjugate/slices.ink_structure publishes it: the producer's
    own read-back of the drawn PNG, looking for the periodic passage array."""
    if ok:
        return {"shows_structure": True, "repeats": 40, "period_px": 32.0, "spread": 0.04,
                "ink_fraction": 0.251, "scan": "across the frame"}
    return {"shows_structure": False, "ink_fraction": 0.0041,
            "why": "the ink forms 1 repeat(s) across the frame, under the 6 a periodic array "
                   "has to show"}


def _frame(ws: Path, scope: str, *, window, cells_px: int = 4, draw=True, raw=None,
           content: dict | None = None, period_px: int = 32, patch_px=None) -> dict:
    rel = f"review/cuts/through_the_plate_{scope}.png"
    if draw:
        write_cut_picture(ws / rel, *FRAME_PX, cells_px=cells_px, period_px=period_px,
                          patch_px=patch_px)
    if raw is not None:
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_bytes(raw)
    out = {"scope": scope, "image": rel, "window_m": [window[0], window[1]],
           "metres_per_pixel": max(window[0] / FRAME_PX[0], window[1] / FRAME_PX[1]),
           "centre_m": [0.0672742836, 0.0751898789],
           "centre_source": "the densest window-sized patch of this cut, which is where its "
                            "cells are finest"}
    if content is not None:
        out["content"] = content
    return out


def _manifest(ws: Path, frames: list[dict]) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    regions = [dict(r) for r in REGIONS]
    regions[0]["frames"] = frames
    return {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
            "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                        "max_cell_m": 0.0006788786304584814},
            "inspection_regions": regions}


def _cuts(ws: Path, frames: list[dict]):
    return ArtifactResolver(ws, _manifest(ws, frames)).resolve_cuts().get("through_the_plate", ())


 # the identity the old gate could not see

def test_the_published_scale_is_an_identity_so_it_cannot_be_the_only_test(tmp_path):
    """The producer's window divided by the reader's cell is 4.00 px EXACTLY, always.

    Not approximately: `near_wall_cell_m` IS `background_cell_m / 2**levels` and the window IS
    that same number times `width_px / COUNTABLE_CELL_PX`, so the division gives back
    COUNTABLE_CELL_PX bit for bit on every mesh this engine writes. A gate on it cannot fail.
    """
    assert BACKGROUND_M / 2 ** LEVELS == CELL_M

    cut, = _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=0,
                                   content=_content(True))])
    assert cut.cell_px == COUNTABLE_CELL_PX     # exact, which is the whole problem
    assert not cut.counts_cells, (
        "a frame whose only credential is arithmetic about the plan must not be countable")


def test_a_frame_with_no_cells_in_it_does_not_discharge_its_region(tmp_path):
    """A picture of a port bore is 0.41% inked with no cell in it, and it used to cover a region."""
    cuts = {"through_the_plate": _cuts(
        tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=0,
                          content=_content(True))])}
    offered = expand_region_targets(
        resolve_regions(_manifest(tmp_path, [])), required=True, purpose="p",
        volume_available=False, cuts=cuts)
    assert offered == (), (
        "a region whose only frame has no cells in it must not be advertised as inspectable - "
        "the reviewer would spend its rounds on a picture that cannot discharge it")


def test_a_frame_with_cells_in_it_still_does_discharge_its_region(tmp_path):
    """The gate is a gate, not a wall: a frame measured to contain cells covers its region."""
    frames = _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=4,
                                     content=_content(True))])
    cut, = frames
    assert cut.ink is not None and cut.ink.cells_on_best_row >= CELLS_ON_A_ROW
    assert cut.counts_cells
    offered = expand_region_targets(
        resolve_regions(_manifest(tmp_path, [])), required=True, purpose="p",
        volume_available=False, cuts={"through_the_plate": frames})
    assert {t.target_id for t in offered} == {"region:through_the_plate"}


def test_a_picture_of_channels_is_not_a_picture_of_cells(tmp_path):
    """Forty channels at 201 um is not thirty-nine cells at 25 um, however many shapes it has.

    MEASURED: the along_the_flow zoom drawn with a two-pixel edge resolves 39 enclosed interiors
    on its best row - well over the floor - at a spacing of 32.0 px, which at that frame's
    6.28945 um/px is the 201.26 um CHANNEL PITCH, eight times the cell the same frame claims is
    4 px across. Counting those as cells is counting channels.
    """
    cut, = _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(),
                                   cells_px=16, period_px=32, content=_content(True))])
    assert cut.ink is not None
    # AND EVERY OTHER GATE PASSES ON THIS FRAME, so the pitch is the one doing the work. Measured
    # on the drawn file: 53.1250% ink, 40 interiors on the best row over 900 rows, and the
    # structure measured here is 39 repeats every 32.0 px at 0.975 of a perfect array.
    assert cut.ink.cells_on_best_row >= CELLS_ON_A_ROW, "plenty of shapes were resolved"
    assert cut.ink.cell_rows >= CELL_ROWS, "and on plenty of rows"
    assert cut.ink.ink_fraction >= INK_FLOOR, "and the frame is far from blank"
    assert cut.ink.cell_pitch_px > cut.cell_px * PITCH_TOLERANCE
    assert not cut.counts_cells, (
        "what is drawn is far bigger than the cell this window claims, so it is not cells")
    assert getattr(cut.ink, "shows_structure", True), "and a regular array really is in it"


def test_the_verdict_comes_from_the_file_and_not_from_the_manifest(tmp_path):
    """Two frames whose published numbers are IDENTICAL, and only the picture differs."""
    good = _frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=4,
                  content=_content(True))
    yes, = _cuts(tmp_path, [good])
    # same scope, same window, same stated metres per pixel, same content block - another drawing.
    # The replacement is the PLENUM shape: one solid block of 4 px cells, no passage anywhere,
    # which measures 100% ink and 319 interiors and is exactly the frame that must be refused.
    write_cut_picture(tmp_path / good["image"], *FRAME_PX, cells_px=4, period_px=0)
    no, = _cuts(tmp_path, [good])
    assert yes.window_m == no.window_m and yes.cell_px == no.cell_px
    assert yes.counts_cells, "the passage array was drawn, so this frame counts"
    assert not no.counts_cells, (
        "the same manifest over a picture of solid metal must not count - every published number "
        "is identical and only the pixels differ")


def test_a_frame_that_cannot_be_read_counts_for_nothing(tmp_path):
    """Unreadable is not "assume the best": nothing was measured, so nothing is claimed."""
    assert _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), draw=False,
                                   raw=b"\x89PNG\r\n\x1a\n" + b"rubbish" * 40,
                                   content=_content(True))]) == ()


 # the producer's own measurement, which may veto and may not grant

def test_a_frame_the_producer_measured_no_passages_in_cannot_discharge(tmp_path):
    """The side that drew it read it back and found no periodic array. That is a veto.

    It is a different failure from "no cells are resolved" and it has a different repair: the
    window is right and the cells may well be fine, the CUT IS IN THE WRONG PLACE. On this part
    two of three planes cannot cross the microchannels at all - through_the_plate sits at
    y = 41.070 mm while the channels live between y = 21.51 and 33.55 mm.
    """
    cut, = _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=4,
                                   content=_content(False))])
    assert cut.ink is not None and cut.ink.cells_on_best_row >= CELLS_ON_A_ROW, (
        "the cells ARE drawn - this frame fails for the other reason")
    assert not cut.counts_cells


def test_a_frame_with_no_published_content_is_measured_not_refused(tmp_path):
    """INVERTED, and the inversion is the point.

    This test used to assert that a frame with no `content` block FAILS CLOSED, on the reasoning
    that "a frame nobody measured is not a frame measured and found good". That reasoning was
    sound and its premise was not: somebody HAD measured it - the reading side just was not the
    one doing it. The measurement is taken here now, off this side's own copy of the PNG, so the
    producer's claim is no longer needed for a frame to be read, and an absent claim refuses
    nothing.

    WHAT THE OLD BEHAVIOUR COST. Every archived or replayed manifest written before `content`
    existed carries no such field, and every frame in it was refused however good its pixels were
    - which turns a re-read of a delivered job into "evidence missing" for a reason that has
    nothing to do with the mesh.
    """
    cut, = _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=4)])
    assert cut.shows_passages, (
        "the manifest says nothing, which is not a denial - and the array is in the picture")
    assert cut.counts_cells
    assert getattr(cut, "claimed_structure", None) is None
    assert cut.ink is not None and getattr(cut.ink, "shows_structure", True), (
        "and this side measured the array in the picture itself")


def test_a_published_claim_of_false_still_refuses_the_frame(tmp_path):
    """The claim may SUBTRACT. The producer holds the plan and the whole picture; this side holds
    one window, so its refusal is taken even when the pixels here look like an array.

    This one does NOT fail against the old tree: a present False refused a frame there too. It is
    here so that making absence neutral cannot quietly make a denial neutral with it.
    """
    cut, = _cuts(tmp_path, [_frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=4,
                                   content=_content(False))])
    assert not cut.shows_passages
    assert not cut.counts_cells
    assert getattr(cut, "claimed_structure", False) is False
    assert cut.ink is not None and getattr(cut.ink, "shows_structure", True), (
        "the array IS in this picture - the frame fails for the other side's reason")


 # item 9: a question about a plane is not answered with a hundredth of the plane

def test_a_region_is_not_answered_with_a_hundredth_of_its_own_plane(tmp_path):
    """inspect_region with no scope serves the PLANE the region names, not a corner of it.

    through_the_plate's zoom covers 8.05 x 6.04 mm of a plane 177.5 x 133.1 mm across. That is
    0.27% of it, and the region's own published question - "where the coolant reaches and where
    the metal is" - is a question about the plane.
    """
    frames = _cuts(tmp_path, [
        _frame(tmp_path, FRAME_WHOLE, window=_whole_window(), cells_px=0, content=_content(True)),
        _frame(tmp_path, FRAME_ZOOM, window=_zoom_window(), cells_px=4, content=_content(True))])
    inputs = ResolvedRenderInputs(surface=None, cuts={"through_the_plate": frames})

    served = inputs.cut_frame("through_the_plate")
    assert served is not None and served.scope == FRAME_WHOLE, (
        "the default must be the whole plane; the caller named the region, not a magnification")
    zoom = inputs.cut_frame("through_the_plate", FRAME_ZOOM)
    assert (zoom.window_m[0] * zoom.window_m[1]
            / (served.window_m[0] * served.window_m[1])) < 0.01, "the zoom really is under 1%"
    # and the one that discharges is still reachable, by name
    assert zoom.counts_cells and not served.counts_cells


 # item 6: the ceiling that was reasoned from a 1:1 read

#: MEASURED peak RSS against file size for a .vtu, the shape `mesh_paths.volume` is. An
#: independently measured 93,924,028 B .vtu came back at 9.27x; a 1,000,000-cell .vtu measured in
#: this worker's own container came back at 7.4x marginal; native.py's own table already recorded
#: 0.80 GB for a ~100 MB .vtu. A .vtp is a different shape entirely and reads at 0.84x (measured
#: 1,238,969,100 B -> 1.037 GB), which is where the 1:1 assumption came from.
VTU_RSS_RATIO = 9.27
#: The Celery worker is capped at 16 GiB and holds an interpreter, VTK and the renderer besides.
WORKER_HEADROOM_BYTES = 12 * 1024 ** 3


def test_the_volume_ceiling_fits_what_a_vtu_actually_costs_to_open():
    """4 GiB of .vtu is about 37 GB of RSS in a 16 GiB worker: the old ceiling permitted the
    OOM kill it was written to prevent."""
    assert VOLUME_READ_CEILING_BYTES * VTU_RSS_RATIO <= WORKER_HEADROOM_BYTES, (
        "a volume this large would not fit in the worker's headroom once opened")
    assert 4 * 1024 ** 3 * VTU_RSS_RATIO > WORKER_HEADROOM_BYTES, (
        "the ceiling this replaced did not bound the read at all")


def test_the_floors_are_the_production_ones():
    """The literals above are the production constants, so the tests are about the real floors."""
    from meshpipeline.sandbox.review_session import (
        CELL_PITCH_TOLERANCE,
        COUNTABLE_CELLS_ON_A_ROW,
    )
    assert (COUNTABLE_CELLS_ON_A_ROW, CELL_PITCH_TOLERANCE) == (CELLS_ON_A_ROW, PITCH_TOLERANCE)
