# Responsibility: Verify the FEA ladder's last rung (LAD-fea-9): fTetWild's settings come from the plan and the measured part, the rung fires only after every gmsh option and only on the parts still without a valid mesh, its tetrahedra become valid 10-node ones, every boundary triangle carries the CAD face nearest to it, and the delivery runs the same checks and writers and says how close the surface is to the CAD.
# Boundaries: in-process and quick: the real fTetWild binary lives in the mesh image (tests/native), so these tests stand a small stand-in on PATH that fills the given surface with gmsh (its boundary is the input's triangles) - every step after it is the real code. One test runs the real binary where it is installed (the lab, the mesh image) and is skipped elsewhere.
from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_tetwild as TW

# #
# the settings: from the plan and the measured part, never guessed
# #


def _plan(base=0.01, zones=(), faces=()):
    return {"base_size_m": base, "zones": list(zones), "faces": list(faces), "growth": 1.3}


def test_the_envelope_is_ftetwilds_default_unless_a_wall_is_thinner():
    # 100 mm part, no wall measured: a thousandth of the diagonal
    assert TW.envelope_m(0.1, None) == pytest.approx(1e-4)
    # a 0.5 mm wall: never more than a tenth of it
    assert TW.envelope_m(0.1, 0.0005) == pytest.approx(5e-5)
    # a 1 micron film: never below the floor share of the diagonal
    assert TW.envelope_m(0.1, 1e-6) == pytest.approx(TW.EPS_FLOOR_SHARE * 0.1)


def test_a_single_part_is_meshed_whole_at_its_base_size():
    plan = _plan(base=0.012, faces=[{"name": "Face_1", "thickness_min_mm": 4.0}])
    p = TW.params(plan, {"bodies": 1}, 0.12, None)
    assert p["parts"] is None and list(p["per_part"]) == ["whole"]
    w = p["per_part"]["whole"]
    assert w["edge_length_m"] == pytest.approx(0.012)
    assert w["epsilon_m"] == pytest.approx(min(1e-3 * 0.12, 0.1 * 0.004))
    assert w["tessellation_m"] == pytest.approx(0.5 * w["epsilon_m"])
    assert p["threads"] == 1 and p["rule"] == "LAD-fea-9" and p["version"].startswith("fTetWild ")


def test_an_assembly_part_gets_its_own_base_diagonal_and_walls():
    record = {"bodies": 2, "part_bases": [{"n": 1, "diag_mm": 50.0}, {"n": 2, "diag_mm": 200.0}]}
    plan = _plan(base=0.02, zones=[{"part": "Part_2", "size_m": 0.015, "kind": "part"}],
                 faces=[{"name": "Part_1_Face_1", "thickness_min_mm": 1.0},
                        {"name": "Part_2_Face_1", "thickness_min_mm": 30.0}])
    p = TW.params(plan, record, 0.25, [2])
    assert p["parts"] == [2] and list(p["per_part"]) == ["Part_2"]
    two = p["per_part"]["Part_2"]
    assert two["edge_length_m"] == pytest.approx(0.015)          # the part's own base
    assert two["diag_m"] == pytest.approx(0.2)
    assert two["epsilon_m"] == pytest.approx(1e-3 * 0.2)          # its 30 mm walls do not bind
    every = TW.params(plan, record, 0.25, None)
    assert every["parts"] is None and sorted(every["per_part"]) == ["Part_1", "Part_2"]
    assert every["per_part"]["Part_1"]["epsilon_m"] == pytest.approx(min(5e-5, 1e-4))


def test_the_rungs_edge_is_never_longer_than_the_part():
    """LAD-fea-10: a coarsened plan floors every part's base at its zone floor (ox_mc_lid: 61 mm); on a part
    smaller than that the rung's ideal edge is the part's own diagonal, and said - a part whose base is within
    its size keeps it."""
    record = {"bodies": 2, "part_bases": [{"n": 1, "diag_mm": 3.2}, {"n": 2, "diag_mm": 200.0}]}
    plan = _plan(base=0.0613, zones=[{"part": "Part_1", "size_m": 0.0613, "kind": "part"},
                                     {"part": "Part_2", "size_m": 0.0613, "kind": "part"}])
    p = TW.params(plan, record, 0.25, None)
    small, big = p["per_part"]["Part_1"], p["per_part"]["Part_2"]
    assert small["edge_length_m"] == pytest.approx(0.0032)
    assert small["edge_rule"] == TW.EDGE_RULE == "LAD-fea-10" and small["plan_base_m"] == pytest.approx(0.0613)
    assert big["edge_length_m"] == pytest.approx(0.0613) and "edge_rule" not in big


