# Responsibility: Verify the reviewer is told what its interior evidence IS, and what each frame cannot answer.
# Boundaries: the words and the tool surface; the resolution and the coverage rule are the sandbox's.
#
# THE SENTENCE THIS FILE EXISTS FOR. When the interior is absent but cuts are present, the
# reviewer must either judge the mesh from the cuts AND SAY WHAT IT JUDGED IT FROM, or decline.
# It must never judge silently from less evidence than it thinks it has. Job 575f76e6 is what the
# other outcome looks like: eight blank frames filed as eight interior inspections, verdict
# passed, concerns none.
from __future__ import annotations

from pathlib import Path

import pytest
from _render_fakes import FakeBackend  # noqa: E402  (tests/unit is on sys.path via conftest)
from _reviewer_passage_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_cut_picture,
)

from meshpipeline.agents.reviewer.render_runtime import open_runtime
from meshpipeline.agents.reviewer.scene_text import (
    CANNOT_COUNT,
    cut_frame_line,
    prepared_cuts_block,
)
from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS
from meshpipeline.contracts.review_evidence import RenderContext
from meshpipeline.sandbox.review_session import COUNTABLE_CELL_PX, ArtifactResolver

ENGINE = "cfmesh_conjugate"

#: Job 0ff6f9e5's own figures.
CELL_M = 2.51578e-05
FEATURE_M = 0.0001006312
#: The across_the_flow plane of that part, 41.826 x 101.207 mm, and the window that resolves it.
WHOLE = (0.041826298460364345, 0.10120718833059072)
CLOSE = (1280 * CELL_M / COUNTABLE_CELL_PX, 960 * CELL_M / COUNTABLE_CELL_PX)

REGIONS = [
    {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
     "origin": [0.027, 0.041, 0.042], "label": "Square across the coolant's path",
     "why": "the cells across each passage, and the metal between them"},
]


#: `frames[].content` as cfmesh_conjugate/slices.ink_structure publishes it: the producer reads
#: its own drawn PNG back and says whether the PERIODIC PASSAGE ARRAY is in it. It is carried here
#: because a real manifest carries it, and NOT because the reader needs it: the reader takes the
#: same measurement on its own copy of the PNG (sandbox/picture_structure), so a claim here may
#: VETO a frame and may not grant one. The frames below are drawn to hold a real passage array,
#: which is what makes them countable.
CONTENT_FOUND = {"shows_structure": True, "repeats": 40, "period_px": 32.0, "spread": 0.04,
                 "ink_fraction": 0.251}


def _frames(ws: Path, *, whole=WHOLE, zoom=CLOSE) -> list[dict]:
    """The frames as cfmesh_conjugate/slices.review_slices publishes them, with files drawn."""
    out = []
    for scope, window in (("whole", whole), ("zoom", zoom)):
        if window is None:
            continue
        write_cut_picture(ws / f"review/cuts/across_{scope}.png")
        out.append({"scope": scope, "image": f"review/cuts/across_{scope}.png",
                    "window_m": [window[0], window[1]], "feature_m": FEATURE_M,
                    "content": dict(CONTENT_FOUND),
                    "metres_per_pixel": max(window[0] / 1280, window[1] / 960)})
    return out


