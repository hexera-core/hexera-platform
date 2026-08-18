# Responsibility: Verify the runtime opens one session per review, answers the viewer tools, and closes on every exit.
# Boundaries: the renderer comes from the engine spec, and no filesystem path ever reaches the model.
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

# The strengthened fake and its per-operation image sentinels live with the adapter tests, and
# pytest only puts a test file's OWN directory on sys.path - hence the explicit reach. Reusing it
# is deliberate: a second fake is a second thing to keep honest, and the last one that drifted
# quietly agreed with two real defects for a whole commit.
from _render_fakes import (  # noqa: E402, F401  (fixtures)
    PATCHES,
    PNG,
    PNG_CROP,
    PNG_SLICE,
    REGIONS,
    FakeBackend,
)

from meshpipeline.agents.reviewer.render_runtime import (
    NOT_VIEWER_TOOL,
    VERDICT_TOOLS,
    VIEWER_CONFIGURE_TOOLS,
    VIEWER_RENDERING_TOOLS,
    ReviewerRenderRuntime,
    open_runtime,
)
from meshpipeline.contracts.review_evidence import RenderContext

ENGINE = "snappy"


def _ctx(tmp_path, *, volume=True):
    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    paths = {"surface": str(tmp_path / "mesh.msh")}
    if volume:
        (tmp_path / "i.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')
        paths["volume"] = "i.vtu"
    return RenderContext(
        workspace=str(tmp_path), save_dir=str(tmp_path),
        manifest={"mesh_paths": paths, "mesh_units": "m",
                  "inspection_regions": REGIONS})


@pytest.fixture
def fake_backend(monkeypatch):
    made: list = []

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        be = FakeBackend(save_dir=Path(context.save_dir))
        made.append(be)
        return (be, ResolvedRenderInputs(surface=artifacts[SURFACE_KEY],
                                         volume=artifacts.get(VOLUME_KEY)),
                resolve_regions(context.manifest))

    for engine in ("snappy", "cfmesh", "snappy_multiregion"):
        mod = __import__(f"meshpipeline.engines.{engine}.review_renderer", fromlist=["x"])
        monkeypatch.setattr(mod, "build_backend", _build)
    return made


@pytest.fixture
def runtime(fake_backend, tmp_path, scene_facts):
    from meshpipeline.engines.registry import ENGINE_CATALOG

    async def _make(volume=True, path=None):
        return open_runtime(ENGINE_CATALOG[ENGINE], _ctx(path or tmp_path, volume=volume))
    return _make


@pytest.fixture
def scene_facts():
    def _facts(self):
        return {"has_geometry": bool(self.patches),
                "bounds": {"xmin": 0.0, "xmax": 100.0, "ymin": -5.0, "ymax": 5.0,
                           "zmin": 0.0, "zmax": 20.0},
                "mesh_units": "m", "pan_step_mm": self.pan_step, "zoom_step": self.zoom_step,
                "patch_legend": [(n, "#ff0000") for n in self.patches]}
    FakeBackend.scene_facts = _facts
    yield
    del FakeBackend.scene_facts


async def _open(runtime, **kw):
    return await runtime(**kw)


# 1-5: ownership and the session
async def test_the_renderer_comes_only_from_the_engine_spec(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        assert (await rt.capabilities()).targets


async def test_one_runtime_opens_exactly_one_session(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        for fn, args in [("set_camera_preset", {"preset": "top"}),
                         ("zoom", {"factor": 2.0}), ("reset_view", {})]:
            await rt.execute_viewer_tool(fn, args)
    assert len(fake_backend) == 1, f"{len(fake_backend)} backends - a session per tool call"


async def test_all_ten_viewer_tools_use_the_same_session(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        for fn, args in [
            ("set_camera_preset", {"preset": "top"}),
            ("move_camera", {"direction": "up", "distance_mm": 5.0}),
            ("rotate_camera", {"axis": "yaw", "degrees": 30.0}),
            ("zoom", {"factor": 2.0}),
            ("go_to_coordinates", {"x": 1.0, "y": 2.0, "z": 3.0, "span": 10.0}),
            ("zoom_to_region", {"screen_x": 0.2, "screen_y": 0.3, "magnification": 8.0}),
            ("inspect_region", {"region_name": "midspan"}),
            ("toggle_patch", {"patch_name": "wall", "visible": False}),
            ("reset_view", {}),
            ("set_navigation_defaults", {"pan_step_mm": 25.0, "zoom_step": 2.0}),
        ]:
            r = await rt.execute_viewer_tool(fn, args)
            assert r is not NOT_VIEWER_TOOL, f"{fn} was not handled"
    assert len(fake_backend) == 1


async def test_verdict_tools_are_not_the_runtimes_business(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        for fn in VERDICT_TOOLS:
            assert await rt.execute_viewer_tool(fn, {}) is NOT_VIEWER_TOOL


def test_every_reviewer_tool_belongs_to_exactly_one_family():
    from meshpipeline.agents.reviewer.tools import REVIEWER_TOOLS

    names = {t["function"]["name"] for t in REVIEWER_TOOLS}
    # THE CONTRACT: the three families partition the inventory exactly - nothing unclassified,
    # nothing invented. A new reviewer tool must join a family; it must not have to change a count.
    assert names, "there are no reviewer tools at all"
    assert VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS | VERDICT_TOOLS == names
    assert VIEWER_RENDERING_TOOLS & VERDICT_TOOLS == frozenset()


def test_the_runtime_handles_exactly_the_viewer_tools():
    for name in VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS:
        assert hasattr(ReviewerRenderRuntime, f"_t_{name}"), f"{name} has no translation"


# 7-10: the initial context
async def test_the_initial_context_is_byte_identical(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        ctx = await rt.initial_context()
    assert ctx.nav_context["bbox_mm"]["xmax"] == 100.0
    assert ctx.nav_context["pan_step_mm"] == 10.0
    assert ctx.patch_colour_legend == "inlet=#ff0000, outlet=#ff0000, wall=#ff0000"
    assert base64.b64decode(ctx.initial_screenshot_b64) == PNG


async def test_the_geometry_guard_uses_explicit_scene_state(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        assert await rt.has_geometry() is True
        fake_backend[0].patches = []
        assert await rt.has_geometry() is False


# 11-17: live state in the text
async def test_move_camera_reports_the_live_default_distance(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        fake_backend[0].pan_step = 12.3456789
        r = await rt.execute_viewer_tool("move_camera", {"direction": "left"})
    assert r[0]["text"] == "Camera moved left by 12.3457 m."


async def test_go_to_coordinates_uses_the_recalibrated_pan_step(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool(
            "go_to_coordinates", {"x": 1.0, "y": 2.0, "z": 3.0, "span": 543.21})
    # the fake recalibrates pan = span * 0.10, exactly as the backend does
    assert "Pan step recalibrated to 54.32 m." in r[0]["text"]


async def test_go_to_coordinates_applies_preset_and_isolation(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool("go_to_coordinates", {
            "x": 1.0, "y": 2.0, "z": 3.0, "span": 10.0, "preset": "top", "patch_name": "wall"})
    fake = fake_backend[0]
    assert fake.kwargs[-1] == {"preset": "top", "patch_name": "wall"}
    assert fake.preset == "top" and fake.isolated == "wall"
    assert "preset=top," in r[0]["text"] and "(isolated: wall)" in r[0]["text"]


async def test_text_cannot_claim_an_isolation_that_was_refused(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool("go_to_coordinates", {
            "x": 1.0, "y": 2.0, "z": 3.0, "span": 10.0, "patch_name": "nonexistent"})
    assert isinstance(r, str), "a refused isolation returned an image-bearing result"
    assert "isolated" not in r
    assert not any(c[0] == "go_to_coordinates" for c in fake_backend[0].calls)


# 18-25: evidence provenance and the approved correction
async def test_zoom_to_region_returns_the_crop(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        before = fake_backend[0].generic_shots
        r = await rt.execute_viewer_tool(
            "zoom_to_region", {"screen_x": 0.2, "screen_y": 0.3, "magnification": 8.0})
    b64 = r[1]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64) == PNG_CROP, "a full view was returned as the crop"
    assert fake_backend[0].generic_shots == before, "a generic screenshot followed the crop"
    assert r[0]["text"] == "Render crop at (0.20, 0.30) - 8.0× magnification, camera unchanged."


async def test_valid_inspect_region_returns_its_own_slice(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool("inspect_region", {"region_name": "midspan"})
    b64 = r[1]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64) == PNG_SLICE
    assert r[0]["text"].startswith("Internal slice through the VOLUME mesh at region 'midspan'.")


async def test_unknown_region_is_truthfully_refused(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool("inspect_region", {"region_name": "invented"})
    assert isinstance(r, str), "an unknown region produced an image"
    assert "No inspection region named 'invented' is declared" in r
    assert "Internal slice through the VOLUME mesh" not in r, "the false claim survived"
    assert "midspan" in r and "nearwall" in r, "the declared regions were not offered"
    assert not any(c[0] == "inspect_region" for c in fake_backend[0].calls)


async def test_unknown_patch_preserves_the_exact_legacy_text(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool(
            "toggle_patch", {"patch_name": "ghost", "visible": True})
    assert r == ("Patch 'ghost' carries no review geometry and cannot be shown. "
                 "Renderable patches: inlet, outlet, wall. "
                 "Do not draw conclusions about this patch from the viewer; judge it "
                 "from the mesh metrics instead.")
    assert not any(c[0] == "toggle_patch" for c in fake_backend[0].calls)


async def test_configuration_is_text_only_and_byte_identical(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        r = await rt.execute_viewer_tool(
            "set_navigation_defaults", {"pan_step_mm": 25.0, "zoom_step": 2.0})
        assert r == "Navigation defaults updated: pan_step=25 mm, zoom_step=2.00×."
        # ...and it is visible to what follows
        r2 = await rt.execute_viewer_tool("move_camera", {"direction": "up"})
    assert r2[0]["text"] == "Camera moved up by 25 m."


async def test_a_failed_render_never_becomes_a_text_plus_image_pair(runtime, fake_backend,
                                                                    tmp_path):
    async with await _open(runtime) as rt:
        fake_backend[0]._shot = False
        r = await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})
    assert isinstance(r, str) and "could not be produced" in r


async def test_the_model_never_sees_a_path(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        results = [await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"}),
                   await rt.execute_viewer_tool("inspect_region", {"region_name": "midspan"})]
    for r in results:
        blob = repr(r)
        assert str(tmp_path) not in blob, "an absolute path reached the model"
        assert ".png" not in blob, "an image path reached the model"


# 26-28: state persists
async def test_state_persists_across_commands(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        await rt.execute_viewer_tool("toggle_patch", {"patch_name": "wall", "visible": False})
        await rt.execute_viewer_tool("set_navigation_defaults", {"pan_step_mm": 42.0})
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})
        fake = fake_backend[0]
        assert fake.visible["wall"] is False, "patch visibility reset between commands"
        assert fake.pan_step == 42.0 and fake.preset == "top"


# 33: concurrency
async def test_two_concurrent_runtimes_on_the_same_engine_are_isolated(fake_backend, tmp_path,
                                                                       scene_facts):
    from meshpipeline.engines.registry import ENGINE_CATALOG

    a_dir = tmp_path / "a"; a_dir.mkdir()
    b_dir = tmp_path / "b"; b_dir.mkdir()

    async with open_runtime(ENGINE_CATALOG[ENGINE], _ctx(a_dir)) as ra, \
               open_runtime(ENGINE_CATALOG[ENGINE], _ctx(b_dir)) as rb:
        assert fake_backend[0] is not fake_backend[1], "the two reviews share a backend"

        await ra.execute_viewer_tool("set_navigation_defaults", {"pan_step_mm": 42.0})
        await ra.execute_viewer_tool("toggle_patch", {"patch_name": "wall", "visible": False})
        await ra.execute_viewer_tool("set_camera_preset", {"preset": "top"})

        await rb.execute_viewer_tool("set_navigation_defaults", {"pan_step_mm": 7.0})

        assert (await ra.scene()).pan_step_mm == 42.0
        assert (await rb.scene()).pan_step_mm == 7.0, "one review moved the other's navigation state"
        assert fake_backend[0].visible["wall"] is False
        assert fake_backend[1].visible["wall"] is True, "one review hid the other's patch"
        assert fake_backend[1].preset == "iso", "one review moved the other's camera"


# 36-38: lifecycle
async def test_the_session_closes_on_normal_completion(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        await rt.execute_viewer_tool("reset_view", {})
    assert fake_backend[0].closed == 1 and rt.closed


async def test_close_runs_on_the_lane_before_the_executor_shuts_down(runtime, fake_backend,
                                                                     monkeypatch):
    from meshpipeline.sandbox import execution_lane as EL
    order: list[str] = []
    orig_final, orig_sd = EL.RendererExecutionLane.run_final, EL.RendererExecutionLane.shutdown

    async def _final(self, fn, *a, **k):
        order.append("close")
        return await orig_final(self, fn, *a, **k)

    async def _sd(self, *a, **k):
        order.append("shutdown")
        return await orig_sd(self, *a, **k)
    monkeypatch.setattr(EL.RendererExecutionLane, "run_final", _final)
    monkeypatch.setattr(EL.RendererExecutionLane, "shutdown", _sd)
    async with await _open(runtime) as rt:
        await rt.execute_viewer_tool("reset_view", {})
    assert order == ["close", "shutdown"], order


async def test_the_session_closes_when_the_caller_raises(runtime, fake_backend, tmp_path):
    with pytest.raises(RuntimeError, match="provider exploded"):
        async with await _open(runtime) as rt:
            await rt.execute_viewer_tool("reset_view", {})
            raise RuntimeError("provider exploded")
    assert fake_backend[0].closed == 1


async def test_cancellation_closes_and_propagates(runtime, fake_backend, tmp_path):
    with pytest.raises(asyncio.CancelledError):
        async with await _open(runtime) as rt:
            await rt.execute_viewer_tool("reset_view", {})
            raise asyncio.CancelledError()
    assert fake_backend[0].closed == 1, "cancellation leaked the session"


async def test_a_close_failure_does_not_replace_the_primary_error(runtime, fake_backend,
                                                                  tmp_path):
    with pytest.raises(RuntimeError, match="the real cause"):
        async with await _open(runtime) as rt:
            def _boom(): raise RuntimeError("close failed too")
            fake_backend[0].close = _boom
            raise RuntimeError("the real cause")


async def test_commands_after_close_are_refused(runtime, fake_backend, tmp_path):
    async with await _open(runtime) as rt:
        pass
    with pytest.raises(RuntimeError, match="closed"):
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})


# 34: idle model time is not renderer time
async def test_long_model_wait_does_not_expire_a_healthy_session(runtime, fake_backend,
                                                                 tmp_path, monkeypatch):
    import time

    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 5000.0)
    async with await _open(runtime) as rt:
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})
        monkeypatch.setattr(time, "monotonic", lambda: base + 11000.0)  # >600s of thinking
        r = await rt.execute_viewer_tool("set_camera_preset", {"preset": "front"})
    assert isinstance(r, list) and r[1]["type"] == "image_url", "an idle session expired"


# architecture
def test_the_runtime_holds_no_module_or_class_level_session():
    from meshpipeline.agents.reviewer import render_runtime

    for name, value in vars(render_runtime).items():
        if name.startswith("_") or callable(value):
            continue
        assert not hasattr(value, "execute"), f"module-level session-like state: {name}"
    for name, value in vars(ReviewerRenderRuntime).items():
        assert not hasattr(value, "execute") or callable(value), (
            f"class-level session state: {name}")


@pytest.mark.parametrize("engine", ["gmsh", "vmtk"])
def test_gmsh_and_vmtk_render_for_review_in_production(engine):
    from meshpipeline.engines.assurance import derive_assurance_plan
    from meshpipeline.engines.registry import ENGINE_CATALOG

    spec = ENGINE_CATALOG[engine]
    assert not hasattr(spec, "visual_review")
    assert spec.renders_for_review is True
    plan = derive_assurance_plan(spec, "")
    assert plan.requires_render


async def test_the_runtime_is_marked_closed_even_when_the_exit_was_an_exception(
        runtime, fake_backend, tmp_path):
    rt_ref = []
    with pytest.raises(RuntimeError, match="provider exploded"):
        async with await _open(runtime) as rt:
            rt_ref.append(rt)
            raise RuntimeError("provider exploded")

    rt = rt_ref[0]
    assert rt.closed is True, "the runtime survived its own exit still marked open"
    assert fake_backend[0].closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
