# Responsibility: Verify the last rung's own sizes and bounds (LAD-fea-17, LAD-fea-18): fTetWild's envelope is set by the part's real walls only (never a crossing face's, a sliver's or two overlapping faces' reading) with a floor relative to the part, its sizing field has a floor, it stops itself near its time with the mesh it has and is stopped past it with the reason said, it reports its rounds to the live line, and it uses every core.
# Boundaries: in-process and quick: a stand-in fTetWild (a small Python script on PATH that prints fTetWild's own progress lines and honours the stop file) stands for the binary; the CAD tests build small solids with OpenCASCADE.
from __future__ import annotations

import json
import stat
import sys
import time

import numpy as np
import pytest

from meshpipeline.engines import fea_tetwild as TW


def _plan(base=0.01, zones=(), faces=()):
    return {"base_size_m": base, "zones": list(zones), "faces": list(faces), "growth": 1.3}


# #
# LAD-fea-17: the rung's own sizes - never a crossing face's, a sliver's or an overlap's
# #

def test_a_wall_reading_on_a_crossing_or_sliver_face_sets_no_envelope():
    """The dev part (shared dev, 1 Nov): a 0.04 mm2 sliver face reading a 0.11 mm wall held a 640 mm part's
    envelope at 13 microns; the dev pivot's faces found crossing read a 0.5 mm "wall" between them."""
    plan = _plan(base=0.064, faces=[{"name": "Face_1", "thickness_min_mm": 0.11, "area_mm2": 0.043},
                                    {"name": "Face_2", "thickness_min_mm": 0.5, "area_mm2": 500.0},
                                    {"name": "Face_3", "thickness_min_mm": 3.0, "area_mm2": 900.0}])
    record = {"bodies": 1, "suspects": [{"kind": "self_overlap", "faces": ["Face_2"], "confirmed": False}]}
    w = TW.params(plan, record, 0.64, None)["per_part"]["whole"]
    assert w["thinnest_wall_m"] == pytest.approx(0.003)
    assert w["epsilon_m"] == pytest.approx(min(1e-3 * 0.64, 0.1 * 0.003))
    assert {a["face"]: a["why"] for a in w["walls_set_aside"]} == {"Face_1": "narrow", "Face_2": "crossing"}
    # LAD-fea-16's own crossing faces are set aside the same way
    plain = {"bodies": 1}
    by_rung = TW.params(plan, plain, 0.64, None, crossing={"faces": ["Face_2"]})["per_part"]["whole"]
    assert by_rung["thinnest_wall_m"] == pytest.approx(0.003)
    # with neither: the 0.5 mm reading binds (it is on a wide face) and is handed to the room test
    raw = TW.params(plan, plain, 0.64, None)["per_part"]["whole"]
    assert raw["thinnest_wall_m"] == pytest.approx(0.0005)
    assert [c["face"] for c in raw["wall_checks"]] == ["Face_2", "Face_3"]
    # the floor: a thin real wall lifts the envelope to EPS_FLOOR_SHARE of the part, but never past
    # WALL_KEEP_SHARE of the wall, and never under EPS_MIN_SHARE of the part
    def eps(t_mm):
        film = _plan(base=0.064, faces=[{"name": "Face_1", "thickness_min_mm": t_mm, "area_mm2": 1e6}])
        return TW.params(film, plain, 0.64, None)["per_part"]["whole"]["epsilon_m"]
    assert eps(1.0) == pytest.approx(TW.EPS_FLOOR_SHARE * 0.64)                # 0.1 mm lifted to the floor
    assert eps(0.3) == pytest.approx(TW.WALL_KEEP_SHARE * 0.0003)             # held under half the wall
    assert eps(0.001) == pytest.approx(TW.EPS_MIN_SHARE * 0.64)
    assert "LAD-fea-17" in TW.size_words(TW.params(plan, record, 0.64, None))


def _unsewn(*pieces):
    """One solid whose shell is every face of `pieces`, never fused (their faces may cross or overlap)."""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shell, TopoDS_Solid

    b = BRep_Builder()
    shell = TopoDS_Shell()
    b.MakeShell(shell)
    for p in pieces:
        ex = TopExp_Explorer(p, TopAbs_FACE)
        while ex.More():
            b.Add(shell, TopoDS.Face_s(ex.Current()))
            ex.Next()
    solid = TopoDS_Solid()
    b.MakeSolid(solid)
    b.Add(solid, shell)
    return solid


def _surface(solid):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(solid, TopAbs_FACE, fmap)
    xyz, tris, label = TW.tessellate(fmap, list(range(fmap.Extent())), 0.05)
    return xyz, tris, label, [f"Face_{k + 1}" for k in range(fmap.Extent())]


