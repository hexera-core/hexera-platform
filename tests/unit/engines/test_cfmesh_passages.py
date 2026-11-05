# Responsibility: Verify that a cfMesh case whose confirmed openings lie on several separate
# passages meshes every one of them: staged one surface per passage, each further one meshed in a
# case of its own and merged, written into the launch contract, and judged solvable passage by
# passage - so no declared port comes back with no faces because the mesher kept another region.
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import meshpipeline.engines.cfmesh.native as N
from meshpipeline.cad.fluid_passages import PASSAGES_FILE
from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary
from meshpipeline.engines.case_contract import _passage_solids, stl_solid_names
from meshpipeline.engines.cfmesh.cfmesh_runner import _stage_passages
from meshpipeline.engines.cfmesh.solvability import _passage_pins


def _tube(ws: Path, lo, hi, port: str) -> tuple[list, list]:
    tris = _box_triangles(lo, hi)
    return tris[:-2], tris[-2:]


def _staged_two(ws: Path) -> dict:
    wa, la = _tube(ws, (0, 0, 0), (1, 1, 4), "inlet")
    wb, lb = _tube(ws, (3, 0, 0), (4, 1, 4), "outlet_1")
    out = {}
    for name, tris in {"wall": wa + wb, "inlet": la, "outlet_1": lb}.items():
        p = ws / f"{name}.stl"
        write_stl_binary(p, tris)
        out[name] = str(p)
    return out


def test_two_passages_stage_the_first_in_the_case_and_the_second_as_its_own_run(tmp_path):
    decl = [{"name": "inlet", "type": "inlet"}, {"name": "outlet_1", "type": "outlet"},
            {"name": "wall", "type": "wall"}]
    st = _stage_passages(tmp_path, _staged_two(tmp_path), "wall", decl)
    assert "error" not in st and sorted(st["srcs"]) == ["inlet", "wall"]
    rec = json.loads((tmp_path / PASSAGES_FILE).read_text())
    assert rec["runs"] == ["_passages/p1"] and rec["ports"] == [["inlet"], ["outlet_1"]]
    assert stl_solid_names(tmp_path / "_passages" / "p1" / "geom.stl") == ["wall", "outlet_1"]
    assert st["bbox"] == [[0.0, 0.0, 0.0], [4.0, 1.0, 4.0]]          # every passage's box
    assert sorted(st["sizing"]) == ["inlet", "outlet_1", "wall"] and len(st["sizing"]["wall"]) == 2
    assert any("2 separate passages" in n for n in st["notes"])
    # the launch contract reads the further passage's solids as the case's boundary too
    assert _passage_solids(tmp_path) == ["wall", "outlet_1"]


def test_one_passage_is_staged_as_it_was(tmp_path):
    w, lid = _tube(tmp_path, (0, 0, 0), (1, 1, 1), "inlet")
    srcs = {}
    for name, tris in {"wall": w, "inlet": lid}.items():
        write_stl_binary(tmp_path / f"{name}.stl", tris)
        srcs[name] = str(tmp_path / f"{name}.stl")
    st = _stage_passages(tmp_path, srcs, "wall", [{"name": "inlet", "type": "inlet"}])
    assert st["srcs"] == srcs and st["bbox"] is None and not (tmp_path / PASSAGES_FILE).exists()


def test_a_port_into_nothing_meshable_is_refused_before_the_run_with_the_way_out(tmp_path):
    w, lid = _tube(tmp_path, (0, 0, 0), (1, 1, 4), "inlet")
    free = [((5, 5, 0), (6, 5, 0), (6, 6, 0)), ((5, 5, 0), (6, 6, 0), (5, 6, 0))]
    srcs = {}
    for name, tris in {"wall": w, "inlet": lid, "outlet_2": free}.items():
        write_stl_binary(tmp_path / f"{name}.stl", tris)
        srcs[name] = str(tmp_path / f"{name}.stl")
    st = _stage_passages(tmp_path, srcs, "wall", [{"name": "inlet", "type": "inlet"}])
    assert "outlet_2" in st["error"] and "not an opening" in st["error"] and "verbatim" in st["next"]


class _Proc:
    def __init__(self, rc=0):
        self.returncode = rc


def test_each_further_passage_is_meshed_in_its_own_case_and_merged_in(tmp_path, monkeypatch):
    ws = tmp_path
    (ws / "system").mkdir()
    (ws / "system" / "meshDict").write_text('surfaceFile "geom.fms";\nmaxCellSize 0.1;\n')
    (ws / "system" / "controlDict").write_text("x")
    sub = ws / "_passages" / "p1"
    sub.mkdir(parents=True)
    (sub / "geom.stl").write_text("solid wall\nendsolid wall\n")
    (ws / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"], "ports": [["inlet"], ["outlet_1"]]}))
    calls = []

    def _fake(argv, **kw):
        cmd, cwd = argv[-1], Path(kw["cwd"])
        calls.append((cmd.split("&& ")[-1], cwd))
        if "cartesianMesh" in cmd:
            (cwd / "constant" / "polyMesh").mkdir(parents=True, exist_ok=True)
            (cwd / "constant" / "polyMesh" / "owner").write_text("x")
        return _Proc(1 if "surfaceFeatureEdges" in cmd else 0)    # no fms: the raw STL is meshed

    monkeypatch.setattr(N, "run_guarded", _fake)
    rc, timed_out, why = N.mesh_passages(ws)
    assert (rc, timed_out, why) == (0, False, "")
    steps = [c for c, _ in calls]
    assert steps[1] == "cartesianMesh" and calls[1][1] == sub.resolve()
    assert steps[2] == "mergeMeshes -overwrite . _passages/p1" and calls[2][1] == ws
    assert 'surfaceFile "geom.stl";' in (sub / "system" / "meshDict").read_text()
    assert not (sub / "constant" / "polyMesh").exists()          # the merged mesh is the case's


def test_a_passage_that_does_not_mesh_fails_the_run_naming_its_openings(tmp_path, monkeypatch):
    ws = tmp_path
    (ws / "system").mkdir()
    (ws / "system" / "meshDict").write_text('surfaceFile "geom.fms";\n')
    (ws / "_passages" / "p1").mkdir(parents=True)
    (ws / "_passages" / "p1" / "geom.stl").write_text("solid wall\nendsolid wall\n")
    (ws / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"], "ports": [["inlet"], ["outlet_1"]]}))
    monkeypatch.setattr(N, "run_guarded", lambda argv, **kw: _Proc(1 if "cartesianMesh" in argv[-1] else 0))
    rc, _t, why = N.mesh_passages(ws)
    assert rc == 1 and "passage 2 (outlet_1) did not mesh" in why


def test_a_mesh_of_two_passages_pins_a_pressure_in_each_and_an_orphan_region_stays_unpinned():
    owner = np.array([0, 1, 3, 4])            # cells 0-1-2 and 3-4-5: two groups
    neigh = np.array([1, 2, 4, 5])
    assert _passage_pins(owner, neigh, 6, 2) == (0, 3)
    assert _passage_pins(owner, neigh, 6, 1) == (0,)        # one passage declared: 0 alone
    assert _passage_pins(owner, neigh, 6, 3) == (0,)        # a split it was not meshed as


@pytest.mark.parametrize("bad", ["{", json.dumps({"runs": ["_passages/p9"]})])
def test_an_unreadable_passages_record_is_refused_by_the_launch_check(tmp_path, bad):
    from meshpipeline.engines.case_contract import CaseUnreadable
    (tmp_path / PASSAGES_FILE).write_text(bad)
    with pytest.raises(CaseUnreadable):
        _passage_solids(tmp_path)
