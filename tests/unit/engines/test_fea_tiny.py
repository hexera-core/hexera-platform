# Responsibility: Verify MESH-9: a CAD face far narrower (or an edge far shorter) than the planned element size there is meshed as part of its smooth neighbour - the mesh is better for it, the neighbour's named face covers both areas, the record says which face went into which - and never across a crease, a joint, a part, a hot spot or a CFD group; a part with nothing tiny meshes to the same bytes; a build the merges break is built again as drawn; the CFD gmsh path does the same.
# Boundaries: small parts built in the test (tests/fea_fixtures.py), planned and meshed in-process; the corpus counts are the lab's (~/tinyfaces_lab).
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter as A  # noqa: E402
from meshpipeline.engines.gmsh import fea_tiny as T  # noqa: E402


def _stage(shape, ws: Path, *, plan: bool = True) -> Path:
    """The upload read in mm, written in metres (STEP and gmsh's B-rep), as the builder stages it."""
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
    if plan:
        record, surface, diag = FD.measure_staged(ws)
        FD.write_plan(ws, FD.make_plan(record, surface, diag))
    return ws


def _copy(src: Path, dst: Path) -> Path:
    import shutil

    shutil.copytree(src, dst)
    return dst


def _q(ws: Path) -> dict:
    return json.loads((ws / "quality.json").read_text())


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _areas(ws: Path) -> tuple[dict[int, float], dict[int, float], dict[int, int]]:
    """(each CAD face's area, the area of the triangles its surface carries in mesh.msh, how many times
    each boundary edge is used) - in mm^2."""
    import numpy as np

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("cad")
        gmsh.model.occ.importShapes(str(ws / "geometry.brep"))
        gmsh.model.occ.synchronize()
        cad = {int(t): gmsh.model.occ.getMass(2, t) * 1e6 for _d, t in gmsh.model.getEntities(2)}
        gmsh.model.add("mesh")
        gmsh.merge(str(ws / "mesh.msh"))
        nt, xyz, _ = gmsh.model.mesh.getNodes()
        pts = np.zeros((int(nt.max()) + 1, 3))
        pts[nt] = np.asarray(xyz).reshape(-1, 3)
        mesh: dict[int, float] = {}
        use: dict[tuple[int, int], int] = {}
        for _d, s in gmsh.model.getEntities(2):
            types, _tags, nodes = gmsh.model.mesh.getElements(2, s)
            a = 0.0
            for et, nd in zip(types, nodes, strict=True):
                tri = np.asarray(nd, dtype=np.int64).reshape(-1, 3 if int(et) == 2 else 6)[:, :3]
                a += 0.5 * float(np.linalg.norm(np.cross(pts[tri[:, 1]] - pts[tri[:, 0]],
                                                         pts[tri[:, 2]] - pts[tri[:, 0]]), axis=1).sum())
                for t in np.sort(tri, axis=1).tolist():
                    for e in ((t[0], t[1]), (t[1], t[2]), (t[0], t[2])):
                        use[e] = use.get(e, 0) + 1
            mesh[int(s)] = a * 1e6
        counts: dict[int, int] = {}
        for n in use.values():
            counts[n] = counts.get(n, 0) + 1
        return cad, mesh, counts
    finally:
        gmsh.finalize()


@pytest.fixture(scope="module")
def strip(tmp_path_factory) -> Path:
    return _stage(fx.strip_block(), tmp_path_factory.mktemp("tiny_strip"))


@pytest.fixture(scope="module")
def strip_meshed(strip, tmp_path_factory) -> tuple[Path, Path]:
    """The strip block meshed with MESH-9 and, for comparison, with every face as drawn."""
    on = _copy(strip, tmp_path_factory.mktemp("tiny_on") / "ws")
    assert A.mesh(on) == 0
    off = _copy(strip, tmp_path_factory.mktemp("tiny_off") / "ws")
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        T.TINY_SHARE = keep
    return on, off


