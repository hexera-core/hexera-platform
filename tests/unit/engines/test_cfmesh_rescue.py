# Responsibility: Verify the near-edge rescue of a cfMesh mesh: when it triggers, the order of the levers, that the best
# measured mesh is the one kept (the mesh as built included), and that it never runs past its deadline or leaves scratch.
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


def _runner(calls: list, *, optimise_rc=0, remesh_rc=0):
    def run(argv, *, cwd, env, stdout, stderr, timeout):
        cmd = argv[-1]
        calls.append({"cmd": cmd, "timeout": timeout})
        pm = Path(cwd) / "constant" / "polyMesh"
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
    assert RS.better({"max_non_ortho": 88.0, "fatal": []}, {"max_non_ortho": 60.0, "fatal": ["x"]})
    assert not RS.better({"max_non_ortho": 60.0, "fatal": ["x"]}, {"max_non_ortho": 88.0})
    assert RS.better({"max_non_ortho": 79.0}, {"max_non_ortho": 84.3})
    assert not RS.better({"max_non_ortho": 84.0}, {"max_non_ortho": 84.3}), "noise is no gain"


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
    table = {"remeshed": {"max_non_ortho": 71.0, "fatal": []}}
    out = _rescue(ws, dict(_BROKEN), table, calls, optimise_rc=127)
    assert out["rescue"]["kept"] == "re-meshed protecting the layers"
    assert out["rescue"]["tried"][0]["rc"] == 127


def test_no_re_mesh_when_the_cap_does_not_hold_another_run(tmp_path):
    ws = _ws(tmp_path)
    calls: list = []
    table = {"optimised(as built)": {"max_non_ortho": 84.3, "fatal": ["incorrectly oriented faces"]}}
    out = _rescue(ws, dict(_BROKEN), table, calls, left=400.0, mesher=600.0)
    assert [c["cmd"].split("&& ")[-1].split()[0] for c in calls] == ["improveMeshQuality"]
    assert calls[0]["timeout"] <= 400
    assert any("no time left" in str(s.get("skipped")) for s in out["rescue"]["tried"])
    assert (ws / "constant" / "polyMesh" / "owner").read_text() == "as built"


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
