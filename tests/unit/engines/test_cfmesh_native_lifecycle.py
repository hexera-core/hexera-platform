# Responsibility: Verify the native cfMesh run's stages, budget, teardown and failure vocabulary, judging nothing.
from __future__ import annotations

import subprocess

import pytest

import meshpipeline.engines.cfmesh.native as N
from meshpipeline.sandbox.safe_exec import NativeOutcome


class _Proc:
    def __init__(self, rc=0):
        self.returncode = rc


@pytest.fixture
def spawns(monkeypatch):
    calls: list[dict] = []

    def _fake(argv, **kw):
        calls.append({"argv": argv, "cwd": kw.get("cwd"), "env": kw.get("env"),
                      "timeout": kw.get("timeout")})
        return _Proc(0)

    monkeypatch.setattr(N, "run_guarded", _fake)
    return calls


def _case(ws, *, two_d=False, create_patch=True):
    if two_d:
        (ws / N.TWO_D_MARKER).write_text("cartesian2DMesh\n")
        if create_patch:
            (ws / "system").mkdir(exist_ok=True)
            (ws / "system" / "createPatchDict").write_text("x")
    return ws


# 1-3: the lifecycle, the stage order, and the exact command
def test_a_3d_case_runs_exactly_one_stage(tmp_path, spawns):
    out = N._run_cartesian_mesh_local(tmp_path)
    assert len(spawns) == 1, f"expected one native stage, got {len(spawns)}"
    assert N.CARTESIAN_MESH in spawns[0]["argv"][-1]
    assert N.CARTESIAN_2D_MESH not in spawns[0]["argv"][-1]
    assert out["rc"] == 0 and out["outcome"] == NativeOutcome.ok.value


def test_a_2d_case_runs_the_2d_mesher_then_the_patch_merge_in_that_order(tmp_path, spawns):
    _case(tmp_path, two_d=True)
    N._run_cartesian_mesh_local(tmp_path)
    assert len(spawns) == 2, f"expected two native stages, got {len(spawns)}"
    assert N.CARTESIAN_2D_MESH in spawns[0]["argv"][-1], "the mesher did not run first"
    assert "createPatch -overwrite" in spawns[1]["argv"][-1], "the merge did not run second"


def test_the_2d_merge_is_skipped_when_the_case_declares_no_merge_dict(tmp_path, spawns):
    _case(tmp_path, two_d=True, create_patch=False)
    N._run_cartesian_mesh_local(tmp_path)
    assert len(spawns) == 1, "createPatch ran without a createPatchDict to run from"


def test_the_command_is_argv_with_the_environment_and_workspace_pinned(tmp_path, spawns):
    N._run_cartesian_mesh_local(tmp_path)
    call = spawns[0]
    assert call["argv"][:2] == ["bash", "-lc"], "the mesher is no longer launched as an argv"
    assert call["cwd"] == str(tmp_path), "cfMesh ran outside the workspace it was given"
    assert call["env"] is not None, "the OpenFOAM environment was not supplied"
    assert call["timeout"] == 1800, "the default mesh budget changed"


def test_the_merge_stage_carries_its_own_shorter_budget(tmp_path, spawns):
    _case(tmp_path, two_d=True)
    N._run_cartesian_mesh_local(tmp_path)
    assert spawns[1]["timeout"] == N.CREATE_PATCH_TIMEOUT_S == 300, (
        "the patch merge inherited the full meshing budget")


def test_the_caller_can_shorten_the_budget(tmp_path, spawns):
    N._run_cartesian_mesh_local(tmp_path, timeout=42)
    assert spawns[0]["timeout"] == 42


# 4-6: every way the native phase fails
@pytest.mark.parametrize("rc", [1, 2, 127])
def test_a_nonzero_exit_is_an_ordinary_failure_not_a_crash(tmp_path, monkeypatch, rc):
    monkeypatch.setattr(N, "run_guarded", lambda *a, **k: _Proc(rc))
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["rc"] == rc
    assert out["outcome"] == NativeOutcome.failed.value


