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

_RESCUE_PASSAGE = N.rescue_passage


@pytest.fixture(autouse=True)
def _no_passage_check(monkeypatch):
    """No checkMesh here: a further passage's own check (INS-31) is its own test."""
    monkeypatch.setattr(N, "rescue_passage", lambda sub, **kw: None)


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


# INS-29: an opening of a further passage that lands in a region cartesianMesh dropped

def _boundary_text(counts: dict) -> str:
    body = "".join(f"    {n}\n    {{\n        type patch;\n        nFaces {c};\n        startFace 0;\n    }}\n"
                   for n, c in counts.items())
    return f"FoamFile\n{{\n    format ascii;\n    class polyBoundaryMesh;\n}}\n{len(counts)}\n(\n{body})\n"


def _two_passage_case(ws: Path) -> Path:
    (ws / "system").mkdir()
    (ws / "system" / "meshDict").write_text('surfaceFile "geom.fms";\nmaxCellSize 0.1;\nlocalRefinement\n{\n'
                                            "    wall { cellSize 0.02; }\n}\n")
    sub = ws / "_passages" / "p1"
    sub.mkdir(parents=True)
    (sub / "geom.stl").write_text("solid wall\nendsolid wall\n")
    (ws / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"],
                                                "ports": [["inlet"], ["outlet_2", "outlet_3"]]}))
    return sub


def _fake_mesher(found: list[int], sizes: list[str]):
    """cartesianMesh on the passage: outlet_3 gets `found[i]` faces on its i-th run, 4,350 cells each time."""
    def _fake(argv, **kw):
        cmd, cwd = argv[-1], Path(kw["cwd"])
        if "cartesianMesh" in cmd:
            sizes.append((cwd / "system" / "meshDict").read_text())
            pm = cwd / "constant" / "polyMesh"
            pm.mkdir(parents=True, exist_ok=True)
            (pm / "owner").write_text('FoamFile { note "nPoints:9  nCells:4350  nFaces:9"; }\n')
            n3 = found[min(len(sizes), len(found)) - 1]
            (pm / "boundary").write_text(_boundary_text({"wall": 900, "outlet_2": 40, "outlet_3": n3}))
        return _Proc(1 if "surfaceFeatureEdges" in cmd else 0)
    return _fake


def test_a_further_passage_whose_opening_was_dropped_is_meshed_again_finer(tmp_path, monkeypatch):
    """abc 63401 / 99768: the passage's outlet_3 came back with no faces (cartesianMesh kept another region);
    the passage is meshed again at half every cell size, and that mesh - outlet_3 with faces - is merged."""
    sub = _two_passage_case(tmp_path)
    sizes: list[str] = []
    monkeypatch.setattr(N, "run_guarded", _fake_mesher([0, 12], sizes))
    rc, timed_out, why = N.mesh_passages(tmp_path)
    assert (rc, timed_out, why) == (0, False, "")
    assert len(sizes) == 2 and "maxCellSize 0.1;" in sizes[0]
    assert "maxCellSize 0.05;" in sizes[1] and "cellSize 0.01;" in sizes[1]
    assert not (sub / "constant" / "polyMesh").exists()            # merged into the case


def test_an_opening_still_dropped_fails_the_attempt_saying_the_same_plan_drops_it(tmp_path, monkeypatch):
    _two_passage_case(tmp_path)
    sizes: list[str] = []
    monkeypatch.setattr(N, "run_guarded", _fake_mesher([0], sizes))
    rc, _t, why = N.mesh_passages(tmp_path)
    assert rc == 1 and N.PORT_REGION_DROPPED in why and "outlet_3" in why
    # 4,350 cells (the fake's every run) -> 3 finer runs at most: PASSAGE_REFINES
    assert len(sizes) == N.PASSAGE_REFINES + 1


def test_the_first_passages_dropped_opening_fails_before_the_further_passages_run(tmp_path, monkeypatch):
    _two_passage_case(tmp_path)
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "boundary").write_text(_boundary_text({"wall": 900, "inlet": 0}))
    calls: list = []
    monkeypatch.setattr(N, "run_guarded", lambda argv, **kw: calls.append(argv) or _Proc(0))
    rc, _t, why = N.mesh_passages(tmp_path)
    assert rc == 1 and "passage 1" in why and "inlet" in why and not calls


def test_finer_mesh_dict_halves_every_cell_size_and_nothing_else():
    text = "maxCellSize 0.004;\nlocalRefinement { wall { cellSize 0.001; refinementThickness 0.003; } }\n"
    out = N.finer_mesh_dict(text)
    assert "maxCellSize 0.002;" in out and "cellSize 0.0005;" in out and "refinementThickness 0.003;" in out


