# Responsibility: Verify the lane keeps one worker off the event loop, serialises native work and shuts down cleanly.
from __future__ import annotations

import asyncio
import threading

import pytest

from meshpipeline.sandbox.execution_lane import (
    THREAD_NAME_PREFIX,
    LaneClosedError,
    LaneReentrancyError,
    RendererExecutionLane,
)


async def test_the_lane_runs_work_off_the_event_loop():
    lane = RendererExecutionLane()
    loop_id = threading.get_ident()
    try:
        worker_id = await lane.run(threading.get_ident)
        assert worker_id != loop_id, "native work ran on the event-loop thread"
    finally:
        await lane.shutdown()


async def test_every_call_uses_the_same_worker_thread():
    lane = RendererExecutionLane()
    try:
        ids = {await lane.run(threading.get_ident) for _ in range(12)}
        assert len(ids) == 1, f"native work spread across {len(ids)} threads"
    finally:
        await lane.shutdown()


async def test_the_worker_keeps_the_established_thread_name():
    lane = RendererExecutionLane()
    try:
        name = await lane.run(lambda: threading.current_thread().name)
        assert name.startswith(THREAD_NAME_PREFIX)
    finally:
        await lane.shutdown()


async def test_a_slow_render_does_not_block_the_event_loop():
    lane = RendererExecutionLane()
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    try:
        await lane.run(lambda: __import__("time").sleep(0.25))
        assert ticks > 5, f"the event loop stalled during a render (only {ticks} ticks)"
    finally:
        hb.cancel()
        await lane.shutdown()


async def test_native_operations_serialize():
    lane = RendererExecutionLane()
    active = 0
    peak = 0
    order: list[int] = []
    guard = threading.Lock()

    def op(i):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        __import__("time").sleep(0.02)
        order.append(i)
        with guard:
            active -= 1

    try:
        await asyncio.gather(*(lane.run(op, i) for i in range(5)))
        assert peak == 1, f"{peak} native operations ran at once"
        assert order == [0, 1, 2, 3, 4], f"FIFO order broken: {order}"
    finally:
        await lane.shutdown()


async def test_work_is_refused_once_closing_begins():
    lane = RendererExecutionLane()
    lane.begin_closing()
    with pytest.raises(LaneClosedError):
        await lane.run(threading.get_ident)
    await lane.shutdown()


async def test_close_still_runs_after_closing_begins():
    lane = RendererExecutionLane()
    worker = await lane.run(threading.get_ident)
    lane.begin_closing()
    assert await lane.run_final(threading.get_ident) == worker, "close left the renderer thread"
    await lane.shutdown()


async def test_close_queues_behind_in_flight_native_work():
    lane = RendererExecutionLane()
    journal: list[str] = []

    def slow_render():
        __import__("time").sleep(0.15)
        journal.append("render-done")

    task = asyncio.create_task(lane.run(slow_render))
    await asyncio.sleep(0.02)
    lane.begin_closing()
    await lane.run_final(lambda: journal.append("close"))
    await task
    assert journal == ["render-done", "close"], f"close jumped the queue: {journal}"
    await lane.shutdown()


async def test_shutdown_does_not_block_the_event_loop():
    lane = RendererExecutionLane()
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    asyncio.create_task(lane.run(lambda: __import__("time").sleep(0.2)))
    await asyncio.sleep(0.01)
    hb = asyncio.create_task(heartbeat())
    try:
        await lane.shutdown()          # waits for the worker to drain
        assert ticks > 3, "shutdown blocked the event loop while draining"
    finally:
        hb.cancel()


async def test_no_worker_survives_shutdown():
    lane = RendererExecutionLane()
    await lane.run(threading.get_ident)
    await lane.shutdown()
    await asyncio.sleep(0.05)
    assert not [t for t in threading.enumerate()
                if t.name.startswith(THREAD_NAME_PREFIX) and t.is_alive()
                and t.ident == lane.thread_id], "a renderer worker leaked"


