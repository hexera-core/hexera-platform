# Responsibility: Verify a cut taken where the mesh was built reaches the reviewer, bounded by what it can resolve.
# Boundaries: the home side only - resolution, the capability gate, and which frame discharges a region.
#
# THE JOB THIS IS WRITTEN FROM. 0ff6f9e5: a 283,410,955-cell conjugate cold plate (91,323,244
# fluid + 192,087,711 solid). Its fluid interior is 10,366,611,525 bytes, the worker that would
# slice it is capped at 16 GiB and reads the file whole, so the interior will never come home.
# The review failed with api_failure=reviewer_evidence_missing because the render adapter refused:
# "3 region(s) not offered - no volume artifact".
from __future__ import annotations

import json
from pathlib import Path

import pytest
from _passage_pictures import write_cut_picture  # noqa: E402  (this dir is on sys.path)
from _render_fakes import FakeBackend  # noqa: E402  (tests/unit is on sys.path via conftest)

from meshpipeline.contracts.review_evidence import (
    ArtifactFormat,
    CommandKind,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewRenderError,
)
from meshpipeline.sandbox.render_adapter import (
    BackendRenderSession,
    expand_region_targets,
)
from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
from meshpipeline.sandbox.review_session import (
    COUNTABLE_CELL_PX,
    ArtifactResolver,
    ResolvedEvidence,
    manifest_cut_claims,
    open_review_session,
)

#: Job 0ff6f9e5's own numbers, from the manifest and the plan it ran with.
CELL_M = 2.51578e-05            # quality.near_wall_cell_m - the pinned thin cell
FEATURE_M = 0.0001006312        # conjugate_plan fine_surfaces[0].thickness_m - the channel
#: report.review.bbox, so the whole-part frames below are the frames the reviewer really got.
BBOX = (-0.061679501086473465, 0.020110199227929115, -0.008694685064256191,
        0.11601100116968155, 0.06193649768829346, 0.09251250326633453)
EXTENT = tuple(BBOX[k + 3] - BBOX[k] for k in range(3))     # 0.17769 x 0.041826 x 0.101207 m

REGIONS = [
    {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
     "origin": [0.02735098004, 0.04102818, 0.0419095995],
     "label": "Square across the coolant's path",
     "why": "the cells across each passage, and the metal between them"},
    {"name": "through_the_plate", "kind": "slice", "normal": [0.0, 1.0, 0.0],
     "origin": [0.02717348, 0.041070016, 0.0419095995],
     "label": "Through the part at mid-thickness",
     "why": "where the coolant reaches and where the metal is"},
]


#: `frames[].content` as cfmesh_conjugate/slices.ink_structure publishes it: the producer reads
#: its own drawn PNG back and says whether the PERIODIC PASSAGE ARRAY is in it. It is carried here
#: because a real manifest carries it, NOT because the reader needs it: the reader takes the same
#: measurement on its own copy of the PNG (sandbox/picture_structure), so a claim here may VETO a
#: frame and may not grant one. What makes the frames below countable is that they are DRAWN to
#: hold a passage array - see _passage_pictures for the shape and what it measures.
CONTENT_FOUND = {"shows_structure": True, "repeats": 40, "period_px": 32.0, "spread": 0.04,
                 "ink_fraction": 0.251}


def _window_for(px: int, size_m: float, across: float) -> float:
    """The window width that puts `across` pixels on one `size_m` feature."""
    return px * size_m / across


def _cut(ws: Path, region: str, scope: str, *, window, px=(1280, 960), feature=FEATURE_M,
         name=None, make=True) -> dict:
    """One frame as cfmesh_conjugate/slices.review_slices publishes it, with its file drawn."""
    rel = f"review/cuts/{name or f'{region}_{scope}'}.png"
    if make:
        write_cut_picture(ws / rel, *px)
    out = {"scope": scope, "image": rel, "window_m": [window[0], window[1]],
           "content": dict(CONTENT_FOUND),
           "metres_per_pixel": max(window[0] / px[0], window[1] / px[1])}
    if feature:
        out["feature_m"] = feature
    return out


