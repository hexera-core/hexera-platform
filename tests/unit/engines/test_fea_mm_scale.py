# Responsibility: Verify MESH-4, MESH-5 (a face whose surface mesh repeats a triangle keeps one copy), LAD-fea-8 (a surface stage that stalls is meshed again without gmsh's switch to MeshAdapt) and LAD-fea-6's locked slivers: a staged solid the FEA mesher cannot mesh in metres (a surface no 2D algorithm meshes, a fill that fails, a volume left empty) is meshed again at the CAD's own millimetre scale and delivered in metres, every face still named; a solid that meshes in metres never takes that way, and a failure at both scales still names its surfaces; and an invalid sliver locked between two faces names only one of them, so the next pass breaks their mirrored meshes.
# Boundaries: the metre failure is forced on a small built part (the real one, libredrive_pulley_housing's 24 nm grazing edges, is the lab's: ~/graze_lab); gmsh runs in-process.
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter as A  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    """The upload read in mm, written in metres, as the builder stages it for gmsh."""
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
    finally:
        gmsh.finalize()
    record, surface, diag = FD.measure_staged(ws)
    FD.write_plan(ws, FD.make_plan(record, surface, diag))
    return ws


#: a small block (40 x 20 x 6 mm): quick to plan and mesh, six named faces
def _part():
    return fx._box(0, 0, 0, 40, 20, 6)


@pytest.fixture(scope="module")
def staged(tmp_path_factory) -> Path:
    return _stage(_part(), tmp_path_factory.mktemp("fea_mm_staged"))


@pytest.fixture(scope="module")
def metre_mesh(staged, tmp_path_factory) -> Path:
    """The block meshed the ordinary way, in metres - the reference the millimetre mesh is held to."""
    ws = _ws(staged, tmp_path_factory.mktemp("fea_mm_ref") / "ref")
    assert A.mesh(ws) == 0
    return ws


def _ws(staged: Path, dst: Path) -> Path:
    import shutil

    shutil.copytree(staged, dst)
    return dst


def _fail_in_metres(monkeypatch, *, always: bool = False) -> list[float]:
    """build_volume as it is, but failing the way the housing failed whenever the scale is metres
    (or at every scale). Returns the scales it was asked for."""
    real = A.build_volume
    asked: list[float] = []

    def build(gmsh_, ws, plan, geom, asm, sidecar, *, scale=1.0, tags=None):
        asked.append(scale)
        if scale == 1.0 or always:
            gmsh_.model.add("fea")
            raise A.FillFailed(RuntimeError("Impossible to mesh periodic surface 3"), [3], None,
                               {"surface_tag": {}, "volume_tag": {}})
        return real(gmsh_, ws, plan, geom, asm, sidecar, scale=scale, tags=tags)

    monkeypatch.setattr(A, "build_volume", build)
    return asked


def test_every_length_of_the_plan_is_scaled_and_nothing_else():
    plan = {"base_size_m": 0.01, "limit": {"elements": 5}, "zones": [
        {"kind": "hole", "size_m": 0.001, "reach_m": 0.002, "radius_m": 0.004, "faces": ["Face_3"]},
        {"kind": "wall_spot", "size_m": 0.0005, "reach_m": 0.001, "at_m": [0.1, 0.2, 0.3], "radius_m": 0.002,
         "faces": ["Face_1"]}]}
    out = A.scaled_plan(plan, 1000.0)
    assert out["base_size_m"] == pytest.approx(10.0) and out["limit"] == {"elements": 5}
    assert out["zones"][0]["size_m"] == pytest.approx(1.0) and out["zones"][0]["reach_m"] == pytest.approx(2.0)
    assert out["zones"][0]["radius_m"] == pytest.approx(4.0) and out["zones"][0]["faces"] == ["Face_3"]
    assert out["zones"][1]["at_m"] == pytest.approx([100.0, 200.0, 300.0])
    assert plan["zones"][0]["size_m"] == 0.001                     # the plan itself is untouched
    assert A.scaled_plan(plan, 1.0) is plan


def test_a_solid_that_meshes_in_metres_never_takes_the_millimetre_way(staged, tmp_path, monkeypatch):
    ws = _ws(staged, tmp_path / "m")
    real = A.build_volume
    asked: list[float] = []
    monkeypatch.setattr(A, "build_volume", lambda *a, **k: asked.append(k.get("scale", 1.0)) or real(*a, **k))
    assert A.mesh(ws) == 0
    assert asked == [1.0]
    assert "fea_mesh_scale" not in json.loads((ws / "quality.json").read_text())


