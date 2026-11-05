# Responsibility: Verify the discrete pressure-Poisson operator rejects a disconnected mesh and malformed labels.
from __future__ import annotations

from pathlib import Path

import numpy as np

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines.cfmesh import solvability as S  # noqa: E402


def _chain(n: int):
    owner = np.arange(0, n - 1, dtype=np.int64)
    neigh = np.arange(1, n, dtype=np.int64)
    return owner, neigh


def test_connected_chain_is_solvable():
    owner, neigh = _chain(50)
    metrics: dict = {}
    ok, diag = S._solve_fv_laplacian(owner, neigh, 50, metrics)
    assert ok, diag
    # _solve_fv_laplacian records the solve metrics (n_cells is set by the caller).
    assert metrics.get("iters") is not None
    assert metrics.get("residual") is not None


def test_boundary_face_neighbours_do_not_crash():
    # cfMesh/OpenFOAM may write `neighbour` as length-nFaces with -1 for boundary
    # faces. Those -1 indices used to detonate coo_matrix ("negative axis 0 index").
    # The solver must drop them and still solve the interior connectivity.
    owner, neigh = _chain(50)
    owner = np.concatenate([owner, np.array([0, 10, 49], dtype=np.int64)])   # 3 boundary faces
    neigh = np.concatenate([neigh, np.array([-1, -1, -1], dtype=np.int64)])  # neighbour = -1
    metrics: dict = {}
    ok, diag = S._solve_fv_laplacian(owner, neigh, 50, metrics)
    assert ok, diag
    assert metrics.get("residual") is not None


def test_out_of_range_indices_are_dropped():
    # An index >= n_cells must also be filtered rather than crash/silently corrupt.
    owner, neigh = _chain(10)
    owner = np.concatenate([owner, np.array([0], dtype=np.int64)])
    neigh = np.concatenate([neigh, np.array([9999], dtype=np.int64)])
    metrics: dict = {}
    ok, diag = S._solve_fv_laplacian(owner, neigh, 10, metrics)
    assert ok, diag


def test_disconnected_mesh_is_rejected():
    # Two separate chains [0..24] and [25..49] with NO face joining them.
    o1, n1 = _chain(25)                      # cells 0..24
    o2, n2 = _chain(25)
    o2 = o2 + 25                              # cells 25..49
    n2 = n2 + 25
    owner = np.concatenate([o1, o2])
    neigh = np.concatenate([n1, n2])
    metrics: dict = {}
    ok, diag = S._solve_fv_laplacian(owner, neigh, 50, metrics)
    assert not ok
    assert "[UNSOLVABLE]" in diag


def test_read_foam_labels_parses_ascii(tmp_path):
    p = tmp_path / "owner"
    p.write_text(
        "FoamFile\n{ version 2.0; format ascii; class labelList; object owner; }\n"
        "6\n(\n0 0 1 1 2 2\n)\n"
    )
    arr = S._read_foam_labels(p)
    assert arr is not None
    assert list(arr) == [0, 0, 1, 1, 2, 2]


def test_read_foam_labels_rejects_binary(tmp_path):
    p = tmp_path / "owner"
    p.write_text("FoamFile\n{ version 2.0; format binary; class labelList; }\n10\n(\n")
    assert S._read_foam_labels(p) is None
