# Responsibility: Verify how an assembly's ties are settled for every solver (engines/fea_assembly_deck.settle_ties): never a node tied to a face it already lies on (a fused joint), only nodes within the tolerance, one master per node, no loop of ties, ADJUST=NO and the same tolerance and slave in the CalculiX, Nastran and Ansys files.
# Boundaries: small hand-built tet meshes; no CAD, no mesher, no solver (tests/unit/engines/test_fea_textbook.py solves a fused-and-tied joint in CalculiX).
from __future__ import annotations

import re

import numpy as np
import pytest

from meshpipeline.engines import fea_assembly_deck as AD
from meshpipeline.engines.gmsh.abaqus_deck import TET_FACE_CORNERS, FaceSet, TetFaceIndex

#: the six tets of a unit cube (Kuhn), by its corner numbers (bit 0 = x, bit 1 = y, bit 2 = z)
_KUHN = [(0, 1, 3, 7), (0, 3, 2, 7), (0, 2, 6, 7), (0, 6, 4, 7), (0, 4, 5, 7), (0, 5, 1, 7)]


class _Mesh:
    """Boxes of tets, each a part, numbered as prepare() numbers them; a box may reuse another's nodes
    where they coincide (`share`), as a fused mesh does."""

    def __init__(self):
        self.xyz: list[tuple[float, float, float]] = []
        self.elements: list[tuple[int, ...]] = []
        self.part: list[int] = []
        self.where: dict[tuple[float, float, float], int] = {}

    def box(self, part: int, lo, hi, n, share: bool = False) -> None:
        nx, ny, nz = n
        grid = {}
        for i in range(nx + 1):
            for j in range(ny + 1):
                for k in range(nz + 1):
                    p = tuple(round(lo[d] + (hi[d] - lo[d]) * (i, j, k)[d] / n[d], 9) for d in range(3))
                    if share and p in self.where:
                        grid[i, j, k] = self.where[p]
                        continue
                    self.xyz.append(p)
                    grid[i, j, k] = len(self.xyz)
                    self.where.setdefault(p, len(self.xyz))
        for i in range(nx):
            for j in range(ny):
                for k in range(nz):
                    c = [grid[i + (b & 1), j + (b >> 1 & 1), k + (b >> 2 & 1)] for b in range(8)]
                    for t in _KUHN:
                        self.elements.append(tuple(c[v] for v in t))
                        self.part.append(part)

    def face(self, name: str, part: int, axis: int, value: float, within=None) -> FaceSet:
        """The element faces of `part` on the plane axis = value (and inside `within`: ((lo, hi), ...))."""
        els = np.asarray(self.elements)
        xyz = np.asarray(self.xyz)
        pick = np.flatnonzero(np.asarray(self.part) == part)
        tris = els[pick][:, np.asarray(TET_FACE_CORNERS)].reshape(-1, 3)
        on = np.all(np.abs(xyz[tris - 1][:, :, axis] - value) < 1e-9, axis=1)
        if within is not None:
            cen = xyz[tris - 1].mean(axis=1)
            for d, (a, b) in enumerate(within):
                if a is not None:
                    on &= (cen[:, d] > a) & (cen[:, d] < b)
        e, lab = TetFaceIndex(els[pick]).lookup(tris[on])
        e = pick[e]
        nodes = np.unique(els[e[:, None], np.asarray(TET_FACE_CORNERS)[lab - 1]])
        return FaceSet(name=name, tag=0, elements=e + 1, labels=lab, nodes=nodes)

    def settle(self, joints: list[AD.Joint]) -> AD.Prepared:
        els = np.asarray(self.elements, dtype=np.int64)
        xyz = np.asarray(self.xyz, dtype=float) * 1e-3
        ids = np.arange(1, len(xyz) + 1)
        index = np.zeros(len(ids) + 1, dtype=np.int64)
        index[ids] = np.arange(len(ids))
        part = np.asarray(self.part)
        side = {p: part == p for p in sorted(set(self.part))}
        joints = AD.settle_ties(joints, els, xyz, index, side)
        parts = [{"n": p, "id": f"Part_{p}", "name": "", "first": int(np.flatnonzero(part == p)[0]) + 1,
                  "last": int(np.flatnonzero(part == p)[-1]) + 1} for p in sorted(side)]
        return AD.Prepared(node_ids=ids, xyz=xyz, elements=els, element_part=part, parts=parts, faces=[], joints=joints)


