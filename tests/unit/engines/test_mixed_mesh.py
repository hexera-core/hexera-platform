# Responsibility: Hold the mixed shell + solid pieces to their rules without a solver: the SHLINK-4 ties reproduce
# every linear field (so a moment and an in-plane bend cross whole), the hat shares add up, link rows chain in
# order, the split of a plate with a thick boss keeps the boss solid and cuts it two walls into the plate with the
# metal accounted for, the join ties every link node, the decks carry the solid and the ties, and Auto's words.
# Boundaries: OpenCASCADE and gmsh in process; no CalculiX (tests/unit/engines/test_mixed_textbook.py solves).
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines import mixed_links as ml
from meshpipeline.engines import mixed_mesh as mm
from meshpipeline.engines import mixed_run as mr


def test_the_tie_weights_read_any_linear_field_exactly_even_at_a_row_end():
    rng = np.random.default_rng(1)
    pts = np.column_stack([rng.uniform(0, 5, 40), np.zeros(40), rng.uniform(-1, 1, 40)])
    w = rng.uniform(0.1, 1.0, 40) * (1 - pts[:, 0] / 5)          # a one-sided hat, as at a row's end
    c = ml.linear_weights(np.zeros(3), pts, w)
    for f in (lambda p: 3.0 + 0 * p[:, 0], lambda p: 2 * p[:, 0] - p[:, 2], lambda p: p[:, 2]):
        assert float(c @ f(pts)) == pytest.approx(float(f(np.zeros((1, 3)))[0]), abs=1e-9)
    # the plain weighted mean does not: its centre sits inward
    assert abs(float((w / w.sum()) @ pts[:, 0])) > 1.0


def test_the_hat_shares_of_a_row_add_up_to_one_everywhere():
    row = ml.Row([1, 2, 3], np.array([[0, 0, 0], [5, 0, 0], [10, 0, 0.0]]), False)
    x = np.array([[-1, 0, 0], [2.5, 0, 0], [7, 0, 1], [12, 0, 0.0]])
    h = ml.hat_weights(row, x)
    assert np.allclose(h.sum(axis=1), 1.0)
    assert h[0].tolist() == [1.0, 0.0, 0.0] and h[3].tolist() == [0.0, 0.0, 1.0]
    assert h[1].tolist() == pytest.approx([0.5, 0.5, 0.0])


def test_link_edges_chain_in_order_open_and_closed():
    open_, closed = mm._chains([(1, 2), (2, 3), (3, 4)]), mm._chains([(1, 2), (2, 3), (3, 1)])
    assert open_ == [([1, 2, 3, 4], False)]
    assert closed == [([1, 2, 3], True)]


def test_a_quadratic_triangle_gives_its_nodes_its_whole_area():
    xyz = {1: np.array([0, 0, 0.0]), 2: np.array([2, 0, 0.0]), 3: np.array([0, 2, 0.0]),
           4: np.array([1, 0, 0.0]), 5: np.array([1, 1, 0.0]), 6: np.array([0, 1, 0.0])}
    a = ml.face_node_areas(xyz, np.array([[1, 2, 3, 4, 5, 6]]))
    assert sum(a.values()) == pytest.approx(2.0)
    assert a[1] == pytest.approx(2.0 / 12) and a[4] == pytest.approx(2.0 / 4)


def _plate_boss():
    from tests.fea_fixtures import _box, _cyl, _fuse

    return _fuse(_box(0, 0, 0, 120, 80, 3), _cyl(80, 40, 3, 10, 20))


@pytest.fixture(scope="module")
def boss_split():
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    shape = _plate_boss()
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    mids = {m["id"]: m for m in rec["mids"]}
    kept = [(f, float(mids[o]["thickness_mm"]), 0.0) for f, o in zip(ms._faces(surface), rec["surface_faces"],
                                                                     strict=True)]
    faces = ms._faces(shape)
    thick = [faces[k] for k, r in enumerate(rec["faces"]) if r["role"] in ("thick", "solid")]
    return rec, surface, mx.split_body(mx._solids(shape)[0], kept, thick, scale=1.0)


def test_a_thick_boss_stays_solid_cut_two_walls_into_the_plate(boss_split):
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    rec, _surface, sp = boss_split
    assert rec["bodies"][0]["verdict"] == "mixed"
    assert sp.ok, sp.why
    assert sp.cores == 1 and sp.cut_distance == [pytest.approx(mx.CUT_WALLS * 3.0)]
    # the solid: the boss and a disc of plate of radius 10 + 6 under it
    want = np.pi * 10 ** 2 * 20 + np.pi * 16 ** 2 * 3
    assert sp.solid_volume == pytest.approx(want, rel=1e-4)
    assert (sp.solid_volume + sp.shell_volume) / sp.body_volume == pytest.approx(1.0, abs=1e-3)
    # the plate's mid-face has a hole of radius 16 where the cut is; the cut face is the cylinder there
    assert len(sp.cut_faces) == 1
    assert ms._area(sp.cut_faces[0]) == pytest.approx(2 * np.pi * 16 * 3, rel=1e-4)
    assert sum(ms._area(f) for f in sp.shell_faces) == pytest.approx(120 * 80 - np.pi * 16 ** 2, rel=1e-4)