def test_a_cfmesh_owner_file_without_a_cell_note_is_counted_from_its_faces(tmp_path):
    (tmp_path / "owner").write_bytes(b"FoamFile\n{\n    format ascii;\n    class labelList;\n}\n\n13050\n(\n0\n1\n")
    assert N.cells_of(tmp_path) == 4350          # abc 63401's passage: no note, so it was never refined


def test_a_passage_whose_opening_is_not_in_the_merged_mesh_fails_before_the_rescue(tmp_path, monkeypatch):
    """abc 99768: the passage was "meshed and merged", yet outlet_3 was no patch of the case's mesh."""
    _two_passage_case(tmp_path)
    case = tmp_path / "constant" / "polyMesh"
    sizes: list[str] = []
    inner = _fake_mesher([12], sizes)

    def _fake(argv, **kw):
        if "mergeMeshes" in argv[-1]:
            case.mkdir(parents=True, exist_ok=True)
            (case / "boundary").write_text(_boundary_text({"wall": 900, "inlet": 9, "outlet_2": 40}))
            return _Proc(0)
        return inner(argv, **kw)

    monkeypatch.setattr(N, "run_guarded", _fake)
    rc, _t, why = N.mesh_passages(tmp_path)
    assert rc == 1 and "could not be merged" in why and "outlet_3" in why


def test_a_dropped_opening_is_no_reason_to_run_the_same_plan_again(tmp_path, monkeypatch):
    """abc 63401 / 99768: attempts 2 and 3 repeated the plan whose outlet_3 had no faces. The builder ends its
    turn on it, and the ladder reads the refusal as one no attempt changes."""
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools import meshing as M
    from meshpipeline.contracts import native_time as NT
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal
    from meshpipeline.pipeline.engine_fallback import NEVER, classify

    why = f"[CFMESH] passage 2: {N.PORT_REGION_DROPPED} (outlet_3 has no faces after 2 finer re-mesh(es))"

    class _Engine:
        name = "cfmesh"

        def run_cartesian_mesh(self, workspace, *, timeout, context=None):
            return {"rc": 1, "timed_out": False, "log_tail": f"cartesianMesh ok\n{why}"}

    monkeypatch.setattr(runtime, "get_engine", lambda name: _Engine())
    ctx = BuilderToolContext(workspace=tmp_path, geometry=object(), job_id="j", engine="cfmesh")
    out = M._run_and_judge(ctx, M.PreparedMeshRun(engine="cfmesh", cap=NT.CLOUD_RUN_WALL_S))
    assert out["success"] is False and "outlet_3" in out["guidance"] and "end your turn" in out["guidance"]
    ref = read_refusal(tmp_path)
    state = {"engine": "cfmesh", "executor_success": False, "executor_failed_gate": "finalize",
             "executor_failure_cause": ref.cause, "executor_failure_facts": dict(ref.facts)}
    assert classify(state).kind == NEVER


def test_a_further_passage_at_its_edge_is_rescued_before_it_is_merged(tmp_path, monkeypatch):
    # INS-31: abc 99768's passage 2 carried 171-degree, negative-volume cells into the merged mesh
    import meshpipeline.engines.cfmesh.foam_exec as FE
    import meshpipeline.engines.cfmesh.rescue as RS
    seen: list = []
    monkeypatch.setattr(FE, "check_mesh", lambda ws, **kw: {"cells": 26_000, "max_non_ortho": 171.0,
                                                           "fatal": ["negative-volume cells"]})
    monkeypatch.setattr(RS, "rescue", lambda ws, q, **kw: seen.append((Path(ws), q["max_non_ortho"])) or
                        {**q, "max_non_ortho": 70.0, "fatal": []})
    out = _RESCUE_PASSAGE(tmp_path, bashrc="/dev/null", deadline=0.0)
    assert seen == [(tmp_path, 171.0)] and out["max_non_ortho"] == 70.0
    # a passage mesh inside the edge is merged as built
    monkeypatch.setattr(FE, "check_mesh", lambda ws, **kw: {"cells": 26_000, "max_non_ortho": 60.0, "fatal": []})
    assert _RESCUE_PASSAGE(tmp_path, bashrc="/dev/null", deadline=0.0)["max_non_ortho"] == 60.0 and len(seen) == 1