def _manifest(ws: Path, *, cuts: dict | None = None, volume: str | None = None,
              cell_m: float = CELL_M) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    regions = [dict(r) for r in REGIONS]
    for r in regions:
        if cuts and r["name"] in cuts:
            r["frames"] = cuts[r["name"]]
    paths = {"surface": "mesh.msh"}
    if volume:
        paths["volume"] = volume
    return {"mesh_units": "m", "mesh_paths": paths, "quality": {"near_wall_cell_m": cell_m},
            "inspection_regions": regions}


def _resolver(ws: Path, manifest: dict) -> ArtifactResolver:
    return ArtifactResolver(ws, manifest)


def _session(ws: Path, manifest: dict, *, volume=False) -> tuple[BackendRenderSession, FakeBackend]:
    """A session over the FAKE backend, carrying whatever cuts the manifest resolves to."""
    regions = resolve_regions(manifest)
    cuts = _resolver(ws, manifest).resolve_cuts()
    inputs = ResolvedRenderInputs(
        surface=ResolvedArtifact("mesh_paths.surface", ws / "mesh.msh", ArtifactFormat.GMSH_MSH),
        volume=(ResolvedArtifact("mesh_paths.volume", ws / "internal.vtu",
                                 ArtifactFormat.VTK_VTU) if volume else None),
        cuts={} if volume else cuts)
    be = FakeBackend(save_dir=ws)
    return BackendRenderSession(
        be, inputs=inputs, regions=regions,
        patch_targets=(),
        region_targets=expand_region_targets(
            regions, required=True, purpose="reveal internal structure",
            volume_available=inputs.has_volume, cuts=inputs.cuts),
        opening_purpose="both regions as delivered"), be


# 1. the capability gate: a cut already made is something to cut
def test_a_region_with_a_prepared_cut_is_offered_without_a_volume(tmp_path):
    """The exact refusal job 0ff6f9e5 died on: "N region(s) not offered - no volume artifact"."""
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom",
             window=(_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
                     _window_for(960, CELL_M, COUNTABLE_CELL_PX)))]})
    regions = resolve_regions(man)
    cuts = _resolver(tmp_path, man).resolve_cuts()

    offered = {t.target_id for t in expand_region_targets(
        regions, required=True, purpose="p", volume_available=False, cuts=cuts)}
    assert offered == {"region:across_the_flow"}, (
        "a region whose cut is already taken must be offered; the other has no frame")

    # and nothing changes for the four engines that pass a volume and no cuts
    assert expand_region_targets(regions, required=True, purpose="p",
                                 volume_available=False) == ()
    assert len(expand_region_targets(regions, required=True, purpose="p",
                                     volume_available=True)) == 2


def test_inspect_target_is_an_offered_operation_when_only_cuts_came_home(tmp_path):
    """Without this the reviewer's own inspect_region call is a RENDERER FAULT.

    review_session.ReviewSession.execute raises ReviewRenderError(RENDERER_UNAVAILABLE) for any
    command the capabilities do not support, and the reviewer's render-failure path reruns the
    review and then delivers the mesh with no verdict.
    """
    man = _manifest(tmp_path, cuts={r["name"]: [
        _cut(tmp_path, r["name"], "zoom",
             window=(_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
                     _window_for(960, CELL_M, COUNTABLE_CELL_PX)))] for r in REGIONS})
    session, _ = _session(tmp_path, man)
    caps = session.capabilities()
    assert caps.supports(CommandKind.INSPECT_TARGET)
    assert caps.required_target_ids == {"region:across_the_flow", "region:through_the_plate"}


def test_with_neither_a_volume_nor_a_cut_nothing_is_offered(tmp_path):
    """The honest state job 0ff6f9e5 was in, which must stay honest rather than become a frame."""
    session, _ = _session(tmp_path, _manifest(tmp_path))
    assert not session.capabilities().supports(CommandKind.INSPECT_TARGET)
    assert session.capabilities().targets == ()