def test_a_parts_peak_memory_is_its_own_not_the_process_that_started_it(tmp_path, fake_ftetwild):
    """The reaped rusage carries the starting process's resident memory across fork and exec: beside a 1.5 GB
    parent every small part read 1.5 GB (lab, 26 Oct). The peak is fTetWild's own high-water mark."""
    import os

    if not Path("/proc/self/status").exists():
        pytest.skip("no /proc here")
    assert TW._hwm_kb(os.getpid()) > 0 and TW._hwm_kb(2 ** 22 + 7) == 0
    ballast = np.ones(400 * 1024 * 1024 // 8)                 # this process: 400 MB more than the stand-in
    xyz = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float) * 0.01
    tris = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])
    r = TW.run_part({"dir": str(tmp_path / "p"), "xyz": xyz, "tris": tris, "edge": 0.005, "epsilon": 1e-5,
                     "bg": None, "name": "tet"})
    assert "error" not in r, r
    mine = TW._hwm_kb(os.getpid()) / 1024.0 ** 2
    assert 0.0 < r["peak_gib"] < mine, (r["peak_gib"], mine)
    assert float(ballast[-1]) == 1.0


def test_the_rungs_machine_holds_gmshs_model_and_ftetwilds_parts_beside_it():
    """RUN-6: the rung's pass is placed by gmsh's model plus fTetWild's own measured one, one part per CPU."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import fea_driver as FD

    small = TW.rung_machine(2e5, *FD.memory_need(2e5), margin=FD.MEMORY_MARGIN)
    assert small["where"] == "cloud_run" and small["ftetwild"]["workers"] == TW.CLOUD_RUN_CPUS
    assert small["ftetwild"]["rule"] == TW.MEMORY_RULE == "RUN-6"
    ox = 11_212_890                                   # ox_mc_lid's plan
    gmsh_gib, files = FD.memory_need(ox)
    d = TW.rung_machine(ox, gmsh_gib, files, margin=FD.MEMORY_MARGIN)
    m = next(x for x in MM.MACHINES if x.name == d["machine"])
    need = gmsh_gib + FD.MEMORY_MARGIN * TW.ftetwild_gib(ox, m.cpus)
    assert need <= MM.usable_gib(m) and d["need_gib"] == pytest.approx(need, abs=0.01)
    assert d["ftetwild"]["workers"] == m.cpus and d["ftetwild"]["gmsh_gib"] == pytest.approx(gmsh_gib, abs=0.01)
    # gmsh's model alone just fits a machine: fTetWild's parts beside it take the next one
    edge = next(x for x in MM.MACHINES if x.name == "n2-highmem-8")
    tight = MM.usable_gib(edge) - 0.01
    up = TW.rung_machine(ox, tight, 0.0, margin=FD.MEMORY_MARGIN)
    assert up["machine"] != "n2-highmem-8" and up["memory_gib"] > edge.memory_gib


def test_the_rung_takes_the_named_parts_first_then_every_part():
    asm = {"bodies": 3}
    passes = [{"bad_faces": ["Part_3_Face_2", "Part_3_Face_7"]}, {"bad_faces": []}]
    assert TW.rung_parts(asm, passes, 1) == [3]
    assert TW.rung_parts(asm, passes, 2) is None                  # the second rung pass: every part
    assert TW.rung_parts(asm, [{"bad_faces": []}], 1) is None     # none named: every part at once
    assert TW.rung_parts({"bodies": 1}, passes, 1) is None        # a single part is always whole


def test_the_plan_words_say_which_parts_and_why():
    said = L.rung_words([2, 5], 1)
    assert "Part_2, Part_5" in said and "LAD-fea-9" in said and "the tie" in said
    assert "the part" in L.rung_words(None, 1)


# #
# geometry: quadratic tetrahedra, the boundary, the nearest face
# #

def _cube_tets():
    xyz = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1]], float)
    kuhn = [(0, 1, 3, 7), (0, 1, 5, 7), (0, 2, 3, 7), (0, 2, 6, 7), (0, 4, 5, 7), (0, 4, 6, 7)]
    return xyz, np.array(kuhn)


def test_tetrahedra_are_turned_positive_and_their_boundary_found():
    xyz, tets = _cube_tets()
    t, vol = TW.orient(xyz, tets)
    a, b, c, d = (xyz[t[:, i]] for i in range(4))
    assert (np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) > 0).all()
    assert vol.sum() == pytest.approx(1.0)
    tris = TW.boundary(t)
    assert len(tris) == 12                                        # two triangles a cube face
    # outward: every boundary triangle's normal points away from the cube's centre
    n = np.cross(xyz[tris[:, 1]] - xyz[tris[:, 0]], xyz[tris[:, 2]] - xyz[tris[:, 0]])
    out = xyz[tris].mean(axis=1) - 0.5
    assert (np.einsum("ij,ij->i", n, out) > 0).all()


def test_quadratic_tetrahedra_have_a_node_at_every_edge_middle_shared_with_their_faces():
    xyz, tets = _cube_tets()
    t, _ = TW.orient(xyz, tets)
    tris = TW.boundary(t)
    x10, t10, tri6 = TW.quadratic(xyz, t, tris)
    assert t10.shape == (6, 10) and tri6.shape == (12, 6)
    # gmsh's 10-node order: edges 0-1, 1-2, 2-0, 3-0, 3-2, 3-1
    for row in t10:
        for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 0), (3, 0), (3, 2), (3, 1)), start=4):
            assert np.allclose(x10[row[k]], (x10[row[i]] + x10[row[j]]) / 2)
    for row in tri6:
        for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 0)), start=3):
            assert np.allclose(x10[row[k]], (x10[row[i]] + x10[row[j]]) / 2)
    # every edge's middle node is one node (19 edges of the Kuhn cube: 12 sides, 6 face diagonals, 1 main)
    assert len(x10) == 8 + 19
    # numbered by the sorted edges: the same mesh gives the same numbers
    again = TW.quadratic(xyz, t, tris)
    assert all(np.array_equal(a, b) for a, b in zip(again, (x10, t10, tri6), strict=True))


def test_the_nearest_face_is_found_even_on_large_triangles():
    # two faces of a unit square split along its diagonal: label 0 below y = x, label 1 above
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    near = TW.Nearest(xyz, tris, np.array([0, 1]), spacing=0.05)
    q = np.array([[0.9, 0.1, 0.01], [0.1, 0.9, -0.02], [0.5, 0.2, 0.3], [2.0, 0.5, 0.0]])
    lab, dist = near.query(q)
    assert lab.tolist()[:3] == [0, 1, 0]
    assert dist[:3] == pytest.approx([0.01, 0.02, 0.3])
    assert dist[3] == pytest.approx(1.0)                          # beyond the edge x = 1


def test_the_sizing_field_resolves_a_thin_pane_through_its_thickness():
    """fc_glass_frames: a 2000 x 1000 x 10 mm pane - the sizing field's cells follow EACH side, so the
    pane's 7.5 mm wall zone is held through its 10 mm, not lost in one 50 mm cell."""
    pytest.importorskip("vtkmodules.vtkFiltersCore")
    x, y, z = 2.0, 1.0, 0.01
    xyz = np.array([[0, 0, 0], [x, 0, 0], [x, y, 0], [0, y, 0], [0, 0, z], [x, 0, z], [x, y, z], [0, y, z]], float)
    tris = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                     [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    label = np.array([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
    names = [f"Face_{k}" for k in range(1, 7)]
    plan = {"growth": 1.3, "zones": [{"kind": "wall", "size_m": 0.0075, "reach_m": 0.0, "faces": ["Face_1", "Face_2"]}]}
    pts, tets, h = TW.sizing_grid(xyz, tris, label, names, plan, edge=0.2, pad=0.002)
    across = (pts[:, 2] >= 0) & (pts[:, 2] <= z)
    assert len(np.unique(np.round(pts[across, 2], 9))) >= TW.BG_DIVISIONS     # cells across the 10 mm
    inside = across & (pts[:, 0] >= 0) & (pts[:, 0] <= x) & (pts[:, 1] >= 0) & (pts[:, 1] <= y)
    assert h[inside].max() <= 0.0075 * 1.3 + 1e-9                               # the wall's size holds inside
    assert tets.shape[1] == 4 and tets.max() < len(pts)


def test_a_point_on_a_big_face_beside_a_narrow_one_is_on_the_big_face():
    """fc_glass_frames: a 10 mm edge face split into many small triangles must not crowd the big face
    out of the search - a point ON the big face is 0 from it, whatever lies beside it."""
    x, z, t = 1.0, 2.0, 0.01
    # the big face (y = 0) and the narrow edge face (z = 2, 10 mm deep in y) sharing the edge z = 2
    xyz = np.array([[0, 0, 0], [x, 0, 0], [x, 0, z], [0, 0, z], [0, t, z], [x, t, z]], float)
    tris = np.array([[0, 1, 2], [0, 2, 3], [3, 2, 5], [3, 5, 4]])
    near = TW.Nearest(xyz, tris, np.array([0, 0, 1, 1]), spacing=0.05)
    q = np.array([[0.4, 0.0, 1.993], [0.7, 0.0, 1.0], [0.5, 0.005, 2.0]])
    lab, dist = near.query(q)
    assert lab.tolist()[:2] == [0, 0] and dist[:2] == pytest.approx([0.0, 0.0], abs=1e-12)
    assert lab[2] == 1 and dist[2] == pytest.approx(0.0, abs=1e-12)
    # against brute force over every split triangle, on random points
    rng = np.random.default_rng(7)
    pts = rng.uniform([-0.1, -0.05, -0.1], [1.1, 0.06, 2.1], size=(400, 3))
    lab, dist = near.query(pts)
    c = near.corners
    brute = np.array([np.linalg.norm(TW.closest_on_triangles(np.repeat(p[None], len(c), 0), c[:, 0], c[:, 1], c[:, 2])
                                     - p, axis=1).min() for p in pts])
    assert dist == pytest.approx(brute, abs=1e-12)


def test_parts_run_in_order_and_one_killed_for_memory_runs_again_alone(monkeypatch):
    seen: list[str] = []

    def fake(job):
        seen.append(job["name"])
        if job["name"] == "b" and seen.count("b") == 1:
            return {"error": "fTetWild exited -9", "rc": -9}
        return {"name": job["name"]}

    monkeypatch.setattr(TW, "run_part", fake)
    monkeypatch.setattr(TW, "memory_room", lambda: 0.0)          # no room: one part at a time
    out = TW.run_parts([{"name": n} for n in "abc"], workers=4)
    assert [r["name"] for r in out] == ["a", "b", "c"]
    assert seen == ["a", "b", "c", "b"]


def test_closest_points_on_triangles_cover_every_region():
    a, b, c = np.array([[0.0, 0, 0]]), np.array([[1.0, 0, 0]]), np.array([[0.0, 1, 0]])
    cases = {(-1, -1, 0): (0, 0, 0), (2, -1, 0): (1, 0, 0), (-1, 2, 0): (0, 1, 0), (0.5, -1, 0): (0.5, 0, 0),
             (-1, 0.5, 0): (0, 0.5, 0), (1, 1, 0): (0.5, 0.5, 0), (0.2, 0.2, 5): (0.2, 0.2, 0)}
    for p, want in cases.items():
        got = TW.closest_on_triangles(np.array([p], float), a, b, c)[0]
        assert np.allclose(got, want), (p, got)


# #
# the rung through the real driver and adapter, a stand-in fTetWild on PATH
# #

FAKE = r'''
import sys
import numpy as np
import gmsh

args = sys.argv[1:]
src, out = args[args.index("-i") + 1], args[args.index("-o") + 1]
la = float(args[args.index("--la") + 1])
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
print("stand-in fTetWild: filling", len(f), "triangles", flush=True)
gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
gmsh.merge(stl)
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
def fake_ftetwild(tmp_path, monkeypatch):
    pytest.importorskip("gmsh")
    pytest.importorskip("meshio")
    exe = tmp_path / "bin" / "fTetWild"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{FAKE}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr(TW, "binary", lambda: str(exe))
    return exe


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _stage(shape, ws: Path) -> Path:
    import gmsh
    from tests import fea_fixtures as fx

    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    return ws


def _drive(ws: Path, monkeypatch, state=None):
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh import fea_adapter

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": _safe(fea_adapter.mesh, w)}))
    return asyncio.run(FD.drive(ws, state or {}, job_id=f"t-{ws.name}", publish=None, run=_Run()))


def _safe(fn, w):
    try:
        return fn(w)
    except Exception:  # noqa: BLE001 - as the mesh runner sees a pass that raised: a failed pass
        return 1


def _gmsh_fails_on(monkeypatch, volumes: set[int] | None, faces: list[str]):
    """Every gmsh pass fails (its build raises, naming `faces`, as a fill that failed does); the last
    rung's build (rung="named" / "all") is the real one, meshing the parts fTetWild does not take.
    `volumes`: kept for the reader - every gmsh pass holds them all."""
    from meshpipeline.engines.gmsh import fea_adapter

    real = fea_adapter.build_volume
    calls: list[str | None] = []

    def build(gmsh, ws, plan, geom, asm, sidecar, *, scale=1.0, tags=None, rung=None):
        calls.append(rung)
        if rung is None:
            print(f"{fea_adapter.BAD_FACES} {','.join(faces)}", file=sys.stderr)
            fea_adapter.note_failure(ws, [], faces, "the fill failed")
            raise RuntimeError("PLC Error: A segment and a facet intersect")
        return real(gmsh, ws, plan, geom, asm, sidecar, scale=scale, tags=tags, rung=rung)

    monkeypatch.setattr(fea_adapter, "build_volume", build)
    return calls


def test_a_part_gmsh_cannot_mesh_is_delivered_by_the_last_rung(tmp_path, monkeypatch, fake_ftetwild):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.engines.fea_results import results
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    calls = _gmsh_fails_on(monkeypatch, None, ["Face_5"])
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    assert read_refusal(ws) is None
    passes = json.loads((ws / "fea_passes.json").read_text())
    rung = [p for p in passes["passes"] if p.get("engine") == "ftetwild"]
    gm = [p for p in passes["passes"] if p.get("engine") != "ftetwild"]
    # every gmsh option first (the fixed ladder's passes), the rung after them, delivered
    assert len(gm) == calls.count(None) >= 2 and not any(p["valid"] for p in gm)
    assert len(rung) == 1 and rung[0]["valid"] and passes["delivered"] == rung[0]["pass"]
    cited = [e for e in dr.cited_for(f"t-{ws.name}") if e["rule"] == "LAD-fea-9"]
    assert cited and cited[0]["inputs"]["per_part"]["whole"]["epsilon_m"] > 0
    q = json.loads((ws / "quality.json").read_text())
    assert q["engine_rung"] == "ftetwild" and q["element_order"] == 2 and not q["fatal"]
    assert q["min_sicn"] > 0 and q["fea_inverted"] == 0
    ftw = q["fea"]["ftetwild"]
    assert ftw["parts"] == ["whole"] and ftw["lost_faces"] == [] and "fTetWild" in ftw["words"]
    assert ftw["deviation_mm"] <= ftw["epsilon_mm"] + 1e-9        # the stand-in keeps the input surface
    # the same deck, with every CAD face named
    deck = (ws / "mesh.inp").read_text()
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert "TYPE=C3D10" in deck and "fTetWild" in deck
    for k in range(1, plan["measure"]["faces"] + 1):
        assert f"*SURFACE, NAME=Face_{k}," in deck
    assert (ws / "mesh.bdf").exists() and (ws / "mesh.cdb").exists()
    res = results(ws)
    assert res["safety_net"]["engine"] == "fTetWild" and res["safety_net"]["words"] in res["plan_words"]


def test_the_same_part_gives_the_same_rung_mesh(tmp_path, monkeypatch, fake_ftetwild):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    digests = []
    for name in ("a", "b"):
        ws = _stage(fx.bracket(fillet=None), tmp_path / name)
        _gmsh_fails_on(monkeypatch, None, ["Face_1"])
        ok, marker, _o = _drive(ws, monkeypatch)
        assert ok, marker
        digests.append(hashlib.sha256((ws / "mesh.inp").read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_only_the_failing_part_of_an_assembly_goes_to_ftetwild_the_rest_keeps_gmsh(tmp_path, monkeypatch,
                                                                                   fake_ftetwild):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    ws = _stage(fx.touching_blocks(), tmp_path / "pair")
    calls = _gmsh_fails_on(monkeypatch, {2}, ["Part_2_Face_1"])
    ok, marker, _o = _drive(ws, monkeypatch, {"purpose": "structural", "fea_connection": "contact"})
    assert ok, marker
    q = json.loads((ws / "quality.json").read_text())
    ftw = q["fea"]["ftetwild"]
    assert ftw["parts"] == ["Part_2"] and ftw["tied"] and ftw["gmsh_parts"] == 1
    assert calls[-1] == "named"                                   # gmsh meshed Part_1 alone in the rung pass
    asm = q["fea"]["assembly"]
    assert [p["id"] for p in asm["parts"]] == ["Part_1", "Part_2"] and all(p["elements"] for p in asm["parts"])
    assert asm["joints"], "the parts are still paired where they touch"
    deck = (ws / "mesh.inp").read_text()
    for name in ("Part_1_Face_1", "Part_2_Face_1", "Part_2_Face_6"):
        assert f"NAME={name}," in deck or f"NAME={name}\n" in deck


def test_the_rung_never_fires_when_gmsh_meshes_the_part(tmp_path, monkeypatch, fake_ftetwild):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.contracts import decision_record as dr

    ws = _stage(fx.bracket(fillet=None), tmp_path / "fine")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    assert not [e for e in dr.cited_for(f"t-{ws.name}") if e["rule"] == "LAD-fea-9"]
    q = json.loads((ws / "quality.json").read_text())
    assert "engine_rung" not in q and "ftetwild" not in q["fea"]
    assert not (ws / "_ftetwild").exists()


def test_a_guard_trip_never_reaches_the_rung(tmp_path, monkeypatch, fake_ftetwild):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime

    ws = _stage(fx.plate_with_hole(), tmp_path / "trip")
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": -1, "timed_out": True, "stalled": True}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-rung-trip", publish=None, run=_Run()))
    assert not ok and marker == FD.GUARD_TRIP
    assert not [e for e in dr.cited_for("t-rung-trip") if e["rule"] == "LAD-fea-9"]


def test_when_ftetwild_fails_too_the_ladder_says_so(tmp_path, monkeypatch):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines.preflight import read_refusal

    monkeypatch.setattr(TW, "binary", lambda: None)              # not installed: the rung's pass fails
    ws = _stage(fx.plate_with_hole(), tmp_path / "none")
    _gmsh_fails_on(monkeypatch, None, ["Face_2"])
    ok, marker, _o = _drive(ws, monkeypatch)
    assert not ok and marker == FD.BUILD_FAILED
    ref = read_refusal(ws)
    assert "fTetWild (the last rung) built no valid mesh either" in ref.builder_text


# #
# the real binary (the mesh image, the lab): skipped where it is not installed
# #

@pytest.mark.skipif(TW.binary() is None, reason="fTetWild is installed only in the mesh image")
def test_the_real_ftetwild_meshes_a_box_the_same_way_twice(tmp_path):
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 0.5, 0], [0, 0.5, 0], [0, 0, 0.2], [1, 0, 0.2], [1, 0.5, 0.2],
                    [0, 0.5, 0.2]], float) * 0.1
    tris = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                     [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    got = []
    for k in range(2):
        r = TW.run_part({"dir": str(tmp_path / f"r{k}"), "xyz": xyz, "tris": tris, "edge": 0.01, "epsilon": 1e-4,
                         "bg": None, "name": "box"})
        assert "error" not in r, r
        t, vol = TW.orient(r["xyz"], r["tets"])
        assert (vol > 0).all() and vol.sum() == pytest.approx(0.1 * 0.05 * 0.02, rel=1e-3)
        got.append(hashlib.sha256(r["xyz"].tobytes() + r["tets"].tobytes()).hexdigest())
    assert got[0] == got[1]


def test_nothing_here_reads_an_environment_variable():
    # the binary is found on PATH or at its install path, never through a setting (settings/inventory)
    src = Path(TW.__file__).read_text()
    assert "os.environ" not in src and "optional_env" not in src


def test_the_rung_takes_only_the_part_gmsh_failed_on_and_keeps_the_others_as_gmsh_filled_them(tmp_path, monkeypatch,
                                                                                            fake_ftetwild, capsys):
    """LAD-fea-11 with the last rung: parts meshed apart; gmsh's fill fails on Part_2 on every pass. Pass 1 keeps
    the other parts, pass 2 meshes only Part_2, and the rung's pass gives Part_2 to fTetWild with the others
    read back as pass 1 filled them - never meshed by gmsh again."""
    pytest.importorskip("OCP.BRepPrimAPI")
    import gmsh
    from tests import fea_fixtures as fx


    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 8, 8, 8), fx._box(40, 0, 0, 6, 6, 6))
    ws = _stage(shape, tmp_path / "keep_rung")
    real = gmsh.model.mesh.generate

    def generate(dim=3):
        if dim == 3 and gmsh.option.getNumber("Mesh.MeshOnlyVisible") and gmsh.model.getVisibility(3, 2):
            raise Exception("Invalid boundary mesh (overlapping facets) on surface 7 surface 8")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate))
    ok, marker, _o = _drive(ws, monkeypatch, {"purpose": "structural", "fea_connection": "contact"})
    assert ok, marker
    said = capsys.readouterr().err
    # pass 2 (gmsh on Part_2 alone: HXT, its faces with MeshAdapt - then no gmsh option is left) and the rung's
    # pass (fTetWild on Part_2 alone)
    assert said.count("2 part volume(s) kept from the pass before; meshing the other 1") == 2
    q = json.loads((ws / "quality.json").read_text())
    ftw = q["fea"]["ftetwild"]
    assert ftw["parts"] == ["Part_2"] and ftw["gmsh_parts"] == 2 and not q["fatal"]
    passes = json.loads((ws / "fea_passes.json").read_text())["passes"]
    assert [p.get("engine", "gmsh") for p in passes] == ["gmsh", "gmsh", "ftetwild"] and passes[-1]["valid"]


def test_a_part_a_later_gmsh_pass_did_fill_is_never_given_to_ftetwild_too(tmp_path, monkeypatch, fake_ftetwild):
    """LAD-fea-11: pass 1 fails on Part_2 and Part_3; pass 2 fills Part_3 but not Part_2. The rung takes Part_2
    alone - Part_3 keeps its gmsh mesh, never meshed a second time (two overlapping element sets in the deck)."""
    pytest.importorskip("OCP.BRepPrimAPI")
    import gmsh
    from tests import fea_fixtures as fx

    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 8, 8, 8), fx._box(40, 0, 0, 6, 6, 6))
    ws = _stage(shape, tmp_path / "later")
    real = gmsh.model.mesh.generate
    seen: list[str] = []

    def generate(dim=3):
        if dim == 3 and gmsh.option.getNumber("Mesh.MeshOnlyVisible"):
            plan = json.loads((ws / "fea_plan.json").read_text())
            first = not (plan.get("mesh") or {}).get("keep")
            for v in ((2, 3) if first else (2,)):
                if gmsh.model.getVisibility(3, v):
                    seen.append(f"fail {v}")
                    raise Exception(f"Invalid boundary mesh (overlapping facets) on surface {v * 6} surface {v * 6 + 1}")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate))
    ok, marker, _o = _drive(ws, monkeypatch, {"purpose": "structural", "fea_connection": "contact"})
    assert ok, marker
    q = json.loads((ws / "quality.json").read_text())
    assert q["fea"]["ftetwild"]["parts"] == ["Part_2"] and not q["fatal"]
    parts = {p["id"]: p["elements"] for p in q["fea"]["assembly"]["parts"]}
    assert set(parts) == {"Part_1", "Part_2", "Part_3"} and all(n > 0 for n in parts.values())
    assert q["cells"] == sum(parts.values())                     # no part meshed twice