def _manifest(ws: Path, *, cuts=None, volume=False) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    paths = {"surface": "mesh.msh"}
    if volume:
        (ws / "internal.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')
        paths["volume"] = "internal.vtu"
    regions = [dict(REGIONS[0])]
    if cuts is not None:
        regions[0]["frames"] = cuts
    return {"mesh_units": "m", "mesh_paths": paths,
            "quality": {"near_wall_cell_m": CELL_M},
            "patches": {"fluid/fluid_to_solid": {}},
            "inspection_regions": regions}


@pytest.fixture
def conjugate_backend(monkeypatch):
    """The conjugate renderer over the shared fake, taking its cuts the way production does."""
    made: list[FakeBackend] = []

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        volume = artifacts.get(VOLUME_KEY)
        be = FakeBackend(save_dir=Path(context.save_dir))
        made.append(be)
        return (be,
                ResolvedRenderInputs(surface=artifacts[SURFACE_KEY], volume=volume,
                                     cuts={} if volume else getattr(artifacts, "cuts", {})),
                resolve_regions(context.manifest))

    mod = __import__(f"meshpipeline.engines.{ENGINE}.review_renderer", fromlist=["x"])
    monkeypatch.setattr(mod, "build_backend", _build)
    return made


@pytest.fixture
def scene_facts():
    def _facts(self):
        return {"has_geometry": bool(self.patches),
                "bounds": {"xmin": 0.0, "xmax": 0.1, "ymin": 0.0, "ymax": 0.04,
                           "zmin": 0.0, "zmax": 0.1},
                "mesh_units": "m", "pan_step_mm": self.pan_step, "zoom_step": self.zoom_step,
                "patch_legend": [(n, "#ff0000") for n in self.patches]}
    FakeBackend.scene_facts = _facts
    yield
    del FakeBackend.scene_facts


def _runtime(tmp_path, man):
    from meshpipeline.engines.registry import ENGINE_CATALOG
    return open_runtime(ENGINE_CATALOG[ENGINE], RenderContext(
        workspace=str(tmp_path), save_dir=str(tmp_path), manifest=man))


def _text(result) -> str:
    assert isinstance(result, list), f"expected a rendered view, got: {result}"
    return " ".join(p["text"] for p in result if p.get("type") == "text")


def _images(result) -> int:
    return sum(1 for p in result if p.get("type") == "image_url")


# 1. the reviewer asks, and is handed a frame rather than a renderer fault
async def test_inspect_region_returns_the_prepared_frame_with_its_window(
        tmp_path, conjugate_backend, scene_facts):
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        bare = await rt.execute_viewer_tool("inspect_region",
                                            {"region_name": "across_the_flow"})
        close = await rt.execute_viewer_tool("inspect_region",
                                             {"region_name": "across_the_flow",
                                              "scope": "zoom"})
    # CHANGED. A bare inspect_region used to hand back the ZOOM. That zoom is 8.05 x 6.04 mm of
    # a plane 41.83 x 101.2 mm across, and on this part's other two cuts it is 0.27% of the
    # plane - so a caller that named the region was being answered with a magnified corner of it
    # and could not tell, because both come back as one picture of one region. The bare call now
    # serves the plane the region names and says where the close frame is.
    plane = _text(bare)
    assert _images(bare) == 1
    assert "covers 41.83 x 101.2 mm of the plane" in plane
    assert "does NOT count as an inspection" in plane
    assert "scope='zoom'" in plane, "the way out has to be named, or it is never reached"

    text = _text(close)
    assert _images(close) == 1
    # the window, the resolution, and how many pixels across a cell - every time
    assert "covers 8.05 x 6.038 mm of the plane" in text
    assert "6.289 um per pixel" in text
    assert "a cell of the size the PLAN pinned (25.16 um) would be 4.00 px across here" in text
    assert "judged on" in text


async def test_the_frame_is_never_described_as_the_volume(
        tmp_path, conjugate_backend, scene_facts):
    """The sentence this path used to say - "what is drawn IS the interior" - is true of a live
    cut and misleading about a fixed frame: it invites a cell count from a picture where a
    channel can be under a pixel wide."""
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        text = _text(await rt.execute_viewer_tool("inspect_region",
                                                  {"region_name": "across_the_flow"}))
    assert "what is drawn IS the interior" not in text
    assert "PICTURE OF ONE PLANE" in text
    assert "camera cannot be moved in it" in text


async def test_the_context_frame_says_it_is_not_the_inspection(
        tmp_path, conjugate_backend, scene_facts):
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        text = _text(await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "across_the_flow", "scope": "whole"}))
    assert "does NOT count as an inspection" in text
    assert CANNOT_COUNT in text, "at 105 um/px a cell is a quarter of a pixel"
    assert "105.4 um per pixel" in text


async def test_a_frame_the_mesh_does_not_have_is_a_correction_not_a_refusal(
        tmp_path, conjugate_backend, scene_facts):
    """A correction the reviewer can act on. A bare refusal costs a round and teaches nothing -
    job 53bbce4b spent three rounds that way and ended with no verdict."""
    man = _manifest(tmp_path, cuts=_frames(tmp_path, whole=None))
    async with _runtime(tmp_path, man) as rt:
        out = await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "across_the_flow", "scope": "whole"})
    assert isinstance(out, str)
    assert "'zoom'" in out and "no 'whole' frame" in out


async def test_a_live_volume_job_says_there_are_no_frames_to_choose_between(
        tmp_path, conjugate_backend, scene_facts):
    """CHANGED. A scope named on a live cut used to be REFUSED here, and the refusal is what
    stalled every review on 2026-11-23: jobs 74bc3e2e and 41fb5cb3 named one nine times and
    never got the interior evidence their own gate required. The thing this test is for - that
    the reviewer can tell a live cut from a chosen frame - is unchanged, and is now carried by
    the words of the reply rather than by withholding the picture."""
    man = _manifest(tmp_path, volume=True, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        scoped = await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "across_the_flow", "scope": "zoom"})
        # and the ordinary call still slices the volume, worded as it always was
        text = _text(await rt.execute_viewer_tool("inspect_region",
                                                  {"region_name": "across_the_flow"}))
    scoped_text = _text(scoped)
    assert _images(scoped) == 1, "the scope must not cost the reviewer its interior evidence"
    assert "does not apply here and was not used" in scoped_text
    assert "no prepared frames to choose between" in scoped_text
    assert "Internal slice through the VOLUME mesh" in text


