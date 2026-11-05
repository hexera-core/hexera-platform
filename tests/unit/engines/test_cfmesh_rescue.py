# Responsibility: Verify the near-edge rescue of a cfMesh mesh: when it triggers, the order of the levers, that the best
# measured mesh is the one kept (the mesh as built included), that the clock never skips a step (it is a guard: a step
# it stops is a guard trip), that each step's work is planned from the cells (TIME-7), that a trip with a valid mesh
# in hand keeps it (TIME-8), and that it leaves no scratch.
# No cfMesh binary runs: the runner and checkMesh are stand-ins that write and read a marker in the polyMesh.
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import meshpipeline.engines.cfmesh.rescue as RS

_BASE_DICT = ("surfaceFile \"geom.stl\";\nmaxCellSize 0.01;\n"
              "boundaryLayers\n{\n    patchBoundaryLayers\n    {\n        wall\n"
              "        { nLayers 5; thicknessRatio 1.2; allowDiscontinuity 0; }\n"
              "    }\n    optimiseLayer 1;\n}\n"
              "renameBoundary\n{\n    defaultName fixedWalls;\n}\n")


def _ws(tmp_path: Path, marker: str = "as built", meshdict: str = _BASE_DICT) -> Path:
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text(marker)
    (pm / "faces").write_text("faces of " + marker)
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text(meshdict)
    return tmp_path


class _Proc:
    def __init__(self, rc):
        self.returncode = rc


def _runner(calls: list, *, optimise_rc=0, remesh_rc=0, optimise_log=None):
    def run(argv, *, cwd, env, stdout, stderr, timeout, stall_s=None, watch=None):
        cmd = argv[-1]
        calls.append({"cmd": cmd, "timeout": timeout, "watched": watch is not None})
        pm = Path(cwd) / "constant" / "polyMesh"
        if "improveMeshQuality" in cmd and optimise_log is not None:
            # the optimiser prints its loops; the watch reads them from the log as they come
            stdout.write(optimise_log)
            stdout.flush()
            if watch is not None and watch():
                from meshpipeline.sandbox.safe_exec import StoppedByWatch
                raise StoppedByWatch(argv, watch())
        if "improveMeshQuality" in cmd:
            if optimise_rc == 0:
                prev = (pm / "owner").read_text()
                (pm / "owner").write_text(f"optimised({prev})")
            return _Proc(optimise_rc)
        if "cartesianMesh" in cmd:
            if remesh_rc == 0:
                pm.mkdir(parents=True, exist_ok=True)
                dic = (Path(cwd) / "system" / "meshDict").read_text()
                assert "featureSizeFactor" in dic, "the re-mesh ran on the dict as asked"
                (pm / "owner").write_text("remeshed")
            return _Proc(remesh_rc)
        raise AssertionError(cmd)
    return run


def _measure(table: dict):
    def m(ws, *, bashrc):
        assert not (Path(ws) / "mesh_quality.json").exists(), "a cached figure was measured"
        marker = (Path(ws) / "constant" / "polyMesh" / "owner").read_text()
        return dict(table[marker])
    return m


_BROKEN = {"cells": 979_224, "max_non_ortho": 84.3, "fatal": ["incorrectly oriented faces"],
           "skew_faces": 66}


def _rescue(ws, q, table, calls, *, left=3600.0, mesher=30.0, **kw):
    return RS.rescue(ws, q, bashrc="/dev/null", deadline=time.monotonic() + left,
                     mesher_seconds=mesher, measure=_measure(table),
                     runner=_runner(calls, **kw), env={})


