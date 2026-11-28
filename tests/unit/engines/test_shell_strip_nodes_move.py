"""SHMESH-102 (round 9): a mapped strip's nodes move in the quality passes like any free node, under the same guard;
a node next to another structure's node stays fixed."""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines.gmsh import shell_adapter as sa
from meshpipeline.engines.gmsh import shell_quality_passes as QP
from meshpipeline.engines.shell_mesh import ShellMesh


def _two_faces(n: int = 4, h: float = 10.0) -> tuple[ShellMesh, np.ndarray]:
    """An n x n plate of quads: the left half a mapped strip (face 0, its nodes held), the right half free (face 1)."""
    xs = np.arange(n + 1) * h
    xyz = np.array([[x, y, 0.0] for y in xs for x in xs])
    quads = np.array([[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
                      for j in range(n) for i in range(n)], dtype=np.int64)
    m = ShellMesh(xyz=xyz, quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(n * n, 2.0),
                  mid=["Mid_1"] * (n * n), part=["Part_1"] * (n * n), groups={"Part_1": np.arange(n * n)})
    face_of = np.array([0 if i < n // 2 else 1 for _j in range(n) for i in range(n)], dtype=np.int64)
    held = np.zeros(len(xyz), dtype=bool)
    held[np.unique(quads[face_of == 0])] = True
    kind = np.where(held, sa.HELD_KINDS.index("mapped strip"), 0).astype(np.int8)
    m.face_of = face_of  # type: ignore[attr-defined]
    m.held = held  # type: ignore[attr-defined]
    m.held_kind = kind  # type: ignore[attr-defined]
    return m, face_of


def _sweep(m: ShellMesh, face_of: np.ndarray) -> QP._Sweep:
    return QP._Sweep(m, face_of, [], np.zeros(len(m.quads), dtype=bool), None, None, m.held)  # type: ignore[attr-defined]


def test_strip_node_on_the_shared_edge_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    m, face_of = _two_faces()
    shared = 1 * 5 + 2                     # (x 20, y 10): on the straight edge between the strip and the free face
    monkeypatch.setattr(QP, "STRIP_NODES_MOVE", True)
    sw = _sweep(m, face_of)
    assert sw._can(shared) and not sw.pinned[shared]
    assert bool(sw.held[shared])           # (still held for the census, the turns and the re-meshes)
    monkeypatch.setattr(QP, "STRIP_NODES_MOVE", False)
    assert not _sweep(m, face_of)._can(shared)


def test_node_next_to_another_structure_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    m, face_of = _two_faces()
    shared = 1 * 5 + 2
    m.held_kind[1 * 5 + 1] = sa.HELD_KINDS.index("ring")  # type: ignore[attr-defined]
    monkeypatch.setattr(QP, "STRIP_NODES_MOVE", True)
    sw = _sweep(m, face_of)
    assert sw.pinned[shared] and not sw._can(shared)


def test_no_held_kind_keeps_every_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    m, face_of = _two_faces()
    del m.held_kind  # type: ignore[attr-defined]
    monkeypatch.setattr(QP, "STRIP_NODES_MOVE", True)
    sw = _sweep(m, face_of)
    assert np.array_equal(sw.pinned, sw.held)