def test_the_passage_check_runs_between_its_mesh_and_the_merge(tmp_path, monkeypatch):
    ws = tmp_path
    (ws / "system").mkdir()
    (ws / "system" / "meshDict").write_text('surfaceFile "geom.fms";\nmaxCellSize 0.1;\n')
    sub = ws / "_passages" / "p1"
    sub.mkdir(parents=True)
    (sub / "geom.stl").write_text("solid wall\nendsolid wall\n")
    (ws / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"], "ports": [["inlet"], ["outlet_1"]]}))
    order: list = []

    def _fake(argv, **kw):
        cmd, cwd = argv[-1], Path(kw["cwd"])
        order.append(cmd.split("&& ")[-1].split()[0])
        if "cartesianMesh" in cmd:
            (cwd / "constant" / "polyMesh").mkdir(parents=True, exist_ok=True)
            (cwd / "constant" / "polyMesh" / "owner").write_text("x")
        return _Proc(0)

    monkeypatch.setattr(N, "run_guarded", _fake)
    monkeypatch.setattr(N, "ports_without_faces", lambda pm, ports: [])
    monkeypatch.setattr(N, "rescue_passage", lambda s, **kw: order.append(("check", Path(s))))
    assert N.mesh_passages(ws)[0] == 0
    i = order.index(("check", sub.resolve()))
    assert order[i - 1] == "cartesianMesh" and order[i + 1] == "mergeMeshes"


# INS-35: a passage that lost an opening still brings the openings it kept into the mesh the gate judges

def test_a_passage_that_lost_one_opening_still_merges_the_one_it_kept_and_names_only_the_lost_one(tmp_path, monkeypatch):
    """abc 63401: passage 2's mesh held outlet_2 (6,392 faces) and lost outlet_3; it was never merged, so the gate
    read outlet_2 AND outlet_3 at zero faces. Now outlet_2 is merged in and the refusal names outlet_3 alone."""
    _two_passage_case(tmp_path)
    sizes: list[str] = []
    mesher = _fake_mesher([0], sizes)
    merged: list[str] = []

    def _fake(argv, **kw):
        if "mergeMeshes" in argv[-1]:
            merged.append(argv[-1])
        return mesher(argv, **kw)
    monkeypatch.setattr(N, "run_guarded", _fake)
    rc, _t, why = N.mesh_passages(tmp_path)
    assert rc == 1 and N.PORT_REGION_DROPPED in why
    assert len(merged) == 1 and "_passages/p1" in merged[0]         # what the passage built is in the mesh
    assert "(outlet_3 has no faces" in why and "In the mesh with its faces: outlet_2." in why
    assert len(why.splitlines()) == 1                               # one line: the builder quotes that line


def test_a_passage_with_every_opening_lost_merges_nothing(tmp_path, monkeypatch):
    _two_passage_case(tmp_path)
    (tmp_path / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"], "ports": [["inlet"], ["outlet_3"]]}))
    merged: list[str] = []
    mesher = _fake_mesher([0], [])
    monkeypatch.setattr(N, "run_guarded", lambda argv, **kw: merged.append(argv[-1]) if "mergeMeshes" in argv[-1]
                        else mesher(argv, **kw))
    rc, _t, why = N.mesh_passages(tmp_path)
    assert rc == 1 and not merged and "In the mesh" not in why


def test_the_first_passage_s_lost_opening_names_the_passages_the_run_never_meshed(tmp_path, monkeypatch):
    _two_passage_case(tmp_path)
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "boundary").write_text(_boundary_text({"wall": 900, "inlet": 0}))
    monkeypatch.setattr(N, "run_guarded", lambda argv, **kw: _Proc(0))
    rc, _t, why = N.mesh_passages(tmp_path)
    assert rc == 1 and "Not meshed, as the run stopped here: passage 2 (outlet_2, outlet_3)." in why


def test_a_passage_whose_rescue_its_guard_stopped_is_told_in_the_finished_mesh_s_note(tmp_path):
    """TIME-14: the passage goes on as built; the run's rescue note (what the user hears) says so in words."""
    stopped = {"rescue": {"guard_step_only": True, "note": "The 'optimised' step ran past its guard clock."}}
    N._note_passage_trip(tmp_path, 2, stopped)
    N._note_passage_trip(tmp_path, 3, {"rescue": {"note": "Kept the optimised mesh."}})   # not stopped: not told
    N._note_passage_trip(tmp_path, 4, None)
    words = N.passage_trip_words(tmp_path)
    assert words.startswith("Passage 2, before it was merged:") and "Passage 3" not in words
    q = N._with_passage_trips(tmp_path, {"cells": 10, "rescue": {"note": "Kept the optimised mesh."}})
    assert q["rescue"]["note"] == "Kept the optimised mesh. " + words
    assert N._with_passage_trips(tmp_path / "none", {"cells": 10}) == {"cells": 10}
