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


def _brep_text(shape) -> str:
    import os
    import tempfile

    from OCP.BRepTools import BRepTools

    fd, path = tempfile.mkstemp(suffix=".brep")
    os.close(fd)
    try:
        BRepTools.Clean_s(shape)
        BRepTools.Write_s(shape, path)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        os.unlink(path)


def test_canonical_order_is_read_from_the_geometry_not_from_the_order_it_was_built_in():
    """MIXED-2b: the same solid with its faces (and their wires' edges) stored in another order comes out of
    canonical() byte for byte the same, and is the same solid."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS, TopoDS_Shell, TopoDS_Solid
    from tests.fea_fixtures import _box, _cyl, _fuse

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    shape = mx._solids(_fuse(_box(0, 0, 0, 30, 20, 4), _cyl(15, 10, 4, 5, 8)))[0]

    def reordered(solid):
        b = BRep_Builder()
        out, shell = TopoDS_Solid(), TopoDS_Shell()
        b.MakeSolid(out)
        b.MakeShell(shell)
        old = TopoDS.Shell_s(mx._children(solid)[0])
        for f in reversed(mx._children(old)):
            b.Add(shell, f)
        shell.Closed(True)
        b.Add(out, shell)
        return out

    a, b = mx.canonical(shape), mx.canonical(reordered(shape))
    assert _brep_text(a) == _brep_text(b)
    assert _brep_text(reordered(shape)) != _brep_text(shape)
    assert ms._volume(a) == pytest.approx(ms._volume(shape), rel=1e-12)
    assert len(ms._faces(a)) == len(ms._faces(shape))


def _same_records(text: str) -> str:
    """A B-rep text with each edge's continuity record (the two faces it joins smoothly, "4 G1 s1 l1 s2 l2") naming
    its two faces in one order: OCC writes that pair in the order it met the faces, which no mesh reads."""
    out = []
    for ln in text.splitlines():
        r = ln.split()
        if len(r) == 6 and r[0] == "4":
            ln = " ".join([r[0], r[1], *sorted([" ".join(r[2:4]), " ".join(r[4:6])])])
        out.append(ln)
    return "\n".join(out)


def test_a_flat_sided_core_grows_the_same_every_time():
    """MIXED-2b: a stiffened panel's mount, less its walls' slabs, grown 6 mm: OCC's offset hands back its faces in a
    different order call to call (four calls, four different files); grown() the same every time."""
    from tests.fea_fixtures import _box, _fuse

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    t = 3.0
    shape = _fuse(_fuse(_fuse(_box(0, 0, 0, 160, 100, t), _box(0, 25 - t / 2, t, 160, t, 25)),
                        _box(0, 75 - t / 2, t, 160, t, 25)), _box(60, 40, t, 40, 20, 20))
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    mids = {m["id"]: m for m in rec["mids"]}
    kept = [(f, float(mids[o]["thickness_mm"])) for f, o in zip(ms._faces(surface), rec["surface_faces"], strict=True)]
    left = mx._solids(mx._bop("cut", mx._solids(shape)[0], [mx.slab(f, 0.5 * w * 1.02) for f, w in kept]))
    core = max(left, key=ms._volume)
    assert len({_same_records(_brep_text(mx.grown(core, 6.0))) for _ in range(4)}) == 1


def _filleted(shape, pick, r: float):
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    mk = BRepFilletAPI_MakeFillet(shape)
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    while ex.More():
        e = TopoDS.Edge_s(ex.Current())
        c = BRepAdaptor_Curve(e)
        if pick(c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))):
            mk.Add(r, e)
        ex.Next()
    mk.Build()
    return mk.Shape()


def test_cores_with_rounds_tighter_than_the_growth_still_grow():
    """MIXED-2c: concave 2 mm rounds grown 4 mm turn inside out in OCC's plain offset (no solid). A boss's foot round
    cut by the plate's slab grows by the complete intersection; a whole round along an L's inside corner by taking
    the round out first."""
    from tests.fea_fixtures import _box, _cut, _fuse

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    body = _filleted(_fuse(_box(0, 0, 0, 60, 60, 2), _box(24, 24, 2, 12, 12, 10)),
                     lambda p: abs(p.Z() - 2) < 1e-6 and 23 < p.X() < 37 and 23 < p.Y() < 37, 2.0)
    boss = mx._solids(_cut(body, _box(-1, -1, -1, 62, 62, 3.02)))[0]
    ell = mx._solids(_filleted(_fuse(_box(0, 0, 0, 30, 20, 5), _box(0, 0, 0, 5, 20, 30)),
                               lambda p: abs(p.X() - 5) < 1e-6 and abs(p.Z() - 5) < 1e-6, 2.0))[0]
    for core in (boss, ell):
        assert ms._volume(mx.grown(core, 4.0)) > 2.5 * ms._volume(core)


def test_a_mixed_part_s_walls_are_panels_however_few_walls_wide():
    """MIXED-7: an 18-wall-wide 2 mm plate is no panel to the shell plan (4 across by its width, a tenth of its
    size); in a mixed part's record it is, 20 across."""
    from meshpipeline.engines import shell_plan as sp

    rec = {"mids": [{"id": "Mid_1", "pair": "P1", "body": "Part_1"}],
           "pairs": [{"id": "P1", "thickness_mm": 2.0, "size_mm": 84.0}]}
    plain = sp.face_plan(rec, "Mid_1", width=37.0, radii=[])
    mixed = sp.face_plan({**rec, "panel_walls": mr.MIXED_PANEL_WALLS}, "Mid_1", width=37.0, radii=[])
    assert plain["rule"] == "SHMESH-1" and plain["size_mm"] == pytest.approx(8.4)
    assert mixed["rule"] == "SHMESH-15" and mixed["size_mm"] == pytest.approx(37.0 / sp.PANEL_ACROSS)


