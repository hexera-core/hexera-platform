# Responsibility: Verify the reading side decides what a prepared frame contains from the FRAME,
# that an untrusted claim about it may only subtract, and that the work one manifest can ask for
# is bounded.
# Boundaries: the home side. Nothing here renders, and nothing here trusts a number it was handed.
#
# THE DEFECT THIS IS WRITTEN FROM, verified in this tree before the change. The one measurement
# doing the load-bearing work was `content.shows_structure`, a boolean the home side read OUT OF
# THE MANIFEST THE MESH VM WROTE, and `ResolvedCut.shows_cells` returned False the moment it was
# absent and took it as satisfied when it was true.
#
# MEASURED, 2026-11-17, every figure through the production ArtifactResolver and ResolvedCut:
#
#   a frame of SOLID METAL - a uniform 4 px cell grid filling the whole frame, no passage
#   anywhere - measures 100.0000% ink, 319 enclosed interiors on its best row at a 4.0 px pitch,
#   720 rows carrying a countable run, and exactly 4.00 px per planned cell. With
#   `shows_structure: true` beside it the old reader answered counts_cells TRUE and discharged the
#   region. With the byte-identical PNG and `shows_structure: false` it refused. Every number the
#   home side measured was the same in both runs.
#
#   a frame that is 99.96% BLANK - one 25x21 px patch, 525 inked pixels of 1,228,800,
#   ink_fraction 0.0427% - measures 6 enclosed interiors at a 4.0 px pitch on its best row and
#   also answered counts_cells TRUE. It clears EMPTY_FRAME_PIXEL_SHARE too, because 4.27e-4 is
#   greater than 1e-4.
#
# AND THE SEPARATION WAS ALREADY THERE, unused. The real plate's frames (job 0ff6f9e5, cuts of
# fluid_to_solid.vtp) measure 0.0335%, 0.3682%, 0.4142%, 0.5921%, 0.6834% and 3.2585% ink; a
# fluid-only volume cut drawn through the same renderer measures 53.1250%. The frame that slipped
# through is 0.0427%, two orders of magnitude under the real array frame.
from __future__ import annotations

from pathlib import Path

import pytest
from _passage_pictures import write_cut_picture  # noqa: E402  (this dir is on sys.path)
from _render_fakes import FakeBackend  # noqa: E402  (tests/unit is on sys.path via conftest)

from meshpipeline.contracts.review_evidence import (
    ArtifactFormat,
    RenderArtifactRequirement,
    RenderContext,
    ResolvedArtifact,
)
from meshpipeline.sandbox import review_session as RS
from meshpipeline.sandbox.render_adapter import (
    VOLUME_KEY,
    VOLUME_READ_CEILING_BYTES,
    expand_region_targets,
)
from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
from meshpipeline.sandbox.review_session import (
    COUNTABLE_CELL_PX,
    FRAME_ZOOM,
    ArtifactResolver,
    ResolvedEvidence,
    open_review_session,
)

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
CELL_M = 2.51578e-05                 # quality.near_wall_cell_m, and fine_cell_pinned_m
FRAME_PX = (1280, 960)
#: conjugate_report.review.bbox of the same job, for the one test that calls the real producer.
PLATE_LO = (-0.061679501086473465, 0.020110199227929115, -0.008694685064256191)
PLATE_HI = (0.11601100116968155, 0.06193649768829346, 0.09251250326633453)

#: The floors, restated as literals rather than imported, so every test below runs against the
#: PRE-CHANGE tree and fails there on its assertion rather than on a missing name.
#: `test_the_floors_are_the_production_ones` is the one test that imports them.
CELLS_ON_A_ROW = 4
CELL_ROWS = 12
INK_FLOOR = 1e-3
FRAMES_PER_SESSION = 64