def test_a_mesh_inside_the_edge_is_left_alone(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    q = {"cells": 1000, "max_non_ortho": 61.0, "fatal": []}
    assert _rescue(ws, q, {}, calls) is q
    assert calls == [] and not (ws / RS.RESCUE_FILE).exists()


def test_the_edge_is_a_fatal_defect_or_cells_past_75_degrees():
    assert RS.near_edge({"fatal": ["negative-volume cells"]})
    assert RS.near_edge({"max_non_ortho": 82.4, "fatal": []})
    assert not RS.near_edge({"max_non_ortho": 74.9, "fatal": []})
    assert not RS.near_edge({})


def test_no_fatal_defect_beats_any_angle_then_the_lowest_angle_wins():
    c = {"cells": 1000}
    assert RS.better({**c, "max_non_ortho": 88.0, "fatal": []},
                     {**c, "max_non_ortho": 60.0, "fatal": ["x"]})
    assert not RS.better({**c, "max_non_ortho": 60.0, "fatal": ["x"]}, {**c, "max_non_ortho": 88.0})
    assert RS.better({**c, "max_non_ortho": 79.0}, {**c, "max_non_ortho": 84.3})
    assert not RS.better({**c, "max_non_ortho": 84.0}, {**c, "max_non_ortho": 84.3}), \
        "noise is no gain"


def test_the_optimiser_fixes_a_broken_mesh_and_its_mesh_is_kept(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"cells": 979_224, "max_non_ortho": 79.1, "fatal": []},
             "remeshed": {"cells": 980_000, "max_non_ortho": 81.0, "fatal": []},
             "optimised(remeshed)": {"cells": 980_000, "max_non_ortho": 77.0, "fatal": []}}
    out = _rescue(ws, dict(_BROKEN), table, calls)
    assert out["fatal"] == [] and out["max_non_ortho"] == 77.0
    assert out["rescue"]["kept"] == "re-meshed protecting the layers, then optimised"
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == [
        "improveMeshQuality", "cartesianMesh", "improveMeshQuality"]
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "optimised(remeshed)"
    # the meshDict beside the mesh is the one that built it
    assert "featureSizeFactor 0.15" in (ws / "system" / "meshDict").read_text()
    assert not (ws / RS.SCRATCH).exists()
    rec = json.loads((ws / RS.RESCUE_FILE).read_text())
    assert rec["before"]["fatal"] and not rec["after"]["fatal"]
    assert "validity edge" in rec["note"] and "Kept" in rec["note"]


def test_a_fixed_mesh_is_not_re_meshed(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"cells": 979_224, "max_non_ortho": 70.2, "fatal": []}}
    out = _rescue(ws, dict(_BROKEN), table, calls)
    assert out["rescue"]["kept"] == "optimised" and len(calls) == 1
    assert (ws / "system" / "meshDict").read_text() == _BASE_DICT, "the dict as asked stays"
    assert (ws / "constant" / "polyMesh" / "faces").read_text() == "faces of as built"


def test_when_nothing_measures_better_the_mesh_as_built_is_kept(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"max_non_ortho": 84.3, "fatal": ["incorrectly oriented faces"]},
             "remeshed": {"max_non_ortho": 90.0, "fatal": ["negative-volume cells"]},
             "optimised(remeshed)": {"max_non_ortho": 89.0, "fatal": ["negative-volume cells"]}}
    out = _rescue(ws, dict(_BROKEN), table, calls)
    assert out["rescue"]["kept"] == "as built" and out["fatal"] == _BROKEN["fatal"]
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"
    assert (ws / "system" / "meshDict").read_text() == _BASE_DICT
    assert "none measured better" in out["rescue"]["note"]