def _tie(m: _Mesh, slave: FaceSet, master: FaceSet, parts, reach=None, name="Tie_1") -> AD.Joint:
    return AD.Joint(name=name, kind="tie", parts=parts, sides=(slave, master), reach=reach)


def _decks(tmp_path, prep):
    AD.write_inp(tmp_path / "m.inp", prep)
    AD.write_bdf(tmp_path / "m.bdf", prep)
    AD.write_cdb(tmp_path / "m.cdb", prep)
    return (tmp_path / "m.inp").read_text(), (tmp_path / "m.bdf").read_text(), (tmp_path / "m.cdb").read_text()


def _nset(inp: str, name: str) -> set[int]:
    m = re.search(rf"\*NSET, NSET={name}\n((?:[^*][^\n]*\n)*)", inp)
    return {int(v) for v in re.findall(r"\d+", m.group(1))} if m else set()


def _bctable_error(bdf: str) -> list[float]:
    rows = [ln for ln in bdf.splitlines() if ln[8:16].strip() == "SLAVE"]
    return [float(r[24:32].replace("-", "E-").replace("+", "E+").lstrip("E")) for r in rows]


def test_a_gap_tie_ties_every_slave_node_where_it_stands_with_one_tolerance_in_every_file(tmp_path):
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (2, 2, 2))
    m.box(2, (1.2, 0, 0), (2.2, 1, 1), (2, 2, 2))
    a, b = m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_1_Part_2", 2, 0, 1.2)
    prep = m.settle([_tie(m, a, b, (1, 2), reach=0.25e-3)])
    (j,) = prep.joints
    assert j.kind == "tie" and set(j.tied) == set(a.nodes) and j.tolerance == pytest.approx(0.25e-3)
    inp, bdf, cdb = _decks(tmp_path, prep)
    assert "*TIE, NAME=Tie_1, POSITION TOLERANCE=" in inp and ", ADJUST=NO\nTie_1_Part_1_Tied, Tie_1_Part_2\n" in inp
    assert "*SURFACE, NAME=Tie_1_Part_1_Tied, TYPE=NODE\nTie_1_Part_1_Tied\n" in inp
    assert _nset(inp, "Tie_1_Part_1_Tied") == set(a.nodes.tolist())
    assert _bctable_error(bdf) == [pytest.approx(0.25e-3, rel=1e-3)]
    real = next(ln for ln in cdb.splitlines() if ln.startswith("R,2"))
    assert float(real.split(",")[7]) == pytest.approx(0.25e-3) and "KEYOPT,5,9,1" in cdb