REGION = {"name": "through_the_plate", "kind": "slice", "normal": [0.0, 1.0, 0.0],
          "origin": [0.02717348, 0.041070016, 0.0419095995], "plane_axes": [0, 2],
          "label": "Through the part at mid-thickness",
          "why": "where the coolant reaches and where the metal is"}


def _zoom_window() -> list[float]:
    """The window the producer publishes: sized so one planned cell is COUNTABLE_CELL_PX across."""
    mpp = CELL_M / COUNTABLE_CELL_PX
    return [mpp * FRAME_PX[0], mpp * FRAME_PX[1]]


def _claim(found: bool) -> dict:
    """`frames[].content` as cfmesh_conjugate/slices.ink_structure publishes it."""
    if found:
        return {"shows_structure": True, "repeats": 40, "period_px": 32.0,
                "ink_fraction": 0.251}
    return {"shows_structure": False, "ink_fraction": 0.0041,
            "why": "the ink forms 1 repeat(s) across the frame, under the 6 a periodic array "
                   "has to show"}


def _manifest(ws: Path, frames: list[dict], *, volume: str | None = None) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    region = dict(REGION)
    region["frames"] = frames
    paths = {"surface": "mesh.msh"}
    if volume:
        paths["volume"] = volume
    return {"mesh_units": "m", "mesh_paths": paths,
            "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                        "max_cell_m": 0.0006788786304584814},
            "inspection_regions": [region]}


def _frame(ws: Path, *, claim: dict | None, name: str = "z", **draw) -> dict:
    rel = f"review/cuts/{name}.png"
    write_cut_picture(ws / rel, *FRAME_PX, **draw)
    mpp = CELL_M / COUNTABLE_CELL_PX
    out = {"scope": FRAME_ZOOM, "image": rel, "window_m": _zoom_window(),
           "metres_per_pixel": mpp}
    if claim is not None:
        out["content"] = claim
    return out


def _cut(ws: Path, *, claim: dict | None, **draw):
    cuts = ArtifactResolver(ws, _manifest(ws, [_frame(ws, claim=claim, **draw)])).resolve_cuts()
    got = cuts.get("through_the_plate", ())
    return got[0] if got else None


 # 1. the measurement is taken where it has to be trusted

def test_a_frame_of_solid_metal_is_refused_however_the_manifest_describes_it(tmp_path):
    """THE COUNTER-EXAMPLE. A uniform grid filling the frame is a plenum, not a passage array.

    MEASURED on the drawn file: 100.0000% ink, 319 interiors on the best row, 720 rows, a 4.0 px
    pitch, and exactly 4.00 px per planned cell - every number the home side had, and all of them
    satisfied. The only thing that separated it from a real cut was a boolean in the manifest.
    """
    for tag, claim in (("a claim of true", _claim(True)),
                       ("a claim of false", _claim(False)),
                       ("no claim at all", None)):
        cut = _cut(tmp_path, claim=claim, cells_px=4, period_px=0, name=f"metal_{len(tag)}")
        assert cut is not None and cut.ink is not None
        # THE BEHAVIOUR FIRST, so this test fails on the verdict and not on a field name.
        assert not cut.counts_cells, (
            f"with {tag} a picture of solid metal must not discharge a region - the home side "
            "can see for itself that there is no passage in it")
        # and then WHY it must not: everything the old reader had was satisfied
        assert cut.ink.ink_fraction == 1.0, tag
        assert cut.ink.cells_on_best_row >= CELLS_ON_A_ROW, tag
        assert cut.ink.cell_rows >= CELL_ROWS, tag
        assert cut.cell_px == COUNTABLE_CELL_PX, tag
        assert not getattr(cut.ink, "shows_structure", False), (
            tag, "every column of solid metal is inked, so it has no repeat in it")