def _room(solid, t):
    xyz, tris, label, names = _surface(solid)
    return {n: TW.walls_with_room([{"face": n, "t_m": t}], xyz, tris, label, names)[0] is not None for n in names}


def test_a_wall_is_real_only_with_room_beyond_it():
    """LAD-fea-17: past a real wall's far side is space; between two faces overlapping inside the material
    (a pad laid 0.5 mm into a bar, never fused - the dev pivot's fake wall) there is none, so it sets nothing."""
    pytest.importorskip("OCP.BRepPrimAPI")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    plate = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 100.0, 100.0, 1.0).Shape()
    room = _room(_unsewn(plate), 1.0)
    assert room["Face_5"] and room["Face_6"]                          # 1 mm through the plate
    assert not any(room[f"Face_{k}"] for k in range(1, 5))            # its sides are 100 mm across
    bar = BRepPrimAPI_MakeBox(gp_Pnt(0, -6, 1), 63.0, 12.0, 8.0).Shape()
    pad = BRepPrimAPI_MakeBox(gp_Pnt(20, -4, 8.5), 22.0, 8.0, 3.0).Shape()
    both = _unsewn(bar, pad)
    overlap = _room(both, 0.5)
    assert not overlap["Face_6"] and not overlap["Face_11"]           # the bar's top, the pad's bottom
    # the winding number counts the material: two layers where they overlap, one in the bar, none outside
    xyz, tris, _l, _n = _surface(both)
    w = TW.winding(np.array([[30.0, 0.0, 8.8], [30.0, 0.0, 5.0], [30.0, 0.0, 20.0]]), xyz, tris)
    assert w == pytest.approx([2.0, 1.0, 0.0], abs=1e-6)
    # thinnest first: a reading with no room is passed over, the first real one sets the envelope
    t, none = TW.walls_with_room([{"face": "Face_6", "t_m": 0.5}, {"face": "Face_5", "t_m": 8.0}],
                                 xyz, tris, _l, _n)
    assert t == pytest.approx(8.0) and [x["face"] for x in none] == ["Face_6"]


def test_no_zone_is_drawn_from_a_wall_with_no_room_and_none_is_under_the_floor():
    plan = _plan(base=0.01, zones=[{"id": "W6", "kind": "wall", "faces": ["Face_6"], "size_m": 0.00025,
                                    "thickness_m": 0.0005},
                                   {"id": "C1", "kind": "corner", "size_m": 0.000125, "thickness_m": 0.0005},
                                   {"id": "H1", "kind": "hole", "faces": ["Face_2"], "size_m": 0.0004}])
    kept = TW.without_walls(plan, [{"face": "Face_6", "wall_mm": 0.5}])
    assert [z["id"] for z in kept["zones"]] == ["H1"]
    assert TW.without_walls(plan, []) is plan
    # the sizing field's floor: no node finer than it; a floor past the edge leaves no field at all
    xyz = np.array([[0, 0, 0], [0.02, 0, 0], [0, 0.02, 0], [0, 0, 0.02]], float)
    tris = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])
    one = _plan(base=0.01, zones=[{"id": "W1", "kind": "wall", "faces": ["Face_1"], "size_m": 0.0002}])
    grid = TW.sizing_grid(xyz, tris, np.zeros(4, dtype=np.int64), ["Face_1"], one, 0.01, floor=0.001)
    assert grid is not None and grid[2].min() >= 0.001 - 1e-12
    assert TW.sizing_grid(xyz, tris, np.zeros(4, dtype=np.int64), ["Face_1"], one, 0.01, floor=0.02) is None


# #
# LAD-fea-18: its own stop controls, a budget from the part, progress on the live line, every core
# #

FTW_LINES = """TBB threads 8
[info] preprocessing 0.53s
[info] tetrahedralizing 0.1s
[info] matched #f = 222, uninserted #f = 298
[info] uninserted #f = 3/520
[info] cutting 1.5s
//////////////// pass 0 ////////////////
edge splitting...
#t = 15000
max_energy = 1545.44
//////////////// pass 1 ////////////////
#t = 16000
max_energy = 88.5
stop file: optimization ends at pass 2
//////////////// postprocessing ////////////////
"""