# 2. what a frame can resolve decides whether it discharges the region
def test_only_the_frame_a_cell_is_countable_in_covers_its_region(tmp_path):
    """COVERAGE IS THE COUNTABLE FRAME'S. `covers_target` is what the floor reads, and a frame in
    which a channel is under one pixel wide cannot answer either question the cut exists for."""
    whole = (EXTENT[1], EXTENT[2])          # the across_the_flow plane, whole part
    close = (_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
             _window_for(960, CELL_M, COUNTABLE_CELL_PX))
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "whole", window=whole),
        _cut(tmp_path, "across_the_flow", "zoom", window=close)]})
    session, be = _session(tmp_path, man)

    ctx = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                        target_id="region:across_the_flow#whole"))
    det = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                        target_id="region:across_the_flow#zoom"))
    assert ctx.image_ref and det.image_ref, "both frames are shown"
    assert ctx.covers_target == "", "a sub-pixel frame must not discharge the region"
    assert det.covers_target == "region:across_the_flow"
    # Both say which region they addressed, so unified.record_operation_evidence's two
    # statements agree and the covering frame actually advances the floor.
    assert ctx.target_ref == det.target_ref == "region:across_the_flow"
    assert det.artifact_key == "inspection_regions.across_the_flow.frames.zoom"
    assert not be.calls, "a prepared frame is served from disk; no backend render happens"


def test_asking_for_no_frame_gets_the_whole_plane_the_region_names(tmp_path):
    """CHANGED, and the old expectation was the defect. This asserted that a bare
    inspect_region hands back the ZOOM, because that is the frame an inspection is judged on.

    On through_the_plate the zoom covers 8.05 x 6.04 mm of a plane 177.5 x 133.1 mm across -
    0.27% of it - and that region's own published question is "where the coolant reaches and
    where the metal is", which is a question about the plane. Both frames come back as one
    picture of one region, so a caller that named the region and was handed a hundredth of a
    percent of it cannot see the substitution. The zoom is still the only frame that discharges;
    the reviewer is told so and asks for it by name.
    """
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "whole", window=(EXTENT[1], EXTENT[2])),
        _cut(tmp_path, "across_the_flow", "zoom",
             window=(_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
                     _window_for(960, CELL_M, COUNTABLE_CELL_PX)))]})
    session, _ = _session(tmp_path, man)
    ev = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                       target_id="region:across_the_flow"))
    assert ev.artifact_key.endswith(".whole")
    assert ev.covers_target == "", "the whole plane cannot count cells, so it discharges nothing"
    named = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                          target_id="region:across_the_flow#zoom"))
    assert named.artifact_key.endswith(".zoom")
    assert named.covers_target == "region:across_the_flow"


def test_the_four_px_feature_window_is_exactly_one_pixel_per_cell(tmp_path):
    """MEASURED, AND IT REFUTES THE OBVIOUS WINDOW SIZE.

    Sizing the zoom for 4 px across the part's FEATURE gives um/px = feature/4 - and the mesher
    pinned about four cells across that same feature, so feature/4 IS the cell. On this part:
    100.6312 um / 4 = 25.158 um/px against a pinned cell of 25.1578 um. The window that puts four
    pixels on the channel puts ONE pixel on a cell, and a cell drawn one pixel wide has no
    countable edges. Sizing from the CELL instead gives 8.05 mm and 4 px per cell.
    """
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom",
             window=(_window_for(1280, FEATURE_M, 4.0), _window_for(960, FEATURE_M, 4.0)))]})
    cut = _resolver(tmp_path, man).resolve_cuts()["across_the_flow"][0]
    assert round(cut.feature_px, 2) == 4.00
    assert round(cut.cell_px, 2) == 1.00
    assert not cut.counts_cells, "four pixels across the channel is one pixel across a cell"

    from_cell = _window_for(1280, CELL_M, COUNTABLE_CELL_PX)
    assert round(from_cell * 1e3, 2) == 8.05, "the window that does work, in mm"
    man2 = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", name="from_cell",
             window=(from_cell, _window_for(960, CELL_M, COUNTABLE_CELL_PX)))]})
    better = _resolver(tmp_path, man2).resolve_cuts()["across_the_flow"][0]
    assert round(better.cell_px, 2) == 4.00
    assert round(better.feature_px, 2) == 16.00
    assert better.counts_cells