def test_each_strip_is_meshed_as_part_of_the_face_beside_it_and_said(strip_meshed):
    on, off = strip_meshed
    tiny = _q(on)["fea_tiny"]
    assert tiny["rule"] == "MESH-9" and tiny["share"] == T.TINY_SHARE == 0.005
    assert len(tiny["merged"]) == 4                  # the strip on the top, the bottom and both ends
    for m in tiny["merged"]:
        assert m["width_mm"] == pytest.approx(0.005, rel=1e-2) and m["share"] < T.TINY_SHARE
        assert len(m["face"]) == len(m["into"]) == 1 and m["face"] != m["into"]
    assert tiny["short_edges"] and all(e["length_mm"] == pytest.approx(0.005, rel=1e-2) for e in tiny["short_edges"])
    assert "fea_tiny" not in _q(off)


def test_the_mesh_is_better_for_it(strip_meshed):
    on, off = strip_meshed
    q_on, q_off = _q(on), _q(off)
    # as drawn, the 0.005 mm strips pin slivers that are set aside (PLAN-fea-12); merged, there are none
    assert q_off["fea_set_aside"]["micro_elements"] > 0 and q_off["min_sicn"] < 0.25
    assert q_on["fea_set_aside"]["elements"] == 0 and q_on["min_sicn"] > 0.4
    assert q_on["fea_min_scaled_jacobian"] > 2 * q_off["fea_min_scaled_jacobian"]
    assert q_on["cells"] <= q_off["cells"]
    assert not q_on["fatal"] and q_on["fea"]["rules"]["short"] == []


def test_the_neighbours_named_face_covers_both_areas_and_the_boundary_closes(strip_meshed):
    on, _off = strip_meshed
    cad, mesh, use = _areas(on)
    assert set(use) == {2}                            # every boundary edge bounds two triangles
    for m in _q(on)["fea_tiny"]["merged"]:
        f, n = (int(m[k][0].rsplit("_", 1)[-1]) for k in ("face", "into"))
        assert mesh[f] == pytest.approx(0.0, abs=1e-9)
        assert mesh[n] == pytest.approx(cad[n] + cad[f], rel=1e-6)
    assert sum(mesh.values()) == pytest.approx(sum(cad.values()), rel=1e-6)
    deck = (on / "mesh.inp").read_text()
    for m in _q(on)["fea_tiny"]["merged"]:
        assert f"*SURFACE, NAME={m['into'][0]}," in deck and f"*SURFACE, NAME={m['face'][0]}," not in deck


def test_a_part_with_nothing_tiny_meshes_to_the_same_bytes(tmp_path):
    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    off = _copy(ws, tmp_path / "off")
    assert A.mesh(ws) == 0
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        T.TINY_SHARE = keep
    assert "fea_tiny" not in _q(ws)
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        assert _sha(ws / name) == _sha(off / name)


def test_a_sliver_between_two_creases_collapses_onto_its_edge_as_ansys_defeatures(tmp_path):
    """A 0.003 mm chamfer meets both faces beside it at 45 degrees: no smooth neighbour, so - as Ansys's mesh
    defeaturing does - it collapses onto its edge with one of them. Its elements are no longer set aside,
    its area is in that neighbour's named face, and the record says it collapsed, and across what crease."""
    ws = _stage(fx.micro_chamfer(), tmp_path / "chamfer")
    off = _copy(ws, tmp_path / "off")
    assert A.mesh(ws) == 0
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        T.TINY_SHARE = keep
    q, q_off = _q(ws), _q(off)
    merged = q["fea_tiny"]["merged"]
    assert len(merged) == 1 and merged[0]["collapsed"] and merged[0]["crease_deg"] == pytest.approx(45.0, abs=1.0)
    assert q_off["fea_set_aside"]["micro_elements"] > 0 and q["fea_set_aside"]["elements"] == 0
    assert q["min_sicn"] > q_off["min_sicn"] and not q["fatal"]
    cad, mesh, use = _areas(ws)
    f, n = (int(merged[0][k][0].rsplit("_", 1)[-1]) for k in ("face", "into"))
    assert set(use) == {2} and mesh[f] == pytest.approx(0.0, abs=1e-9)
    # folded onto the edge: the neighbour covers both areas, to within the sliver's own (the corner is cut)
    assert abs(mesh[n] - (cad[n] + cad[f])) <= cad[f]