def test_a_solid_that_fails_in_metres_is_meshed_in_millimetres_and_delivered_in_metres(staged, metre_mesh, tmp_path,
                                                                                      monkeypatch):
    ref = metre_mesh
    ws = _ws(staged, tmp_path / "mm")
    asked = _fail_in_metres(monkeypatch)
    assert A.mesh(ws) == 0
    assert asked == [1.0, A.MM_PER_M]
    q, q_ref = (json.loads((w / "quality.json").read_text()) for w in (ws, ref))
    assert q["fea_mesh_scale"] == "mm" and not q["fatal"] and q["min_sicn"] > 0.0
    # the same part, in metres, sized as planned: the bounds of the 40 x 20 x 6 mm block, about as many
    # elements as the metre mesh, the walls held to their rule
    assert q["bounds"] == pytest.approx(q_ref["bounds"], abs=1e-6)
    assert 0.9 * q_ref["cells"] <= q["cells"] <= 1.1 * q_ref["cells"]
    assert q["fea"]["rules"]["short"] == []
    deck = (ws / "mesh.inp").read_text()
    for k in range(1, 7):
        assert f"*SURFACE, NAME=Face_{k}," in deck


def test_a_failure_at_both_scales_names_its_surfaces_for_the_ladder(staged, tmp_path, monkeypatch, capsys):
    ws = _ws(staged, tmp_path / "both")
    _fail_in_metres(monkeypatch, always=True)
    with pytest.raises(RuntimeError, match="periodic surface 3"):
        A.mesh(ws)
    err = capsys.readouterr().err
    assert f"{A.BAD_SURFACES} 3" in err and f"{A.BAD_FACES} Face_3" in err
    said = json.loads((ws / A.FAILURE_FILE).read_text())
    assert said["bad_surfaces"] == [3] and said["bad_faces"] == ["Face_3"]


def test_a_surface_gmsh_cannot_mesh_is_a_named_failure_not_a_bare_traceback(staged, tmp_path, monkeypatch):
    """The 2D stage's own error ("Impossible to mesh periodic surface 26") names the surface: the ladder
    reads it (the housing's tied passes failed naming nothing before)."""
    ws = _ws(staged, tmp_path / "2d")

    def surface_mesh(gmsh_, surfaces, remesh):
        raise Exception("Impossible to mesh periodic surface 2")

    monkeypatch.setattr(A, "surface_mesh", surface_mesh)
    with pytest.raises(Exception, match="periodic surface 2"):
        A.mesh(ws)
    said = json.loads((ws / A.FAILURE_FILE).read_text())
    assert said["bad_surfaces"] == [2] and said["bad_faces"] == ["Face_2"]


def _cfd_ws(staged: Path, ws: Path) -> Path:
    """A CAD solid staged for the CFD gmsh path (geometry.step in metres, a plain spec)."""
    _ws(staged, ws)
    (ws / "fea_plan.json").unlink()                 # the CFD driver, not the FEA plan
    (ws / "gmsh_spec.json").write_text(json.dumps({"size": {"mode": "factor", "value": 0.08},
                                                   "dimensionality": "3D", "element_order": 2}))
    return ws


def test_the_cfd_path_meshes_a_cad_solid_it_cannot_mesh_in_metres_in_millimetres(staged, tmp_path, monkeypatch, capsys):
    """MESH-4 covers the CFD gmsh path too: the same metre-scale fill failure on a CAD solid is meshed
    again at millimetre scale and delivered in metres, its groups intact."""
    from meshpipeline.engines.gmsh import driver as D

    ref = _cfd_ws(staged, tmp_path / "ref")
    assert D.main(str(ref)) == 0
    ws = _cfd_ws(staged, tmp_path / "mm")
    real = gmsh.model.mesh.generate
    calls: list[int] = []

    def generate(dim=3):
        calls.append(dim)
        if len(calls) == 1:                         # the first fill, in metres, fails as the housing's did
            raise Exception("Invalid boundary mesh (overlapping facets) on surface 3 surface 5")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate))
    assert D.main(str(ws)) == 0
    assert "MESH-4" in capsys.readouterr().err
    q, q_ref = (json.loads((w / "quality.json").read_text()) for w in (ws, ref))
    assert q["bounds"] == pytest.approx(q_ref["bounds"], abs=1e-6)
    assert not q["fatal"] and q["min_sicn"] > 0.0 and q["groups"] == q_ref["groups"]
    assert 0.9 * q_ref["cells"] <= q["cells"] <= 1.1 * q_ref["cells"]