def test_the_whole_part_frame_is_the_sub_pixel_one_the_run_really_produced(tmp_path):
    """The frames the reviewer would have been handed by a fixed 1280x960 fit to the whole part,
    measured against job 0ff6f9e5's own bbox: 105.42 um/px across the flow, 0.24 px per cell."""
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "whole", window=(EXTENT[1], EXTENT[2]))]})
    cut = _resolver(tmp_path, man).resolve_cuts()["across_the_flow"][0]
    assert round(cut.um_per_px, 2) == 105.42
    assert round(cut.cell_px, 2) == 0.24
    assert round(cut.feature_px, 2) == 0.95
    assert not cut.counts_cells


# 3. a cut whose window is not stated is not evidence
def test_a_cut_with_no_stated_window_is_not_offered(tmp_path):
    """A picture of a plane with no window supports any claim at all: the same frame is a channel
    fifteen pixels across or one under a pixel, depending on a number nowhere written down."""
    entry = _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))
    del entry["window_m"]
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    assert _resolver(tmp_path, man).resolve_cuts() == {}


@pytest.mark.parametrize("window", [[0.0, 0.006], [-0.008, 0.006], ["8mm", 0.006],
                                    [float("nan"), 0.006], [float("inf"), 0.006], [0.008]])
def test_an_unusable_window_is_refused_rather_than_half_honoured(tmp_path, window):
    entry = _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))
    entry["window_m"] = window
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    assert _resolver(tmp_path, man).resolve_cuts() == {}


def test_a_frame_with_no_declared_role_is_not_offered(tmp_path):
    entry = _cut(tmp_path, "across_the_flow", "wide", window=(0.00805, 0.00604))
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    assert _resolver(tmp_path, man).resolve_cuts() == {}


# 4. the picture's own pixels, and the box
def test_the_resolution_is_measured_from_the_file_not_claimed_by_the_manifest(tmp_path):
    """Every number the reviewer is told is the stated window over the frame's REAL pixel count.
    A claimed pixel size would let an untrusted manifest state any resolution over any picture."""
    entry = _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604), px=(640, 480))
    entry["pixels"] = [1280, 960]                 # the claim, which must change nothing
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    cut = _resolver(tmp_path, man).resolve_cuts()["across_the_flow"][0]
    assert cut.pixels == (640, 480)
    assert round(cut.cell_px, 2) == 2.00, "half the pixels is half the cell width"
    assert not cut.counts_cells


def test_a_frame_that_is_not_a_png_is_left_out_and_does_not_fail_the_review(tmp_path):
    """Dropping a frame can only make a review DECLINE where it would have judged - never pass
    where it would have refused - so a delivered mesh is not thrown away over a picture of it."""
    entry = _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604), make=False)
    (tmp_path / "review" / "cuts").mkdir(parents=True, exist_ok=True)
    (tmp_path / entry["image"]).write_bytes(b"PK\x03\x04 not a png at all")
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    assert _resolver(tmp_path, man).resolve_cuts() == {}


def test_a_missing_frame_is_left_out(tmp_path):
    entry = _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604), make=False)
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    assert _resolver(tmp_path, man).resolve_cuts() == {}


def test_a_frame_outside_the_workspace_is_refused_outright(tmp_path):
    """The one cut failure that is not about evidence quality: the manifest is untrusted, and a
    frame pointing out of the job is an attempt to read outside the box."""
    entry = _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604), make=False)
    entry["image"] = "../../../etc/passwd"
    man = _manifest(tmp_path, cuts={"across_the_flow": [entry]})
    with pytest.raises(ReviewRenderError) as exc:
        _resolver(tmp_path, man).resolve_cuts()
    assert "outside the job workspace" in str(exc.value)
    assert "etc/passwd" not in str(exc.value), "the key, never the path"


# 5. the volume wins when it is here
def test_prepared_frames_are_set_aside_when_the_volume_came_home(tmp_path):
    """A live cut can be re-aimed and a frame cannot, so the live cut is strictly better evidence
    - and two answers to one question is how a reviewer cites the one that agrees with it."""
    man = _manifest(tmp_path, volume="internal.vtu", cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))]})
    session, be = _session(tmp_path, man, volume=True)
    assert session.prepared_cuts() == ()
    ev = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                       target_id="region:across_the_flow"))
    assert ev.image_ref and ev.artifact_key == "mesh_paths.volume"
    assert ("inspect_region", "across_the_flow") in be.calls, "the volume is cut live"

    refused = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                            target_id="region:across_the_flow#zoom"))
    assert refused.image_ref == "" and "cut live" in " ".join(refused.diagnostics)