def test_a_sliver_at_a_capped_corner_collapses_too(tmp_path):
    """micro_step's 0.003 mm ledge makes a sharp INSIDE corner with the wall above it, a corner the plan caps
    (PLAN-fea-5) - not a stress hot spot (its stress never settles): it collapses like any sliver."""
    ws = _stage(fx.micro_step(), tmp_path / "step")
    off = _copy(ws, tmp_path / "off")
    assert A.mesh(ws) == 0
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        T.TINY_SHARE = keep
    q, q_off = _q(ws), _q(off)
    assert [m["collapsed"] for m in q["fea_tiny"]["merged"]] == [True]
    assert q["fea_set_aside"]["elements"] == 0 < q_off["fea_set_aside"]["elements"] and q["min_sicn"] > q_off["min_sicn"]


def test_a_curved_strip_is_merged_and_the_quadratic_mesh_still_lies_on_the_cylinder(tmp_path):
    import numpy as np

    ws = _stage(fx.split_cylinder(), tmp_path / "cyl")
    off = _copy(ws, tmp_path / "off")
    assert A.mesh(ws) == 0
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert A.mesh(off) == 0
    finally:
        T.TINY_SHARE = keep
    q, q_off = _q(ws), _q(off)
    assert len(q["fea_tiny"]["merged"]) == 3          # the side strip and the two end wedges
    assert q["min_sicn"] > q_off["min_sicn"] and q["fea_set_aside"]["elements"] == 0
    # every node of the side (corner and midside) on the 5 mm radius: settled back onto the CAD, and
    # curved on the right surface
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.merge(str(ws / "mesh.msh"))
        side = [int(m["into"][0].rsplit("_", 1)[-1]) for m in q["fea_tiny"]["merged"] if m["width_mm"] > 0.0015]
        assert side
        _types, _tags, nodes = gmsh.model.mesh.getElements(2, side[0])
        ids = np.unique(np.concatenate([np.asarray(n) for n in nodes]))
        xyz = np.asarray([gmsh.model.mesh.getNode(int(i))[0] for i in ids])
        assert np.abs(np.hypot(xyz[:, 0], xyz[:, 1]) - 5e-3).max() < 1e-8
    finally:
        gmsh.finalize()


def _model(ws: Path):
    """A staged shape read into the current gmsh model (in metres)."""
    gmsh.model.add("t")
    gmsh.model.occ.importShapes(str(ws / "geometry.brep"))
    gmsh.model.occ.synchronize()


def _strip_faces() -> list[int]:
    return [int(t) for _d, t in gmsh.model.getEntities(2) if gmsh.model.occ.getMass(2, t) < 2e-7]


def _plan(base_m: float = 3e-3, zones=None) -> dict:
    return {"base_size_m": base_m, "zones": list(zones or []), "growth": 1.3}


def test_never_into_a_joint_a_hot_spot_or_another_cfd_group(tmp_path):
    ws = _stage(fx.strip_block(), tmp_path / "s", plan=False)
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        _model(ws)
        strips = _strip_faces()
        free = T.plan_merges(gmsh, _plan())
        assert sorted(free["faces"]) == sorted(strips)
        s, n = strips[0], free["faces"][strips[0]]
        # a joint face (PLAN-fea-17's bonded or contact faces) is never merged into, nor merged away
        others = [x for c in T._curves(gmsh, s) for x in T._faces_of(gmsh, c) if x != s]
        assert s not in T.plan_merges(gmsh, _plan(), protected={s})["faces"]
        assert s not in T.plan_merges(gmsh, _plan(), protected=set(others))["faces"]
        # a face a stress hot spot names stays as drawn (a hole here)
        hot = _plan(zones=[{"id": "H1", "kind": "hole", "faces": [f"Face_{s}"], "size_m": 3e-3, "reach_m": 0.0}])
        assert s not in T.plan_merges(gmsh, hot)["faces"]
        # the CFD path's groups: never into a face of another group (an inlet into the wall)
        groups = {int(t): "wall" for _d, t in gmsh.model.getEntities(2)}
        groups[s] = "inlet"
        assert s not in T.plan_merges(gmsh, _plan(), groups=groups)["faces"]
        # ...but a face whose group has others may fold into the wall beside it (its area joins the wall)
        far = next(int(t) for _d, t in gmsh.model.getEntities(2)
                   if int(t) != s and int(t) not in T._faces_near(gmsh, s))
        groups[far] = "inlet"
        assert s in T.plan_merges(gmsh, _plan(), groups=groups)["faces"]
        assert n in free["faces"].values()
    finally:
        gmsh.finalize()


