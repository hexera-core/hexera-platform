# Responsibility: Verify a native run is pinned to fixed threads, ranks and hash order, and stamped with what it ran under.
from __future__ import annotations

import json

from meshpipeline.engines import determinism as det
from meshpipeline.engines import dispatch


def test_the_native_environment_is_pinned_not_taken_from_the_host():
    env = det.native_environment("cfmesh")
    assert env["OMP_NUM_THREADS"] == str(det.threads_for("cfmesh"))
    assert env["OMP_DYNAMIC"] == "FALSE"
    assert env["PYTHONHASHSEED"] == "0"
    assert env["OPENBLAS_NUM_THREADS"] == env["OMP_NUM_THREADS"]


def test_snappy_ranks_come_from_the_job_not_the_host(monkeypatch):
    monkeypatch.setattr(det, "_SNAPPY_RANKS", None)
    # no quota: MESH_THREADS, never the host's 64 cores
    ranks, basis = det.snappy_ranks(None, 0, host_cpus=64)
    assert ranks == min(det.MESH_THREADS, 12) and "MESH_THREADS" in basis
    # the job's quota decides
    assert det.snappy_ranks(8, 0)[0] == 8
    # memory only lowers it, and says so
    ranks, basis = det.snappy_ranks(8, 20 * (1 << 30))
    assert ranks == 6 and "lowered from 8 to 6 by memory" in basis
    monkeypatch.setattr(det, "_SNAPPY_RANKS", 4)
    assert det.snappy_ranks(8, 0) == (4, "pinned by MESH_SNAPPY_RANKS=4")


def test_a_time_limited_decision_is_recorded_in_its_run():
    # only the CAD repair steps still decide by the clock (TIME-6)
    with det.recording() as rec:
        det.note_time_limit("cad_repair", "the original file is meshed", budget_s=120.04)
        det.note_setting("snappy_ranks", 8, "quota")
    assert rec.time_limited == [{"where": "cad_repair", "decision": "the original file is meshed",
                                 "rule": "TIME-6", "budget_s": 120.0}]
    assert rec.settings["snappy_ranks"] == {"value": 8, "basis": "quota"}
    # outside a run the note lands in the process list, drained once
    det.drain_process_notes()
    det.note_time_limit("cad_inspect", "inconclusive")
    assert [n["where"] for n in det.drain_process_notes()] == ["cad_inspect"]
    assert det.drain_process_notes() == []


def test_a_guard_trip_lands_in_its_run_beside_the_case_and_clears_for_the_replay(tmp_path):
    with det.recording() as rec:
        det.note_guard_trip("passage.measure", guard_s=240.0)
        det.note_guard_trip("snappy", workspace=tmp_path, step="pass 2", guard_s=4000.0)
    assert [t["where"] for t in rec.guard_trips] == ["passage.measure", "snappy"]
    assert rec.time_limited == []
    # only the one given a workspace is beside the case
    assert det.guard_trips(tmp_path) == [{"where": "snappy", "rule": "TIME-3", "step": "pass 2",
                                          "guard_s": 4000.0}]
    # a trip file is the run's output, never part of the case it hashes
    assert det.workspace_hashes(tmp_path)["files"] == 0
    det.clear_guard_trips(tmp_path)
    assert det.guard_trips(tmp_path) == []
    # outside a run: the process list, drained once
    det.drain_process_guard_trips()
    det.note_guard_trip("engine_ladder", rule="TIME-4", guard_s=900.0)
    assert [t["rule"] for t in det.drain_process_guard_trips()] == ["TIME-4"]
    assert det.drain_process_guard_trips() == []


def test_the_case_hash_sees_the_plan_and_ignores_what_the_run_writes(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text("maxCellSize 1;")
    (tmp_path / "geom.stl").write_text("solid x\nendsolid x\n")
    a = det.workspace_hashes(tmp_path)
    (tmp_path / "processor0").mkdir()
    (tmp_path / "processor0" / "junk").write_text("x")
    (tmp_path / det.STAMP_FILE).write_text('{"seconds": 12.3}')       # an earlier pass's record
    (tmp_path / "snappyHexMesh.log").write_text("Finished")
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "owner").write_text("1")
    assert det.workspace_hashes(tmp_path) == a
    (tmp_path / "system" / "meshDict").write_text("maxCellSize 2;")
    b = det.workspace_hashes(tmp_path)
    assert b["plan_sha256"] != a["plan_sha256"] and b["payload_sha256"] != a["payload_sha256"]


def test_two_stamps_of_one_setup_are_comparable():
    with det.recording() as rec:
        det.note_setting("snappy_ranks", 8)
        s1 = det.stamp("snappy", record=rec, hashes={"plan_sha256": "p"})
    with det.recording() as rec:
        det.note_setting("snappy_ranks", 8)
        s2 = det.stamp("snappy", record=rec, hashes={"plan_sha256": "p"})
    assert det.comparable(s1, s2) == []
    with det.recording() as rec:
        det.note_setting("snappy_ranks", 6)
        s3 = det.stamp("snappy", record=rec, hashes={"plan_sha256": "p"})
    assert det.comparable(s1, s3) == ["settings"]


def test_dispatch_pins_and_stamps_every_native_run(tmp_path, monkeypatch):
    seen = {}

    def runner(ws, *, bashrc, timeout):
        import os
        seen["omp"] = os.environ.get("OMP_NUM_THREADS")
        det.note_setting("snappy_ranks", 8, "test")
        return {"rc": 0, "timed_out": True}

    for name in det.native_environment("gmsh"):        # restored after the test
        monkeypatch.setenv(name, "unset-by-test")
    out = dispatch._run_stamped(runner, tmp_path, engine="gmsh", bashrc="", timeout=60)
    assert seen["omp"] == str(det.MESH_THREADS)
    st = out["reproducibility"]
    assert st["engine"] == "gmsh" and st["seeds"]["Mesh.RandomSeed"] == det.GMSH_RANDOM_SEED
    assert st["settings"]["snappy_ranks"]["value"] == 8
    # a run stopped at its cap is a GUARD TRIP (docs/RULES.md TIME-3), never a decision
    assert st["time_limited"] == []
    assert st["guard_trips"] == [{"where": "gmsh", "rule": "TIME-3", "guard_s": 60.0}]
    assert json.loads((tmp_path / det.STAMP_FILE).read_text())["engine"] == "gmsh"
    assert det.guard_trips(tmp_path) == st["guard_trips"]


def test_a_runner_that_names_its_tripped_step_is_not_noted_twice(tmp_path):
    def runner(ws, *, bashrc, timeout):
        det.note_guard_trip("vmtk", step="ladder step 1", guard_s=float(timeout))
        return {"rc": -1, "timed_out": True}

    out = dispatch._run_stamped(runner, tmp_path, engine="vmtk", bashrc="", timeout=60)
    assert [t.get("step") for t in out["reproducibility"]["guard_trips"]] == ["ladder step 1"]