def _reach(shape):
    """The extraction's record and each shell wall's MIXED-8 reach, mm (in process)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    return rec, surface, mr.reach_of(rec, mx._solids(shape)[0], surface, 1.0)


def _plate_lip_block():
    """A 2 mm plate 150 x 100 with a thick block on it and a 2 mm lip 12 tall along one edge (an L joint)."""
    from tests.fea_fixtures import _box, _fuse

    return _fuse(_fuse(_box(0, 0, 0, 150, 100, 2), _box(65, 40, 2, 20, 20, 15)), _box(148, 0, 2, 2, 100, 12))


@pytest.mark.parametrize("name,lo,hi", [("box_corners", 17.0, 31.0), ("l_bracket_pad", 30.0, 80.0),
                                        ("tube_flange", 115.0, 121.0), ("stiff_panel_mount", 22.0, 28.0)])
def test_a_wall_s_reach_is_the_length_it_bends_over_from_its_joints(name, lo, hi):
    """MIXED-8: the farthest any point of a wall lies from its nearest joint or thick region. The box's side walls
    reach half the 36 or 46 mm between its corner columns, its bottom half the 52 mm between its side walls; the L
    bracket's leg runs 77 mm from the corner to its free end and its plate 33 mm between the corner and the pad; the
    tube 120 mm from its flange (the seam holds nothing); the stiffened panel's stiffeners 25 mm from the plate."""
    from tests.unit.engines import test_mixed_families as fam

    rec, _s, reach = _reach(fam._family(name)[0])
    shell = {m["id"]: m for m in rec["mids"] if m.get("shell")}
    assert set(reach) == set(shell)
    assert all(lo - 1e-9 <= r <= hi for r in reach.values()), reach
    if name == "box_corners":
        assert sorted(round(r) for r in reach.values()) == [20, 20, 25, 25, 27]
    if name == "l_bracket_pad":
        assert sorted(round(r) for r in reach.values()) == [33, 76]