def test_the_same_picture_gets_the_same_verdict_whatever_the_manifest_says(tmp_path):
    """One file, three manifests. The verdict is a property of the picture.

    This is the defect stated as an equality: the old reader gave the byte-identical PNG two
    different answers depending on a field it could not check.
    """
    rel = "review/cuts/one.png"
    write_cut_picture(tmp_path / rel, *FRAME_PX, cells_px=4, period_px=32)
    mpp = CELL_M / COUNTABLE_CELL_PX
    verdicts = {}
    for tag, claim in (("true", _claim(True)), ("absent", None)):
        frame = {"scope": FRAME_ZOOM, "image": rel, "window_m": _zoom_window(),
                 "metres_per_pixel": mpp}
        if claim is not None:
            frame["content"] = claim
        cut, = ArtifactResolver(tmp_path, _manifest(tmp_path, [frame])).resolve_cuts()[
            "through_the_plate"]
        verdicts[tag] = cut.counts_cells
    assert verdicts == {"true": True, "absent": True}, (
        "a claim that agrees, and no claim at all, must reach the same verdict as each other - "
        "the picture is the same picture")


def test_an_absent_claim_is_neutral_and_a_present_false_one_still_vetoes(tmp_path):
    """THREE VALUES, THREE ANSWERS. `is True` collapsed absent into a denial.

    An absent claim refusing a good frame breaks every archived or replayed manifest written
    before the field existed. A present False is still taken: the producer holds the plan and the
    whole picture while this side holds one window, so it may subtract.
    """
    good = {"cells_px": 4, "period_px": 32}
    assert _cut(tmp_path, claim=None, name="n1", **good).counts_cells, (
        "an absent claim must not refuse a frame the pixels vouch for")
    assert _cut(tmp_path, claim=_claim(True), name="n2", **good).counts_cells
    assert not _cut(tmp_path, claim=_claim(False), name="n3", **good).counts_cells
    # and an unreadable claim is absent by another route, not a grant
    odd = _cut(tmp_path, claim={"shows_structure": "yes"}, name="n4", **good)
    assert odd.claimed_structure is None and odd.counts_cells


def test_the_claim_cannot_make_a_frame_countable_that_the_picture_refuses(tmp_path):
    """A true claim BUYS NOTHING. Measured over four shapes, with the claim set true on all."""
    refused = {
        "solid metal": {"cells_px": 4, "period_px": 0},
        "a port bore": {"cells_px": 0, "period_px": 0, "patch_px": (120, 120)},
        "passages with no resolvable cell": {"cells_px": 1, "period_px": 32},
        "forty channels read as cells": {"cells_px": 16, "period_px": 32},
    }
    for i, (label, draw) in enumerate(refused.items()):
        cut = _cut(tmp_path, claim=_claim(True), name=f"r{i}", **draw)
        assert cut is not None and not cut.counts_cells, label


 # 2. the ink floor

def test_a_frame_that_is_almost_entirely_blank_cannot_discharge_its_region(tmp_path):
    """THE SECOND COUNTER-EXAMPLE. When this was written the ink floor was the only gate that
    refused it; the EXTENT floor added in round 5 refuses the same frame independently, and the
    last assertion below records that rather than still claiming the floor stands alone.

    MEASURED on the drawn file: 0.0938% of the frame has anything in it at all, and the cell
    tests all pass - 7 repeats every 16.0 px at 0.867 of a perfect array, 16 interiors on the
    best row, 12 rows, a 4.0 px pitch against the 4.00 px a cell should be. A count taken off a
    frame this empty is not a count of the region the window names.

    MEASURED AGAIN 2026-11-17, after the extent floor: the repeat reaches 8.83% of the width and
    the ink 1.67% of the height, so the structure covers 0.15% of the frame against the 10.00% a
    cut of a region needs. Two independent readings of the same emptiness, which is the right
    number of readings for a frame that used to discharge a region.
    """
    cut = _cut(tmp_path, claim=_claim(True), name="blankish",
               cells_px=4, period_px=16, patch_px=(128, 16))
    assert cut is not None and cut.ink is not None
    assert cut.ink.ink_fraction < INK_FLOOR, cut.ink.ink_fraction
    assert not cut.counts_cells, (
        "a frame with 0.09% of it drawn on must not discharge a region")
    # and every CELL gate on the frame really does pass, so the emptiness is doing the work
    assert cut.ink.cells_on_best_row >= CELLS_ON_A_ROW, "the row count passes"
    assert cut.ink.cell_rows >= CELL_ROWS, "the row-depth count passes"
    assert cut.ink.cell_pitch_px == COUNTABLE_CELL_PX, "the pitch agrees with the claimed cell"
    assert not getattr(cut.ink, "shows_structure", True), (
        "and the extent floor refuses it too - a 128x16 px patch of a 1280x960 frame is a speck "
        "whichever way it is measured")