def test_the_cfd_path_never_takes_the_millimetre_way_for_a_solid_that_meshes(staged, tmp_path, capsys):
    from meshpipeline.engines.gmsh import driver as D

    ws = _cfd_ws(staged, tmp_path / "m")
    assert D.main(str(ws)) == 0
    err = capsys.readouterr().err
    assert "MESH-4" not in err and "MILLIMETRE" not in err


class _Mesh:
    """Just enough of gmsh.model.mesh for bad_surfaces: elements of four corner nodes, each node on an
    entity (dim, tag)."""

    def __init__(self, elements: dict[int, list[int]], nodes: dict[int, tuple[int, int]]):
        self.elements, self.nodes = elements, nodes

    def getElement(self, t):
        return 4, self.elements[t], 3, 1

    def getNode(self, n):
        return [0.0, 0.0, 0.0], [], *self.nodes[n]


def _stub(elements, nodes, curves=None):
    from types import SimpleNamespace

    adj = curves or {}
    return SimpleNamespace(model=SimpleNamespace(mesh=_Mesh(elements, nodes),
                                                 getAdjacencies=lambda dim, ent: (adj.get((dim, ent), []), [])))


def test_a_sliver_locked_between_two_faces_names_only_the_second_face():
    """LAD-fea-6: two faces facing each other across a wall one element thick, meshed alike - remeshing
    BOTH the other way mirrors them again, so only the second is named (fc_glass_frames' pane)."""
    nodes = {1: (2, 5), 2: (2, 5), 3: (2, 6), 4: (2, 6), 5: (2, 5), 6: (2, 6)}
    g = _stub({10: [1, 2, 3, 4], 11: [2, 5, 4, 6]}, nodes)
    assert A.bad_surfaces(g, [10, 11, 12], [-1e-6, 0.0, 0.5]) == [6]


def test_an_invalid_element_with_a_node_inside_still_names_every_face_it_touches():
    nodes = {1: (2, 5), 2: (2, 5), 3: (2, 6), 4: (3, 1)}
    g = _stub({10: [1, 2, 3, 4]}, nodes)
    assert A.bad_surfaces(g, [10], [-0.2]) == [5, 6]


def test_a_face_another_invalid_element_touches_is_never_kept_back():
    nodes = {1: (2, 5), 2: (2, 5), 3: (2, 6), 4: (2, 6), 7: (2, 5), 8: (2, 7), 9: (3, 1)}
    g = _stub({10: [1, 2, 3, 4], 11: [7, 8, 9, 1]}, nodes)
    assert A.bad_surfaces(g, [10, 11], [-0.1, -0.1]) == [5, 6, 7]


def _box_with_a_repeat(repeat: bool):
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add("rep")
    gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", 0.3)
    gmsh.model.mesh.generate(2)
    surfaces = [t for _d, t in gmsh.model.getEntities(2)]
    if repeat:
        _t, _tags, nodes = gmsh.model.mesh.getElements(2, surfaces[2])
        first = [int(n) for n in nodes[0][:3]]
        gmsh.model.mesh.addElementsByType(surfaces[2], 2, [int(gmsh.model.mesh.getMaxElementTag()) + 1], first[::-1])
    return surfaces


def test_a_face_whose_surface_mesh_repeats_a_triangle_keeps_one_copy_and_fills():
    """MESH-5: the same three nodes laid down twice on one face (nakuja_rocket's nose cone, where the seam
    meets the apex) stop the fill ("Found two duplicated facets"); one copy dropped, it fills - and a
    face with no repeat is left exactly as it was."""
    import numpy as np

    try:
        surfaces = _box_with_a_repeat(False)
        before = {s: gmsh.model.mesh.getElements(2, s)[1][0] for s in surfaces}
        assert A.drop_repeated_triangles(gmsh, surfaces) == {}
        for s in surfaces:                                  # nothing touched
            assert np.array_equal(gmsh.model.mesh.getElements(2, s)[1][0], before[s])
        gmsh.finalize()
        _box_with_a_repeat(True)
        with pytest.raises(Exception):                     # the repeat stops the fill
            gmsh.model.mesh.generate(3)
        gmsh.finalize()
        surfaces = _box_with_a_repeat(True)
        assert A.drop_repeated_triangles(gmsh, surfaces) == {surfaces[2]: 1}
        gmsh.model.mesh.generate(3)
        assert sum(len(t) for t in gmsh.model.mesh.getElements(3)[1]) > 0
    finally:
        if gmsh.isInitialized():
            gmsh.finalize()


