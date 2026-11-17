# Responsibility: Verify the slow-FEA rules of 1 Nov: cosmetic fillets (PLAN-fea-24), a micro round's own edge-break reading is no wall (PLAN-fea-25), the slow-to-mesh warning (PLAN-fea-26), the ladder remeshing only the surfaces that failed (LAD-fea-6), a pass budget sized from its estimate (LAD-fea-15), and the surface stage on worker processes giving the same mesh on any number (MESH-23).
# Boundaries: records written by hand for the plan rules; a small OpenCASCADE solid meshed by gmsh for MESH-23.
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines.fea_size_field import Histogram


def _record(**over):
    """A 640 mm part: a 14 mm wall with a 0.1 mm fillet (H1), a 2 mm fillet (H2), a 0.05 mm edge break whose wall
    reading crosses its own round (Face_9), a real 0.3 mm fin (Face_5), a hole of 0.1 mm (H3) and a corner (H4)
    beside the edge break."""
    rec = {
        "bodies": 1, "diag_mm": 640.0,
        "thickness": {"min_mm": 0.104, "face": "Face_9"},
        "faces": {
            "Face_1": {"kind": "plane", "thickness_min_mm": 14.0},
            "Face_2": {"kind": "cylinder", "thickness_min_mm": 14.0, "radius_mm": 0.1, "concave": True},
            "Face_3": {"kind": "cylinder", "thickness_min_mm": 14.0, "radius_mm": 2.0, "concave": True},
            "Face_4": {"kind": "cylinder", "thickness_min_mm": 14.0, "radius_mm": 0.1, "concave": True},
            "Face_5": {"kind": "plane", "thickness_min_mm": 0.3},
            "Face_9": {"kind": "cylinder", "thickness_min_mm": 0.104, "radius_mm": 0.05, "concave": False},
        },
        "hot_spots": [
            {"id": "H1", "type": "fillet", "shape": "fillet", "faces": ["Face_2"], "radius_mm": 0.1,
             "local_thickness_mm": 14.0},
            {"id": "H2", "type": "fillet", "shape": "fillet", "faces": ["Face_3"], "radius_mm": 2.0,
             "local_thickness_mm": 14.0},
            {"id": "H3", "type": "hole", "shape": "hole", "faces": ["Face_4"], "radius_mm": 0.1,
             "local_thickness_mm": 14.0},
            {"id": "H4", "type": "sharp_corner", "faces": ["Face_9", "Face_1"], "angle_deg": 90.0,
             "cap_basis_mm": 0.104, "local_thickness_mm": 0.104, "capped": True},
        ],
    }
    rec.update(over)
    return rec


def _ids(zs):
    return {z["id"] for z in zs}


def test_a_fillet_far_smaller_than_its_wall_and_its_part_is_cosmetic():
    zs = P.zones(_record())
    assert "H1" not in _ids(zs)                      # 0.1 mm in a 14 mm wall of a 640 mm part
    assert "H2" in _ids(zs)                          # 2 mm: a real stress raiser
    assert "H3" in _ids(zs)                          # a hole is never cosmetic
    # its face gets no round zone (it keeps its shape as a micro round does); the 14 mm wall rule still holds there
    assert {z["kind"] for z in zs if "Face_2" in z["faces"]} == {"wall"}
    assert [c["spot"] for c in P.cosmetic_fillets(_record())] == ["H1"]


def test_a_small_fillet_is_kept_in_a_thin_wall_or_on_a_small_part():
    thin = _record()
    thin["hot_spots"][0]["local_thickness_mm"] = 1.0         # r/t = 0.1: resolved
    assert "H1" in _ids(P.zones(thin))
    small = _record(diag_mm=50.0)                             # 0.1 mm is 0.2% of a 50 mm part: resolved
    assert "H1" in _ids(P.zones(small))