async def test_shutdown_is_idempotent():
    lane = RendererExecutionLane()
    await lane.run(threading.get_ident)
    await lane.shutdown()
    await lane.shutdown()


async def test_nested_submission_cannot_deadlock_because_it_cannot_be_reached():
    lane = RendererExecutionLane()
    try:
        with pytest.raises(RuntimeError, match="no running event loop"):
            await lane.run(asyncio.get_running_loop)
    finally:
        await lane.shutdown()


def test_the_reentrancy_guard_exists_for_the_unreachable_case():
    lane = RendererExecutionLane()
    lane._thread_id = threading.get_ident()      # pretend we are the worker
    # Own an explicit loop rather than asyncio.get_event_loop(): on 3.12 the latter raises
    # "no current event loop" when a prior async test left the thread without one, making this
    # sync test order-dependent (surfaced by test-random). A fresh loop is deterministic.
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(LaneReentrancyError):
            loop.run_until_complete(lane.run(threading.get_ident))
    finally:
        loop.close()


async def test_two_lanes_own_different_workers():
    a, b = RendererExecutionLane(), RendererExecutionLane()
    try:
        ia = await a.run(threading.get_ident)
        ib = await b.run(threading.get_ident)
        assert ia != ib, "two concurrent reviews shared a renderer thread"
    finally:
        await a.shutdown()
        await b.shutdown()


def test_the_lane_never_exposes_its_executor():
    lane = RendererExecutionLane()
    assert not [n for n in dir(lane) if not n.startswith("_") and "executor" in n.lower()]


def test_the_lane_uses_exactly_one_worker():
    lane = RendererExecutionLane()
    assert lane._executor._max_workers == 1


# the RUNTIME must actually use the lane
# The tests above prove the lane works. Reverting the runtime to `session.execute(command)` on the
# event loop passed every one of them - they never asked who calls it. Proving a mechanism exists
# is not proving it is used, and that gap is the whole defect.
async def test_the_runtime_runs_every_native_call_off_the_event_loop(monkeypatch, tmp_path):
    from _render_fakes import REGIONS, FakeBackend

    from meshpipeline.agents.reviewer.render_runtime import open_runtime
    from meshpipeline.contracts.review_evidence import RenderContext
    from meshpipeline.engines.registry import ENGINE_CATALOG

    seen: list[int] = []

    def _facts(self):
        seen.append(threading.get_ident())
        return {"has_geometry": True, "bounds": {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0,
                "ymax": 1.0, "zmin": 0.0, "zmax": 1.0}, "mesh_units": "m",
                "pan_step_mm": self.pan_step, "zoom_step": self.zoom_step,
                "patch_legend": [(n, "#ff0000") for n in self.patches]}
    monkeypatch.setattr(FakeBackend, "scene_facts", _facts, raising=False)

    _real_shot = FakeBackend.take_screenshot
    monkeypatch.setattr(FakeBackend, "take_screenshot",
                        lambda self: (seen.append(threading.get_ident()), _real_shot(self))[1])

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        seen.append(threading.get_ident())                 # construction
        be = FakeBackend(save_dir=__import__("pathlib").Path(context.save_dir))
        return (be, ResolvedRenderInputs(surface=artifacts[SURFACE_KEY],
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

    loop_id = threading.get_ident()
    async with open_runtime(ENGINE_CATALOG["snappy"], ctx) as rt:
        await rt.initial_context()
        await rt.capabilities()
        await rt.scene()
        await rt.execute_viewer_tool("set_camera_preset", {"preset": "top"})
        await rt.execute_viewer_tool("inspect_region", {"region_name": "midspan"})
        await rt.execute_viewer_tool("set_navigation_defaults", {"pan_step_mm": 25.0})

    assert seen, "nothing was recorded - the instrument is broken"
    assert loop_id not in seen, (
        f"{seen.count(loop_id)} of {len(seen)} native calls ran on the EVENT-LOOP thread")
    assert len(set(seen)) == 1, f"native work spread across {len(set(seen))} threads"


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