_STALLED_2D = ("Info    : Meshing 2D...\nInfo    : [ 40%] Meshing surface 4 (BSpline surface, Frontal-Delaunay)\n"
               "Info    : [ 40%] 2 triangles are equivalent\nInfo    : [ 40%] Meshing surface 4 (BSpline surface, MeshAdapt)\n")


def test_a_stall_in_the_surface_stage_names_the_surface_and_one_after_it_does_not():
    from meshpipeline.engines import fea_ladder as L

    assert L.stalled_surface(_STALLED_2D) == 4
    assert L.stalled_surface(_STALLED_2D + "Info    : Done meshing 2D (Wall 2s)\nInfo    : Meshing 3D...\n") is None
    assert L.stalled_surface("") is None
    lever = L.surface_stall_lever(4, [], n=1)
    assert lever["lever"] == L.SURFACE_STALL and L.surface_stall_lever(4, [lever], n=2) is None
    assert L.mesh_settings([lever])["surface_switch"] is False
    assert "surface_switch" not in L.mesh_settings([])


def test_a_surface_stage_that_stalls_is_meshed_again_without_gmshs_switch(staged, tmp_path, monkeypatch):
    """LAD-fea-8: the watchdog stopped the mesher in 2D (gmsh's MeshAdapt on one face ran past the watchdog): the part's,
    not ours - the next pass keeps gmsh's first mesh of every face and delivers."""
    import asyncio
    from types import SimpleNamespace

    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.engines import runtime

    ws = _ws(staged, tmp_path / "stall")
    seen: list[object] = []

    def _run(w, timeout):
        plan = json.loads((Path(w) / "fea_plan.json").read_text())
        seen.append((plan.get("mesh") or {}).get("surface_switch"))
        if len(seen) == 1:
            return {"rc": -1, "timed_out": True, "stalled": True, "log_tail": _STALLED_2D, "watchdog": {}}
        return {"rc": A.mesh(w)}

    class _Run:
        async def fence(self, _why):
            return None

        def note_authoring(self):
            pass

        def note_native_run(self, produced_usable_mesh):
            pass

        def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
            return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=_run))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-stall", publish=None, run=_Run()))
    assert ok, marker
    assert seen == [None, False]
    assert any(e["rule"] == "LAD-fea-8" and e["decided"] == "lever" for e in dr.cited_for("t-stall"))


_SPUN_3D = ("Info    : Done meshing 2D (Wall 31s)\nInfo    : Meshing 3D...\n"
            "Info    : It. 9267500 - 210945 nodes created - worst tet radius 13.2469 (nodes removed 128096 8928459)\n")


def test_a_refinement_that_spins_is_a_failed_fill_and_the_next_pass_uses_hxt(staged, tmp_path, monkeypatch):
    """LAD-fea-90: the watchdog stopped gmsh's 3D refinement as spinning (g_c0103, 3.8 hours): the part's,
    not ours - the pass is a fill that failed, so the ladder goes on (LAD-fea-2: HXT) and delivers."""
    import asyncio
    from types import SimpleNamespace

    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.engines import runtime

    ws = _ws(staged, tmp_path / "spin")
    seen: list[object] = []

    def _run(w, timeout):
        plan = json.loads((Path(w) / "fea_plan.json").read_text())
        seen.append((plan.get("mesh") or {}).get("algorithm3d"))
        if len(seen) == 1:
            return {"rc": -1, "timed_out": True, "stalled": True, "spinning": True, "log_tail": _SPUN_3D,
                    "watchdog": {"stopped": "spinning",
                                 "refine": {"iterations": 9267500, "kept": 210945, "rejected": 8928459}}}
        return {"rc": A.mesh(w)}

    class _Run:
        async def fence(self, _why):
            return None

        def note_authoring(self):
            pass

        def note_native_run(self, produced_usable_mesh):
            pass

        def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
            return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=_run))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-spin", publish=None, run=_Run()))
    assert ok, marker
    assert seen[:2] == ["delaunay", "hxt"]
    cited = dr.cited_for("t-spin")
    assert any(e["rule"] == "LAD-fea-90" and e["decided"] == "spinning" for e in cited)
    assert any(e["rule"] == "LAD-fea-2" for e in cited)
    # never told as a guard trip (ours): no stall or ceiling verdict
    assert not any(e.get("stage") == "fea_time" and e["decided"] in ("stalled", "ceiling") for e in cited)


