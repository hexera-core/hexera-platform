# Responsibility: Hold the shell solver files (engines/shell_writers.py) to a small hand-made shell mesh of known
# numbers: every node, element, property, corner wall, group and junction written to Nastran, Abaqus / CalculiX
# and Ansys reads back the same; the Femap names and the 'add MAT1' note are there.
# Boundaries: pure files under tmp_path; no mesher, no solver.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines import shell_writers as sw
from meshpipeline.engines.shell_mesh import ShellMesh


def _mesh() -> ShellMesh:
    # an L of two plates meeting on the line x = 0 (Mid_1, 2 mm, flat in z = 0; Mid_2, 1.5 mm, upright in x = 0),
    # plus one tapered triangle (Mid_3, its wall 1 to 3 mm by corner) on Part_2
    xyz = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0],
                    [0, 0, 10], [0, 10, 10], [20, 0, 0], [30, 0, 0], [20, 10, 0]], dtype=float)
    quads = np.array([[0, 1, 2, 3], [0, 3, 5, 4]])
    trias = np.array([[6, 7, 8]])
    wall = np.array([2.0, 1.5, 3.0])
    node_wall = np.array([0, 0, 0, 0, 0, 0, 1.0, 2.0, 3.0])
    m = ShellMesh(xyz=xyz, quads=quads, trias=trias, wall=wall, mid=["Mid_1", "Mid_2", "Mid_3"],
                  part=["Part_1", "Part_1", "Part_2"], node_wall=node_wall, varies=np.array([False, False, True]))
    m.groups = {"Part_1_Face_1+Face_7": np.array([0]), "Part_1_Face_2+Face_8": np.array([1]),
                "Part_1": np.array([0, 1]), "Part_2": np.array([2])}
    return m


@pytest.mark.parametrize("units", ["m", "mm"])
def test_every_file_reads_back_the_same_mesh(tmp_path, units):
    m = _mesh()
    sw.write_all(tmp_path, m, units=units)
    s = 1e-3 if units == "m" else 1.0
    prep = sw.prepare(m, units=units)
    # SHWRITE-1: a part of several walls has a property per wall, "<part> t=<wall>mm"; a part of one, the part's name
    assert [p[0] for p in prep.props] == ["Part_1 t=2mm", "Part_1 t=1.5mm", "Part_2"]
    assert [p[2] for p in prep.props] == [1, 1, 2]
    assert prep.junctions == {"J_Mid_1_Mid_2": pytest.approx(np.array([1, 4]))}
    for read in (sw.read_bdf(tmp_path / "shell.bdf"), sw.read_inp(tmp_path / "shell.inp"),
                 sw.read_cdb(tmp_path / "shell.cdb")):
        assert sorted(read.nodes) == list(range(1, 10))
        assert np.allclose([read.nodes[k] for k in range(1, 10)], m.xyz * s, rtol=1e-7)
        assert read.elements == {1: (1, 2, 3, 4), 2: (1, 4, 6, 5), 3: (7, 8, 9)}
        assert {e: read.walls[p] for e, p in read.prop.items() if e < 3} == pytest.approx({1: 2.0 * s, 2: 1.5 * s})
        assert {k.upper(): v for k, v in read.junctions.items()} == {"J_MID_1_MID_2": {1, 4}}
        names = {k.upper() for k in read.groups}
        assert {"PART_1", "PART_2"} <= names and len(read.groups) == 4
    bdf = sw.read_bdf(tmp_path / "shell.bdf")
    assert bdf.corner == {3: pytest.approx((1.0 * s, 2.0 * s, 3.0 * s))}
    assert bdf.names == bdf.hm_props == {1: "Part_1 t=2mm", 2: "Part_1 t=1.5mm", 3: "Part_2"}
    assert bdf.parts == {"Part_1": {1, 2}, "Part_2": {3}}
    assert bdf.groups["Part_1"] == {1, 2}
    inp = sw.read_inp(tmp_path / "shell.inp")
    assert inp.corner == {3: pytest.approx((1.0 * s, 2.0 * s, 3.0 * s))}
    text = (tmp_path / "shell.bdf").read_text()
    assert "add MAT1 k" in text and "$ Femap Property 1 : Part_1 t=2mm" in text
    assert "not yet tested in the real solver" in text.lower()


@pytest.mark.parametrize("x", [2e-3, 1.5e-3, 0.0123456, 1.0, 5.0 / 6.0, 125.5, 1.2345e-7])
def test_a_small_field_real_keeps_its_digits(x):
    s = sw._r8(x)
    assert len(s) <= 8 and sw._real(s) == pytest.approx(x, rel=1e-4)


def _ties():
    from meshpipeline.engines.shell_links import Tie

    # a shell node tied to the mean of three solid nodes (SHLINK-1), and a solid node riding on a shell (SHLINK-2)
    return [Tie(7, 1, [(11, 1, 1 / 3), (12, 1, 1 / 3), (13, 1, 1 / 3)]),
            Tie(20, 3, [(7, 3, 0.5), (8, 3, 0.5), (7, 4, 1.25), (8, 5, -0.75)])]


