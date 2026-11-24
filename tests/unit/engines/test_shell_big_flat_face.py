"""SHMESH-21g: a big flat face left free is meshed by the quad-aligned algorithm (gmsh's Blossom recombination grows
far faster than the elements on algorithm 6's triangles of one big face); under the gate every face meshes as before."""

import time

import pytest

from meshpipeline.engines.gmsh import shell_adapter as sa


class _Model:
    def __init__(self, kinds: dict[int, str]) -> None:
        self.kinds = kinds

    def getType(self, dim: int, tag: int) -> str:  # noqa: N802 (gmsh's own name)
        return self.kinds[tag]


class _Gmsh:
    def __init__(self, kinds: dict[int, str]) -> None:
        self.model = _Model(kinds)


def test_the_gate_takes_only_big_flat_free_faces():
    gate = sa.BIG_FLAT_FACE
    # face k planned = area / size^2 (mm); size 2 mm
    facts = [
        {"area": 4.0 * gate},
        {"area": 4.0 * (gate + 1)},
        {"area": 4.0 * 10 * gate},
        {"area": 4.0 * 10 * gate},
        {"area": 4.0 * 10 * gate},
    ]
    rows = [{"size_mm": 2.0}, {"size_mm": 2.0}, {"size_mm": 2.0}, {"size_mm": 2.0}, {"size_mm": 0.0}]
    tag_of = {11: 0, 12: 1, 13: 2, 14: 3, 15: 4}
    g = _Gmsh({11: "Plane", 12: "Plane", 13: "Cylinder", 14: "Plane", 15: "Plane"})
    assert sa.big_flat_faces(g, tag_of, facts, rows, 1.0) == {12: 1, 14: 3}
    assert sa.big_flat_faces(g, tag_of, facts, rows, 1.0, skip={14}) == {12: 1}  # a grid or mapped face: untouched
    # millimetres per unit counts: the same faces drawn in metres
    facts_m = [{"area": f["area"] * 1e-6} for f in facts]
    assert sa.big_flat_faces(g, tag_of, facts_m, rows, 1000.0) == {12: 1, 14: 3}


def test_the_gate_is_cheap_on_many_faces():
    # the gate itself must never be a cost: 50,000 faces in well under a second
    n = 50_000
    facts = [{"area": 1.0e6 if i % 2 else 10.0} for i in range(n)]
    rows = [{"size_mm": 1.0}] * n
    tag_of = {i + 1: i for i in range(n)}
    g = _Gmsh(dict.fromkeys(tag_of, "Plane"))
    t0 = time.perf_counter()
    got = sa.big_flat_faces(g, tag_of, facts, rows, 1.0)
    assert len(got) == n // 2
    assert time.perf_counter() - t0 < 5.0


def _plate_mesh(tmp_path, monkeypatch, gate: int):
    from OCP.BRepTools import BRepTools
    from tests import shell_fixtures as sf

    from meshpipeline.cad import midsurface as ms

    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 300.0)
    monkeypatch.setattr(sa, "BIG_FLAT_FACE", gate)
    rec, surface = ms.extract(sf.plate_with_hole(), mm_per_unit=1.0)
    brep = tmp_path / f"plate_{gate}.brep"
    BRepTools.Write_s(surface, str(brep))
    return sa.mesh_shell(brep, rec)


def _figures(mesh, applied) -> tuple:
    q = applied["quality"]
    return (
        len(mesh.quads),
        len(mesh.trias),
        bool(q.get("valid")),
        q.get("tria_share"),
        round(float(mesh.xyz.sum()), 6),
    )


def test_a_small_flat_face_meshes_exactly_as_before(tmp_path, monkeypatch):
    # under the gate (the default) the plate meshes exactly as with the gate off: same elements, same nodes
    mesh, applied = _plate_mesh(tmp_path, monkeypatch, sa.BIG_FLAT_FACE)
    assert not any(r.get("big_flat") for r in applied["faces"])
    off, off_applied = _plate_mesh(tmp_path, monkeypatch, 0)
    assert _figures(mesh, applied) == _figures(off, off_applied)


def test_a_big_flat_face_meshes_quad_aligned_and_valid(tmp_path, monkeypatch):
    # the same plate with the gate taken under its face: meshed by algorithm 8, valid, quads only (or nearly)
    mesh, applied = _plate_mesh(tmp_path, monkeypatch, 50)
    assert [r.get("big_flat") for r in applied["faces"] if r.get("big_flat")] == [sa.BIG_FLAT_ALGORITHM]
    assert applied["quality"]["valid"]
    assert applied["quality"]["tria_share"] < 0.05


def test_blossom_on_a_big_flat_face_stays_near_its_elements():
    # gmsh alone on a 1200 x 800 face with 24 holes, ~31,000 quads, as SHMESH-21g meshes it: on a test VM ~15 s
    # (algorithm 6's triangles took 600 s for 69,000 quads); the bound is generous for a busy laptop
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Mesh.Algorithm", sa.BIG_FLAT_ALGORITHM)
        gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.RecombinationAlgorithm", sa.RECOMBINATION)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.MeshSizeMax", 5.5)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 2.75)
        gmsh.option.setNumber("Mesh.Smoothing", 5)
        gmsh.model.add("big_flat")
        r = gmsh.model.occ.addRectangle(0, 0, 0, 1200, 800)
        holes = [
            (2, gmsh.model.occ.addDisk(150 + 180 * i, 130 + 180 * j, 0, 20, 20)) for i in range(6) for j in range(4)
        ]
        gmsh.model.occ.cut([(2, r)], holes)
        gmsh.model.occ.synchronize()
        t0 = time.perf_counter()
        gmsh.model.mesh.generate(2)
        took = time.perf_counter() - t0
        types, tags, _ = gmsh.model.mesh.getElements(2)
        counts = {int(t): len(x) for t, x in zip(types, tags)}
    finally:
        gmsh.finalize()
    assert counts.get(3, 0) > 25_000 and counts.get(2, 0) == 0  # quads only
    assert took < 180.0