def test_ftetwilds_own_lines_say_where_it_is():
    pr = TW.Progress(40)
    moved = [pr.feed(ln) for ln in FTW_LINES.splitlines()]
    assert pr.round == 2 and pr.energy == pytest.approx(88.5) and pr.tets == 16000
    assert pr.uninserted == (3, 520) and pr.stopped and pr.phase == "finishing"
    assert pr.phases == {"prepared": pytest.approx(0.53), "tetrahedralized": pytest.approx(0.1),
                         "inserted": pytest.approx(1.5)}
    assert sum(moved) >= 4                                         # each phase and each round moved the line
    live = TW.Progress(40)
    for ln in FTW_LINES.splitlines()[:10]:
        live.feed(ln)
    assert live.label("Part_2") == ("fTetWild Part_2: improving the elements, round 1 of at most 40, "
                                    "worst energy 1.55e+03, 15,000 tets")
    assert TW.Progress(40).label("part") == "fTetWild part: preparing the surface"
    assert TW.Progress(40).record()["rounds"] == 0


def test_the_rungs_time_is_sized_from_the_part_within_the_wall():
    small = TW.rung_budget(2e4, cpus=8, wall_s=13_800)
    assert small["budget_s"] == int(TW.BUDGET_FLOOR_S) and small["rule"] == "LAD-fea-18"
    assert small["ceiling_s"] == int(TW.BUDGET_FLOOR_S + TW.MARGIN_FLOOR_S)
    big = TW.rung_budget(1e6, cpus=8, wall_s=72_000)
    planned = 1e6 / TW.rung_rate(8)
    assert big["planned_s"] == round(planned) and big["budget_s"] == round(TW.BUDGET_FACTOR * planned)
    assert abs(big["ceiling_s"] - big["budget_s"] * (1 + TW.MARGIN_SHARE)) <= 1
    capped = TW.rung_budget(4e8, cpus=8, wall_s=72_000)
    assert capped["budget_s"] == int(TW.BUDGET_CAP_S)            # never past 4 hours, whatever the part
    walled = TW.rung_budget(4e8, cpus=8, wall_s=13_800)
    assert walled["ceiling_s"] <= 13_800 and walled["budget_s"] == round(13_800 / (1 + TW.MARGIN_SHARE))
    assert TW.rung_rate(8) > TW.rung_rate(1)
    assert "LAD-fea-18" in TW.budget_words(big)


STOPPABLE = r'''
import os, sys, time
args = sys.argv[1:]
out = args[args.index("-o") + 1]
mode = os.path.basename(os.path.dirname(out))
print("TBB threads", args[args.index("--max-threads") + 1], flush=True)
print("[info] cutting 0.1s", flush=True)
for it in range(100000):
    if mode == "stops" and os.path.exists(out + ".stop"):
        print(f"stop file: optimization ends at pass {it}", flush=True)
        break
    print(f"//////////////// pass {it} ////////////////", flush=True)
    print(f"max_energy = {1000.0 / (it + 1)}", flush=True)
    time.sleep(0.05)
with open(out, "w") as fh:
    fh.write("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n4\n1 0 0 0\n2 0.01 0 0\n3 0 0.01 0\n4 0 0 0.01\n"
             "$EndNodes\n$Elements\n1\n1 4 2 0 1 1 2 3 4\n$EndElements\n")
'''


@pytest.fixture()
def stoppable(tmp_path, monkeypatch):
    pytest.importorskip("meshio")
    exe = tmp_path / "bin" / "fTetWild"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{STOPPABLE}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(TW, "binary", lambda: str(exe))
    return exe


_TET = (np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float) * 0.01,
        np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]]))


def test_near_its_time_ftetwild_finishes_the_mesh_it_has(tmp_path, stoppable, monkeypatch):
    """LAD-fea-18: at the soft deadline the stop file asks fTetWild to end its rounds where it is; it writes
    that mesh, and the record says it stopped, after how many rounds, on how many threads."""
    ws = tmp_path / "ws"
    ws.mkdir()
    said = []
    real_note = TW._stage_note
    monkeypatch.setattr(TW, "_stage_note", lambda w, k, label, n=None, of=None: (said.append((k, n, of)),
                                                                                 real_note(w, k, label, n=n, of=of)))
    now = time.monotonic()
    ok = TW.run_part({"dir": str(tmp_path / "stops"), "xyz": _TET[0], "tris": _TET[1], "edge": 0.005,
                      "epsilon": 1e-5, "bg": None, "name": "part", "threads": 4, "ws": str(ws),
                      "soft_at": now + 1.0, "hard_at": now + 60})
    assert "error" not in ok, ok
    assert ok["stopped"] and ok["rounds"] >= 2 and ok["threads"] == 4 and len(ok["tets"]) == 1
    cmd = ok["command"]
    assert cmd[cmd.index("--max-threads") + 1] == "4" and cmd[cmd.index("--max-its") + 1] == str(TW.MAX_ITS)
    rounds = [n for k, n, of in said if k == "ftetwild_improving" and of == TW.MAX_ITS]
    assert rounds[:3] == [1, 2, 3]                                  # the live line moved round by round
    assert json.loads((ws / "fea_stage.json").read_text())["key"] == "ftetwild"   # then: finishing