def test_a_frame_the_region_does_not_have_fails_and_names_what_it_does(tmp_path):
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))]})
    session, _ = _session(tmp_path, man)
    ev = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                       target_id="region:across_the_flow#whole"))
    assert ev.image_ref == ""
    assert "'zoom'" in " ".join(ev.diagnostics)


def test_a_region_with_no_frame_at_all_says_which_two_things_are_missing(tmp_path):
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))]})
    regions = resolve_regions(man)
    cuts = _resolver(tmp_path, man).resolve_cuts()
    inputs = ResolvedRenderInputs(
        surface=ResolvedArtifact("mesh_paths.surface", tmp_path / "mesh.msh",
                                 ArtifactFormat.GMSH_MSH), cuts=cuts)
    session = BackendRenderSession(
        FakeBackend(save_dir=tmp_path), inputs=inputs, regions=regions, patch_targets=(),
        # deliberately offered, to prove the handler refuses rather than renders the surface
        region_targets=expand_region_targets(regions, required=True, purpose="p",
                                             volume_available=True),
        opening_purpose="o")
    ev = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                       target_id="region:through_the_plate"))
    assert ev.image_ref == ""
    why = " ".join(ev.diagnostics)
    assert "volume artifact" in why and "prepared where the mesh was built" in why


# 6. the whole way through, as the reviewer reaches it
class _Spec:
    def __init__(self, renderer, artifacts):
        self.review_renderer = renderer
        self.render_artifacts = artifacts


class _Renderer:
    def __init__(self, ws):
        self.ws = ws
        self.opened: ResolvedEvidence | None = None

    def open(self, context, artifacts):
        self.opened = artifacts
        regions = resolve_regions(context.manifest)
        inputs = ResolvedRenderInputs(surface=artifacts["mesh_paths.surface"],
                                      cuts=getattr(artifacts, "cuts", {}))
        return BackendRenderSession(
            FakeBackend(save_dir=self.ws), inputs=inputs, regions=regions, patch_targets=(),
            region_targets=expand_region_targets(
                regions, required=True, purpose="p", volume_available=False, cuts=inputs.cuts),
            opening_purpose="o")


async def test_the_session_resolves_the_cuts_and_serves_one_end_to_end(tmp_path):
    """Resolution happens in open_review_session, beside the declared artifacts, so the
    confinement fence this module states in its own header stays meaningful: the renderer is
    handed resolved handles and finds no path of its own."""
    from meshpipeline.contracts.review_evidence import RenderArtifactRequirement

    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "whole", window=(EXTENT[1], EXTENT[2])),
        _cut(tmp_path, "across_the_flow", "zoom",
             window=(_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
                     _window_for(960, CELL_M, COUNTABLE_CELL_PX)))]})
    renderer = _Renderer(tmp_path)
    spec = _Spec(renderer, (RenderArtifactRequirement(
        artifact_key="mesh_paths.surface", allowed_formats=(ArtifactFormat.GMSH_MSH,),
        purpose="the delivered boundaries", required=True),))
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)

    async with open_review_session(spec, ctx) as session:
        assert [c.artifact_key for c in session.prepared_cuts()] == [
            "inspection_regions.across_the_flow.frames.whole",
            "inspection_regions.across_the_flow.frames.zoom"], "whole first, then zoom"
        ev = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                           target_id="region:across_the_flow#zoom"))
        # Through ReviewSession._validate: confined, inside the size band, and MEASURED to have
        # something drawn in it. Addressed BY FRAME: a bare inspect_region now serves the whole
        # plane, which is the region's own question and cannot count cells.
        assert ev.image_ref and ev.covers_target == "region:across_the_flow"
        assert not ev.diagnostics
    assert isinstance(renderer.opened, ResolvedEvidence)
    assert all(isinstance(v, ResolvedArtifact) for v in renderer.opened.values()), (
        "every ITEM of the mapping is still a resolved artifact; the cuts ride beside it")