def test_never_into_another_part(tmp_path):
    """Two blocks side by side (a bonded pair), a strip on the second's top beside the first: it may only
    go into a face of its own block, never into the block beside it."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("two")
        a = gmsh.model.occ.addBox(0, 0, 0, 0.01, 0.01, 0.01)
        b = gmsh.model.occ.addBox(0.01, 0, 0, 0.01, 0.01, 0.01)
        r = gmsh.model.occ.addRectangle(0.01, 0, 0.01, 5e-6, 0.01)
        gmsh.model.occ.fragment([(3, a), (3, b)], [(2, r)])
        gmsh.model.occ.synchronize()
        strips = [int(t) for _d, t in gmsh.model.getEntities(2) if gmsh.model.occ.getMass(2, t) < 2e-7]
        assert len(strips) == 1
        s = strips[0]
        vols = T._volumes_of(gmsh, s)
        m = T.plan_merges(gmsh, _plan())
        assert T._volumes_of(gmsh, m["faces"][s]) == vols
        # with its own block's top a joint face, it may only fold onto another face of its own block
        other = T.plan_merges(gmsh, _plan(), protected={m["faces"][s]})["faces"]
        assert s not in other or T._volumes_of(gmsh, other[s]) == vols
    finally:
        gmsh.finalize()


def test_the_share_is_of_the_local_size_never_a_fixed_length(tmp_path):
    """The same strip is tiny under a coarse plan and not under a plan whose zone on the faces beside it
    asks for elements fine enough to follow it."""
    ws = _stage(fx.strip_block(), tmp_path / "s", plan=False)
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        _model(ws)
        assert T.plan_merges(gmsh, _plan(3e-3))["faces"]
        fine = [{"id": "W1", "kind": "wall", "faces": [f"Face_{int(t)}" for _d, t in gmsh.model.getEntities(2)],
                 "size_m": 5e-6 / T.TINY_SHARE * 0.5, "reach_m": 0.0}]
        assert not T.plan_merges(gmsh, _plan(3e-3, fine))["faces"]
    finally:
        gmsh.finalize()


def test_a_build_the_merges_break_is_built_again_as_drawn(strip, strip_meshed, tmp_path, monkeypatch, capsys):
    _on, off = strip_meshed
    """A surface stage that fails on a face a merge meshes: the same pass builds again with every face as
    drawn - the mesh the part had before MESH-9, to the byte."""
    ws = _copy(strip, tmp_path / "ws")
    real_surface, real_apply = A.surface_mesh, T.apply
    merged: list[dict] = []

    def apply(gmsh_, merges):
        if T.active(merges):
            merged.append(merges)
        return real_apply(gmsh_, merges)

    def surface(gmsh_, surfaces, remesh):
        if merged and len(merged) == 1 and not getattr(surface, "failed", False):
            surface.failed = True  # type: ignore[attr-defined]
            raise RuntimeError(f"Impossible to mesh surface {next(iter(merged[0]['faces'].values()))}")
        return real_surface(gmsh_, surfaces, remesh)

    monkeypatch.setattr(T, "apply", apply)
    monkeypatch.setattr(A, "surface_mesh", surface)
    assert A.mesh(ws) == 0
    assert len(merged) == 1 and "built again with every face as drawn" in capsys.readouterr().err
    assert _sha(ws / "mesh.inp") == _sha(off / "mesh.inp")
    assert "fea_tiny" not in _q(ws)


def _cfd_ws(staged: Path, ws: Path) -> Path:
    _copy(staged, ws)
    (ws / "fea_plan.json").unlink()
    (ws / "gmsh_spec.json").write_text(json.dumps({"size": {"mode": "factor", "value": 0.08},
                                                   "dimensionality": "3D", "element_order": 2}))
    return ws


def test_the_cfd_gmsh_path_merges_the_same_way(strip, tmp_path):
    from meshpipeline.engines.gmsh import driver as D

    ws = _cfd_ws(strip, tmp_path / "cfd")
    off = _cfd_ws(strip, tmp_path / "cfd_off")
    assert D.main(str(ws)) == 0
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert D.main(str(off)) == 0
    finally:
        T.TINY_SHARE = keep
    q, q_off = _q(ws), _q(off)
    assert len(q["tiny_faces"]["merged"]) == 4 and "tiny_faces" not in q_off
    assert q["min_sicn"] > q_off["min_sicn"] and not q["fatal"]
    assert q["groups"] == q_off["groups"]
    assert sum(q["patch_areas_m2"].values()) == pytest.approx(sum(q_off["patch_areas_m2"].values()), rel=1e-6)


def test_the_result_screen_says_which_face_went_into_which(strip_meshed):
    from meshpipeline.engines.fea_results import results

    on, off = strip_meshed
    r, r_off = results(on), results(off)
    assert len(r["merged_faces"]) == 4 and 'merged_faces' not in r_off
    assert any(w.startswith('Tiny faces: 4 CAD face(s)') for w in r['plan_words'])
    gone = {m['face'][0] for m in r["merged_faces"]}
    listed = {f['name'] for f in r['faces']}
    assert not gone & listed and listed | gone == {f['name'] for f in r_off['faces']}


def test_a_cfd_fill_the_merges_break_is_meshed_again_as_drawn_in_a_fresh_session(strip, tmp_path, monkeypatch, capsys):
    """The CFD gmsh path: a fill that fails with the merges set is meshed again with every face as drawn, in
    a fresh gmsh session (a failed fill leaves gmsh's state behind it) - the mesh the solid has without MESH-9."""
    from meshpipeline.engines.gmsh import driver as D

    off = _cfd_ws(strip, tmp_path / "off")
    keep = T.TINY_SHARE
    T.TINY_SHARE = 0.0
    try:
        assert D.main(str(off)) == 0
    finally:
        T.TINY_SHARE = keep
    ws = _cfd_ws(strip, tmp_path / "ws")
    real = gmsh.model.mesh.generate
    calls: list[int] = []

    def generate(dim=3):
        calls.append(dim)
        if dim == 3 and calls.count(3) == 1:
            raise Exception("PLC Error:  Two facets intersect at point")
        return real(dim)

    monkeypatch.setattr(gmsh.model.mesh, "generate", staticmethod(generate))
    assert D.main(str(ws)) == 0
    assert "meshing it again with every face as drawn" in capsys.readouterr().err
    q, q_off = _q(ws), _q(off)
    assert "tiny_faces" not in q and q["cells"] == q_off["cells"] and q["min_sicn"] == q_off["min_sicn"]


def test_a_merged_mesh_that_is_not_valid_is_meshed_again_as_drawn(strip, strip_meshed, tmp_path, monkeypatch, capsys):
    """A mesh the merges left with an inverted element is meshed again in the same pass with every face as
    drawn: the mesh the part has without MESH-9, to the byte."""
    _on, off = strip_meshed
    ws = _copy(strip, tmp_path / "ws")
    real = A._mesh
    seen: list[bool] = []

    def once(w):
        seen.append(A._AS_DRAWN[0])
        rc = real(w)
        if len(seen) == 1:
            q = _q(Path(w))
            q["fatal"] = ["1 inverted element(s) (Jacobian not above zero)"]
            (Path(w) / "quality.json").write_text(json.dumps(q))
            return 4
        return rc

    monkeypatch.setattr(A, "_mesh", once)
    assert A.mesh(ws) == 0
    assert seen == [False, True] and "meshed again with every face as drawn" in capsys.readouterr().err
    assert "fea_tiny" not in _q(ws) and _sha(ws / "mesh.inp") == _sha(off / "mesh.inp")
    assert A._AS_DRAWN[0] is False
