# Responsibility: Verify one review is one runtime thread, lane-dispatched and never stalling the event loop.
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from _render_fakes import REGIONS, FakeBackend  # noqa: E402

from meshpipeline.agents.reviewer.render_runtime import open_runtime  # noqa: E402
from meshpipeline.contracts.review_evidence import RenderContext  # noqa: E402
from meshpipeline.engines.registry import ENGINE_CATALOG  # noqa: E402


class Journal:

    def __init__(self):
        self.entries: list[dict] = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def record(self, op, fn, *a, **k):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.entries.append({"op": op, "tid": threading.get_ident(),
                                 "name": threading.current_thread().name})
            return fn(*a, **k)
        finally:
            with self._lock:
                self.active -= 1

    @property
    def tids(self):
        return {e["tid"] for e in self.entries}


def _ctx(tmp_path, volume=True):
    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    paths = {"surface": str(tmp_path / "mesh.msh")}
    if volume:
        (tmp_path / "i.vtu").write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="U">\n')
        paths["volume"] = "i.vtu"
    return RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path), manifest={
        "mesh_paths": paths, "mesh_units": "m", "inspection_regions": REGIONS})


@pytest.fixture
def journaled(monkeypatch):
    j = Journal()

    def _facts(self):
        return j.record("scene_context", lambda: {
            "has_geometry": True,
            "bounds": {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 1.0,
                       "zmin": 0.0, "zmax": 1.0},
            "mesh_units": "m", "pan_step_mm": self.pan_step, "zoom_step": self.zoom_step,
            "patch_legend": [(n, "#ff0000") for n in self.patches]})
    monkeypatch.setattr(FakeBackend, "scene_facts", _facts, raising=False)

    for name in ("take_screenshot", "inspect_region", "zoom_to_region", "close"):
        real = getattr(FakeBackend, name)
        monkeypatch.setattr(FakeBackend, name,
                            (lambda r, n: lambda self, *a, **k: j.record(n, r, self, *a, **k))(
                                real, name))

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        return j.record("construct", lambda: (
            FakeBackend(save_dir=pathlib.Path(context.save_dir)),
            ResolvedRenderInputs(surface=artifacts[SURFACE_KEY],
                                 volume=artifacts.get(VOLUME_KEY)),
            resolve_regions(context.manifest)))

    for engine in ("snappy", "cfmesh", "snappy_multiregion"):
        mod = __import__(f"meshpipeline.engines.{engine}.review_renderer", fromlist=["x"])
        monkeypatch.setattr(mod, "build_backend", _build)
    return j


# legacy driver: the REAL executor pattern, not an inline fake
class LegacyDriver:

    def __init__(self, journal):
        self.j = journal
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mesh-sb")

    async def run(self, op, fn, *a):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, lambda: self.j.record(op, fn, *a))

    def shutdown(self):
        self.pool.shutdown(wait=True)


async def test_both_models_are_one_review_one_stable_off_loop_thread(journaled, tmp_path):
    loop_id = threading.get_ident()

    legacy_j = Journal()
    legacy = LegacyDriver(legacy_j)
    try:
        for op in ("construct", "opening_evidence", "scene_context", "execute", "close"):
            await legacy.run(op, lambda: None)
    finally:
        legacy.shutdown()

    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
        await rt.initial_context()
        await rt.scene()
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})

    for name, jn in (("legacy", legacy_j), ("runtime", journaled)):
        assert len(jn.tids) == 1, f"{name}: native work on {len(jn.tids)} threads"
        assert loop_id not in jn.tids, f"{name}: native work on the event-loop thread"
        assert all(e["name"].startswith("mesh-sb") for e in jn.entries), f"{name}: thread name"


# thread affinity across the complete surface
async def test_every_session_interaction_shares_one_runtime_thread(journaled, tmp_path):
    loop_id = threading.get_ident()
    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
        await rt.initial_context()
        await rt.capabilities()
        await rt.scene()
        for fn, args in [
            ("set_camera_preset", {"preset": "top"}),
            ("move_camera", {"direction": "up"}),
            ("rotate_camera", {"axis": "yaw", "degrees": 30.0}),
            ("zoom", {"factor": 2.0}),
            ("go_to_coordinates", {"x": 1.0, "y": 2.0, "z": 3.0, "span": 10.0}),
            ("zoom_to_region", {"screen_x": 0.2, "screen_y": 0.3, "magnification": 8.0}),
            ("inspect_region", {"region_name": "midspan"}),
            ("toggle_patch", {"patch_name": "wall", "visible": False}),
            ("reset_view", {}),
            ("set_navigation_defaults", {"pan_step_mm": 25.0}),
        ]:
            await rt.execute_viewer_tool(fn, args)
        await rt.scene()

    ops = {e["op"] for e in journaled.entries}
    assert {"construct", "scene_context", "close"} <= ops, f"missing coverage: {ops}"
    assert len(journaled.tids) == 1, f"native work spread across {len(journaled.tids)} threads"
    assert loop_id not in journaled.tids
    assert journaled.peak == 1, f"{journaled.peak} native operations overlapped"


async def test_construction_and_close_share_the_command_thread(journaled, tmp_path):
    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
        await rt.execute_viewer_tool("reset_view", {})
    by_op = {e["op"]: e["tid"] for e in journaled.entries}
    assert by_op["construct"] == by_op["close"] == by_op["take_screenshot"]


