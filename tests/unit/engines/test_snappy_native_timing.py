# Responsibility: Verify the snappy native run records the wall seconds of every step beside the
# mesh, and measures the finished mesh (checkMesh) while foamToVTK exports it, not after.
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from meshpipeline.engines.snappy import native as N
from meshpipeline.engines.snappy.parallel_stages import StageResult


def _stage(name: str, command: str, dt: float) -> StageResult:
    return StageResult(name=name, command=command, log=f"{name}.log", rc=0, timed_out=False,
                       dt_s=dt)


def test_stage_timing_names_the_tool_mpirun_launched_and_reads_snappys_own_phase_clocks():
    stages = [_stage("blockMesh", "blockMesh", 1.5),
              _stage("mpirun", "mpirun --allow-run-as-root --oversubscribe -np 8 "
                               "snappyHexMesh -overwrite -parallel", 1010.2),
              _stage("reconstructParMesh", "reconstructParMesh -constant", 50.0),
              _stage("reconstructParMesh", "reconstructParMesh -constant", 3.2)]
    log = ("Mesh refined in = 281.63 s.\nMesh snapped in = 418.86 s.\n"
           "Layers added in = 297.96 s.\nFinished meshing in = 1004.5 s.\n")
    t = N.stage_timing(stages, log, ranks=8)
    assert t["ranks"] == 8
    assert t["snappyHexMesh_s"] == 1010.2 and "mpirun_s" not in t
    assert t["reconstructParMesh_s"] == 53.2                 # a step run twice adds up
    assert (t["castellate_s"], t["snap_s"], t["layers_s"]) == (281.6, 418.9, 298.0)


def test_stage_timing_without_a_log_has_no_phase_keys():
    t = N.stage_timing([_stage("blockMesh", "blockMesh", 1.0)], "", ranks=1)
    assert t == {"ranks": 1, "blockMesh_s": 1.0}


def test_the_mesh_is_measured_while_it_is_exported_and_every_step_is_timed(tmp_path, monkeypatch):
    import meshpipeline.engines.snappy.foam_exec as FE

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        ws = Path(k.get("cwd") or tmp_path)
        (ws / "constant" / "polyMesh").mkdir(parents=True, exist_ok=True)
        for f in ("points", "faces", "owner", "neighbour", "boundary"):
            (ws / "constant" / "polyMesh" / f).write_text("x\n")
        return _Done()

    spans: dict[str, tuple[float, float, str]] = {}

    def fake_check(ws, **k):
        t0 = time.monotonic()
        time.sleep(0.4)
        spans["checkMesh"] = (t0, time.monotonic(), threading.current_thread().name)
        return {"mesh_ok": True, "cells": 10, "fatal": []}

    def fake_vtk(ws, **k):
        t0 = time.monotonic()
        time.sleep(0.4)
        spans["foamToVTK"] = (t0, time.monotonic(), threading.current_thread().name)
        return "VTK/case_0/internal.vtu"

    monkeypatch.setattr(N, "run_guarded", fake_run)
    monkeypatch.setattr(N, "_foam_version", lambda *a, **k: "v2412")
    monkeypatch.setattr(N, "_rank_count", lambda: 1)
    monkeypatch.setattr(FE, "check_mesh", fake_check)
    monkeypatch.setattr(FE, "export_volume_vtk", fake_vtk)
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)

    out = N._run_snappy_local(ws)

    (c0, c1, c_thread), (v0, v1, v_thread) = spans["checkMesh"], spans["foamToVTK"]
    assert c_thread != v_thread, "the export ran on the measuring thread, after the measurement"
    assert c0 < v1 and v0 < c1, "checkMesh and foamToVTK did not overlap"
    t = out["timing"]
    assert t["measure_wall_s"] < t["checkMesh_s"] + t["foamToVTK_s"]
    assert out["quality"]["cells"] == 10
    on_disk = json.loads((ws / N.TIMING_FILE).read_text())
    assert on_disk == t
    for key in ("ranks", "blockMesh_s", "surfaceFeatureExtract_s", "snappyHexMesh_s",
                "checkMesh_s", "foamToVTK_s", "measure_wall_s", "total_s"):
        assert key in t, key
    assert json.loads((ws / "mesh_quality.json").read_text())["cells"] == 10


def test_a_run_without_a_mesh_writes_its_timing_and_measures_nothing(tmp_path, monkeypatch):
    import meshpipeline.engines.snappy.foam_exec as FE

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = ""

    called: list[str] = []
    monkeypatch.setattr(N, "run_guarded", lambda *a, **k: _Failed())
    monkeypatch.setattr(N, "_foam_version", lambda *a, **k: "v2412")
    monkeypatch.setattr(N, "_rank_count", lambda: 8)
    monkeypatch.setattr(FE, "check_mesh", lambda *a, **k: called.append("checkMesh") or {})
    monkeypatch.setattr(FE, "export_volume_vtk", lambda *a, **k: called.append("foamToVTK"))
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)

    out = N._run_snappy_local(ws)

    assert called == []
    assert out["timing"]["ranks"] == 8 and "blockMesh_s" in out["timing"]
    assert json.loads((ws / N.TIMING_FILE).read_text())["ranks"] == 8