async def test_a_blank_prepared_frame_is_rejected_like_any_other(tmp_path):
    """review_session._validate measures the pixels. A frame with nothing drawn in it must not
    become an inspection just because it is a prepared one."""
    from PIL import Image

    from meshpipeline.contracts.review_evidence import RenderArtifactRequirement

    # TWO FRAMES, and the blank one is the WHOLE. A region is only offered when some frame of
    # it is measured to contain cells, so a region whose single frame is blank is now declined
    # before a command is ever executed - a better refusal, but not the one under test here.
    # This keeps the countable zoom so the region is genuinely offered, and blanks the frame a
    # bare inspect_region serves, which is the path _validate guards.
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "whole", window=(EXTENT[1], EXTENT[2])),
        _cut(tmp_path, "across_the_flow", "zoom",
             window=(_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
                     _window_for(960, CELL_M, COUNTABLE_CELL_PX)))]})
    blank = tmp_path / "review/cuts/across_the_flow_whole.png"
    Image.new("RGB", (1280, 960), (255, 255, 255)).save(blank, format="PNG")

    spec = _Spec(_Renderer(tmp_path), (RenderArtifactRequirement(
        artifact_key="mesh_paths.surface", allowed_formats=(ArtifactFormat.GMSH_MSH,),
        purpose="p", required=True),))
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)
    async with open_review_session(spec, ctx) as session:
        ev = session.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                           target_id="region:across_the_flow"))
    assert ev.image_ref == "" and not ev.rendered
    assert "blank" in " ".join(ev.diagnostics)
    # The coverage claim survives the demotion as an ATTEMPT (pinned in tests/unit/security),
    # and `rendered` is what keeps it from being counted. Asserted through the ledger rather
    # than off the field, because the ledger is what the floor actually reads.
    from meshpipeline.contracts.review_evidence import EvidenceLedger

    ledger = EvidenceLedger(required_targets=frozenset({"region:across_the_flow"}))
    ledger.record(ev)
    assert ledger.coverage().outstanding == {"region:across_the_flow"}


# 7. build_backend, which is where the extra channel is actually read
@pytest.fixture
def backend_class(monkeypatch):
    """The REAL build_backend over a fake render backend, so its own wiring is under test."""
    made: list = []

    class _B(FakeBackend):
        def __init__(self, inputs=None, metadata=None, save_dir=None, regions=None, **kw):
            super().__init__(save_dir=Path(save_dir) if save_dir else None)
            self.inputs = inputs
            made.append(self)

    import meshpipeline.sandbox.backend as BE
    monkeypatch.setattr(BE, "MeshRenderBackend", _B)
    return made


def _evidence_for(tmp_path, man) -> ResolvedEvidence:
    ev = ResolvedEvidence({"mesh_paths.surface": ResolvedArtifact(
        "mesh_paths.surface", tmp_path / "mesh.msh", ArtifactFormat.GMSH_MSH)})
    ev.cuts = ArtifactResolver(tmp_path, man).resolve_cuts()
    return ev


def test_build_backend_takes_the_cuts_off_the_resolved_mapping(tmp_path, backend_class):
    from meshpipeline.sandbox.render_adapter import build_backend

    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))]})
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)
    _, inputs, _ = build_backend(ctx, _evidence_for(tmp_path, man))
    assert inputs.has_cuts and set(inputs.cuts) == {"across_the_flow"}
    assert inputs.cut_frame("across_the_flow").scope == "zoom"


def test_build_backend_sets_the_cuts_aside_when_the_volume_is_there(tmp_path, backend_class):
    from meshpipeline.sandbox.render_adapter import build_backend

    man = _manifest(tmp_path, volume="internal.vtu", cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))]})
    (tmp_path / "internal.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')
    ev = _evidence_for(tmp_path, man)
    ev["mesh_paths.volume"] = ResolvedArtifact("mesh_paths.volume", tmp_path / "internal.vtu",
                                               ArtifactFormat.VTK_VTU)
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)
    _, inputs, _ = build_backend(ctx, ev)
    assert inputs.has_volume and not inputs.has_cuts


