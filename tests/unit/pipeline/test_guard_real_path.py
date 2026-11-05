# Responsibility: Verify D3's guard trips on the route the deployed worker takes (dev-areen, 2 Nov): the native run in the mesh job's MAIN thread with its stamp riding home, read back through run_mesh, the builder's trip check, the executor, the graph and the user's sentence - never the lab's in-process shortcut, where a main-thread-only clock never arms.
# Boundaries: contracts/mesh_execution.run_mesh with a mesh executor shaped like the Cloud Run one (engines/dispatch._run_stamped inside it, as runtime/mesh_runner runs it); engines/passage's planned guard; pipeline/executor, pipeline/graph and contracts/failure_cause read what comes back. Nothing native runs.
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from tests.foam_fixtures import write_row_of_hexes

from meshpipeline.contracts import mesh_execution as ME
from meshpipeline.contracts import work_limits as W
from meshpipeline.contracts.failure_cause import FailureCause, describe
from meshpipeline.engines import determinism as det
from meshpipeline.engines import dispatch
from meshpipeline.engines import passage as P
from meshpipeline.pipeline import executor as X
from meshpipeline.pipeline import graph as G


class _MeshJob:
    """The deployed route, minus GCS: the worker's MeshExecutor hands the workspace to the mesh
    job, which runs the engine's stamped run (engines/dispatch._run_stamped, as run_engine_local
    does) in the MAIN thread of its process (runtime/mesh_runner.run); the stamp is written into
    the workspace and rides home with the mesh, and the result document comes back."""

    def __init__(self, native):
        self.native = native

    def run(self, workspace, *, engine: str, timeout: int, operation_key: str = "") -> dict:
        assert threading.current_thread() is threading.main_thread()

        def runner(ws, *, bashrc, timeout):
            return self.native(ws, timeout)
        return dispatch._run_stamped(runner, workspace, engine=engine, bashrc="",
                                     timeout=int(timeout))


@pytest.fixture
def mesh_job(monkeypatch):
    for name in det.native_environment("cfmesh"):          # restored after the test
        monkeypatch.setenv(name, "unset-by-test")
    monkeypatch.setattr("meshpipeline.engines.snappy.native._foam_version", lambda _b: "")
    monkeypatch.setattr(ME, "_launch_check", None)

    def install(native):
        monkeypatch.setattr(ME, "_executor", _MeshJob(native))
    return install


def _slow_measure(monkeypatch, seconds: float = 3.0) -> None:
    """A measure slower than its guard, with the guard's floor at the 1 s minimum."""
    def _slow(points, faces, **kw):
        time.sleep(seconds)
        return {"passage_radius": {"median": 0.5}}
    monkeypatch.setattr(P, "passage_of_surface", _slow)
    monkeypatch.setattr(P, "PASSAGE_MEASURE_BUDGET_S", 1.0)


def _cfmesh_native(ws, _timeout):
    """cfMesh's native run as engines/cfmesh/native runs it: a finished mesh, checkMesh, then the
    passage measure beside it."""
    q = {"mesh_ok": True, "cells": 3, "fatal": []}
    q.update(P.passage_of_polymesh(ws))
    return {"rc": 0, "timed_out": False, "quality": q, "log_tail": ""}


class _Pub:
    async def astage(self, *a, **k): pass
    async def anote(self, *a, **k): pass
    async def acheck(self, *a, **k): pass


def _executor_stubs(monkeypatch):
    async def _owner(*_a, **_k):
        return None
    monkeypatch.setattr(X, "execution_publisher", lambda *a, **k: _Pub())
    monkeypatch.setattr("meshpipeline.application.execution_fence.assert_current_owner", _owner)
    monkeypatch.setattr(X, "get_engine", lambda *_a: pytest.fail("finalize ran on a trip"))


# #
# the passage measure: planned work sets its guard (TIME-2)
# #