@pytest.mark.parametrize("name", ["box_corners", "casting_ribs_block", "stiff_panel_mount"])
def test_parts_whose_walls_are_too_thick_for_shells_are_meshed_solid(name, tmp_path, monkeypatch):
    """MIXED-8 AS A GATE (REACH_GATE True, the rule before SHELL-23): the box (4 mm walls reaching 5 to 6.7 walls: its
    back wall read +23% mixed), the casting (3 mm ribs reaching 6.7 walls: a mode -6.8%) and the stiffened panel (3 mm,
    7.8 to 8.3 walls) stop before any meshing, and say why in plain words."""
    from tests import fea_fixtures as fx

    monkeypatch.setattr(mr, "REACH_GATE", True)
    from tests.unit.engines import test_mixed_families as fam

    step = fx.write_step(fam._family(name)[0], tmp_path / "part.step")
    rec: dict = {}
    with pytest.raises(mr._Stop) as stop:
        mr.build(step, tmp_path / "mixed", mm_per_unit=1.0, rec=rec)
    assert str(stop.value).startswith("Walls too thick for shells: ")
    assert rec["recommended"] is False and rec["recommend_why"] == str(stop.value)
    assert rec["too_thick"] and "split" not in rec
    assert not (tmp_path / "mixed" / "shell_trimmed.brep").exists()


def test_a_wall_too_thick_for_shells_is_kept_solid_and_the_rest_still_shell(monkeypatch):
    """MIXED-8 AS A GATE (REACH_GATE True): the 12 mm lip of a 2 mm plate reaches 6 walls from its corner (12 mm above
    the plate); it is kept solid with the block (two solid regions), and the plate, reaching far past 10 walls, is
    still shell, cut two walls in from both."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    monkeypatch.setattr(mr, "REACH_GATE", True)

    shape = _plate_lip_block()
    rec, surface, reach = _reach(shape)
    bad, share, stop = mr.too_thick(rec, reach)
    assert len(bad) == 1 and stop == "" and share > mr.MIXED_AUTO_SHARE
    lip = {m["id"]: m for m in rec["mids"]}[bad[0]]
    assert reach[bad[0]] == pytest.approx(12.0, abs=1.0) and lip["thickness_mm"] == pytest.approx(2.0)
    ok, why = mr.recommend(rec, reach)
    assert ok and why.endswith(" 1 wall too thick for shells (under 10 times its thickness from a joint) kept solid "
                               "with them.")
    kept = [(f, float({m["id"]: m for m in rec["mids"]}[o]["thickness_mm"]), 0.0)
            for f, o in zip(ms._faces(surface), rec["surface_faces"], strict=True)]
    faces = ms._faces(shape)
    thick = [faces[k] for k, r in enumerate(rec["faces"]) if r["role"] in ("thick", "solid")]
    order, walls, marks = mr.kept_solid(rec, kept, thick, bad)
    assert len(walls) == len(kept) - sum(o in bad for o in rec["surface_faces"]) and len(marks) > len(thick)
    sp = mx.split_body(mx._solids(shape)[0], walls, marks, scale=1.0)
    assert sp.ok, sp.why
    assert sp.cores == 2
    # the lip (2 x 12 x 100) and the block are in the solid; no shell face is left on the lip
    assert sp.solid_volume > 20 * 20 * 15 + 2 * 12 * 100
    assert all(rec["surface_faces"][order[k]] not in bad for k in sp.shell_from)
    assert (sp.solid_volume + sp.shell_volume) / sp.body_volume == pytest.approx(1.0, abs=0.02)
    # with nothing too thick, the split's input is as it was
    assert mr.kept_solid(rec, kept, thick, []) == (list(range(len(kept))), kept, thick)


def test_short_walls_are_shell_in_the_mixed_mesh_and_said():
    """SHELL-23: MIXED-8 is advice. A wall that reaches under 10 of its walls from a joint is meshed as shell with the
    rest - never kept solid with the thick regions - and one line says shell theory is less exact there."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    assert mr.REACH_GATE is False
    rec = {"bodies": [{"body": "Part_1", "verdict": "mixed", "volume_mm3": 1000.0, "mid_volume_ratio": 0.8,
                       "solid_regions": [{"faces": ["Face_7"]}]}],
           "sewn": [{"open_edges": 0}],
           "mids": [{"id": "Mid_1", "thickness_mm": 2.0, "shell": True, "area_mm2": 350.0},
                    {"id": "Mid_2", "thickness_mm": 2.0, "shell": True, "area_mm2": 50.0}]}
    assert mr.too_thick(rec, {"Mid_1": 60.0, "Mid_2": 12.0}) == (["Mid_2"], pytest.approx(0.8), "")
    assert mr.recommend(rec, {"Mid_1": 60.0, "Mid_2": 12.0}) == (True, (
        "Thin walls meshed as shell (80% of the part, 2 mm thick); 1 thick region kept solid and joined to them. 1 wall "
        "reaches under 10 times its thickness from a joint: shell theory is less exact there."))
    # every wall short: still a shell-and-solid mesh, said so (the gate would have meshed it solid)
    ok, why = mr.recommend(rec, {"Mid_1": 15.0, "Mid_2": 12.0})
    assert ok and why.endswith(" 2 walls reach under 10 times their thickness from a joint: shell theory is less exact "
                               "there.")
    assert mr.words({"made": True, "counts": {}, "too_thick": ["Mid_2"], "reach_gate": False}).endswith(
        " 1 wall reaches under 10 times its thickness from a joint: shell theory is less exact there.")
    # on the part: the 12 mm lip of a 2 mm plate (6 walls from its corner) stays a shell wall of the split - one
    # solid region, the block - where the gate kept it solid with the block (two)
    shape = _plate_lip_block()
    rec, surface, reach = _reach(shape)
    bad, _share, stop = mr.too_thick(rec, reach)
    assert len(bad) == 1 and stop == ""
    kept = [(f, float({m["id"]: m for m in rec["mids"]}[o]["thickness_mm"]), 0.0)
            for f, o in zip(ms._faces(surface), rec["surface_faces"], strict=True)]
    faces = ms._faces(shape)
    thick = [faces[k] for k, r in enumerate(rec["faces"]) if r["role"] in ("thick", "solid")]
    order, walls, marks = mr.kept_solid(rec, kept, thick, [])
    sp = mx.split_body(mx._solids(shape)[0], walls, marks, scale=1.0)
    assert sp.ok and sp.cores == 1, sp.why
    assert any(rec["surface_faces"][order[k]] in bad for k in sp.shell_from)       # the lip is in the shell