def test_the_ink_floor_sits_in_the_measured_gap(tmp_path):
    """Where the floor is, against what it has to refuse and what it must not touch.

    Every figure here was measured through this same reader. The two above the floor that are
    still refused are refused for their own reasons - a bore is not an array - which is the point:
    the floor removes only the class nothing else can see.
    """
    # the worst thing it must refuse, and the emptiest real frame on the plate
    assert 4.27e-4 < INK_FLOOR and 3.35e-4 < INK_FLOOR
    assert INK_FLOOR / 4.27e-4 > 2.3, "and it clears the slipped frame by a factor, not a hair"
    # the poorest real frame with a feature in it, and the one real frame that holds the array
    assert 3.682e-3 > INK_FLOOR and 3.2585e-2 > INK_FLOOR
    assert 3.682e-3 / INK_FLOOR > 3.6, "with room under the real frames too"
    # a fluid-only volume cut through the same renderer, measured here
    cut = _cut(tmp_path, claim=None, name="vol", cells_px=4, period_px=32)
    assert cut.ink.ink_fraction > 0.5 and cut.ink.ink_fraction / INK_FLOOR > 500


 # 3. the row count

def test_cells_on_one_row_are_not_a_picture_of_an_array(tmp_path):
    """A strip two cells deep, and the ROW COUNT is the only gate that refuses it.

    MEASURED: 160 interiors on the best row at a 4.0 px pitch over 6 rows, 0.4427% ink, 39
    repeats every 32.0 px. The old gate read `cells_on_best_row` alone, so one row out of 960
    granted a frame - and on the real plate the along_the_flow zoom drawn with a two-pixel edge
    resolves 39 interiors on exactly ONE row.
    """
    cut = _cut(tmp_path, claim=_claim(True), name="strip2",
               cells_px=4, period_px=32, patch_px=(1280, 8))
    assert cut is not None and cut.ink is not None
    assert cut.ink.cell_rows < CELL_ROWS, cut.ink.cell_rows
    assert not cut.counts_cells, (
        "two cells deep cannot answer how many cells are through the passage's height")
    # and the CELL gates on this frame all pass, so the depth is doing the work
    assert cut.ink.ink_fraction > INK_FLOOR
    assert cut.ink.cells_on_best_row >= CELLS_ON_A_ROW
    assert cut.ink.cell_pitch_px == COUNTABLE_CELL_PX
    assert not getattr(cut.ink, "shows_structure", True), (
        "round 5's extent floor refuses the same strip independently: 8 px of a 960 px frame")