def test_the_measure_guard_is_planned_from_the_mesh_boundary(tmp_path, monkeypatch):
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    work = P.measure_guard(pm)
    assert work.where == "passage.measure" and work.unit == "boundary faces"
    assert work.amount and work.guard_seconds == 240, "a small mesh keeps the old floor"

    def _boundary(faces):
        return lambda _pm: [{"name": "wall", "type": "wall", "n_faces": faces, "start_face": 0}]
    sp = "meshpipeline.engines.cfmesh.polymesh_surface.read_boundary_patches"
    # the benchmark's own measures (30 Oct, measured where the clock never armed): every one fits
    # 3 x its planned time - the gyroid wall (69,798 points, 1,070 s), the FDA pump housing
    # (302,492, 340 s), the slowest per point (62,062, 1,002 s) and the longest (1.8 M, 1,622 s)
    for faces, took in ((69_798, 1069.7), (302_492, 339.5), (62_062, 1001.5),
                        (1_813_152, 1622.0)):
        monkeypatch.setattr(sp, _boundary(faces))
        assert P.measure_guard(pm).guard_seconds > took, faces
    monkeypatch.setattr(sp, _boundary(20_000))
    assert P.measure_guard(pm).guard_seconds == 500        # 3 x 20,000 / 120
    monkeypatch.setattr(sp, _boundary(50_000_000))
    assert P.measure_guard(pm).guard_seconds == int(P.PASSAGE_MEASURE_CEILING_S)


def test_the_measure_runs_under_its_planned_guard_not_the_old_fixed_one(tmp_path, monkeypatch):
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    seen = {}

    class _Deadline:
        def __init__(self, s):
            seen["s"] = s

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P, "measure_deadline", _Deadline)
    monkeypatch.setattr(P, "measure_guard", lambda _pm: W.PlannedWork(
        where="passage.measure", unit="boundary faces", amount=302_492.0,
        rate=P.PASSAGE_MEASURE_FACES_PER_S, floor_s=240.0, ceiling_s=3600.0))
    P.passage_of_polymesh(tmp_path)
    assert seen["s"] == 3600.0


# #
# the deployed route: a trip in the mesh job comes home in the stamp
# #

def test_a_measure_past_its_guard_on_the_mesh_job_keeps_the_valid_mesh(tmp_path, monkeypatch,
                                                                        mesh_job):
    """dev-areen, 2 Nov: the pump housing's and the gyroid's measures tripped at 240 s in the mesh
    job, the trip came home as TIME-3, the attempt was replayed, tripped the same way on the same
    mesh, and two valid meshes ended as "ran out of time". The measure reads a mesh checkMesh
    already measured: its trip keeps that mesh (TIME-8)."""
    from meshpipeline.agents.builder.tools import meshing as M
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    _slow_measure(monkeypatch)
    mesh_job(_cfmesh_native)
    ME.open_native_record(tmp_path)
    t0 = time.monotonic()
    res = ME.run_mesh(tmp_path, engine="cfmesh", timeout=1200)
    assert time.monotonic() - t0 < 2.9, "the measure's guard did not arm on the mesh job's route"
    assert res["rc"] == 0 and not res["timed_out"]
    assert "passage_radius" not in res["quality"], "the mesh is checked without the measure"
    trips = det.guard_trips(tmp_path)
    assert [(t["where"], t["rule"]) for t in trips] == [("passage.measure", W.TIME_KEEP_VALID)]
    assert trips[0]["unit"] == "boundary faces" and trips[0]["kept"]
    # the worker reads it back as a trip that does NOT end the attempt
    assert M._attempt_tripped(tmp_path) is False
    assert X._guard_trip({"engine": "cfmesh"}, tmp_path) == {}


def test_a_measure_inside_a_worker_thread_never_arms_its_clock(tmp_path, monkeypatch):
    """Why the 721-file benchmark could not see it: the lab harness ran the native run inside the
    builder's thread, where the measure's SIGALRM clock never arms, so every measure ran to its
    end. The deployed mesh job runs it in its main thread (the test above)."""
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    _slow_measure(monkeypatch, seconds=1.5)
    out: dict = {}
    th = threading.Thread(target=lambda: out.update(P.passage_of_polymesh(tmp_path)))
    th.start()
    th.join()
    assert out == {"passage_radius": {"median": 0.5}}