def test_past_its_time_ftetwild_is_stopped_and_the_reason_said(tmp_path, stoppable):
    now = time.monotonic()
    late = TW.run_part({"dir": str(tmp_path / "grinds"), "xyz": _TET[0], "tris": _TET[1], "edge": 0.005,
                        "epsilon": 1e-5, "bg": None, "name": "part", "soft_at": now + 0.5, "hard_at": now + 1.5})
    assert late.get("out_of_time") and TW.OUT_OF_TIME in late["error"] and "LAD-fea-18" in late["error"]
    assert "improving its elements (round" in late["error"]
    assert time.monotonic() - now < 30
    # a part out of its time is never taken for one killed for memory (run again alone)
    calls = []

    def fake(job):
        calls.append(job["name"])
        return {"rc": -9, "out_of_time": True, "error": "fTetWild was still ... " + TW.OUT_OF_TIME}
    import meshpipeline.engines.fea_tetwild as mod
    real = mod.run_part
    mod.run_part = fake
    try:
        out = TW.run_parts([{"name": "a"}], 2)
    finally:
        mod.run_part = real
    assert calls == ["a"] and out[0]["out_of_time"]


def test_every_core_is_used_one_part_alone_or_shared_out(monkeypatch):
    seen = []

    def fake(job):
        seen.append((job["name"], job["threads"]))
        return {"xyz": np.zeros((4, 3)), "tets": np.zeros((1, 4), dtype=np.int64)}
    monkeypatch.setattr(TW, "run_part", fake)
    monkeypatch.setattr(TW, "memory_room", lambda: 1.0)
    TW.run_parts([{"name": "a"}], 8)
    assert seen == [("a", 8)]
    seen.clear()
    TW.run_parts([{"name": n} for n in "abc"], 8)
    assert sorted(seen)[0][1] == 2 and all(t >= 2 for _n, t in seen)


def test_the_live_line_carries_the_rounds(tmp_path):
    from meshpipeline.engines import mesher_stage as MS
    from meshpipeline.engines.gmsh.fea_adapter import note_stage

    (tmp_path / "gmshfea.log").write_text("[FTETWILD] part: //////////////// pass 3 ////////////////\n")
    note_stage(tmp_path, "ftetwild_improving", "fTetWild part: improving the elements, round 4 of at most 80",
               n=4, of=80, unit="rounds")
    assert MS.read_stage(tmp_path) == {"key": "ftetwild_improving", "tool": "gmshfea", "n": 4, "of": 80,
                                       "unit": "rounds",
                                       "label": "fTetWild part: improving the elements, round 4 of at most 80"}
    note_stage(tmp_path, "volume", "filling the solid with elements")
    assert "n" not in MS.read_stage(tmp_path)


def test_a_pass_its_own_budget_stopped_is_never_moved_to_a_vm(tmp_path, monkeypatch):
    """LAD-fea-18: the rung's pass stopped at its own ceiling is the part's slowness; RUN-5 moves only a pass
    the Cloud Run wall stopped."""
    import asyncio

    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import fea_driver as FD

    async def mesh(*_a, **_k):
        return {"rc": -1, "timed_out": True, "stalled": False}
    monkeypatch.setattr(FD, "_relayed_mesh", mesh)
    moved = []
    monkeypatch.setattr(MM, "off_the_wall", lambda m: moved.append(m) or {**m, "where": "gce"})
    machine = {"where": "cloud_run", "machine": "cloud-run", "cpus": 8}
    result, m, _w = asyncio.run(FD._native(tmp_path, machine, {"ceiling_s": 3600, "budget": True}, "t-b", 5, None))
    assert result["timed_out"] and m == machine and not moved


def test_the_sizing_field_rounds_are_said_too():
    pr = TW.Progress(80)
    for ln in ["//////////////// postprocessing ////////////////", "/////////0 2278.7", "/////////1 1300.45"]:
        pr.feed(ln)
    assert pr.phase == "sizing" and pr.sizing == 2 and pr.energy == pytest.approx(1300.45)
    assert pr.label("part") == ("fTetWild part: refining to the sizing field, round 2 of at most "
                                f"{TW.SIZING_ROUNDS}, worst energy 1.3e+03")
    assert pr.record()["sizing_rounds"] == 2