def test_the_smallest_patch_that_answers_both_questions_still_discharges(tmp_path):
    """The floor is a floor, not a wall - but THE PATCH IT PASSES IS NOT THE SAME ONE ANY MORE,
    and that is a narrowing this test records rather than hides.

    MEASURED, as written in round 4: a patch COUNTABLE_CELLS_ON_A_ROW cells deep at
    COUNTABLE_CELL_PX pixels per cell comes back at EXACTLY 12 rows - three per cell row, because
    the fourth is the drawn edge - and that 1280 x 16 px strip discharged its region.

    MEASURED AGAIN 2026-11-17, after `picture_structure.EXTENT_MIN`: that strip covers 0.0163 of
    the frame and now DECLINES. It cannot be kept: the speck that had to be refused - twelve
    bands of cells 384 x 64 px - covers 0.0184, so no floor on the footprint refuses the one and
    passes the other. The same full-width strip discharges from 100 px deep (25 cell rows,
    extent 0.1017); the smallest SQUARE patch that discharges is 384 x 384 px (0.1103), which at
    the producer's 6.28945 um/px is 2.42 x 2.42 mm inside an 8.05 x 6.04 mm window.
    """
    thin = _cut(tmp_path, claim=None, name="strip4",
                cells_px=4, period_px=32, patch_px=(1280, 16))
    assert thin is not None and thin.ink is not None
    assert thin.ink.cell_rows == CELL_ROWS, thin.ink.cell_rows
    assert not thin.counts_cells, (
        "a strip four cells deep is 1.67% of the frame and is refused on its extent now")
    cut = _cut(tmp_path, claim=None, name="strip25",
               cells_px=4, period_px=32, patch_px=(1280, 100))
    assert cut is not None and cut.ink is not None
    assert cut.ink.cell_rows > CELL_ROWS
    assert cut.counts_cells, "the smallest patch that answers both questions must still count"


 # 4. the work one manifest can ask for

#: The budget tests count DECODES, not pixels, so they name a small file: a 1280x960 frame is
#: 0.33 s to measure and 160 of them would put half a minute into a test about arithmetic.
SMALL_PX = (160, 120)


def _many(ws: Path, regions: int, per_region: int) -> dict:
    """A manifest declaring `regions * per_region` frames, all naming one drawn file."""
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    rel = "review/cuts/shared.png"
    write_cut_picture(ws / rel, *SMALL_PX, cells_px=4, period_px=32)
    mpp = CELL_M / COUNTABLE_CELL_PX
    out = []
    for r in range(regions):
        region = dict(REGION, name=f"r{r}")
        region["frames"] = [{"scope": FRAME_ZOOM, "image": rel, "window_m": _zoom_window(),
                             "metres_per_pixel": mpp} for _ in range(per_region)]
        out.append(region)
    return {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
            "quality": {"near_wall_cell_m": CELL_M}, "inspection_regions": out}