def test_a_missing_executable_surfaces_as_the_shells_own_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(N, "run_guarded", lambda *a, **k: _Proc(127))
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["outcome"] == NativeOutcome.failed.value and out["rc"] == 127


@pytest.mark.parametrize("rc,name", [(-11, "SIGSEGV"), (-9, "SIGKILL")])
def test_a_signalled_mesher_is_reported_as_signalled_not_as_a_failure(tmp_path, monkeypatch,
                                                                     rc, name):
    monkeypatch.setattr(N, "run_guarded", lambda *a, **k: _Proc(rc))
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["outcome"] == NativeOutcome.signalled.value
    assert name in out["log_tail"], "the signal is not named in the operator record"


def test_a_timeout_is_reported_as_a_timeout_and_never_as_success(tmp_path, monkeypatch):
    def _slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    monkeypatch.setattr(N, "run_guarded", _slow)
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["outcome"] == NativeOutcome.timed_out.value
    assert out["rc"] != 0, "a timed-out mesh reported a successful return code"


def test_a_failed_second_stage_fails_the_run_even_though_the_mesh_built(tmp_path, monkeypatch):
    _case(tmp_path, two_d=True)
    seen = []

    def _fake(argv, **kw):
        seen.append(argv[-1])
        return _Proc(0 if N.CARTESIAN_2D_MESH in argv[-1] else 3)

    monkeypatch.setattr(N, "run_guarded", _fake)
    out = N._run_cartesian_mesh_local(tmp_path)
    assert len(seen) == 2
    assert out["rc"] == 3 and out["outcome"] == NativeOutcome.failed.value


def test_the_merge_is_not_attempted_when_the_mesher_failed(tmp_path, monkeypatch):
    _case(tmp_path, two_d=True)
    seen = []

    def _fake(argv, **kw):
        seen.append(argv[-1])
        return _Proc(1)

    monkeypatch.setattr(N, "run_guarded", _fake)
    N._run_cartesian_mesh_local(tmp_path)
    assert len(seen) == 1, "the patch merge ran on a mesh that was never built"


# 7-9: dispatch, the dict scan, and confinement
def test_a_rejected_case_is_never_handed_to_an_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(N, "scan_case_dicts", lambda ws: "codeStream in system/meshDict")
    called = []
    monkeypatch.setattr("meshpipeline.contracts.mesh_execution.run_mesh",
                        lambda *a, **k: called.append(a))
    out = N.run_cartesian_mesh(tmp_path, timeout=5)
    assert called == [], "a rejected case was dispatched anyway"
    assert out["rc"] == N.RC_DICTS_REJECTED == -2
    assert "REJECTED" in out["log_tail"] and "codeStream" in out["log_tail"]
    assert out["timed_out"] is False


def test_a_clean_case_is_dispatched_and_not_run_locally(tmp_path, monkeypatch):
    monkeypatch.setattr(N, "scan_case_dicts", lambda ws: None)
    monkeypatch.setattr(N, "_run_cartesian_mesh_local",
                        lambda *a, **k: pytest.fail("the worker meshed locally"))
    seen = {}

    def _run_mesh(ws, *, engine, timeout):
        seen.update(engine=engine, timeout=timeout)
        return {"rc": 0, "log_tail": "remote"}

    monkeypatch.setattr("meshpipeline.contracts.mesh_execution.run_mesh", _run_mesh)
    out = N.run_cartesian_mesh(tmp_path, timeout=77)
    assert seen == {"engine": "cfmesh", "timeout": 77}
    assert out["log_tail"] == "remote"


def test_the_native_run_stays_inside_the_workspace_it_was_given(tmp_path, spawns):
    _case(tmp_path, two_d=True)
    N._run_cartesian_mesh_local(tmp_path)
    assert len(spawns) == 2
    for call in spawns:
        assert call["cwd"] == str(tmp_path)
        assert ".." not in call["argv"][-1], "a stage reaches outside the workspace"


