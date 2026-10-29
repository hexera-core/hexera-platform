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
    assert [p[0] for p in prep.props] == ["Part_1_t2mm", "Part_1_t1p5mm", "Part_2_t3mm"]
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
    assert bdf.names == {1: "Part_1_t2mm", 2: "Part_1_t1p5mm", 3: "Part_2_t3mm"}
    assert bdf.groups["Part_1"] == {1, 2}
    inp = sw.read_inp(tmp_path / "shell.inp")
    assert inp.corner == {3: pytest.approx((1.0 * s, 2.0 * s, 3.0 * s))}
    text = (tmp_path / "shell.bdf").read_text()
    assert "add MAT1 k" in text and "$ Femap Property 1 : Part_1_t2mm" in text
    assert "not yet tested in the real solver" in text.lower()


@pytest.mark.parametrize("x", [2e-3, 1.5e-3, 0.0123456, 1.0, 5.0 / 6.0, 125.5, 1.2345e-7])
def test_a_small_field_real_keeps_its_digits(x):
    s = sw._r8(x)
    assert len(s) <= 8 and sw._real(s) == pytest.approx(x, rel=1e-4)