# structural dispatch inventory
def test_every_session_interaction_is_lane_dispatched():
    from meshpipeline.agents.reviewer import render_runtime

    tree = ast.parse(inspect.getsource(render_runtime))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            if fn.startswith("self._session."):
                offenders.append(ast.unparse(node))
    assert not offenders, (
        f"session methods called outside the lane: {offenders}. Every interaction must go "
        f"through `self._lane.run(self._session.<method>, ...)`.")


def test_the_lane_is_the_only_dispatch_mechanism():
    from meshpipeline.agents.reviewer import render_runtime

    src = inspect.getsource(render_runtime)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            assert "to_thread" not in fn, "renderer work routed through to_thread (no affinity)"
            assert "run_in_executor" not in fn, "the runtime bypasses its lane"


# event-loop responsiveness, real executor, barrier-driven
async def test_a_blocked_render_does_not_stall_the_event_loop(journaled, tmp_path):
    started = threading.Event()
    release = threading.Event()
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    def blocking_shot(self):
        started.set()
        assert release.wait(timeout=5.0), "deadlock guard"
        return self._write("shot", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
        FakeBackend.take_screenshot = blocking_shot
        try:
            hb = asyncio.create_task(heartbeat())
            task = asyncio.create_task(
                rt.execute_viewer_tool("set_camera_preset", {"preset": "top"}))
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5.0)
            await asyncio.sleep(0.08)          # the loop must keep turning while it blocks
            assert ticks > 5, f"the event loop stalled during a render ({ticks} ticks)"
            release.set()
            assert await task is not None
            hb.cancel()
        finally:
            del FakeBackend.take_screenshot


# concurrent runtimes, both alive
async def test_two_concurrent_runtimes_own_separate_live_lanes(journaled, tmp_path):
    a_dir = tmp_path / "a"; a_dir.mkdir()
    b_dir = tmp_path / "b"; b_dir.mkdir()
    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(a_dir)) as ra, \
               open_runtime(ENGINE_CATALOG["snappy"], _ctx(b_dir)) as rb:
        ta = await ra._lane.run(threading.get_ident)
        tb = await rb._lane.run(threading.get_ident)
        assert ta != tb, "two concurrent reviews shared a renderer thread"

        await ra.execute_viewer_tool("set_navigation_defaults", {"pan_step_mm": 42.0})
        await rb.execute_viewer_tool("set_navigation_defaults", {"pan_step_mm": 7.0})
        assert (await ra.scene()).pan_step_mm == 42.0
        assert (await rb.scene()).pan_step_mm == 7.0


async def test_one_blocked_runtime_does_not_stall_another(journaled, tmp_path):
    release = threading.Event()
    started = threading.Event()
    a_dir = tmp_path / "a"; a_dir.mkdir()
    b_dir = tmp_path / "b"; b_dir.mkdir()

    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(a_dir)) as ra, \
               open_runtime(ENGINE_CATALOG["snappy"], _ctx(b_dir)) as rb:
        def block():
            started.set()
            release.wait(timeout=5.0)
        try:
            blocked = asyncio.create_task(ra._lane.run(block))
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5.0)
            # b must still render while a's worker is wedged
            r = await asyncio.wait_for(
                rb.execute_viewer_tool("set_camera_preset", {"preset": "top"}), timeout=3.0)
            assert isinstance(r, list), "a blocked review stalled an unrelated review"
        finally:
            release.set()
            await blocked


# close ordering and worker retirement
async def test_the_worker_does_not_survive_the_runtime(journaled, tmp_path):
    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
        await rt.execute_viewer_tool("reset_view", {})
        worker = rt._lane.thread_id
    await asyncio.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.ident == worker and t.is_alive()], (
        "the renderer worker outlived its runtime")


async def test_no_command_is_accepted_once_closing_begins(journaled, tmp_path):
    async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
        pass
    with pytest.raises(RuntimeError, match="closed"):
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})


async def test_cancellation_lets_native_work_finish_then_closes(journaled, tmp_path):
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def blocking_shot(self):
        started.set()
        release.wait(timeout=5.0)
        order.append("render-done")
        return self._write("shot", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    real_close = FakeBackend.close

    def watched_close(self):
        order.append("close")
        return real_close(self)

    with pytest.raises(asyncio.CancelledError):
        async with open_runtime(ENGINE_CATALOG["snappy"], _ctx(tmp_path)) as rt:
            FakeBackend.take_screenshot = blocking_shot
            FakeBackend.close = watched_close
            try:
                task = asyncio.create_task(
                    rt.execute_viewer_tool("set_camera_preset", {"preset": "top"}))
                await asyncio.get_running_loop().run_in_executor(None, started.wait, 5.0)
                task.cancel()
                release.set()
                raise asyncio.CancelledError()
            finally:
                # `close` is restored AFTER the async-with exits, below: the lifecycle's close
                # runs on the way out of that block, so restoring it here would un-instrument
                # the very call being measured. (It did - the first run recorded no close at all.)
                del FakeBackend.take_screenshot

    FakeBackend.close = real_close
    assert order == ["render-done", "close"], f"close raced the live render: {order}"


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