def test_a_repeated_triangle_that_is_a_flap_is_dropped_whole():
    """MESH-5: the nose cone's pair was a fold out and back over nothing (two of its edges bound no other
    triangle): keeping one copy left a triangle no element has as a face, and the solver files stopped. A
    flap goes whole; the surface closes without it."""
    try:
        surfaces = _box_with_a_repeat(False)
        s = surfaces[2]
        _t, _tags, nodes = gmsh.model.mesh.getElements(2, s)
        a, b = (int(n) for n in nodes[0][:2])                  # an edge of the face
        xa = gmsh.model.mesh.getNode(a)[0]
        tip = int(gmsh.model.mesh.getMaxNodeTag()) + 1
        gmsh.model.mesh.addNodes(2, s, [tip], [xa[0] + 0.01, xa[1] + 0.01, xa[2] + 0.01])
        nxt = int(gmsh.model.mesh.getMaxElementTag()) + 1
        gmsh.model.mesh.addElementsByType(s, 2, [nxt, nxt + 1], [a, b, tip, b, a, tip])
        assert A.drop_repeated_triangles(gmsh, surfaces) == {s: 2}
        gmsh.model.mesh.generate(3)
        assert sum(len(t) for t in gmsh.model.mesh.getElements(3)[1]) > 0
    finally:
        if gmsh.isInitialized():
            gmsh.finalize()


def test_a_sliver_the_record_reads_as_zero_is_traced_like_an_inverted_one():
    """fc_glass_frames' pane: flat tets of SICN 1.5e-5, none inverted - the record (four decimals) reads 0.0
    and the ladder calls the pass invalid, so they are traced too (else LAD-fea-6 had no surface to change
    in any of four passes); elements set aside at a cusp stay out of it."""
    nodes = {1: (2, 5), 2: (2, 5), 3: (2, 6), 4: (2, 6), 5: (2, 7), 6: (2, 7), 7: (2, 8), 8: (2, 8)}
    g = _stub({10: [1, 2, 3, 4], 11: [5, 6, 7, 8]}, nodes)
    assert A.bad_surfaces(g, [10, 11], [1.5e-5, 1.5e-5]) == []                       # nothing at or under 0
    assert A.bad_surfaces(g, [10, 11], [1.5e-5, 1.5e-5], floor=A.DEGENERATE_SICN) == [6, 8]
    assert A.bad_surfaces(g, [10, 11], [1.5e-5, 1.5e-5], floor=A.DEGENERATE_SICN, skip={11}) == [6]
    assert round(A.DEGENERATE_SICN - 1e-9, 4) == 0.0 < round(A.DEGENERATE_SICN + 1e-9, 4)


def test_every_pair_alike_a_locked_pair_is_named_with_it():
    """LAD-fea-6: fc_glass_frames' twelve alike panes locked slivers one after another as sizes moved (6 panes,
    then 3, then 1): the second face of every pair alike the locked one is named at once."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("panes")
        gmsh.model.occ.addBox(0, 0, 0, 100, 50, 1)
        gmsh.model.occ.addBox(0, 80, 0, 100, 50, 1)           # an alike pane, 30 apart
        gmsh.model.occ.addBox(0, 160, 0, 100, 40, 1)          # a pane of another size
        gmsh.model.occ.synchronize()
        faces = {t: gmsh.model.occ.getCenterOfMass(2, t) for _d, t in gmsh.model.getEntities(2)}
        big = sorted(t for t in faces if abs(gmsh.model.occ.getMass(2, t) - 5000.0) < 1e-6)
        assert len(big) == 4                                   # the two alike panes' front and back
        first = (big[0], big[1])
        named = A.alike_seconds(gmsh, {first})
        assert named == {big[1], big[3]}
        assert A.alike_seconds(gmsh, set()) == set()
    finally:
        gmsh.finalize()