async def test_the_opening_facts_carry_the_frames_before_a_round_is_spent(
        tmp_path, conjugate_backend, scene_facts):
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        opening = await rt.initial_context()
    assert [c.scope for c in opening.prepared_cuts] == ["whole", "zoom"]
    assert {t.target_id for t in opening.inspection_targets} >= {"region:across_the_flow"}


async def test_a_job_with_no_volume_and_no_cuts_offers_no_region_at_all(
        tmp_path, conjugate_backend, scene_facts):
    """Job 0ff6f9e5 exactly. The honest state, which must stay honest."""
    man = _manifest(tmp_path)
    async with _runtime(tmp_path, man) as rt:
        opening = await rt.initial_context()
        assert opening.prepared_cuts == ()
        assert await rt.region_names() == []
        out = await rt.execute_viewer_tool("inspect_region",
                                           {"region_name": "across_the_flow"})
    assert isinstance(out, str) and "No inspection region named" in out


# 2. the words themselves
def test_a_frame_line_states_the_window_the_resolution_and_the_cell(tmp_path):
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    frames = ArtifactResolver(tmp_path, man).resolve_cuts()["across_the_flow"]
    ctx_line, det_line = (cut_frame_line(f) for f in frames)
    assert "covers 41.83 x 101.2 mm of the plane" in ctx_line
    assert "drawn 1280x960 px at 105.4 um per pixel" in ctx_line
    # CHANGED, and this is the correction item 8 asked for. "the mesh's own 25.16 um cell"
    # claimed a measurement of the mesh for `quality.near_wall_cell_m`, which finalize.py
    # derives from the PLAN. This mesh's cells span 7.787 to 678.88 um, a factor of 87.
    assert "a cell of the size the PLAN pinned (25.16 um) would be 0.24 px across here" in ctx_line
    assert "100.6 um feature this window was sized from is 0.95 px across" in ctx_line
    assert CANNOT_COUNT in ctx_line
    assert "cells are countable in this frame" in det_line
    assert "a cell of the size the PLAN pinned (25.16 um) would be 4.00 px across here" in det_line
    # AND THE TWO MEASUREMENTS, which are the only facts here that can come out either way.
    assert "MEASURED IN THE PICTURE ITSELF" in det_line
    assert "cell(s) are resolved on its best row" in det_line
    # RE-WORDED IN ROUND 5: the line names the array apart from any periodic structure at all,
    # because "the PASSAGE ARRAY IS IN THIS FRAME" was being said over a cell lattice. AND AGAIN
    # IN ROUND 7: it no longer says the array is this PART'S, which nothing measured here can say.
    assert ("A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME as "
            "measured here") in det_line


def test_the_block_tells_the_reviewer_it_cannot_re_aim_a_frame(tmp_path):
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    frames = ArtifactResolver(tmp_path, man).resolve_cuts()["across_the_flow"]
    block = prepared_cuts_block(frames)
    assert "INTERNAL CUTS (ALREADY TAKEN" in block
    assert "PICTURE OF ONE PLANE, not the volume itself" in block
    assert "cannot move the camera inside one of these frames" in block
    assert "Square across the coolant's path" in block
    assert "the cells across each passage" in block
    assert "state which frame each finding came from" in block
    # AND WHERE TO GO FOR THE ONE THAT COUNTS. A bare inspect_region now serves the whole plane,
    # so the block has to name the close frame or the reviewer never reaches it.
    assert 'scope="zoom"' in block


async def test_a_region_whose_only_frame_is_sub_pixel_is_not_offered_at_all(
        tmp_path, conjugate_backend, scene_facts):
    """THE DECLINE, AND WHY IT IS A DECLINE RATHER THAN A WARNING.

    A frame a cell is not countable in cannot discharge the region's inspection, so offering the
    region would put the review in a loop: eligibility tells the reviewer "it IS available -
    inspect it now", the only frame there never covers, and the rounds run out. Not offering it
    makes visual.py's expected-target pre-check fire, which ends the review honestly before the
    model is asked anything.
    """
    man = _manifest(tmp_path, cuts=_frames(tmp_path, zoom=None))
    async with _runtime(tmp_path, man) as rt:
        assert await rt.region_names() == []
        assert (await rt.initial_context()).prepared_cuts == ()