def test_a_tie_across_a_fused_joint_leaves_the_shared_nodes_out_and_one_wholly_shared_is_bonded(tmp_path):
    # Part_2 shares Part_1's face x = 1 (one conformal mesh), and only half of Part_3's face x = 1 is shared
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (2, 2, 2))
    m.box(2, (1, 0, 0), (2, 1, 1), (2, 2, 2), share=True)
    whole = _tie(m, m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_1_Part_2", 2, 0, 1.0), (1, 2))
    prep = m.settle([whole])
    (j,) = prep.joints
    assert j.kind == "bonded" and j.name == "Bond_1" and [s.name for s in j.sides] == ["Bond_1_Part_1", "Bond_1_Part_2"]
    assert j.left_out["shared"] == len(j.sides[0].nodes)
    inp, bdf, cdb = _decks(tmp_path, prep)
    assert "*TIE" not in inp and "BCTABLE" not in bdf and "ET,5,174" not in cdb


def test_a_partly_fused_tie_ties_only_the_nodes_the_master_does_not_own(tmp_path):
    # Part_3 is one mesh with Part_1 on the upper half of x = 1 (its nodes at y = 0.5 and 1 are Part_1's;
    # Part_1's own at y = 0.75 hang between them) and meshed apart, a hair off, on the lower half
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (4, 4, 1))
    m.box(3, (1, 0.5, 0), (2, 1, 1), (4, 1, 1), share=True)
    m.box(3, (1.0001, 0, 0), (2, 0.5, 1), (4, 1, 1))              # coarser: Part_1 stays the slave
    slave = m.face("Tie_1_Part_1", 1, 0, 1.0)
    master = AD._union("Tie_1_Part_3", [m.face("a", 3, 0, 1.0), m.face("b", 3, 0, 1.0001)])
    prep = m.settle([_tie(m, slave, master, (1, 3))])
    (j,) = prep.joints
    xyz = np.asarray(m.xyz)
    own3 = set(np.unique(np.asarray(m.elements)[np.asarray(m.part) == 3]).tolist())
    assert j.kind == "tie" and j.parts == (1, 3) and j.left_out == {"shared": 4, "beyond": 0, "twice": 0, "chain": 0}
    # every node but the four Part_3 owns is tied: the lower half's, and the two hanging at y = 0.75
    assert not (set(j.tied.tolist()) & own3) and len(j.tied) == 6
    assert sorted({round(xyz[n - 1][1], 3) for n in j.tied}) == [0.0, 0.25, 0.75]
    # Ansys: no bonded contact element on a face that holds a node the tie left out (the shared ones)
    AD.write_cdb(tmp_path / "m.cdb", prep)
    rows = [ln for ln in (tmp_path / "m.cdb").read_text().split("EBLOCK", 1)[1].splitlines()[2:] if len(ln) >= 99]
    conta = [[int(ln[k:k + 9]) for k in range(99, len(ln), 9)] for ln in rows if int(ln[9:18]) == 5]
    assert conta and not any(set(c) & set(j.held_out.tolist()) for c in conta)


def test_a_slave_node_beyond_the_tolerance_is_not_tied(tmp_path):
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (4, 4, 1))
    m.box(2, (1, 0, 0), (2, 0.5, 1), (2, 1, 2))                  # meshed apart, touching half the face
    slave, master = m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_1_Part_2", 2, 0, 1.0)
    prep = m.settle([_tie(m, slave, master, (1, 2))])
    (j,) = prep.joints
    xyz = np.asarray(m.xyz)
    tol = AD.TOUCH_SHARE * 0.5e-3                                   # the master's typical edge: 0.5 mm
    assert j.tolerance == pytest.approx(tol, rel=0.3)
    assert j.left_out["beyond"] > 0 and all(xyz[n - 1][1] <= 0.5 + 1e-9 for n in j.tied)
    inp, _bdf, _cdb = _decks(tmp_path, prep)
    assert f"POSITION TOLERANCE={AD._coord(j.tolerance)}, ADJUST=NO" in inp


@pytest.mark.parametrize("overlap", [True, False])
def test_parts_that_pass_into_each_other_are_tied_where_the_slave_lies_inside_the_master(overlap):
    # a press fit: Part_2's face x = 0.9 lies 0.1 inside Part_1 (edge 0.5): an overlap pair ties it there,
    # a touching pair would not reach it; Part_2's far faces lie outside and are never tied
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (2, 2, 2))
    m.box(2, (0.9, 0, 0), (2, 1, 1), (4, 4, 4))
    slave = AD._union("Tie_1_Part_2", [m.face("a", 2, 0, 0.9), m.face("b", 2, 0, 2.0)])
    master = m.face("Tie_1_Part_1", 1, 0, 1.0)
    joint = AD.Joint(name="Tie_1", kind="tie", parts=(2, 1), sides=(slave, master), overlap=overlap)
    (j,) = m.settle([joint]).joints
    xyz = np.asarray(m.xyz)
    inner = [n for n in slave.nodes if abs(xyz[n - 1][0] - 0.9) < 1e-9]
    if overlap:
        assert j.parts == (2, 1) and set(j.tied.tolist()) == set(inner)
        assert j.tolerance == pytest.approx(AD.OVERLAP_DEPTH_SHARE * 0.5e-3, rel=0.5)
    else:
        assert len(j.tied) == 0 and j.left_out["beyond"] == len(slave.nodes)