def test_a_missing_optimiser_binary_costs_nothing(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    table = {"remeshed": {"cells": 980_000, "max_non_ortho": 71.0, "fatal": []}}
    out = _rescue(ws, dict(_BROKEN), table, calls, optimise_rc=127)
    assert out["rescue"]["kept"] == "re-meshed protecting the layers"
    assert out["rescue"]["tried"][0]["rc"] == 127


_NOT_FIXED = {"optimised(as built)": {"max_non_ortho": 84.3, "fatal": ["incorrectly oriented faces"]},
              "remeshed": {"cells": 980_000, "max_non_ortho": 81.0, "fatal": []},
              "optimised(remeshed)": {"cells": 980_000, "max_non_ortho": 77.0, "fatal": []}}


@pytest.mark.parametrize("left,mesher", [(3600.0, 30.0), (400.0, 600.0), (90.0, 2000.0)])
def test_the_same_mesh_gets_the_same_rescue_whatever_the_time_left(tmp_path, left, mesher):
    # docs/RULES.md TIME-1: the steps are planned work. Before D3 a cap that "did not hold another
    # run" (400 s left after a 600 s mesher) skipped the re-mesh and kept the mesh as built, so a
    # busy machine delivered a different mesh. Now every case below runs the same steps and keeps
    # the same mesh; the time left is only each step's guard.
    ws = _ws(tmp_path)
    calls: list = []
    out = _rescue(ws, dict(_BROKEN), dict(_NOT_FIXED), calls, left=left, mesher=mesher)
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == [
        "improveMeshQuality", "cartesianMesh", "improveMeshQuality"]
    assert out["rescue"]["kept"] == "re-meshed protecting the layers, then optimised"
    assert out["max_non_ortho"] == 77.0 and "guard_tripped" not in out["rescue"]
    assert all(c["timeout"] <= left for c in calls), "the time left is each step's guard"


def test_a_step_its_guard_stops_is_a_guard_trip_and_the_rescue_stops(tmp_path):
    from meshpipeline.engines import determinism as det
    ws = _ws(tmp_path)
    calls: list = []
    with det.recording() as rec:
        out = _rescue(ws, dict(_BROKEN), dict(_NOT_FIXED), calls, optimise_rc=-1)
    # stopped at the first step: no re-mesh is started after a trip, the as-built mesh is back in
    # place, and the trip is in the run's record (the attempt is replayed as ours, TIME-3)
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == ["improveMeshQuality"]
    assert out["rescue"]["guard_tripped"] == "optimised"
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"
    assert [(t["where"], t["step"]) for t in rec.guard_trips] == [("cfmesh.rescue", "optimised")]
    assert rec.time_limited == []
    assert "guard clock" in out["rescue"]["note"]


def test_the_optimiser_loops_are_planned_from_the_cells():
    # TIME-7: the optimiser's work is cells x loops, planned from the cells - the same mesh gets the
    # same loops on any machine, never more than the rescue asked before, never under cfMesh's own
    assert RS.optimise_loops(500_000) == RS.OPTIMISE_LOOPS_MAX
    assert RS.optimise_loops(4_000_000) == RS.OPTIMISE_CELL_LOOPS // 4_000_000
    assert RS.optimise_loops(5_900_000) == max(RS.OPTIMISE_LOOPS_MIN,
                                               RS.OPTIMISE_CELL_LOOPS // 5_900_000)
    assert RS.optimise_loops(50_000_000) == RS.OPTIMISE_LOOPS_MIN
    assert RS.optimise_loops(None) == RS.OPTIMISE_LOOPS_MIN
    for cells in (1e5, 1e6, 3e6, 6e6):
        assert RS.OPTIMISE_LOOPS_MIN <= RS.optimise_loops(cells) <= RS.OPTIMISE_LOOPS_MAX
    assert f"-nLoops {RS.optimise_loops(4_000_000)} " in RS.optimise_args(4_000_000)


def test_each_step_runs_under_its_own_planned_guard(tmp_path):
    # the run's whole guard is no step's clock: on the full benchmark the optimiser ran 2.5 hours
    # into a 4-hour guard on a tangled 3.5 M cell mesh. Each step's subprocess timeout is its own
    # guard - 3 x its planned work at the slow rate - within what is left of the run's
    ws = _ws(tmp_path)
    calls: list = []
    q = dict(_BROKEN, cells=500_000)
    _rescue(ws, q, dict(_NOT_FIXED), calls, left=14_400.0)
    opt = RS.optimise_work(500_000, ceiling_s=14_400.0)
    assert opt.amount == 500_000 * RS.optimise_loops(500_000)
    assert calls[0]["timeout"] == opt.guard_seconds < 14_000
    assert f"-nLoops {RS.optimise_loops(500_000)} " in calls[0]["cmd"]
    # and a short run guard still bounds a step
    short = RS.optimise_work(3_500_000, ceiling_s=120.0)
    assert short.guard_seconds <= 120


def test_a_trip_with_a_valid_mesh_in_hand_keeps_it(tmp_path):
    # TIME-8: the mesh as built is valid (no fatal defect, only past 75 degrees). Its optimiser
    # running past its guard no longer throws it away for a replay and "ran out of time": the run
    # goes on to its checks with it, with a note, and the trip does not end the attempt
    from meshpipeline.contracts.work_limits import TIME_KEEP_VALID, ends_attempt
    from meshpipeline.engines import determinism as det
    ws = _ws(tmp_path)
    calls: list = []
    valid = {"cells": 979_224, "max_non_ortho": 82.0, "fatal": [], "skew_faces": 3}
    with det.recording() as rec:
        out = _rescue(ws, dict(valid), dict(_NOT_FIXED), calls, optimise_rc=-1)
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == ["improveMeshQuality"]
    assert out["rescue"]["guard_tripped"] == "optimised"
    assert out["rescue"]["guard_kept"] == "as built" and out["rescue"]["kept"] == "as built"
    assert out["max_non_ortho"] == 82.0 and out["fatal"] == []
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"
    assert [t["rule"] for t in rec.guard_trips] == [TIME_KEEP_VALID]
    assert not any(ends_attempt(t) for t in rec.guard_trips)
    assert "is kept" in out["rescue"]["note"] and "run again" not in out["rescue"]["note"]


def test_a_trip_in_the_re_mesh_keeps_the_optimised_mesh(tmp_path):
    # the optimiser cleared the fatal defect but left the mesh past 75 degrees, so the re-mesh ran,
    # and its guard stopped it: the optimised mesh, measured valid before it, is the one kept
    from meshpipeline.contracts.work_limits import TIME_KEEP_VALID
    from meshpipeline.engines import determinism as det
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"cells": 979_224, "max_non_ortho": 79.1, "fatal": []}}
    with det.recording() as rec:
        out = _rescue(ws, dict(_BROKEN), table, calls, remesh_rc=-1)
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == [
        "improveMeshQuality", "cartesianMesh"]
    assert out["rescue"]["guard_tripped"] == RS.REMESHED
    assert out["rescue"]["guard_kept"] == "optimised" and out["fatal"] == []
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "optimised(as built)"
    assert (ws / "system" / "meshDict").read_text() == _BASE_DICT, "the dict that built it"
    assert [t["rule"] for t in rec.guard_trips] == [TIME_KEEP_VALID]
    assert not (ws / RS.SCRATCH).exists()