def test_the_prompt_says_which_of_the_two_reasons_nothing_can_be_shown(tmp_path):
    """"No cut came home" and "a cut came home that nothing can be counted in" lead a reader to
    different repairs, so they are said differently."""
    nothing = _prompt(_manifest(tmp_path, volume=True), inspectable_regions=[])
    assert "no cut of it was prepared" in nothing

    man = _manifest(tmp_path, cuts=_frames(tmp_path, zoom=None))
    unusable = _prompt(man, inspectable_regions=[])
    # CHANGED: three different things can refuse a frame now - the window too coarse for a
    # cell, the side that drew it finding no passage array in it, and this side counting no
    # cells in the drawn pixels. "not one of them resolves a single cell" named only the third.
    assert ("1 cut frame(s) were prepared and not one of them was measured to contain the cells "
            "it was cut to show") in unusable
    assert "Do not call inspect_region" in unusable


def test_the_block_carries_no_unreachable_warning(tmp_path):
    """The "none of these resolves anything" sentence used to live in the block, where it could
    never be reached: such a region is not offered, so its frames never get here."""
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    frames = ArtifactResolver(tmp_path, man).resolve_cuts()["across_the_flow"]
    assert "NO frame here" not in prepared_cuts_block(frames)


def test_a_frame_may_also_name_its_file_as_path(tmp_path):
    """The producer writes `image`; `path` is the obvious alternate and is accepted, so a
    hand-written manifest reads too."""
    frames = _frames(tmp_path)
    frames[1]["path"] = frames[1].pop("image")
    man = _manifest(tmp_path, cuts=frames)
    resolved = ArtifactResolver(tmp_path, man).resolve_cuts()["across_the_flow"]
    assert [f.scope for f in resolved] == ["whole", "zoom"]


def test_no_prepared_cuts_means_no_block():
    assert prepared_cuts_block(()) == ""


# 3. the opening prompt, which is where the reviewer learns what it has
def _prompt(man, **kw):
    from meshpipeline.agents.reviewer.context import build_review_prompt
    _, context = build_review_prompt(
        manifest=man, nav_context={"mesh_units": "m"}, workspace=Path("/tmp"),
        step_basename="plate.step", patch_names=list(man.get("patches") or {}),
        patch_colour_legend="", mesh_units="m", request="r", review_brief="b",
        job_id="t", engine=ENGINE, purpose="", **kw)
    return context


def test_the_prompt_stops_telling_the_reviewer_to_slice_a_volume_that_is_not_here(tmp_path):
    """What job 0ff6f9e5's reviewer was told. The manifest CLAIMED a volume - the 10.4 GB file
    had been pruned on the way home - so the prompt named three regions and told it to "cut the
    VOLUME mesh", while INSPECT_TARGET was not even an offered operation."""
    man = _manifest(tmp_path, volume=True)         # the claim, with nothing behind it
    context = _prompt(man, inspectable_regions=[])
    assert "cut the VOLUME mesh" not in context
    assert "NONE of them can be shown to you" in context
    assert "Do not call inspect_region" in context
    assert "say that in your findings rather than passing or failing it" in context


def test_the_prompt_describes_prepared_frames_when_that_is_what_came_home(tmp_path):
    man = _manifest(tmp_path, cuts=_frames(tmp_path))
    frames = ArtifactResolver(tmp_path, man).resolve_cuts()["across_the_flow"]
    context = _prompt(man, inspectable_regions=["across_the_flow"], prepared_cuts=frames)
    assert "INTERNAL CUTS (ALREADY TAKEN" in context
    assert "INTERNAL SLICES" not in context
    assert "8.05 x 6.038 mm of the plane" in context


def test_a_live_volume_prompt_is_unchanged(tmp_path):
    man = _manifest(tmp_path, volume=True)
    context = _prompt(man, inspectable_regions=["across_the_flow"])
    assert "INTERNAL SLICES (call inspect_region(region_name) to cut the VOLUME mesh" in context
    assert "across_the_flow" in context


def test_a_caller_that_names_no_session_keeps_the_text_it_always_had(tmp_path):
    man = _manifest(tmp_path, volume=True)
    assert "cut the VOLUME mesh" in _prompt(man)


# 4. the tool the reviewer actually calls
def test_the_inspect_region_tool_offers_the_frame_and_explains_both_kinds():
    tool = next(t["function"] for t in REVIEWER_TOOLS
                if t["function"]["name"] == "inspect_region")
    props = tool["parameters"]["properties"]
    assert props["scope"]["enum"] == ["whole", "zoom"]
    assert tool["parameters"]["required"] == ["region_name"], (
        "a live-volume mesh has no frames, so the scope must stay optional")
    assert "ONE PICTURE OF ONE PLANE" in tool["description"]
    assert "how many pixels across a cell" in tool["description"]