@pytest.fixture
def decodes(monkeypatch):
    """How many times the resolver actually un-filtered a PNG."""
    seen: list[Path] = []
    real = RS._measure_png

    def counted(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(RS, "_measure_png", counted)
    return seen


def test_a_manifest_cannot_make_one_session_decode_without_bound(tmp_path, decodes):
    """MEASURED: 2.687 s of pure-Python decode from a 22,548-byte 4096x4096 Paeth-filtered PNG.

    The per-file caps bound one decode; nothing bounded how many a manifest could name. A hundred
    such frames is 2.2 MB of files and 268.7 s of blocked event loop. At the size the producer
    really draws it is 0.33 s a frame, and 10 regions x 10 frames measured 33.5 s here.
    """
    ArtifactResolver(tmp_path, _many(tmp_path, regions=40, per_region=4)).resolve_cuts()
    assert len(decodes) == FRAMES_PER_SESSION, (
        f"160 frames were declared and {len(decodes)} were decoded; the budget is "
        f"{FRAMES_PER_SESSION}")


def test_the_budget_counts_decodes_and_not_frames_kept(tmp_path, decodes):
    """The two are different numbers, and only one of them was ever unbounded.

    `resolve_cuts` de-duplicates by scope AFTER the file has been read, so a region declaring a
    hundred frames under one scope keeps exactly one and decodes all hundred. MEASURED before the
    budget: 20 such frames took 6.68 s to keep one.
    """
    out = ArtifactResolver(tmp_path, _many(tmp_path, regions=1, per_region=100)).resolve_cuts()
    assert sum(len(v) for v in out.values()) == 1, "one scope, one frame kept"
    assert len(decodes) == FRAMES_PER_SESSION, (
        f"{len(decodes)} files were read to keep one; a cap on what is KEPT would not have "
        "bounded this at all")


def test_the_frames_inside_the_budget_are_still_resolved(tmp_path, decodes):
    """The bound refuses work, not evidence: everything up to it is read and offered."""
    out = ArtifactResolver(tmp_path, _many(tmp_path, regions=4, per_region=1)).resolve_cuts()
    assert set(out) == {"r0", "r1", "r2", "r3"}
    assert len(decodes) == 4


 # the decode belongs off the event loop

class _Spec:
    def __init__(self, renderer, artifacts):
        self.review_renderer = renderer
        self.render_artifacts = artifacts


class _Renderer:
    def __init__(self, ws):
        self.ws = ws
        self.opened: ResolvedEvidence | None = None

    def open(self, context, artifacts):
        from meshpipeline.sandbox.render_adapter import BackendRenderSession

        self.opened = artifacts
        regions = resolve_regions(context.manifest)
        inputs = ResolvedRenderInputs(surface=artifacts["mesh_paths.surface"],
                                      cuts=getattr(artifacts, "cuts", {}))
        return BackendRenderSession(
            FakeBackend(save_dir=self.ws), inputs=inputs, regions=regions, patch_targets=(),
            region_targets=expand_region_targets(
                regions, required=True, purpose="p", volume_available=False, cuts=inputs.cuts),
            opening_purpose="o")


async def test_the_prepared_frames_are_decoded_through_the_dispatch_lane(tmp_path):
    """Resolving the cuts is not pathlib any more, so it does not belong on the event loop.

    Every frame is un-filtered scanline by scanline in pure Python - 0.33 s for one 1280x960
    frame, 2.687 s for a 4096x4096 one - and `open_review_session` ran it inline, between an
    `await` and an `await`, for as long as the manifest asked for.
    """
    man = _manifest(tmp_path, [_frame(tmp_path, claim=None, cells_px=4, period_px=32)])
    spec = _Spec(_Renderer(tmp_path), (RenderArtifactRequirement(
        artifact_key="mesh_paths.surface", allowed_formats=(ArtifactFormat.GMSH_MSH,),
        purpose="p", required=True),))
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)

    sent: list[str] = []

    class _Lane:
        async def __call__(self, fn, *a, **kw):
            sent.append(getattr(fn, "__name__", repr(fn)))
            return fn(*a, **kw)

        async def run_final(self, fn, *a, **kw):
            return fn(*a, **kw)

    lane = _Lane()
    async with open_review_session(spec, ctx, native_dispatch=lane) as session:
        assert "resolve_cuts" in sent, (
            f"the decode went straight onto the event loop; the lane only saw {sent}")
        assert len(session.prepared_cuts()) == 1


 # 5. the cross-boundary contract, against the REAL producer

def test_the_producers_frame_writer_needs_nothing_added_but_the_picture(tmp_path):
    """THE CONTRACT, CALLED FOR REAL. `review_slices` authors the frames; the VM adds the PNG.

    The reader used also to REQUIRE `content.shows_structure`, which `review_slices` does not
    write and never has: that field is added later by `native._measure_frame`, only when a render
    happened, and it is absent from every manifest written before it existed. So the reader
    required a key the producer's own frame writer does not publish, and the test that spanned
    this boundary pasted the key on itself before resolving - which is how a reader can require a
    field nothing produces and still look covered.
    """
    from meshpipeline.engines.cfmesh_conjugate import slices as S

    cuts = S.review_slices(PLATE_LO, PLATE_HI, cell_m=CELL_M)
    assert cuts, "the producer published cuts for this block"
    # NOTHING IS ADDED BUT `image`, which is the one key the VM's renderer writes.
    for c in cuts:
        for f in c["frames"]:
            assert "content" not in f, (
                "review_slices does not publish a content block, so the reader must not need one")
            rel = f"review/cuts/{c['name']}_{f['scope']}.png"
            write_cut_picture(tmp_path / rel, S.FRAME_W, S.FRAME_H, cells_px=4, period_px=32)
            f["image"] = rel
    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    man = {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
           "quality": {"near_wall_cell_m": CELL_M}, "inspection_regions": cuts}

    resolved = ArtifactResolver(tmp_path, man).resolve_cuts()
    assert set(resolved) == {c["name"] for c in cuts}
    for name, frames in resolved.items():
        zoom = next(f for f in frames if f.scope == FRAME_ZOOM)
        assert zoom.counts_cells, (
            f"{name}: a frame built from the producer's own frame writer, with nothing added but "
            "the picture, must be able to discharge its region")
        assert getattr(zoom, "claimed_structure", None) is None, name


