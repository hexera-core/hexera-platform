# Responsibility: Verify a scope named on a LIVE cut still produces the cut, and says the scope
# did not apply.
# Boundaries: the tool surface and its words; which frames exist is the sandbox's.
#
# THE SENTENCE THIS FILE EXISTS FOR. The reviewer may not be sent away from the one piece of
# evidence its own eligibility gate requires. On 2026-11-23 it was: jobs 74bc3e2e and 41fb5cb3
# each called inspect_region('model_midplane') with a scope nine times, were refused nine times
# with "call it without a scope", never inspected the interior, and ended reviewer_stalled on
# meshes that had passed every gate. The scope parameter's own description is why the model keeps
# naming one - it calls 'zoom' "the only frame an inspection can be judged on" - so the refusal
# and the instruction cannot both be obeyed. The cut is taken either way; the scope is ignored
# either way; only the words change.
from __future__ import annotations

from pathlib import Path

import pytest
from _render_fakes import FakeBackend  # noqa: E402  (tests/unit is on sys.path via conftest)
from _reviewer_passage_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_cut_picture,
)

from meshpipeline.agents.reviewer.render_runtime import open_runtime
from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS
from meshpipeline.contracts.review_evidence import RenderContext
from meshpipeline.sandbox.review_session import COUNTABLE_CELL_PX

ENGINE = "cfmesh_conjugate"

CELL_M = 2.51578e-05
FEATURE_M = 0.0001006312
WHOLE = (0.041826298460364345, 0.10120718833059072)
CLOSE = (1280 * CELL_M / COUNTABLE_CELL_PX, 960 * CELL_M / COUNTABLE_CELL_PX)
CONTENT_FOUND = {"shows_structure": True, "repeats": 40, "period_px": 32.0, "spread": 0.04,
                 "ink_fraction": 0.251}

REGIONS = [
    {"name": "model_midplane", "kind": "slice", "normal": [1.0, 0.0, 0.0],
     "origin": [0.027, 0.041, 0.042], "label": "Square across the coolant's path",
     "why": "the cells across each passage, and the metal between them"},
]


def _frames(ws: Path) -> list[dict]:
    out = []
    for scope, window in (("whole", WHOLE), ("zoom", CLOSE)):
        write_cut_picture(ws / f"review/cuts/mid_{scope}.png")
        out.append({"scope": scope, "image": f"review/cuts/mid_{scope}.png",
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
    assert isinstance(result, list), f"expected a rendered view, got a refusal: {result!r}"
    return " ".join(p["text"] for p in result if p.get("type") == "text")


def _images(result) -> int:
    return sum(1 for p in result if p.get("type") == "image_url")


# ---- the defect itself -------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["zoom", "whole"])
async def test_a_scope_on_a_live_cut_still_produces_the_cut(
        tmp_path, conjugate_backend, scene_facts, scope):
    """The reviewer's nine refused rounds. Either scope must come back as a picture."""
    man = _manifest(tmp_path, volume=True, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        out = await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "model_midplane", "scope": scope})
    assert _images(out) == 1, "a named scope must not cost the reviewer its interior evidence"
    assert "Internal slice through the VOLUME mesh" in _text(out)


async def test_the_live_cut_says_the_scope_was_not_applied(
        tmp_path, conjugate_backend, scene_facts):
    """The honesty the refusal was for: the reviewer must not think it chose a frame."""
    man = _manifest(tmp_path, volume=True, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        text = _text(await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "model_midplane", "scope": "zoom"}))
    assert "does not apply here and was not used" in text
    assert "no prepared frames to choose between" in text
    assert "IS your inspection" in text


async def test_the_scoped_and_bare_live_cuts_are_the_same_picture(
        tmp_path, conjugate_backend, scene_facts):
    """Which is why refusing the scoped one bought nothing: the command ignores the scope."""
    man = _manifest(tmp_path, volume=True, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        bare = await rt.execute_viewer_tool("inspect_region", {"region_name": "model_midplane"})
        scoped = await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "model_midplane", "scope": "zoom"})
    assert "Internal slice through the VOLUME mesh" in _text(bare)
    assert "Internal slice through the VOLUME mesh" in _text(scoped)
    assert "does not apply here" not in _text(bare), "a bare call has no scope to disclaim"


async def test_a_bare_live_cut_is_unchanged(tmp_path, conjugate_backend, scene_facts):
    """The control. A call with no scope behaved correctly before and must still."""
    man = _manifest(tmp_path, volume=True, cuts=_frames(tmp_path))
    async with _runtime(tmp_path, man) as rt:
        text = _text(await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "model_midplane"}))
    assert "what is drawn IS the interior" in text
    assert "does not apply" not in text


# ---- the other half: the instruction that makes the model name a scope ---------------------

def test_the_scope_description_says_its_rules_are_about_a_prepared_cut():
    """'zoom is the only frame an inspection can be judged on' is true of a PREPARED cut only.
    Said flatly, it is why the model named a scope on a live cut nine times running."""
    tool = next(t for t in REVIEWER_TOOLS
                if (t.get("function") or {}).get("name") == "inspect_region")
    desc = tool["function"]["parameters"]["properties"]["scope"]["description"]
    assert "refuses this" not in desc, "the tool no longer refuses a scope on a live cut"
    assert "the scope is ignored" in desc
    assert "IS the inspection" in desc


def test_scope_is_still_optional():
    """The control on the schema: nothing here may make scope mandatory."""
    tool = next(t for t in REVIEWER_TOOLS
                if (t.get("function") or {}).get("name") == "inspect_region")
    assert tool["function"]["parameters"]["required"] == ["region_name"]


# ---- a prepared cut is untouched -----------------------------------------------------------

async def test_a_prepared_cut_still_screens_a_frame_it_does_not_have(
        tmp_path, conjugate_backend, scene_facts):
    """The control on the guard that stays: with real frames, an unknown one is still corrected."""
    man = _manifest(tmp_path, cuts=[_frames(tmp_path)[1]])  # 'zoom' only
    async with _runtime(tmp_path, man) as rt:
        out = await rt.execute_viewer_tool(
            "inspect_region", {"region_name": "model_midplane", "scope": "whole"})
    assert isinstance(out, str)
    assert "no 'whole' frame" in out and "'zoom'" in out
