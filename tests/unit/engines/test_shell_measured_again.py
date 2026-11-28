# Responsibility: Verify that what a shell's files and its told report say is what was measured on the mesh delivered:
# a node the extra quality passes (SHMESH-100) moved on a tapered wall has its wall read again where it stands
# (SHELL-9b), and a joined shell-and-solid mesh's requirement report (SHMESH-26) is made again with the elements the
# join added (MIXED-13 / MIXED-22).
# Boundaries: made-up meshes and wall readings, pure numpy; the passes themselves are stood in for (their move is
# given); no gmsh, no OpenCASCADE.
from __future__ import annotations

import dataclasses

import numpy as np
from tests.unit.engines.test_shell_requirements import criteria

from meshpipeline.engines import mixed_run as MX
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines.gmsh import shell_adapter as sa
from meshpipeline.engines.gmsh import shell_quality_passes as QP
from meshpipeline.engines.shell_mesh import ShellMesh


def _grid(n: int = 3, h: float = 10.0) -> ShellMesh:
    """An n x n plate of h-mm quads in z = 0, one mid-face."""
    xs = np.arange(n + 1) * h
    xyz = np.array([[x, y, 0.0] for y in xs for x in xs])
    k = n + 1
    quads = np.array([[j * k + i, j * k + i + 1, (j + 1) * k + i + 1, (j + 1) * k + i]
                      for j in range(n) for i in range(n)], dtype=np.int64)
    m = len(quads)
    return ShellMesh(xyz=xyz, quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(m, 3.0),
                     mid=["Mid_1"] * m, part=["Part_1"] * m)


#: a wall tapering from 2 mm at x = 0 to 5 mm at x = 30 (a drafted wall), read on a 5 mm grid
TAPER = {"Mid_1": {"thickness_mm": 3.5, "thickness_range_mm": [2.0, 5.0],
                   "wall_samples_mm": [[x, y, 0.0, 2.0 + x / 10.0] for x in range(0, 31, 5) for y in range(0, 31, 5)]}}


def test_a_node_the_quality_passes_moved_on_a_tapered_wall_has_its_wall_read_again(monkeypatch):
    mesh = _grid()
    assert sa.node_walls(mesh, TAPER) == 9
    at = int(np.flatnonzero((mesh.xyz[:, 0] == 10.0) & (mesh.xyz[:, 1] == 10.0))[0])
    other = int(np.flatnonzero((mesh.xyz[:, 0] == 20.0) & (mesh.xyz[:, 1] == 10.0))[0])
    assert abs(mesh.node_wall[at] - 3.0) < 1e-6 and abs(mesh.node_wall[other] - 4.0) < 1e-6
    xyz = mesh.xyz.copy()
    xyz[at, 0] = 14.0                                   # the passes move that node 4 mm along the taper

    def passes(m, applied, requirements, **_k):
        return dataclasses.replace(m, xyz=xyz), dict(applied), {"rule": QP.RULE, "kept": 1, "seconds": 0.0}

    monkeypatch.setattr(QP, "run", passes)
    monkeypatch.setitem(sa._CLOCK, "t0", 0.0)
    monkeypatch.setitem(sa._CLOCK, "allowed", 1e12)
    out, applied = sa._quality_passes((mesh, {"quality": {}}), None, mids=TAPER)
    assert abs(out.node_wall[at] - 3.4) < 1e-6, "the moved node carries the wall where it now stands"
    assert abs(out.node_wall[other] - 4.0) < 1e-9, "a node that did not move keeps its wall"
    assert applied["quality"]["walls_read_again"] == 1
    # a constant wall has no corner walls to read again
    flat = _grid()
    assert sa.node_walls_again(flat, TAPER, flat.xyz) == 0


def _row(rep: dict, word: str) -> dict:
    return next(r for r in rep["rows"] if word in str(r["field"]))


def test_a_joined_meshs_report_is_made_again_with_the_elements_the_join_added():
    req = criteria()                                    # global 10 mm, aspect < 4 (outer 6), ...
    shell = _grid()
    before = SR.report(shell, req, None)
    assert _row(before, "aspect")["met"] is True
    # the join adds an overlap quad 10 mm by 1 mm along the plate's edge (aspect 10): past the typed limit
    n = len(shell.xyz)
    xyz = np.vstack([shell.xyz, [[0.0, -1.0, 0.0], [10.0, -1.0, 0.0]]])
    quads = np.vstack([shell.quads, [[n, n + 1, 1, 0]]])
    joined = dataclasses.replace(shell, xyz=xyz, quads=quads, wall=np.r_[shell.wall, 3.0],
                                 mid=[*shell.mid, "Mid_1"], part=[*shell.part, "Part_1"])
    again = MX.joined_requirements(joined, req, {"sizing": None}, before)
    row = _row(again, "aspect")
    assert row["met"] is not True and row["outside"] >= 1, "the added element is measured and told"
    # nothing typed: nothing to make again, the report given stands
    assert MX.joined_requirements(joined, None, {}, before) is before