def test_a_micro_round_s_own_edge_break_is_no_wall_but_a_real_fin_is():
    zs = P.zones(_record())
    assert "W9" not in _ids(zs)                      # 0.104 mm across a 0.05 mm round: its own edge break
    fin = next(z for z in zs if z["id"] == "W5")     # the 0.3 mm fin keeps 2 elements through it
    assert fin["size_m"] == pytest.approx(0.15e-3)
    # a micro round reading a wall well past its own size keeps it
    far = _record()
    far["faces"]["Face_9"]["thickness_min_mm"] = 0.5
    assert "W9" in _ids(P.zones(far))


def test_a_corner_beside_an_edge_break_is_capped_by_its_other_wall():
    corner = next(z for z in P.zones(_record()) if z["id"] == "C4")
    assert corner["thickness_m"] == pytest.approx(14e-3)


def test_the_plan_says_what_it_set_aside_and_warns_when_it_will_be_slow():
    rec = _record()
    zs = P.zones(rec)
    hist = Histogram(rows=[(1e-3, 1e-6), (float("inf"), 1e-3)])
    plan = P.finish(rec, zs, hist, diag_m=0.64, limit=8_000_000)
    text = " ".join(plan["words"])
    assert "Cosmetic fillets: 1" in text and "not resolved" in text
    assert "edge break" in text
    rules = {d["rule"] for d in plan["decisions"]}
    assert {"PLAN-fea-24", "PLAN-fea-25"} <= rules
    # the 0.1 mm hole is a real stress raiser: 0.026 mm elements under a 64 mm base, so the plan says it is slow
    assert plan["slow"]["zone"] == "H3" and plan["slow"]["ratio"] > P.SLOW_RATIO
    assert any(w.startswith("Slow to mesh") for w in plan["words"])
    holeless = _record()
    holeless["hot_spots"] = [h for h in holeless["hot_spots"] if h["id"] != "H3"]
    holeless["faces"]["Face_5"]["thickness_min_mm"] = 1.0
    plan0 = P.finish(holeless, P.zones(holeless), hist, diag_m=0.64, limit=8_000_000)
    assert "slow" not in plan0                       # 64 mm over a 1 mm wall's 0.5 mm elements: 128 to 1
    slow = P.slow_ratio([{"id": "W1", "size_m": 1e-5}], 0.064)
    assert slow and slow["ratio"] == pytest.approx(6400) and slow["zone"] == "W1"
    plan2 = P.finish(rec, [*zs, {"id": "W99", "kind": "wall", "faces": ["Face_5"], "size_m": 1e-5, "reach_m": 0.0,
                                 "rule": "PLAN-fea-3", "thickness_m": 2e-5, "capped": False}], hist,
                     diag_m=0.64, limit=8_000_000)
    assert any(w.startswith("Slow to mesh") for w in plan2["words"])
    assert "PLAN-fea-26" in {d["rule"] for d in plan2["decisions"]}


def test_a_part_with_nothing_cosmetic_plans_as_before():
    rec = _record(diag_mm=50.0)
    rec["faces"]["Face_9"]["thickness_min_mm"] = 0.5
    plan = P.finish(rec, P.zones(rec), Histogram(rows=[(1e-3, 1e-6)]), diag_m=0.05, limit=8_000_000)
    assert "cosmetic" not in plan and "round_walls" not in plan


PLAN = {"limit": {"elements": 8_000_000}, "estimate": {"elements": 100_000}, "measure": {"faces": 40}}


def test_a_failure_naming_most_surfaces_switches_none_to_meshadapt():
    from meshpipeline.engines.gmsh.fea_adapter import BAD_SURFACES

    every = ",".join(str(s) for s in range(1, 41))
    facts = L.pass_facts(1, 1, {}, PLAN, log=f"{BAD_SURFACES} {every}")
    lever = L.next_lever(facts, [], PLAN)
    assert lever["lever"] == "LAD-fea-2" and lever["surfaces"] == []
    assert "remesh_surfaces" not in L.mesh_settings([lever])
    few = L.next_lever(L.pass_facts(1, 1, {}, PLAN, log=f"{BAD_SURFACES} 3,7"), [], PLAN)
    assert few["surfaces"] == [3, 7]