def test_the_log_lands_in_the_workspace_and_is_bounded(tmp_path, monkeypatch):
    def _fake(argv, **kw):
        fh = kw.get("stdout")
        if fh is not None and hasattr(fh, "write"):
            fh.write("\n".join(f"line {i}" for i in range(200)))
        return _Proc(0)

    monkeypatch.setattr(N, "run_guarded", _fake)
    out = N._run_cartesian_mesh_local(tmp_path)
    assert (tmp_path / "cartesianMesh.log").exists(), "the native log left the workspace"
    assert len(out["log_tail"].splitlines()) <= N.LOG_TAIL_LINES == 25


def test_stale_output_from_an_earlier_attempt_is_not_read_as_this_run(tmp_path, monkeypatch):
    (tmp_path / "cartesianMesh.log").write_text("STALE from a previous attempt\n" * 40)
    monkeypatch.setattr(N, "run_guarded", lambda *a, **k: _Proc(0))
    out = N._run_cartesian_mesh_local(tmp_path)
    assert "STALE" not in out["log_tail"], "a previous attempt's log was reported as this run's"


def test_the_native_phase_returns_facts_and_never_a_verdict(tmp_path, spawns):
    out = N._run_cartesian_mesh_local(tmp_path)
    assert "success" not in out, "the native phase declared success on its own"
    assert set(out) >= {"rc", "outcome", "log_tail"}


# 7-9: the measurement beside the mesh (HEX-6 pilot: a built mesh refused for missing evidence)
def _finished_mesh(ws):
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    (ws / "constant" / "polyMesh" / "owner").write_text("owner")


def test_a_finished_mesh_is_measured_beside_itself_and_the_figures_travel_home(tmp_path, spawns,
                                                                               monkeypatch):
    import json
    _finished_mesh(tmp_path)
    seen = []
    monkeypatch.setattr(N, "check_mesh", lambda ws, **kw: (seen.append(ws), {
        "mesh_ok": True, "max_non_ortho": 41.5, "max_skewness": 2.1, "cells": 1200})[1])
    monkeypatch.setattr(N, "export_volume_vtk", lambda ws, **kw: "VTK/case_0/internal.vtu")
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["rc"] == 0 and seen == [tmp_path], "checkMesh did not run beside the mesh"
    assert out["quality"]["max_non_ortho"] == 41.5
    on_disk = json.loads((tmp_path / N.QUALITY_FILE).read_text())
    assert on_disk["max_non_ortho"] == 41.5 and on_disk["cells"] == 1200, (
        "the measurement must be written next to the polyMesh so the worker can read it")


def test_a_failed_measurement_never_loses_the_finished_mesh(tmp_path, spawns, monkeypatch):
    _finished_mesh(tmp_path)

    def _boom(ws, **kw):
        raise RuntimeError("checkMesh exploded")
    monkeypatch.setattr(N, "check_mesh", _boom)
    monkeypatch.setattr(N, "export_volume_vtk", _boom)
    out = N._run_cartesian_mesh_local(tmp_path)
    assert out["rc"] == 0 and out["outcome"] == NativeOutcome.ok.value
    assert "quality" not in out and not (tmp_path / N.QUALITY_FILE).exists()


def test_nothing_is_measured_when_the_mesher_left_no_mesh(tmp_path, spawns, monkeypatch):
    called = []
    monkeypatch.setattr(N, "check_mesh", lambda ws, **kw: called.append("check") or {})
    monkeypatch.setattr(N, "export_volume_vtk", lambda ws, **kw: called.append("vtk"))
    N._run_cartesian_mesh_local(tmp_path)
    assert called == [], "a measurement ran on a workspace with no polyMesh"
    assert len(spawns) == 1