def test_a_node_follows_one_master_only():
    # two masters on top of each other at different distances: each slave node is tied to the nearer
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (2, 2, 2))
    m.box(2, (1.1, 0, 0), (2.1, 1, 1), (2, 2, 2))
    m.box(3, (1.05, 0, 0), (2.05, 1, 1), (2, 2, 2))
    s1, s2 = m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_2_Part_1", 1, 0, 1.0)
    prep = m.settle([_tie(m, s1, m.face("Tie_1_Part_2", 2, 0, 1.1), (1, 2), reach=0.2e-3),
                     _tie(m, s2, m.face("Tie_2_Part_3", 3, 0, 1.05), (1, 3), reach=0.2e-3, name="Tie_2")])
    far, near = prep.joints
    assert len(far.tied) == 0 and far.left_out["twice"] == len(s1.nodes) and set(near.tied) == set(s2.nodes)


@pytest.mark.parametrize("coarse", [True, False])
def test_a_master_is_never_a_slave_too(coarse):
    # Part_2's face x = 1.1 (y 0..2) is the master Part_1 (y 0..1) is tied to, and faces Part_3 (y 1..2)
    # across the same 0.1 gap. A chain of ties comes back round where parts are also fused (h_g_c0003), so
    # Part_2's face stays a master: Part_3 is the slave there, fine or coarse
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (4, 4, 4))
    m.box(2, (1.1, 0, 0), (2, 2, 1), (2, 4, 2))
    m.box(3, (0, 1, 0), (1, 2, 1), (1, 1, 1) if coarse else (2, 4, 4))
    a = _tie(m, m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_1_Part_2", 2, 0, 1.1), (1, 2), reach=0.15e-3)
    b = _tie(m, m.face("Tie_2_Part_2", 2, 0, 1.1), m.face("Tie_2_Part_3", 3, 0, 1.0), (2, 3), reach=0.15e-3,
             name="Tie_2")
    t1, t2 = m.settle([a, b]).joints
    assert t1.parts == (1, 2) and set(t1.tied.tolist()) == set(a.sides[0].nodes.tolist())
    assert t2.parts == (3, 2) and len(t2.tied) == len(b.sides[1].nodes) and t2.left_out["chain"] == 0
    # no node is both a slave and on a master surface
    masters = set(t1.sides[1].nodes.tolist()) | set(t2.sides[1].nodes.tolist())
    assert not ((set(t1.tied.tolist()) | set(t2.tied.tolist())) & masters)


@pytest.mark.parametrize("staged_slave", [1, 2])
def test_the_slave_is_the_side_with_more_nodes_on_the_joint(staged_slave):
    # Part_1 is fine and twice the area of the joint, Part_2 coarse: Part_1's nodes on the joint tie it all
    # over, Part_2's few would leave the fine side's nodes between them free - whichever side was staged first
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 2, 1), (2, 8, 4))
    m.box(2, (1, 0, 0), (2, 1, 1), (1, 1, 1))
    a, b = m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_1_Part_2", 2, 0, 1.0)
    joint = _tie(m, a, b, (1, 2)) if staged_slave == 1 else _tie(m, b, a, (2, 1))
    (j,) = m.settle([joint]).joints
    assert j.parts == (1, 2) and j.sides[0].name == "Tie_1_Part_1"
    xyz = np.asarray(m.xyz)
    on_joint = [n for n in a.nodes if xyz[n - 1][1] <= 1.0 + 1e-9]
    assert set(j.tied.tolist()) == set(on_joint) and j.left_out["beyond"] == len(a.nodes) - len(on_joint)