def test_the_reader_and_the_producer_measure_a_picture_with_the_same_code(tmp_path):
    """The two halves of the contract now agree BECAUSE THERE IS ONE MEASUREMENT, not two.

    `COUNTABLE_CELL_PX` was pinned to the producer's `CELL_PIXELS` exactly this way and the field
    doing all the work was not pinned at all. It cannot drift now: both sides call the same
    function object out of sandbox/picture_structure.

    THE AGREEMENT IS ASSERTED AS BEHAVIOUR FIRST. `shows_passages` is what the reader acts on and
    it exists on both sides of this change, so running the producer's own detector over the
    reader's own file and comparing the two verdicts is a claim an older tree can be held to - and
    fails against one, because there the reader answers from the manifest and the manifest here
    says nothing.
    """
    import numpy as np
    from PIL import Image

    from meshpipeline.engines.cfmesh_conjugate import slices as S

    shapes = ({"cells_px": 4, "period_px": 32},
              {"cells_px": 4, "period_px": 0},
              {"cells_px": 0, "period_px": 0, "patch_px": (120, 120)},
              {"cells_px": 4, "period_px": 16, "patch_px": (128, 16)})
    for i, draw in enumerate(shapes):
        cut = _cut(tmp_path, claim=None, name=f"agree{i}", **draw)
        assert cut is not None
        theirs = S.ink_structure(np.asarray(Image.open(cut.path).convert("L")))
        assert cut.shows_passages == bool(theirs["shows_structure"]), (
            draw, "the reader's verdict on the passages must be the producer's detector's "
                  "verdict on the reader's own copy of the file")
        assert cut.ink.repeats == int(theirs["repeats"]), draw
    # and it cannot drift, because there is one function and not two copies of one
    assert S.ink_structure is RS.ink_structure, (
        "the producer and the reader must run the same detector, not two copies of one")


 # 6. the shared ceiling, and the regression of lowering it

def test_the_lowered_volume_ceiling_leaves_four_engines_with_no_fallback():
    """THE BLAST RADIUS, DERIVED FROM THE REGISTRY rather than from a count of importers.

    Seven engines render for review and all seven reach the interior through
    `render_adapter.build_backend`; the constant itself is used in exactly one place. Five declare
    `mesh_paths.volume`, and only cfmesh_conjugate publishes prepared frames to fall back on - so
    for the other four a volume between 1.2 GiB and the old 4 GiB has no live cut and no
    replacement, and the review becomes reviewer_evidence_missing where it used to get a verdict.
    """
    from meshpipeline.engines.registry import all_engine_names, get_spec

    with_volume = set()
    for name in all_engine_names():
        spec = get_spec(name)
        if any(a.artifact_key == VOLUME_KEY for a in (spec.render_artifacts or ())):
            with_volume.add(name)
    assert with_volume == {"cfmesh", "cfmesh_conjugate", "snapgrid", "snappy",
                           "snappy_multiregion"}, with_volume
    # gmsh declares no volume at all, and vmtk's lumen rides under a DIFFERENT key, so this
    # ceiling never sees it - an existing gap in that engine's declaration, not this one's.
    assert "vmtk" not in with_volume and "gmsh" not in with_volume
    assert with_volume - {"cfmesh_conjugate"} == {"cfmesh", "snapgrid", "snappy",
                                                  "snappy_multiregion"}, (
        "these four regress when the ceiling drops, and the constant's comment names them")