def test_a_surface_the_stage_stalled_on_is_never_put_on_meshadapt_again():
    two = {"lever": "LAD-fea-2", "surfaces": [2, 3], "inputs": {}}
    stall = L.surface_stall_lever(2, [two], n=2)
    assert L.mesh_settings([two, stall])["remesh_surfaces"] == [3]


def test_the_pass_budget_is_sized_from_the_estimate():
    small = FD.pass_work({"estimate": {"elements": 10_000}}, rate=1000.0, cls="simple", machine={"where": "gce"})
    assert small["ceiling_s"] == int(FD.BUDGET_FLOOR_S) and small["budget"]
    mid = FD.pass_work({"estimate": {"elements": 1_000_000}}, rate=1000.0, cls="simple", machine={"where": "gce"})
    assert mid["ceiling_s"] == int(FD.BUDGET_FACTOR * 1000) and mid["budget"]
    huge = FD.pass_work({"estimate": {"elements": 50_000_000}}, rate=1000.0, cls="simple", machine={"where": "gce"})
    assert huge["ceiling_s"] == int(FD.VM_CEILING_S) and not huge["budget"]
    assert "minutes its size allows" in FD.budget_words(mid, 1, 2)


# MESH-23: the surface stage on worker processes

def _plate_model(gmsh):
    gmsh.initialize(interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setNumber("Mesh.RandomSeed", 1)
    box = gmsh.model.occ.addBox(0, 0, 0, 40, 20, 6)
    for x in (8, 20, 32):
        hole = gmsh.model.occ.addCylinder(x, 10, -1, 0, 0, 8, 3)
        gmsh.model.occ.cut([(3, box)], [(3, hole)], removeTool=True)
    gmsh.model.occ.synchronize()
    edges = [abs(t) for _d, t in gmsh.model.getBoundary(gmsh.model.getEntities(3), combined=False, recursive=True)
             if _d == 1][:4]
    if edges:
        gmsh.model.occ.fillet([v for _d, v in gmsh.model.getEntities(3)], edges, [1.0], removeVolume=True)
        gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", 1.5)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)


def _digest(gmsh) -> str:
    h = hashlib.sha256()
    tags, xyz, _ = gmsh.model.mesh.getNodes()
    h.update(np.asarray(tags, np.int64).tobytes())
    h.update(np.asarray(xyz, float).tobytes())
    for _d, s in gmsh.model.getEntities(2):
        _t, et, en = gmsh.model.mesh.getElements(2, s)
        for a, b in zip(et, en, strict=True):
            h.update(np.asarray(a, np.int64).tobytes())
            h.update(np.asarray(b, np.int64).tobytes())
    return h.hexdigest()


def _surface_mesh(workers: int) -> str:
    import gmsh

    from meshpipeline.engines.gmsh import fea_parallel2d as P2D
    _plate_model(gmsh)
    try:
        assert P2D.can_split(gmsh)
        P2D.mesh_surfaces(gmsh, workers)
        return _digest(gmsh)
    finally:
        gmsh.finalize()


def test_the_surfaces_mesh_the_same_on_one_process_or_several():
    pytest.importorskip("gmsh")
    one = _surface_mesh(1)
    assert _surface_mesh(3) == one
    assert _surface_mesh(3) == one          # and the same run twice


def test_the_groups_are_decided_by_the_curve_mesh_alone():
    import gmsh

    from meshpipeline.engines.gmsh import fea_parallel2d as P2D
    pytest.importorskip("gmsh")
    _plate_model(gmsh)
    try:
        gmsh.model.mesh.generate(1)
        surfaces = sorted(t for _d, t in gmsh.model.getEntities(2))
        a = P2D.blocks(gmsh, surfaces)
        assert P2D.blocks(gmsh, surfaces) == a
        assert sorted(s for g in a for s in g) == surfaces
    finally:
        gmsh.finalize()


def test_a_face_gmsh_switched_to_meshadapt_by_itself_keeps_its_first_mesh_next_pass():
    log = ("Info    : [ 20%] Meshing surface 77 (BSpline surface, Frontal-Delaunay)\n"
           "Info    : [ 20%] Meshing surface 78 (BSpline surface, Frontal-Delaunay)\n"
           "Info    : [ 30%] Meshing surface 91 (Plane, Frontal-Delaunay)\n"        # another worker, interleaved
           "Info    : [ 20%] Meshing surface 78 (BSpline surface, MeshAdapt)\n"
           "Info    : [ 40%] Meshing surface 120 (Plane, MeshAdapt)\n")             # a LAD-fea-6 surface: MeshAdapt from the start
    assert L.switched_surfaces(log) == [78]
    facts = {**L.pass_facts(1, 1, {}, PLAN), "switched": [78]}
    lever = L.next_lever(facts, [], PLAN)
    assert lever["lever"] == "LAD-fea-2" and lever["no_switch"] == [78] and "LAD-fea-8" in lever["words"]
    assert L.mesh_settings([lever])["surface_switch"] is False
    # once is enough: the next failure names no new switched face
    again = L.next_lever({**L.pass_facts(2, 1, {}, PLAN), "switched": [78]}, [lever], PLAN)
    assert again is None or "no_switch" not in again


_AFTER_OPENMP = """
import gmsh
from meshpipeline.engines.gmsh import fea_parallel2d as P2D
gmsh.initialize(interruptible=False)
gmsh.option.setNumber("General.Terminal", 0)
gmsh.option.setNumber("General.NumThreads", 4)
gmsh.option.setNumber("Mesh.MaxNumThreads3D", 4)
gmsh.option.setNumber("Mesh.Algorithm3D", 10)
gmsh.model.occ.addBox(0, 0, 0, 40, 20, 6)
for x in (8, 20, 32):
    gmsh.model.occ.cut([(3, 1)], [(3, gmsh.model.occ.addCylinder(x, 10, -1, 0, 0, 8, 3))])
gmsh.model.occ.synchronize()
gmsh.option.setNumber("Mesh.MeshSizeMax", 1.0)
gmsh.model.mesh.generate(3)
gmsh.model.mesh.optimize("")
gmsh.model.mesh.clear()
P2D.mesh_surfaces(gmsh, 3)
print("DONE", len(gmsh.model.mesh.getNodes()[0]))
gmsh.finalize()
"""


def test_the_surface_workers_never_hang_after_gmsh_used_its_threads():
    """MESH-23: a pass meshed again in the same process (MESH-4, MESH-9) forks its surface workers after gmsh's
    OpenMP threads have run - GNU libgomp's pool does not survive fork(), and a worker that started a team of
    threads hung (lab, 1 Nov). Run in its own process, so a hang fails the test instead of the suite."""
    import subprocess
    import sys

    pytest.importorskip("gmsh")
    done = subprocess.run([sys.executable, "-c", _AFTER_OPENMP], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "DONE" in done.stdout


def test_crossing_faces_are_read_from_the_geometry_check_or_the_repair_inspection():
    assert FD.crossing_located({"suspects": [{"kind": "self_overlap", "confirmed": False, "faces": ["Face_1"]}]}, {}) is None
    got = FD.crossing_located({"suspects": [{"kind": "self_overlap", "confirmed": True, "faces": ["Face_71", "Face_74"]},
                                            {"kind": "fold", "confirmed": True, "faces": ["Face_9"]}]}, {})
    assert got["faces"] == ["Face_71", "Face_74"] and got["by"] == "the geometry check"
    state = {"repair_report": {"report": {"defects": [{"code": "self_intersection", "count": 3},
                                                      {"code": "small_edge", "count": 9}]}}}
    got = FD.crossing_located({}, state)
    assert got["repair_self_intersections"] == 3 and got["by"] == "the repair inspection"
    assert "LAD-fea-16" in FD.crossing_words(got, 1)
