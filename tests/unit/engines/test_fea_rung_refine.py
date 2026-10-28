# Responsibility: Verify LAD-fea-13: after the last rung delivers a mesh short of a rule, one refinement pass re-meshes only the parts the short zones are on, those zones finer and held through fTetWild's sizing grid; the better of the two passes is delivered, and every wall still short is named, never a clean pass.
# Boundaries: the grid and the targets on numbers; the ladder through the real driver and adapter with a stand-in fTetWild on PATH (the real cases, the STEP hunt's five assemblies, are the lab's: ~/fea_hunt).
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_tetwild as TW


def _slab(t: float = 0.002, side: float = 0.1):
    """A square plate `t` thick, as a closed triangle surface labelled face 0 (top), 1 (bottom), 2 (sides)."""
    x = np.array([[0, 0, 0], [side, 0, 0], [side, side, 0], [0, side, 0],
                  [0, 0, t], [side, 0, t], [side, side, t], [0, side, t]], dtype=float)
    tris = np.array([[4, 5, 6], [4, 6, 7], [0, 2, 1], [0, 3, 2],
                     [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    label = np.array([0, 0, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2])
    return x, tris, label, ["Face_1", "Face_2", "Face_3"]


def test_a_zone_being_refined_is_held_through_every_cell_of_the_grid():
    """A 2 mm plate in 2.5 mm cells: the grid's nodes sit up to a cell from the wall and read its zone size grown
    by that distance; held, no node within a cell of it reads coarser than the zone, and none finer."""
    x, tris, label, names = _slab()
    zone = {"id": "W1", "kind": "wall", "faces": ["Face_1", "Face_2"], "size_m": 0.0008, "reach_m": 0.0}
    plan = {"zones": [zone], "growth": 1.3}
    pts, _tets, plain = TW.sizing_grid(x, tris, label, names, plan, 0.02)
    _p2, _t2, held = TW.sizing_grid(x, tris, label, names, plan, 0.02, band=[zone])
    step = (x.max(axis=0) - x.min(axis=0)) / TW.BG_DIVISIONS
    inside = np.all((pts[:, :2] >= 0.0) & (pts[:, :2] <= 0.1), axis=1)
    near = inside & (np.minimum(np.abs(pts[:, 2]), np.abs(pts[:, 2] - 0.002)) <= float(np.linalg.norm(step)))
    assert plain[near].max() > zone["size_m"] * 1.05            # the uniform grid reads it coarser
    assert held[near].max() <= zone["size_m"] + 1e-15             # held: none coarser than the zone
    assert held.min() >= zone["size_m"] - 1e-15                  # never finer than the zone
    assert np.all(held <= plain + 1e-15)                         # only ever finer than before


def test_the_refinement_targets_the_parts_the_short_walls_are_on_and_leaves_the_others_as_they_were():
    """Zone ids repeat across an assembly's parts (T5_1 is the fifth face of every part): a short wall is on the
    part of the face the check measured; a hot spot on its own faces' parts. Split by who meshed the part."""
    plan = {"zones": [{"id": "T5_1", "faces": [f"Part_{k}_Face_5"], "size_m": 0.001} for k in (1, 2, 3, 4)]
            + [{"id": "H9", "faces": ["Part_3_Face_9"], "size_m": 0.002},
               {"id": "W7", "faces": ["Part_1_Face_7"], "size_m": 0.003}]}
    walls = [{"zone": "T5_1", "face": "Part_4_Face_5"}, {"zone": "T5_1", "face": "Part_3_Face_5"}]
    # the rung's pass gave fTetWild part 3 only; parts 1, 2 and 4 were gmsh's
    assert TW.refine_targets(plan, ["T5_1", "T5_1", "H9"], walls, [3]) == {
        "ftetwild": {"Part_3": ["H9", "T5_1"]}, "gmsh": {"Part_4": ["T5_1"]}}
    assert TW.refine_targets(plan, ["W7"], [], None) == {"ftetwild": {"Part_1": ["W7"]}, "gmsh": {}}
    # each target part's own entries made finer, never another part's entry of the same id
    zones = TW.tighten_zones(plan["zones"], {"Part_4": ["T5_1"]}, 0.8)
    assert [z["size_m"] for z in zones] == pytest.approx([0.001, 0.001, 0.001, 0.0008, 0.002, 0.003])
    refine = {"factor": 0.8, "parts": {"Part_3": ["H9", "T5_1"]}}
    mine, band = TW.refined_plan(plan, refine, "Part_3")
    assert [z["size_m"] for z in mine["zones"]] == pytest.approx([0.001, 0.001, 0.0008, 0.001, 0.0016, 0.003])
    assert sorted(z["id"] for z in band) == ["H9", "T5_1"]
    other, none = TW.refined_plan(plan, refine, "Part_2")
    assert other is plan and none == []


def test_better_is_fewer_short_zones_then_the_thicker_worst_wall():
    old = {"valid": True, "over": False, "short": ["W1", "W2"], "through_wall_min": 1.6}
    assert L.refined_better({"valid": True, "over": False, "short": [], "through_wall_min": 2.1}, old)
    assert L.refined_better({"valid": True, "over": False, "short": ["W1", "W2"], "through_wall_min": 1.8}, old)
    assert not L.refined_better({"valid": True, "over": False, "short": ["W1", "W2"], "through_wall_min": 1.6}, old)
    assert not L.refined_better({"valid": False, "over": False, "short": [], "through_wall_min": None}, old)
    assert not L.refined_better({"valid": True, "over": True, "short": [], "through_wall_min": 2.2}, old)
    # a pass marked not deliverable is never the one delivered
    assert L.best_pass([{**old, "pass": 4, "min_sicn": 0.2},
                        {"valid": True, "over": False, "short": [], "pass": 5, "min_sicn": 0.3,
                         "deliverable": False}])[0] == 4


# #
# the ladder through the real driver and adapter, a stand-in fTetWild on PATH
# #

#: the stand-in fills the input surface at one size: the smallest the sizing mesh asks for, else --la
FAKE = r'''
import sys
import numpy as np
import gmsh

args = sys.argv[1:]
src, out = args[args.index("-i") + 1], args[args.index("-o") + 1]
la = float(args[args.index("--la") + 1])
if "--bg-mesh" in args:
    lines = open(args[args.index("--bg-mesh") + 1]).read().split("$NodeData")[1].split("\n")
    la = min(float(ln.split()[1]) for ln in lines[9:] if len(ln.split()) == 2)
v, f = [], []
for line in open(src):
    if line.startswith("v "):
        v.append([float(x) for x in line.split()[1:4]])
    elif line.startswith("f "):
        f.append([int(x) - 1 for x in line.split()[1:4]])
v, f = np.array(v), np.array(f)
stl = out + ".stl"
with open(stl, "w") as fh:
    fh.write("solid s\n")
    for a, b, c in f:
        n = np.cross(v[b] - v[a], v[c] - v[a])
        fh.write(f"facet normal {n[0]} {n[1]} {n[2]}\nouter loop\n")
        for k in (a, b, c):
            fh.write(f"vertex {v[k][0]!r} {v[k][1]!r} {v[k][2]!r}\n")
        fh.write("endloop\nendfacet\n")
    fh.write("endsolid s\n")
print("stand-in fTetWild: filling at", la, flush=True)
gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
gmsh.merge(stl)
# its surface made anew at that size, as fTetWild's is
gmsh.model.mesh.classifySurfaces(40 * np.pi / 180, True, True, np.pi)
gmsh.model.mesh.createGeometry()
loop = gmsh.model.geo.addSurfaceLoop([t for _d, t in gmsh.model.getEntities(2)])
gmsh.model.geo.addVolume([loop])
gmsh.model.geo.synchronize()
gmsh.option.setNumber("Mesh.MeshSizeMax", la)
gmsh.model.mesh.generate(3)
gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
gmsh.write(out)
gmsh.finalize()
'''


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    pytest.importorskip("gmsh")
    pytest.importorskip("meshio")
    pytest.importorskip("OCP.BRepPrimAPI")
    exe = tmp_path / "bin" / "fTetWild"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{FAKE}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr(TW, "binary", lambda: str(exe))
    return exe


def _grid_only_when_held(monkeypatch, *, held: bool):
    """The uniform grid as the lab saw it: it lets the thin walls fall through (here: no sizing mesh at all, the
    stand-in fills at the part's edge). Held (`held`), the refined part gets the real grid with its band."""
    real = TW.sizing_grid

    def grid(xyz, tris, label, names, plan, edge, pad=0.0, band=()):
        return real(xyz, tris, label, names, plan, edge, pad=pad, band=band) if (band and held) else None

    monkeypatch.setattr(TW, "sizing_grid", grid)


def _ladder(ws: Path, monkeypatch):
    from tests.unit.engines.test_fea_tetwild import _drive, _gmsh_fails_on

    _gmsh_fails_on(monkeypatch, None, ["Face_5"])
    return _drive(ws, monkeypatch)


def test_a_rung_mesh_short_of_its_walls_gets_one_refinement_pass_and_the_better_is_delivered(tmp_path, monkeypatch, fake):
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_tetwild import _stage

    from meshpipeline.contracts import decision_record as dr

    _grid_only_when_held(monkeypatch, held=True)
    ws = _stage(fx.plate_with_hole(), tmp_path / "refine_better")
    ok, marker, _o = _ladder(ws, monkeypatch)
    assert ok, marker
    passes = json.loads((ws / "fea_passes.json").read_text())
    rung = [p for p in passes["passes"] if p.get("engine") == "ftetwild"]
    assert len(rung) == 2 and rung[0]["valid"] and rung[0]["short"] and "refine" not in rung[0]
    refined = rung[1]
    assert refined["refine"]["after_pass"] == rung[0]["pass"] and refined["valid"]
    assert L.refined_better(refined, rung[0]) and refined.get("deliverable", True)
    assert passes["delivered"] == refined["pass"]
    from meshpipeline.engines import fea_driver as FD
    assert refined["budget_s"] == pytest.approx(max(FD.REFINE_TIME_FLOOR_S, FD.REFINE_TIME_SHARE * rung[0]["native_s"]), abs=0.1)
    cited = [e for e in dr.cited_for(f"t-{ws.name}") if e["rule"] == "LAD-fea-13"]
    assert [e["decided"] for e in cited] == ["rung refinement", "rung refinement kept"] and cited[1]["value"] is True
    q = json.loads((ws / "quality.json").read_text())
    per = q["fea"]["ftetwild"]["per_part"]
    assert per[0]["refined_zones"] and per[0]["refine_rule"] == "LAD-fea-13"


def test_a_refinement_no_better_leaves_the_rungs_pass_delivered_with_its_short_walls_named(tmp_path, monkeypatch, fake):
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_tetwild import _stage

    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.engines.verdict_limits import fea_limits

    _grid_only_when_held(monkeypatch, held=False)      # the refinement's grid falls through as well
    ws = _stage(fx.plate_with_hole(), tmp_path / "refine_no_better")
    ok, marker, _o = _ladder(ws, monkeypatch)
    assert ok, marker
    passes = json.loads((ws / "fea_passes.json").read_text())
    rung = [p for p in passes["passes"] if p.get("engine") == "ftetwild"]
    assert len(rung) == 2 and rung[1]["deliverable"] is False
    assert passes["delivered"] == rung[0]["pass"]
    # the same inputs, the same run: the refinement re-ran nothing it did not change
    q = json.loads((ws / "quality.json").read_text())
    rows = [r for r in fea_limits(q) if r["key"] == "fea_walls_short"]
    assert len(rows) == 1 and rows[0]["measured"] and all("wall" in m and "needed" in m for m in rows[0]["measured"])
    assert dr.verdict({"valid": True, "gating": [], "limits": fea_limits(q)})[0] == dr.OUTCOME_CONCERNS


def test_a_part_run_with_the_same_inputs_is_taken_from_its_folder(tmp_path, fake):
    x, tris, _label, _names = _slab(0.01, 0.05)
    job = {"dir": str(tmp_path / "p"), "xyz": x, "tris": tris, "edge": 0.01, "epsilon": 1e-4, "bg": None, "name": "p"}
    first = TW.run_part(job)
    again = TW.run_part(job)
    assert "error" not in first and not first.get("reused") and again.get("reused")
    assert np.array_equal(first["tets"], again["tets"]) and np.array_equal(first["xyz"], again["xyz"])
    other = TW.run_part({**job, "edge": 0.008})
    assert not other.get("reused")
    assert TW.run_part({**job, "edge": 0.008}).get("reused")


def test_a_refinement_out_of_time_ends_there_and_the_rungs_pass_is_delivered(tmp_path, monkeypatch, fake):
    """A mesh is in hand: the refinement gets its budget and no more - never a bigger machine or a longer wall."""
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_tetwild import _gmsh_fails_on, _Run, _safe, _stage

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh import fea_adapter

    _grid_only_when_held(monkeypatch, held=True)
    ws = _stage(fx.plate_with_hole(), tmp_path / "refine_out_of_time")
    _gmsh_fails_on(monkeypatch, None, ["Face_5"])
    ceilings = []

    def mesher(w, timeout):
        plan = json.loads((Path(w) / "fea_plan.json").read_text())
        if ((plan.get("mesh") or {}).get("ftetwild") or {}).get("refine"):
            ceilings.append(timeout)
            return {"rc": -9, "timed_out": True}          # the refinement ran past its budget
        return {"rc": _safe(fea_adapter.mesh, w)}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=mesher))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id=f"t-{ws.name}", publish=None, run=_Run()))
    assert ok, marker
    passes = json.loads((ws / "fea_passes.json").read_text())
    rung = [p for p in passes["passes"] if p.get("engine") == "ftetwild"]
    assert len(ceilings) == 1                               # one try: no step up, no move off the wall
    assert ceilings[0] == pytest.approx(rung[1]["budget_s"], abs=1.0)
    assert rung[1]["deliverable"] is False and "time budget" in rung[1]["rung_error"]
    assert passes["delivered"] == rung[0]["pass"]
