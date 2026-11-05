# Responsibility: Verify the visual surface is two methods over closed inputs, satisfied by the runtime directly.
from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from meshpipeline.agents.reviewer.interaction_inputs import VisualReviewInteractionInputs
from meshpipeline.agents.reviewer.render_runtime import ReviewerRenderRuntime
from meshpipeline.agents.reviewer.visual_surface import ReviewerVisualSurface


# the protocol is exactly two methods
def test_the_surface_has_exactly_two_methods():
    methods = {n for n in dir(ReviewerVisualSurface) if not n.startswith("_")}
    assert methods == {"initial_context", "execute_viewer_tool"}, methods


def test_the_runtime_satisfies_the_surface_directly():
    for name in ("initial_context", "execute_viewer_tool"):
        assert hasattr(ReviewerRenderRuntime, name)
        assert inspect.iscoroutinefunction(getattr(ReviewerRenderRuntime, name))


# patch_names is the fourth opening fact
async def test_initial_context_supplies_patch_names(monkeypatch, tmp_path):
    from _render_fakes import PATCHES, REGIONS, FakeBackend

    from meshpipeline.agents.reviewer.render_runtime import open_runtime
    from meshpipeline.contracts.review_evidence import RenderContext
    from meshpipeline.engines.registry import ENGINE_CATALOG

    def _facts(self):
        return {"has_geometry": True, "bounds": {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0,
                "ymax": 1.0, "zmin": 0.0, "zmax": 1.0}, "mesh_units": "m",
                "pan_step_mm": self.pan_step, "zoom_step": self.zoom_step,
                "patch_legend": [(n, "#ff0000") for n in self.patches]}
    monkeypatch.setattr(FakeBackend, "scene_facts", _facts, raising=False)

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        return (FakeBackend(save_dir=Path(context.save_dir)),
                ResolvedRenderInputs(surface=artifacts[SURFACE_KEY],
                                     volume=artifacts.get(VOLUME_KEY)),
                resolve_regions(context.manifest))
    mod = __import__("meshpipeline.engines.snappy.review_renderer", fromlist=["x"])
    monkeypatch.setattr(mod, "build_backend", _build)

    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    (tmp_path / "i.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest={
        "mesh_paths": {"surface": str(tmp_path / "mesh.msh"), "volume": "i.vtu"},
        "mesh_units": "m",
        "inspection_regions": REGIONS})

    async with open_runtime(ENGINE_CATALOG["snappy"], ctx) as rt:
        c = await rt.initial_context()
    import dataclasses

    from meshpipeline.agents.reviewer.visual_surface import OpeningContext
    assert isinstance(c, OpeningContext)
    # `patch_views` joins the opening facts: renderer-derived camera framing, computed from
    # the geometry the renderer loaded. The manifest supplies no framing at all.
    assert {f.name for f in dataclasses.fields(OpeningContext)} == {
        "nav_context", "patch_colour_legend", "initial_screenshot_b64",
        "has_geometry", "patch_names", "inspection_targets", "patch_views"}
    assert c.patch_names == PATCHES, "the opening facts cannot supply the coverage floor"


# the typed input record
def test_the_inputs_are_closed_and_carry_no_renderer():
    fields = {f.name: f.type for f in dataclasses.fields(VisualReviewInteractionInputs)}
    for banned in ("sandbox", "executor", "pool", "runtime", "renderer", "backend", "surface",
                   "session", "lane"):
        assert banned not in fields, f"the inputs carry {banned}"


def test_the_inputs_have_no_dependency_bag():
    for f in dataclasses.fields(VisualReviewInteractionInputs):
        t = str(f.type)
        # `manifest` is the reviewer's own read of build output; `user_dispute` is the typed
        # optional dispute payload the user submitted. Neither is a dependency bag.
        if f.name in ("manifest", "user_dispute"):
            continue
        assert "dict" not in t.lower(), f"{f.name} is an untyped dependency bag"


def _inputs(publish):
    return VisualReviewInteractionInputs(
        job_id="j", step_basename="s.stl", retry_count=0, workspace=Path("/w"),
        manifest={}, review_save_dir=Path("/w/r"), mesh_units="m", review_brief="b",
        request="r", axis_names=[], engine="snappy", purpose="external_cfd", user_id="u",
        publish=publish)


def test_publish_accepts_the_real_ownership_checked_publisher():
    # THE publisher production hands the reviewer. It is the whole point of the type: the review
    # publishes execution-owned events, so it must hold the execution-scoped port.
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher

    gated = OwnershipCheckedPublisher(JobPublisher("j", "reviewer"))
    assert _inputs(gated).publish is gated


def test_publish_is_the_live_publisher_not_copied_state():
    for snapshot in ("not-a-publisher", {"warn": "sent"}, None):
        with pytest.raises(TypeError, match="live execution publisher|must publish through"):
            _inputs(snapshot)


def test_publish_rejects_a_synchronous_only_publisher():
    # The plain adapter publishes intake, terminal and maintenance events. It is a real
    # publisher - just not this port, and its synchronous spellings would reach Redis without
    # the ownership check the awaited ones exist to make.
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    with pytest.raises(TypeError, match="cannot await"):
        _inputs(JobPublisher("j", "reviewer"))


def test_the_ownership_checked_publisher_still_has_no_synchronous_warn():
    # If this ever becomes true, the gate has grown an unowned fallback - which is the thing the
    # awaited-only spelling exists to prevent. The reviewer's guard must never depend on it again.
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher

    gated = OwnershipCheckedPublisher(JobPublisher("j", "reviewer"))
    for sync_spelling in ("warn", "note", "stage", "screenshot", "verdict"):
        assert not hasattr(gated, sync_spelling), (
            f"the ownership-checked publisher grew a synchronous {sync_spelling}()")


def test_the_committed_double_would_not_have_hidden_the_obsolete_guard():
    # The REGRESSION for how this defect survived: a double whose `__getattr__` answers to any
    # name reports that `warn` exists, so the obsolete guard passed in every unit test while the
    # real publisher crashed every run. The committed double answers only for the declared port.
    from tests.execution_publisher_double import RecordingExecutionPublisher

    double = RecordingExecutionPublisher("j", "reviewer")
    obsolete_guard = hasattr(double, "warn")
    assert obsolete_guard is False, (
        "the committed double reports a synchronous `warn`, so the obsolete guard would pass "
        "against it exactly as it did against an unrestricted __getattr__ double")

    class _AnswersToAnything:
        def __getattr__(self, _name):
            async def _any(*_a, **_k):
                return None
            return _any

    assert hasattr(_AnswersToAnything(), "warn") is True, (
        "the double this regression exists to rule out no longer behaves as it did")
    assert _inputs(double).publish is double


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