def test_a_run_stopped_at_its_guard_on_the_mesh_job_is_replayed_then_ours(tmp_path, monkeypatch,
                                                                          mesh_job):
    """A native run that came home stopped at its guard: run_mesh -> the stamp's trip -> the
    executor's guard failure -> one replay -> "ran out of time" on our side, never a lever."""
    write_row_of_hexes(tmp_path / "constant" / "polyMesh")
    mesh_job(lambda ws, timeout: {"rc": ME.RC_TIMED_OUT, "timed_out": True,
                                  "log_tail": "cartesianMesh still running"})
    ME.open_native_record(tmp_path)
    res = ME.run_mesh(tmp_path, engine="cfmesh", timeout=1200)
    assert res["timed_out"] is True
    assert [(t["where"], t["rule"]) for t in det.guard_trips(tmp_path)] == [("cfmesh", "TIME-3")]
    _executor_stubs(monkeypatch)
    out = asyncio.run(X.node_executor({"job_id": "rp-1", "engine": "cfmesh",
                                       "openfoam_workspace": str(tmp_path)}))
    assert out["executor_failure_cause"] == FailureCause.ENGINE_TIMED_OUT.value
    guard = out["executor_failure_facts"]["guard"]
    assert guard["where"] == "cfmesh" and guard["guard_s"] == 1200.0
    state = {**out, "job_id": "rp-1", "engine": "cfmesh", "retry_count": 1}
    assert G.route_after_executor(state) == "node_guard_replay"
    assert G.route_after_executor({**state, "guard_replay_count": 1}) == G.END
    what, nxt = describe(out["executor_failure_cause"], out["executor_failure_facts"],
                         engine="cfMesh")
    assert "ran out of time on our side" in what and "cfmesh ran past its guard clock" in what
    assert "not a result about your part" in what
    assert "less detail" not in nxt and "run it again" in nxt


def test_a_result_the_worker_stopped_waiting_for_is_a_guard_trip_too(tmp_path, monkeypatch):
    """The Cloud Run client's own deadline (cloud_run_client._timed_out): no stamp comes home, the
    run's record says it timed out - the same trip, the same replay, the same words."""
    class _NoResult:
        def run(self, workspace, *, engine, timeout, operation_key=""):
            return {"rc": ME.RC_TIMED_OUT, "timed_out": True, "log_tail": "no result"}
    monkeypatch.setattr(ME, "_executor", _NoResult())
    monkeypatch.setattr(ME, "_launch_check", None)
    ME.open_native_record(tmp_path)
    ME.run_mesh(tmp_path, engine="snappy", timeout=2400)
    _executor_stubs(monkeypatch)
    out = asyncio.run(X.node_executor({"job_id": "rp-2", "engine": "snappy",
                                       "openfoam_workspace": str(tmp_path)}))
    assert out["executor_failure_facts"]["guard"]["native_run"] is True
    assert G.guard_trip_of({**out, "job_id": "rp-2"})
    _, nxt = describe(out["executor_failure_cause"], out["executor_failure_facts"])
    assert "less detail" not in nxt


def test_the_no_mesh_account_of_a_timed_out_run_carries_the_guard(tmp_path):
    ME.open_native_record(tmp_path)
    ME._note_native_run(tmp_path, {"rc": ME.RC_TIMED_OUT, "timed_out": True})
    cause, facts = X._no_mesh_cause({"engine": "vmtk"}, tmp_path,
                                    FailureCause.ENGINE_CRASHED.value, {"engine": "VMTK"})
    assert cause == FailureCause.ENGINE_TIMED_OUT.value
    assert facts["guard"] == {"where": "vmtk", "native_run": True}


def test_a_timeout_with_no_guard_on_record_keeps_its_old_words():
    _, nxt = describe(FailureCause.ENGINE_TIMED_OUT, {"engine": "cfMesh"})
    assert "less detail" in nxt