def test_a_trip_with_nothing_valid_still_ends_the_attempt():
    # TIME-3 is unchanged when no valid mesh is in hand: the attempt is replayed
    from meshpipeline.contracts.work_limits import (
        TIME_JOB_GUARD,
        TIME_KEEP_VALID,
        TIME_TRIP,
        TIME_WAIT,
        ends_attempt,
    )
    assert ends_attempt({"where": "cfmesh.rescue", "rule": TIME_TRIP})
    assert ends_attempt({"where": "builder", "rule": TIME_JOB_GUARD})
    assert ends_attempt({"where": "snappy"})
    assert not ends_attempt({"where": "engine_ladder", "rule": TIME_WAIT})
    assert not ends_attempt({"where": "cfmesh.rescue", "rule": TIME_KEEP_VALID})
    assert not ends_attempt(None)


def test_the_executor_judges_an_attempt_whose_trip_kept_a_valid_mesh():
    from meshpipeline.contracts.work_limits import TIME_KEEP_VALID, TIME_TRIP
    from meshpipeline.pipeline.executor import _attempt_ending
    kept = {"where": "cfmesh.rescue", "rule": TIME_KEEP_VALID, "kept": "as built"}
    ending = {"where": "cfmesh.rescue", "rule": TIME_TRIP}
    assert _attempt_ending([kept]) == []
    assert _attempt_ending([kept, ending]) == [ending]