def _parse(lines, head):
    eqs, cur = [], None
    for ln in lines:
        f = ln.split(",")
        if f[0] == head and not (head == "CE" and f[1] == "HIGH"):
            cur = []
            eqs.append(cur)
            f = f[2:] if head == "MPC" else f[3:]
        elif head == "MPC" and f[0] == "" and f[1] == "":
            f = f[2:]
        elif head == "CE" and f[1] == "HIGH":
            f = f[3:]
        assert cur is not None
        for i in range(0, len(f), 3):
            cur.append((int(f[i]), f[i + 1], float(f[i + 2])))
    return eqs


def test_the_moment_join_writes_the_same_ties_for_nastran_and_ansys():
    from meshpipeline.engines.shell_links import ansys_ces, equations, nastran_mpcs

    ties = _ties()
    want = [[(t.node, t.dof, 1.0)] + [(n, d, -c) for n, d, c in t.terms] for t in ties]
    mpc = nastran_mpcs(ties, sid=4)
    assert all(ln.startswith(("MPC*    ", "*       ")) and len(ln) <= 72 for ln in mpc)
    cards: list[list[str]] = []
    for ln in mpc:
        if ln.startswith("MPC*"):
            cards.append([])
        cards[-1] += [ln[8 + 16 * i:24 + 16 * i] for i in range(4)]
    assert all(int(c[0]) == 4 for c in cards)
    got = []
    for c in cards:
        tok = [f.strip() for f in c[1:] if f.strip()]
        got.append([(int(tok[i]), int(tok[i + 1]), float(tok[i + 2])) for i in range(0, len(tok), 3)])
    assert got == [[(n, d, pytest.approx(c)) for n, d, c in eq] for eq in want]
    dof = {"UX": 1, "UY": 2, "UZ": 3, "ROTX": 4, "ROTY": 5, "ROTZ": 6}
    ce = ansys_ces(ties)
    assert ce[0].startswith("CE,NEXT,0,") and any(ln.startswith("CE,HIGH,0,") for ln in ce)
    got = [[(n, dof[d], c) for n, d, c in eq] for eq in _parse(ce, "CE")]
    assert got == [[(n, d, pytest.approx(c)) for n, d, c in eq] for eq in want]
    assert equations(ties)[0] == "*EQUATION"


def test_pynastran_reads_the_moment_join_mpcs(tmp_path):
    bdf_mod = pytest.importorskip("pyNastran.bdf.bdf")
    from meshpipeline.engines.shell_links import nastran_mpcs

    grids = [f"GRID,{i},,{float(i)},0.,0." for i in (7, 8, 11, 12, 13, 20)]
    (tmp_path / "j.bdf").write_text("\n".join([*grids,*nastran_mpcs(_ties(), sid=4), "ENDDATA"]) + "\n")
    b = bdf_mod.BDF(debug=None)
    b.read_bdf(str(tmp_path / "j.bdf"), punch=True, xref=False)
    mpcs = b.mpcs[4]
    assert len(mpcs) == 2
    assert mpcs[1].nodes == [20, 7, 8, 7, 8] and [int(c) for c in mpcs[1].components] == [3, 3, 3, 4, 5]
    assert mpcs[1].coefficients == pytest.approx([1.0, -0.5, -0.5, -1.25, 0.75])


def test_an_offset_mid_plane_is_written_to_every_deck(tmp_path):
    # SHELL-7: the 1.5 mm plate (Mid_2) is snapped onto a thicker plate's mid-plane; its own mid-plane lies 0.25 mm
    # along its normal: Nastran ZOFFS 0.25, Abaqus / CalculiX OFFSET -0.25 / 1.5 (the nodes below the mid-surface),
    # Ansys SECOFFSET,USER the nodes' distance from the mid-plane, -0.25; the varying triangle keeps its corners
    m = _mesh()
    m.zoffs = np.array([0.0, 0.25, 0.0])
    sw.write_all(tmp_path, m, units="mm")
    prep = sw.prepare(m, units="mm")
    assert [p[0] for p in prep.props] == ["Part_1 t=2mm", "Part_1 t=1.5mm", "Part_2"]
    bdf = sw.read_bdf(tmp_path / "shell.bdf")
    assert bdf.zoffs == {2: pytest.approx(0.25)}
    assert bdf.corner == {3: pytest.approx((1.0, 2.0, 3.0))}
    inp = (tmp_path / "shell.inp").read_text()
    assert "*SHELL SECTION, ELSET=P_2, MATERIAL=MAT_1, OFFSET=-0.166666667" in inp
    assert "SECOFFSET,USER,-0.25" in (tmp_path / "shell.cdb").read_text()
    bdf_mod = pytest.importorskip("pyNastran.bdf.bdf")
    b = bdf_mod.BDF(debug=None)
    b.read_bdf(str(tmp_path / "shell.bdf"), punch=True, xref=False)
    assert b.elements[2].zoffset == pytest.approx(0.25)
    assert b.elements[3].T1 == pytest.approx(1.0) and b.elements[3].T3 == pytest.approx(3.0)