def test_build_backend_drops_a_cut_whose_region_resolved_to_no_plane(tmp_path, backend_class):
    """resolve_regions refuses a region whose plane is unusable (here a zero normal), and a frame
    for one of those would be a target with no plane behind it."""
    from meshpipeline.sandbox.render_adapter import build_backend

    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604))]})
    man["inspection_regions"][0]["normal"] = [0.0, 0.0, 0.0]
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)
    _, inputs, regions = build_backend(ctx, _evidence_for(tmp_path, man))
    assert "across_the_flow" not in regions and not inputs.has_cuts


def test_a_volume_too_large_to_open_is_not_taken_as_a_volume(tmp_path, backend_class, monkeypatch):
    """AN OOM KILL IS WORSE THAN NO EVIDENCE, and that is what reading this file whole would be.

    `backend._load_volume` reads the volume through pv.read into one process and the review
    worker is capped at 16 GiB. Job 0ff6f9e5's fluid interior is 10,366,611,525 bytes; it was
    pruned on the way home against a ceiling computed from the wrong default, and correcting that
    ceiling is what makes this reachable. The file arriving is not an improvement.
    """
    import meshpipeline.sandbox.render_adapter as RA
    from meshpipeline.sandbox.render_adapter import build_backend

    man = _manifest(tmp_path, volume="internal.vtu", cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom",
             window=(_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
                     _window_for(960, CELL_M, COUNTABLE_CELL_PX)))]})
    (tmp_path / "internal.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')
    ev = _evidence_for(tmp_path, man)
    ev["mesh_paths.volume"] = ResolvedArtifact("mesh_paths.volume", tmp_path / "internal.vtu",
                                               ArtifactFormat.VTK_VTU)
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)

    # the real figure, without writing ten gigabytes to a test machine
    monkeypatch.setattr(RA, "VOLUME_READ_CEILING_BYTES", 32)
    _, inputs, _ = build_backend(ctx, ev)
    assert not inputs.has_volume, "a file this size must not be opened"
    assert inputs.has_cuts, "and the frames prepared where the mesh was built take over"
    # under the ceiling, nothing changes
    monkeypatch.setattr(RA, "VOLUME_READ_CEILING_BYTES", 4 * 1024 ** 3)
    _, inputs, _ = build_backend(ctx, ev)
    assert inputs.has_volume and not inputs.has_cuts


def test_build_backend_still_works_for_a_caller_that_hands_a_plain_dict(tmp_path, backend_class):
    """Every engine but one, and every test that builds the mapping itself."""
    from meshpipeline.sandbox.render_adapter import build_backend

    man = _manifest(tmp_path)
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man)
    _, inputs, _ = build_backend(ctx, {"mesh_paths.surface": ResolvedArtifact(
        "mesh_paths.surface", tmp_path / "mesh.msh", ArtifactFormat.GMSH_MSH)})
    assert not inputs.has_cuts and not inputs.has_volume