def test_a_volume_between_the_old_and_the_new_ceiling_is_not_cut_and_not_replaced(tmp_path,
                                                                                  monkeypatch):
    """The named regression, as behaviour: an engine with no prepared frames loses the region.

    Measured peak RSS for this file shape is about 9.5x its bytes, so 4 GiB of .vtu is roughly
    38 GiB inside a 16 GiB worker - the old ceiling permitted the OOM kill it was written to
    prevent. What that correction costs is this test.
    """
    from meshpipeline.sandbox import backend as BE
    from meshpipeline.sandbox.render_adapter import build_backend

    class _B(FakeBackend):
        def __init__(self, inputs=None, metadata=None, save_dir=None, regions=None, **kw):
            super().__init__(save_dir=Path(save_dir) if save_dir else None)
            self.inputs = inputs

    monkeypatch.setattr(BE, "MeshRenderBackend", _B)

    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    big = tmp_path / "internal.vtu"
    with big.open("wb") as fh:                     # sparse: no bytes are written
        fh.truncate(2 * 1024 ** 3)
    assert VOLUME_READ_CEILING_BYTES < big.stat().st_size < 4 * 1024 ** 3, (
        "the file has to sit between the new ceiling and the one it replaced")

    man = _manifest(tmp_path, [], volume="internal.vtu")
    ev = ResolvedEvidence({
        "mesh_paths.surface": ResolvedArtifact("mesh_paths.surface", tmp_path / "mesh.msh",
                                               ArtifactFormat.GMSH_MSH),
        VOLUME_KEY: ResolvedArtifact(VOLUME_KEY, big, ArtifactFormat.VTK_VTU)})
    ev.cuts = {}                                   # an engine that publishes no prepared frames
    _, inputs, regions = build_backend(
        RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man), ev)

    assert not inputs.has_volume, "over the ceiling is not a volume"
    assert not inputs.has_cuts, "and this engine has nothing prepared to put in its place"
    assert regions, "the manifest still declares the region"
    assert expand_region_targets(regions, required=True, purpose="p", volume_available=False,
                                 cuts={}) == (), (
        "so the region is not offered at all - which is the regression the constant's comment "
        "names, and it is covered here rather than rediscovered on a job")


def test_the_floors_are_the_production_ones():
    """The literals above are the production constants, so every test here is about the real
    floors. The one test in this file that imports a name this round added."""
    from meshpipeline.sandbox.review_session import (
        COUNTABLE_CELL_ROWS,
        COUNTABLE_CELLS_ON_A_ROW,
        EVIDENCE_INK_FLOOR,
        MAX_FRAMES_MEASURED_PER_SESSION,
    )

    assert (COUNTABLE_CELLS_ON_A_ROW, COUNTABLE_CELL_ROWS) == (CELLS_ON_A_ROW, CELL_ROWS)
    assert EVIDENCE_INK_FLOOR == INK_FLOOR
    assert MAX_FRAMES_MEASURED_PER_SESSION == FRAMES_PER_SESSION
    # the row floor is derived, not chosen: the minimum honest patch is CELLS_ON_A_ROW cells deep
    assert COUNTABLE_CELL_ROWS == COUNTABLE_CELLS_ON_A_ROW * (int(COUNTABLE_CELL_PX) - 1)
    # and the detector's own period floor is still two cells wide, as it was when it was an
    # expression in the engine rather than a literal in the shared module
    from meshpipeline.engines.cfmesh_conjugate import slices as S
    from meshpipeline.sandbox.picture_structure import MIN_PERIOD_PX

    assert MIN_PERIOD_PX == 2 * S.CELL_PIXELS == 2 * COUNTABLE_CELL_PX