def _loops(*bad: int) -> str:
    return "".join(f"{i}.0 Iteration 0. Number of bad faces is {n}\n" for i, n in enumerate(bad))


def test_the_optimiser_is_not_untangling_only_on_its_own_count():
    # the full benchmark's microfluidics spiral: still at the first loop's count by loop 3
    assert RS.untangle_stalled(_loops(52_518, 35_250, 52_544))
    # every dev-side mesh that untangled, or came down to a handful, is left to run its loops
    assert not RS.untangle_stalled(_loops(288_087, 2_070, 161))      # vmr_0249
    assert not RS.untangle_stalled(_loops(7_184, 103, 169))          # vmr_0288
    assert not RS.untangle_stalled(_loops(3_409, 3, 6, 5, 4))        # vmr_0050
    assert not RS.untangle_stalled(_loops(10_865, 0))                # a mesh that untangles
    # a handful of faces never stops it (the steps after the loops cleared vmr_0050)
    assert not RS.untangle_stalled(_loops(900, 800, 700))
    # too early to say
    assert not RS.untangle_stalled(_loops(52_518, 52_000))
    assert not RS.untangle_stalled("")


def test_an_optimiser_that_is_not_untangling_is_stopped_and_the_rescue_goes_on(tmp_path):
    # stopped on its own count (TIME-7): not a guard trip, nothing it left is kept, and the
    # protective re-mesh still runs - the same steps on any machine
    from meshpipeline.engines import determinism as det
    ws = _ws(tmp_path)
    calls: list = []
    table = {"remeshed": {"cells": 980_000, "max_non_ortho": 81.0, "fatal": []},
             "optimised(remeshed)": {"cells": 980_000, "max_non_ortho": 77.0, "fatal": []}}
    with det.recording() as rec:
        out = RS.rescue(ws, dict(_BROKEN), bashrc="/dev/null", deadline=time.monotonic() + 3600,
                        mesher_seconds=30.0, measure=_measure(table), env={},
                        runner=_runner(calls, optimise_log=_loops(52_518, 35_250, 52_544)))
    assert all(c["watched"] for c in calls if "improveMeshQuality" in c["cmd"])
    first = out["rescue"]["tried"][0]
    assert first["step"] == "optimised" and "not untangling" in first["stopped"]
    assert "guard_tripped" not in out["rescue"] and rec.guard_trips == []
    assert out["rescue"]["kept"] == RS.REMESHED and out["fatal"] == []


def test_a_check_that_died_early_never_wins(tmp_path):
    # checkMesh that stopped before reporting still comes back with no fatal defect listed
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"mesh_ok": False, "fatal": [], "skew_faces": 0},
             "remeshed": {"mesh_ok": False, "fatal": []},
             "optimised(remeshed)": {"mesh_ok": False, "fatal": []}}
    out = _rescue(ws, dict(_BROKEN), table, calls)
    assert out["rescue"]["kept"] == "as built"
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"
    assert not RS.better({"fatal": []}, dict(_BROKEN))


def test_a_candidate_is_measured_whatever_the_clock_says(tmp_path, monkeypatch):
    # before D3 a candidate with "no time left to measure it" was dropped and the mesh as built
    # kept; the measurement is planned work, so the clock never decides it (TIME-1)
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"cells": 9, "max_non_ortho": 60.0, "fatal": []}}
    clock = iter([0.0, 0.0] + [1e6] * 50)          # the optimiser runs, then the clock is spent
    monkeypatch.setattr(RS.time, "monotonic", lambda: next(clock))
    out = RS.rescue(ws, dict(_BROKEN), bashrc="b", deadline=500.0, mesher_seconds=30.0,
                    check_seconds=10.0, measure=_measure(table), runner=_runner(calls), env={})
    assert out["rescue"]["kept"] == "optimised"
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "optimised(as built)"