def test_a_node_over_a_convex_crease_is_on_the_master_and_one_beside_its_rim_is_not():
    # a roof of four facets, its ridge along y at z = 0.1: right over the ridge no flat facet perpendicular
    # reaches (a curved master would), yet the node is on the joint; a node off the roof eave is not
    pts = {1: (-1, 0, 0), 2: (0, 0, 0.1), 3: (0, 1, 0.1), 4: (-1, 1, 0), 5: (1, 0, 0), 6: (1, 1, 0)}
    tri_ids = np.array([[1, 2, 3], [1, 3, 4], [2, 5, 6], [2, 6, 3]])
    tri_xyz = np.array([[pts[n] for n in t] for t in tri_ids], dtype=float)
    over_ridge, beside_eave = (0.0, 0.5, 0.15), (1.05, 0.5, 0.0)
    dist, tri = AD._nearest_master(np.array([over_ridge, beside_eave]), tri_xyz, 0.1, tri_ids=tri_ids)
    assert dist[0] == pytest.approx(0.05) and tri[0] >= 0
    assert not np.isfinite(dist[1]) and tri[1] == -1
    # a farther facet the node also lies over (0.5 below, beyond the tolerance) never hides the nearer crease
    far = np.array([[-1, -1, -0.35], [1, -1, -0.35], [0, 2, -0.35]], dtype=float)
    dist, tri = AD._nearest_master(np.array([over_ridge]), np.concatenate([tri_xyz, far[None]]), 0.1,
                                   tri_ids=np.concatenate([tri_ids, [[7, 8, 9]]]))
    assert dist[0] == pytest.approx(0.05) and tri[0] < 4
    # without the facets' node numbers nothing is known of the rim: the ridge node is not taken
    assert not np.isfinite(AD._nearest_master(np.array([over_ridge]), tri_xyz, 0.1)[0][0])


def test_a_tie_that_ties_nothing_blocks_no_other():
    # Part_1 is nearer Part_3 (0.05) than Part_2 (0.1), so the Part_1 -> Part_2 tie keeps no node; Part_2 is then
    # no master, and its face stays free to be the slave of its own joint with Part_4 (whose corners lie off it)
    m = _Mesh()
    m.box(1, (0, 0, 0), (1, 1, 1), (2, 2, 2))
    m.box(2, (1.1, 0, 0), (2, 2, 1), (2, 4, 2))
    m.box(3, (1.05, 0, 0), (1.08, 1, 1), (1, 1, 1))
    m.box(4, (0.5, 0.9, -0.1), (1.0, 2.1, 1.1), (1, 1, 1))
    ab = _tie(m, m.face("Tie_1_Part_1", 1, 0, 1.0), m.face("Tie_1_Part_2", 2, 0, 1.1), (1, 2), reach=0.15e-3)
    ac = _tie(m, m.face("Tie_2_Part_1", 1, 0, 1.0), m.face("Tie_2_Part_3", 3, 0, 1.05), (1, 3), reach=0.15e-3,
              name="Tie_2")
    bd = _tie(m, m.face("Tie_3_Part_2", 2, 0, 1.1), m.face("Tie_3_Part_4", 4, 0, 1.0), (2, 4), reach=0.15e-3,
              name="Tie_3")
    t1, t2, t3 = m.settle([ab, ac, bd]).joints
    assert len(t1.tied) == 0 and t1.left_out["twice"] == len(ab.sides[0].nodes)
    assert set(t2.tied.tolist()) == set(ac.sides[0].nodes.tolist())
    assert t3.parts == (2, 4) and len(t3.tied) and t3.left_out["chain"] == 0
