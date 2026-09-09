# Responsibility: the FV Laplacian is assembled once, in CSR, with the reference cell pinned -
# never through a LIL copy (shell_tube_bundle_009: 7.3 M cells, the LIL copy killed the worker).
from __future__ import annotations

import inspect

import numpy as np
import pytest
import scipy.sparse as sp

from meshpipeline.engines.cfmesh import solvability as CF
from meshpipeline.engines.snappy import solvability as SN


def _reference(owner, neigh, n_cells):
    # The construction this replaces, kept here as the oracle: COO -> CSR -> +diag -> LIL pin.
    rows = np.concatenate([owner, neigh]); cols = np.concatenate([neigh, owner])
    A = sp.coo_matrix((np.full(rows.size, -1.0), (rows, cols)), shape=(n_cells, n_cells)).tocsr()
    deg = np.asarray(-A.sum(axis=1)).ravel()
    L = (sp.diags(deg) + A).tolil()
    L[0, :] = 0; L[:, 0] = 0; L[0, 0] = 1.0
    return L.tocsr()


def _random_graph(seed, n_cells=60, n_faces=150):
    rng = np.random.default_rng(seed)
    owner = rng.integers(0, n_cells, n_faces); neigh = rng.integers(0, n_cells, n_faces)
    keep = owner != neigh
    return owner[keep], neigh[keep]


@pytest.mark.parametrize("mod", [CF, SN], ids=["cfmesh", "snappy"])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_csr_assembly_equals_the_lil_pinned_operator(mod, seed):
    owner, neigh = _random_graph(seed)
    L = mod._assemble_pinned_laplacian(owner, neigh, 60)
    R = _reference(owner, neigh, 60)
    assert sp.issparse(L) and L.format == "csr"
    assert abs(L - R).max() == 0.0
    assert L.indices.dtype == np.int32


@pytest.mark.parametrize("mod", [CF, SN], ids=["cfmesh", "snappy"])
def test_duplicate_faces_between_two_cells_sum_like_before(mod):
    owner = np.array([0, 1, 1, 2]); neigh = np.array([1, 2, 2, 3])   # cells 1-2 share two faces
    L = mod._assemble_pinned_laplacian(owner, neigh, 4)
    R = _reference(owner, neigh, 4)
    assert abs(L - R).max() == 0.0
    assert L[1, 2] == -2.0 and L[1, 1] == 3.0


@pytest.mark.parametrize("mod", [CF, SN], ids=["cfmesh", "snappy"])
def test_reference_cell_is_pinned(mod):
    owner, neigh = _random_graph(7)
    L = mod._assemble_pinned_laplacian(owner, neigh, 60).toarray()
    assert L[0, 0] == 1.0
    assert not L[0, 1:].any() and not L[1:, 0].any()
    assert np.allclose(L, L.T)


@pytest.mark.parametrize("mod", [CF, SN], ids=["cfmesh", "snappy"])
def test_the_solve_still_passes_a_connected_chain_and_rejects_a_split_one(mod):
    o = np.arange(0, 49); n = np.arange(1, 50)
    ok, diag = mod._solve_fv_laplacian(o, n, 50, {})
    assert ok, diag
    o2 = np.concatenate([np.arange(0, 24), np.arange(25, 49)])
    n2 = np.concatenate([np.arange(1, 25), np.arange(26, 50)])
    ok, diag = mod._solve_fv_laplacian(o2, n2, 50, {})
    assert not ok and "[UNSOLVABLE]" in diag


def test_no_lil_conversion_remains():
    for mod in (CF, SN):
        assert "tolil" not in inspect.getsource(mod)


def test_the_two_engine_copies_stay_identical():
    # engine-adapter seam: cfmesh and snappy each own a copy; a fix must land in both
    assert inspect.getsource(CF._assemble_pinned_laplacian) == inspect.getsource(SN._assemble_pinned_laplacian)
    assert inspect.getsource(CF._solve_fv_laplacian) == inspect.getsource(SN._solve_fv_laplacian)
