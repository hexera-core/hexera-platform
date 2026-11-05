# Responsibility: Verify a renderer fault in the review costs the review, never the validated mesh.
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from _render_fakes import FakeBackend
from tests.execution_publisher_double import RecordingExecutionPublisher

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.execution_guard import StaleWorkerFenced
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.manifest import write_manifest
from meshpipeline.engines.registry import get_spec
from meshpipeline.sandbox.review_session import MAX_IMAGES_PER_SESSION

PATCHES = ["inlet", "outlet", "wall"]
GEOMETRY_SOURCE = {"source_id": "11111111-1111-4111-8111-111111111111", "owner_id": "o",
                   "object_key": "sources/11111111-1111-4111-8111-111111111111",
                   "sha256": "1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8",
                   "size_bytes": 512, "original_filename": "part.step", "suffix_hint": ".step"}


class _SceneBackend(FakeBackend):
    """FakeBackend plus the scene facts the real session reads, and an optional toggle fault."""
    toggle_raises: BaseException | None = None

    def scene_facts(self):
        return {"has_geometry": True,
                "bounds": {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 1.0,
                           "zmin": 0.0, "zmax": 1.0},
                "mesh_units": "m", "pan_step_mm": 10.0, "zoom_step": 1.5,
                "patch_legend": [(p, "#336699") for p in self.patches], "patch_views": {}}

    def toggle_patch(self, name, visible):
        if self.toggle_raises is not None:
            raise self.toggle_raises
        return super().toggle_patch(name, visible)


def _install_backend(monkeypatch, *, open_raises=None, toggle_raises=None):
    """Only the VTK backend is faked; the real session, adapter and runtime run."""
    import meshpipeline.engines.cfmesh.review_renderer as cfmesh_renderer

    def _build(context, artifacts, *a, **k):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        if open_raises is not None:
            raise open_raises
        Path(context.save_dir).mkdir(parents=True, exist_ok=True)
        be = _SceneBackend(patches=PATCHES, save_dir=Path(context.save_dir))
        be.toggle_raises = toggle_raises
        return be, ResolvedRenderInputs(surface=artifacts[SURFACE_KEY],
                                        volume=artifacts.get(VOLUME_KEY)), \
            resolve_regions(context.manifest)

    monkeypatch.setattr(cfmesh_renderer, "build_backend", _build)


class _ScriptedModel:
    """Replays turns; each turn is a list of (tool, args) or a callable on the messages."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    async def __call__(self, *, messages, tools, job_id="", user_id="", **kw):
        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
        self.calls += 1
        if not self.turns:
            return ModelRoundResult(assistant_text="done", finish_reason="stop")
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        return ModelRoundResult(
            tool_calls=tuple(ToolCallRequest(id=f"c{self.calls}_{i}", name=n,
                                             arguments=json.dumps(a))
                             for i, (n, a) in enumerate(turn)),
            finish_reason="tool_calls")


def _toggle(patch):
    return ("toggle_patch", {"patch_name": patch, "visible": True})


def _submit_citing_views(messages):
    # cite only views that produced a picture - a refused view is not evidence
    text = str(messages)
    ids = sorted(set(re.findall(r"\b[a-z]-\d{3}\b", text)) - {"x-001"})
    axes = derive_assurance_plan(get_spec("cfmesh"), "").axis_names
    return [("submit_findings", {"rebuild_required": False, "reasoning": "ok",
                                 "axis_findings": [{"axis_key": a, "finding": "fine",
                                                    "evidence_ids": ids, "passed": True}
                                                   for a in axes]})]


@pytest.fixture
def state(monkeypatch, tmp_path):
    pub = RecordingExecutionPublisher("job", "reviewer")
    monkeypatch.setattr(visual, "execution_publisher", lambda *a, **k: pub)

    class _NoTrainingLog:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _NoTrainingLog)

    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    manifest = write_manifest(
        tmp_path, patch_types={"inlet": "patch", "outlet": "patch", "wall": "wall"},
        patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
        quality={"cells": 10_000, "max_non_ortho": 40.0, "fatal": []},
        body_bbox=None, volume_path=None, mesh_units="m", mesh_mode="cfmesh")
    return {"job_id": "job", "engine": "cfmesh", "purpose": "",
            "openfoam_workspace": str(tmp_path), "mesh_manifest": manifest, "engine_params": {},
            "retry_count": 0, "review_rerun_count": 0, "geometry_source": GEOMETRY_SOURCE,
            "request_txt": "mesh it", "review_brief_txt": "check it", "user_id": "u",
            "executor_success": True}


def _review(monkeypatch, state, turns):
    model = _ScriptedModel(turns)
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools", model)
    return asyncio.run(visual.node_reviewer(state)), model


@pytest.mark.parametrize("fault", [RuntimeError("vtkEGLRenderWindow: could not initialise EGL"),
                                   MemoryError(), OSError("volume unreadable")])
def test_a_raw_fault_opening_the_renderer_is_a_render_failure(monkeypatch, state, fault):
    _install_backend(monkeypatch, open_raises=fault)
    out, _ = _review(monkeypatch, state, [])
    assert out["api_failure"] == "reviewer_render_unavailable"
    assert not out.get("reviewer_verdict")


def test_a_raw_fault_during_a_viewer_tool_is_a_render_failure(monkeypatch, state):
    _install_backend(monkeypatch, toggle_raises=RuntimeError("vtkOpenGLRenderWindow lost context"))
    out, _ = _review(monkeypatch, state, [[_toggle("wall")]])
    assert out["api_failure"] == "reviewer_render_unavailable"
    assert not out.get("reviewer_verdict")


@pytest.mark.parametrize("where", ["open", "tool"])
def test_a_lost_claim_still_propagates(monkeypatch, state, where):
    fenced = StaleWorkerFenced("render", "job", 1)
    if where == "open":
        _install_backend(monkeypatch, open_raises=fenced)
    else:
        _install_backend(monkeypatch, toggle_raises=fenced)
    with pytest.raises(StaleWorkerFenced):
        _review(monkeypatch, state, [[_toggle("wall")]])


def test_an_interrupt_is_never_turned_into_a_render_failure(monkeypatch, state):
    _install_backend(monkeypatch, open_raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _review(monkeypatch, state, [])


def test_the_view_past_the_image_cap_is_refused_and_the_review_still_concludes(monkeypatch,
                                                                                 state):
    _install_backend(monkeypatch)
    # the opening view counts, so this is one view more than the session allows
    views = [_toggle(PATCHES[i % 3]) for i in range(MAX_IMAGES_PER_SESSION)]
    turns = [views[i:i + 2] for i in range(0, len(views), 2)]
    out, model = _review(monkeypatch, state, [*turns, _submit_citing_views])
    assert out.get("api_failure") is None
    assert out["reviewer_verdict"] == "PASS"
    assert model.calls == len(turns) + 1


def test_the_refused_view_tells_the_model_to_submit(monkeypatch, state):
    _install_backend(monkeypatch)
    seen = {}

    def _capture(messages):
        seen["last"] = str(messages[-1])
        return _submit_citing_views(messages)
    views = [_toggle(PATCHES[i % 3]) for i in range(MAX_IMAGES_PER_SESSION)]
    turns = [views[i:i + 2] for i in range(0, len(views), 2)]
    out, _ = _review(monkeypatch, state, [*turns, _capture])
    assert "view budget" in seen["last"] and "submit your findings" in seen["last"]
    assert out["reviewer_verdict"] == "PASS"