def test_a_case_that_cannot_be_read_back_is_not_touched(tmp_path):
    ws = _ws(tmp_path)
    (ws / "system" / "meshDict").unlink()
    (ws / "system" / "meshDict").mkdir()            # reading it fails
    calls: list = []
    out = _rescue(ws, dict(_BROKEN), {}, calls)
    assert out == _BROKEN and calls == []
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"
    assert not (ws / RS.SCRATCH).exists()


def test_a_mesh_too_big_for_a_second_run_gets_the_optimiser_only(tmp_path):
    # the mitral ventricle base: 5.2 M cells, invalid at 5 layers
    ws = _ws(tmp_path)
    calls: list = []
    big = {**_BROKEN, "cells": 5_200_000}
    table = {"optimised(as built)": {"cells": 5_200_000, "max_non_ortho": 80.0, "fatal": []}}
    out = _rescue(ws, big, table, calls)
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == ["improveMeshQuality"]
    assert out["rescue"]["kept"] == "optimised" and out["fatal"] == []
    assert any("second run" in str(s.get("skipped")) for s in out["rescue"]["tried"])


def test_a_mesh_too_big_to_copy_is_not_rescued(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    out = _rescue(ws, {**_BROKEN, "cells": RS.RESCUE_MAX_CELLS + 1}, {}, calls)
    assert calls == [] and "skipped" in out["rescue"]
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"


def test_the_protective_dict_sets_the_layer_optimiser_and_keeps_the_rest():
    out = RS.protective_meshdict(_BASE_DICT)
    assert "optimisationParameters { nSmoothNormals 10; maxNumIterations 10; " \
           "featureSizeFactor 0.15; relThicknessTol 0.05; }" in out
    assert "untangleLayers 1;" in out and out.count("optimiseLayer 1;") == 1
    assert out.startswith(_BASE_DICT.split("boundaryLayers")[0])
    assert out.endswith(_BASE_DICT.split("    optimiseLayer 1;\n}\n")[1])
    # a dict already carrying both (the layer-verify renderer's) gets the parameters only
    again = RS.protective_meshdict(_BASE_DICT.replace(
        "    optimiseLayer 1;\n", "    optimiseLayer 1;\n    untangleLayers 1;\n"))
    assert again.count("untangleLayers") == 1
    assert RS.protective_meshdict(out).count("optimisationParameters") == 1
    assert RS.protective_meshdict("maxCellSize 1;\n") is None


@pytest.mark.parametrize("two_d", [True, False])
def test_the_native_run_rescues_only_a_near_edge_3d_mesh(tmp_path, monkeypatch, two_d):
    import meshpipeline.engines.cfmesh.native as N

    seen: list = []

    def _fake_rescue(ws, q, **kw):
        seen.append(kw)
        return {**q, "rescue": {"note": "n"}}
    monkeypatch.setattr(RS, "rescue", _fake_rescue)
    out = N._rescued(tmp_path, dict(_BROKEN), bashrc="b", timing={"cartesianMesh_s": 40.0,
                     "checkMesh_s": 10.0}, t0=time.monotonic(), timeout=1200, is_2d=two_d)
    assert out["rescue"] == {"note": "n"} and seen[0]["is_2d"] is two_d
    # the steps after the mesher keep their time: the deadline is short of the cap
    assert seen[0]["deadline"] <= time.monotonic() + 1200 - (2 * 10.0 + N.POST_MESH_RESERVE_S) + 1
    assert N._rescued(tmp_path, {"max_non_ortho": 50.0}, bashrc="b", timing={}, t0=0.0,
                      timeout=1, is_2d=False) == {"max_non_ortho": 50.0}