def test_auto_recommends_mixed_with_plain_words_and_not_for_a_lump():
    rec = {"bodies": [{"body": "Part_1", "verdict": "mixed", "volume_mm3": 100.0, "mid_volume_ratio": 0.78,
                       "solid_regions": [{"faces": ["Face_7"]}, {"faces": ["Face_9"]}, {"faces": ["Face_11"]}]}],
           "sewn": [{"open_edges": 0}], "mids": [{"thickness_mm": 2.0, "shell": True}]}
    ok, why = mr.recommend(rec)
    assert ok
    assert why == "Thin walls meshed as shell (78% of the part, 2 mm thick); 3 thick regions kept solid and joined to them."
    rec["bodies"][0]["mid_volume_ratio"] = 0.3
    assert mr.recommend(rec) == (False, mr.WHY_LOW.format(share=30))
    rec["bodies"][0]["mid_volume_ratio"] = 0.8
    rec["sewn"][0]["open_edges"] = 2
    assert not mr.recommend(rec)[0]
    assert mr.recommend({"bodies": [{"verdict": "shell"}]}) == (False, "")


def test_the_join_ties_every_link_node_and_the_decks_carry_it(boss_split, tmp_path):
    import gmsh
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines.gmsh import shell_adapter as sa
    from meshpipeline.engines.gmsh.abaqus_deck import from_gmsh

    rec, _surface, sp = boss_split
    rec2 = dict(rec, surface_faces=[rec["surface_faces"][k] for k in sp.shell_from])
    BRepTools.Write_s(ms._compound(sp.shell_faces), str(tmp_path / "s.brep"))
    shell, _applied = sa.mesh_shell(tmp_path / "s.brep", rec2)
    BRepTools.Write_s(sp.solid, str(tmp_path / "solid.brep"))
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(tmp_path / "solid.brep"))
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 1.5)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)
        gmsh.model.mesh.generate(3)
        sm = from_gmsh(gmsh, [t for _d, t in gmsh.model.getEntities(3)], by_tag=True)
    finally:
        gmsh.finalize()
    new = {int(t): k + 1 for k, t in enumerate(sm.node_ids)}
    solid = mm.SolidPart(xyz=np.asarray(sm.xyz, float), tets=np.vectorize(new.get)(sm.elements))
    n_shell = shell.n_elements
    joined = mm.join(shell, solid, sp.cut_faces, part="Part_1")
    assert not joined.notes
    assert len(joined.links) == 1 and joined.links[0].closed
    tied = {t.node for t in joined.ties}
    lk = joined.links[0]
    assert tied == {n + 1 for n in lk.on_face} | {n + 1 for n in lk.inner}
    assert joined.n_overlap == len(lk.on_face) and joined.shell.n_elements == n_shell + joined.n_overlap
    # each tie reads its own place back (a linear field: the solid's own coordinates)
    base = len(joined.shell.xyz)
    for t in joined.ties[:60]:
        got = sum(c * solid.xyz[n - base - 1][d - 1] for n, d, c in t.terms)
        assert got == pytest.approx(joined.shell.xyz[t.node - 1][t.dof - 1], abs=1e-6)
    out = mm.write_all(tmp_path, joined, units="mm")
    assert out["calculix"]["ties"] == len(joined.ties)
    inp = (tmp_path / "mixed.inp").read_text()
    assert "*ELEMENT, TYPE=C3D10, ELSET=SOLID_Part_1" in inp and inp.count("*EQUATION") == len(joined.ties)
    back = sw.read_bdf(tmp_path / "mixed.bdf")
    assert len(back.elements) == joined.shell.n_elements         # the shell part reads back as a shell deck
    bdf = (tmp_path / "mixed.bdf").read_text()
    assert bdf.count("\nMPC*") == len(joined.ties) and bdf.count("\nCTETRA") == len(solid.tets)
    cdb = (tmp_path / "mixed.cdb").read_text()
    assert cdb.count("\nCE,NEXT") == len(joined.ties) and cdb.rstrip().endswith("FINISH") and "ET,2,187" in cdb


def test_trimmed_mid_faces_keep_the_edges_they_share():
    """An L bracket with a thick pad: its two legs, trimmed at the pad, still meet on ONE edge (cut face by face, each
    came back with its own edge there, and the shell hinged)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from tests.fea_fixtures import _box, _fuse

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    shape = _fuse(_fuse(_box(0, 0, 0, 100, 60, 3), _box(0, 0, 0, 3, 60, 80)), _box(70, 0, 3, 30, 60, 12))
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    mids = {m["id"]: m for m in rec["mids"]}
    kept = [(f, float(mids[o]["thickness_mm"]), 0.0) for f, o in zip(ms._faces(surface), rec["surface_faces"],
                                                                     strict=True)]
    faces = ms._faces(shape)
    thick = [faces[k] for k, r in enumerate(rec["faces"]) if r["role"] in ("thick", "solid")]
    sp = mx.split_body(mx._solids(shape)[0], kept, thick, scale=1.0)
    assert sp.ok, sp.why
    shared = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(ms._compound(sp.shell_faces), TopAbs_EDGE, TopAbs_FACE, shared)
    assert any(shared.FindFromIndex(i).Size() == 2 for i in range(1, shared.Extent() + 1))