def test_too_thick_words_and_shares(monkeypatch):
    """MIXED-8 AS A GATE (REACH_GATE True) on a record: one of two walls too thick, the other holding enough of the
    part, stays mixed; both too thick, the part is solid with the shortest wall named."""
    monkeypatch.setattr(mr, "REACH_GATE", True)
    rec = {"bodies": [{"body": "Part_1", "verdict": "mixed", "volume_mm3": 1000.0, "mid_volume_ratio": 0.8,
                       "solid_regions": [{"faces": ["Face_7"]}]}],
           "sewn": [{"open_edges": 0}],
           "mids": [{"id": "Mid_1", "thickness_mm": 2.0, "shell": True, "area_mm2": 350.0},
                    {"id": "Mid_2", "thickness_mm": 2.0, "shell": True, "area_mm2": 50.0},
                    {"id": "Mid_3", "thickness_mm": 9.0, "shell": False, "area_mm2": 10.0}]}
    assert mr.recommend(rec, {"Mid_1": 60.0, "Mid_2": 30.0})[0]
    bad, share, stop = mr.too_thick(rec, {"Mid_1": 60.0, "Mid_2": 12.0})
    assert bad == ["Mid_2"] and share == pytest.approx(0.7) and stop == ""
    assert mr.recommend(rec, {"Mid_1": 60.0, "Mid_2": 12.0}) == (True, (
        "Thin walls meshed as shell (70% of the part, 2 mm thick); 1 thick region kept solid and joined to them. 1 wall "
        "too thick for shells (under 10 times its thickness from a joint) kept solid with them."))
    bad, share, stop = mr.too_thick(rec, {"Mid_1": 15.0, "Mid_2": 12.0})
    assert bad == ["Mid_2", "Mid_1"] and share == 0.0
    assert stop == ("Walls too thick for shells: 2 of its 2 thin walls reach less than 10 times their thickness from "
                    "the nearest joint or thick region (the shortest, 2 mm thick, reaches 12 mm: 6.0 times). A shell "
                    "that short bends too much at its joint, so the part is meshed as a solid.")
    assert mr.recommend(rec, {"Mid_1": 15.0, "Mid_2": 12.0}) == (False, stop)
    # without the reach, MIXED-8 is not read (the record alone says nothing of it)
    assert mr.recommend(rec)[0]