# 8. the two halves of the contract, joined
def test_the_producers_own_frames_read_back_through_this_reader(tmp_path):
    """THE ONE TEST THAT SPANS THE CONTAINER BOUNDARY.

    cfmesh_conjugate/slices.review_slices AUTHORS these frames on the mesh VM and this module
    READS them at home. Nothing else checks that the two agree, and a rename on either side would
    surface on a live job as "no region offered" - which is indistinguishable from the defect this
    whole change was written to fix. So the producer is called for real here, its frames are given
    the files the VM would have drawn, and they are resolved.

    AND THE ONLY THING ADDED TO WHAT THE PRODUCER RETURNS IS THE PICTURE. `content` is NOT
    fabricated here, which is the correction: this test used to paste a CONTENT_FOUND block onto
    every frame and then assert the frames resolved, so the one field doing the load-bearing work
    was supplied by the test rather than by the producer. The reader must now reach its verdict
    from `review_slices`' own keys plus the PNG, because that is all a replayed or archived
    manifest has.
    """
    from meshpipeline.engines.cfmesh_conjugate import slices as S

    assert COUNTABLE_CELL_PX == S.CELL_PIXELS, (
        "the producer sizes its zoom window to CELL_PIXELS and the reader judges against "
        "COUNTABLE_CELL_PX; if they drift apart every zoom frame is refused by the reader that "
        "asked for it")
    assert (S.FRAME_W, S.FRAME_H) == (1280, 960), "the frame the windows are computed against"

    cuts = S.review_slices(BBOX[:3], BBOX[3:], cell_m=CELL_M)
    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    for c in cuts:
        for f in c["frames"]:
            rel = f"review/cuts/{c['name']}_{f['scope']}.png"
            write_cut_picture(tmp_path / rel, S.FRAME_W, S.FRAME_H)
            f["image"] = rel                       # what the VM's render adds, and nothing else
    man = {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
           "quality": {"near_wall_cell_m": CELL_M}, "inspection_regions": cuts}

    resolved = ArtifactResolver(tmp_path, man).resolve_cuts()
    assert set(resolved) == {"across_the_flow", "through_the_plate", "along_the_flow"}
    for name, frames in resolved.items():
        assert [f.scope for f in frames] == ["whole", "zoom"], name
        whole, zoom = frames
        # the scale the producer published and the scale measured off the drawn file agree
        published = {f["scope"]: f["um_per_pixel"] for f in
                     next(c for c in cuts if c["name"] == name)["frames"]}
        assert round(whole.um_per_px, 4) == published["whole"], name
        assert round(zoom.um_per_px, 4) == published["zoom"], name
        # and the whole-part frame cannot count cells while the zoom can, on this real part
        assert not whole.counts_cells and whole.cell_px < 0.3, name
        assert zoom.counts_cells and round(zoom.cell_px, 2) == 4.00, name
    assert round(resolved["across_the_flow"][0].um_per_px, 2) == 105.42
    assert round(resolved["through_the_plate"][0].um_per_px, 2) == 138.82
    assert round(resolved["along_the_flow"][0].um_per_px, 2) == 138.82


# 9. the claim, read without a filesystem
def test_the_claim_is_readable_without_touching_a_file(tmp_path):
    """engines/base.py decides whether a region obligation stands, in the API process, from the
    manifest alone."""
    man = _manifest(tmp_path, cuts={"across_the_flow": [
        _cut(tmp_path, "across_the_flow", "zoom", window=(0.00805, 0.00604), make=False)]})
    assert manifest_cut_claims(man) == {"across_the_flow": 1}
    assert manifest_cut_claims(_manifest(tmp_path)) == {}
    assert manifest_cut_claims({}) == {}
    assert manifest_cut_claims(None) == {}


def test_the_real_jobs_manifest_reads_and_its_three_cuts_become_offerable(tmp_path):
    """0ff6f9e5's OWN manifest, recorded beside this test: three regions, a pruned volume, no
    cuts. It must read unchanged and offer nothing - the honest "evidence missing" - and the same
    manifest with frames beside its regions must offer all three.
    """
    real = Path(__file__).with_name("job_0ff6f9e5_manifest_regions.json")
    man = json.loads(real.read_text())
    assert [r["name"] for r in man["inspection_regions"]] == [
        "across_the_flow", "through_the_plate", "along_the_flow"]
    assert man["quality"]["near_wall_cell_m"] == CELL_M
    assert manifest_cut_claims(man) == {}
    assert expand_region_targets(resolve_regions(man), required=True, purpose="p",
                                 volume_available=False, cuts={}) == ()

    window = (_window_for(1280, CELL_M, COUNTABLE_CELL_PX),
              _window_for(960, CELL_M, COUNTABLE_CELL_PX))
    for r in man["inspection_regions"]:
        r["frames"] = [_cut(tmp_path, r["name"], "zoom", window=window)]
    cuts = ArtifactResolver(tmp_path, man).resolve_cuts()
    assert set(cuts) == {"across_the_flow", "through_the_plate", "along_the_flow"}
    assert all(c.counts_cells for frames in cuts.values() for c in frames)
    assert {t.target_id for t in expand_region_targets(
        resolve_regions(man), required=True, purpose="p", volume_available=False,
        cuts=cuts)} == {"region:across_the_flow", "region:through_the_plate",
                        "region:along_the_flow"}
