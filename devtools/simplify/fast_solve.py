# Responsibility: A fast linear-static solver for the CAD-simplification bench: 10-node tets (CalculiX C3D10 order), isotropic elasticity, prescribed displacements - the same discrete problem CalculiX solves on the same mesh, assembled in parallel (numba) and solved by MKL PARDISO (small systems) or p-multigrid-preconditioned CG in PETSc (large ones).
# Boundaries: a developer tool for devtools/simplify. Nothing in the product imports it. Parses only the deck cards harness.py and local.py write (*NODE, *ELEMENT TYPE=C3D10, *NSET, *ELASTIC, *BOUNDARY); no loads, no other elements, no nonlinearity.
# Collaborates with: devtools/simplify/harness.py (the Meshed mesh it can solve in memory, the deck format, read_field/read_total_force/read_energy whose quantities it returns), devtools/simplify/local.py (per-node prescribed cut displacements), devtools/simplify/bench_solvers.py (proves it against ccx).
"""THE SAME ANSWER AS CALCULIX, FASTER.

    python devtools/simplify/fast_solve.py job.inp [--method auto|pardiso|pmg|gamg] [--npz out.npz]

The discretisation is CalculiX's own, so the two agree to solver precision, not to discretisation
error: C3D10 shape functions, CalculiX's 4-point Gauss rule for the stiffness (exact for straight-sided
tets, and the same quadrature error for curved ones), nodal stress by linear extrapolation of the four
Gauss-point stresses to the corners, midside = mean of its two corners, then the plain mean over the
elements sharing a node (CalculiX extrapolate.f). The strain energy is the Gauss sum of sigma:eps/2
(CalculiX ELSE); the reaction is the nodal internal force (CalculiX RF), read at any node set.

Dirichlet conditions are imposed by ELIMINATION: the constrained rows and columns become a diagonal
(their own stiffness diagonal, so the scaling is kept), their coupling moves to the right-hand side.
The free-dof solution is that of the reduced system exactly; reactions come from the element internal
forces of the solved field, so they are not perturbed by any penalty.

Solvers ("auto" picks by size, AUTO_DIRECT_DOFS): "pardiso" - MKL PARDISO, SPD, all cores, through its
64-bit interface; "pmg" - CG to ||r|| <= rtol ||b|| (default 1e-10) under mpiexec, one rank per physical
core, preconditioned by P2 -> P1 p-multigrid with GAMG on the P1 level (PETSC_PMG). The matrix is
assembled once, straight into /dev/shm, and each rank maps its rows. Kernels are numba-compiled with
cache=True: the first call after an edit compiles (about 20 s); later processes load the cache.

In-memory use, no deck file:

    import fast_solve as F
    r = F.solve_meshed(m, [("NFIX", 1, 3, 0.0), ("NLOAD", d, d, delta)])   # harness.Meshed
    r.total_force(m.sets["NLOAD"]); r.energy; r.u; r.s; r.vm                 # rows follow m.tags

or the array core, `solve(xyz, conn_rows, bc_rows, bc_comp, bc_val, E, nu)`.

Dependencies (pinned on the bench): numpy, numba 0.68.1, scipy, mkl 2026.2.1; for "pmg" also
petsc 3.24.6 + petsc4py 3.24.6 + mpi4py 4.1.1 (PyPI, built against Open MPI), psutil.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numba import njit, prange

E_MPA, NU = 210_000.0, 0.3

# ------------------------------------------------------------------------------------------------
# the element: C3D10 (corners 1-4; midsides 12, 23, 31, 14, 24, 34), CalculiX's 4-point rule

_A, _B = 0.585410196624968, 0.138196601125011
GAUSS = np.array([[_B, _B, _B], [_A, _B, _B], [_B, _A, _B], [_B, _B, _A]])   # (xi, eta, zeta), CalculiX gauss3d5
WEIGHT = 1.0 / 24.0
EDGES = np.array([[0, 1], [1, 2], [2, 0], [0, 3], [1, 3], [2, 3]])           # midside nodes 5..10


def _dshape(xi: float, eta: float, zeta: float) -> np.ndarray:
    """dN/d(xi, eta, zeta) of the ten C3D10 shape functions, (10, 3)."""
    a = 1.0 - xi - eta - zeta
    # barycentric L = (a, xi, eta, zeta); dL/dxi rows
    dl = np.array([[-1.0, -1.0, -1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    lam = np.array([a, xi, eta, zeta])
    out = np.zeros((10, 3))
    for k in range(4):
        out[k] = (4.0 * lam[k] - 1.0) * dl[k]
    for e, (i, j) in enumerate(EDGES):
        out[4 + e] = 4.0 * (lam[i] * dl[j] + lam[j] * dl[i])
    return out


DN = np.ascontiguousarray(np.stack([_dshape(*g) for g in GAUSS]))           # (4, 10, 3)


def _extrapolation(digits: int | None = None) -> np.ndarray:
    """(10, 4): nodal value = X @ Gauss-point values. Corners by the linear field through the four
    Gauss points (exactly: sqrt5 (1 - b) = 1.9270510 on the diagonal, -sqrt5 b = -0.3090170 off it),
    midsides as the mean of their two corners. `digits` rounds the corner weights as CalculiX's
    extrapolate.f writes them (1.92705, -0.30902)."""
    bary = np.column_stack([1.0 - GAUSS.sum(axis=1), GAUSS])                  # (gauss, corner)
    corner = np.linalg.inv(bary)                                              # corner values from gauss values
    if digits is not None:
        corner = np.round(corner, digits)
    mids = np.array([(corner[i] + corner[j]) / 2 for i, j in EDGES])
    return np.ascontiguousarray(np.vstack([corner, mids]))


#: CalculiX's own 5-decimal weights, so the nodal stress is its .frd stress: with the exact weights the
#: max von Mises differs from CalculiX's by 7e-6 to 9e-6 relative (64k and 460k decks), with these by
#: 0.0 (after rounding to the .frd's 6 digits; the whole field within 2.3e-6, the print's own precision)
EXTRAP = _extrapolation(digits=5)
EXTRAP_EXACT = _extrapolation()


# ------------------------------------------------------------------------------------------------
# kernels (numba, parallel; cache=True so a fresh process pays no compile after the first run)

@njit(cache=True)
def _node_elements(conn, n):
    m = conn.shape[0]
    ptr = np.zeros(n + 1, np.int64)
    for e in range(m):
        for a in range(10):
            ptr[conn[e, a] + 1] += 1
    for i in range(n):
        ptr[i + 1] += ptr[i]
    fill = ptr[:-1].copy()
    idx = np.empty(ptr[n], np.int64)
    for e in range(m):
        for a in range(10):
            v = conn[e, a]
            idx[fill[v]] = e
            fill[v] += 1
    return ptr, idx


@njit(parallel=True, cache=True)
def _row_pattern(conn, ne_ptr, ne_idx, n, out_ptr, out_idx, count_only):
    """Block-row sparsity of the node graph: two passes (count, then fill sorted columns)."""
    for i in prange(n):
        k0, k1 = ne_ptr[i], ne_ptr[i + 1]
        buf = np.empty((k1 - k0) * 10, np.int64)
        t = 0
        for k in range(k0, k1):
            e = ne_idx[k]
            for a in range(10):
                buf[t] = conn[e, a]
                t += 1
        buf = np.sort(buf)
        u = 0
        for j in range(t):
            if j == 0 or buf[j] != buf[j - 1]:
                if not count_only:
                    out_idx[out_ptr[i] + u] = buf[j]
                u += 1
        if count_only:
            out_ptr[i + 1] = u


@njit(cache=True)
def _colour(conn, n):
    """Greedy element colouring: no two elements of one colour share a node, so a colour's elements
    scatter into the global matrix (and nodal sums) in parallel without races."""
    m = conn.shape[0]
    words = 8                                   # up to 512 colours
    used = np.zeros((n, words), np.uint64)
    col = np.empty(m, np.int64)
    one = np.uint64(1)
    for e in range(m):
        c = -1
        for w in range(words):
            mask = np.uint64(0)
            for a in range(10):
                mask |= used[conn[e, a], w]
            if mask != np.uint64(0xFFFFFFFFFFFFFFFF):
                for b in range(64):
                    if (mask >> np.uint64(b)) & one == np.uint64(0):
                        c = w * 64 + b
                        break
                break
        if c < 0:
            return col, -1
        col[e] = c
        bit = one << np.uint64(c % 64)
        for a in range(10):
            used[conn[e, a], c // 64] |= bit
    return col, col.max() + 1


@njit(cache=True, inline="always")
def _jacobian(x, dn):
    """grad N (10, 3) and det J at one Gauss point; J[i, j] = d x_j / d xi_i."""
    j = np.zeros((3, 3))
    for a in range(10):
        for i in range(3):
            for k in range(3):
                j[i, k] += dn[a, i] * x[a, k]
    det = (j[0, 0] * (j[1, 1] * j[2, 2] - j[1, 2] * j[2, 1]) - j[0, 1] * (j[1, 0] * j[2, 2] - j[1, 2] * j[2, 0])
           + j[0, 2] * (j[1, 0] * j[2, 1] - j[1, 1] * j[2, 0]))
    inv = np.empty((3, 3))
    inv[0, 0] = (j[1, 1] * j[2, 2] - j[1, 2] * j[2, 1]) / det
    inv[0, 1] = (j[0, 2] * j[2, 1] - j[0, 1] * j[2, 2]) / det
    inv[0, 2] = (j[0, 1] * j[1, 2] - j[0, 2] * j[1, 1]) / det
    inv[1, 0] = (j[1, 2] * j[2, 0] - j[1, 0] * j[2, 2]) / det
    inv[1, 1] = (j[0, 0] * j[2, 2] - j[0, 2] * j[2, 0]) / det
    inv[1, 2] = (j[0, 2] * j[1, 0] - j[0, 0] * j[1, 2]) / det
    inv[2, 0] = (j[1, 0] * j[2, 1] - j[1, 1] * j[2, 0]) / det
    inv[2, 1] = (j[0, 1] * j[2, 0] - j[0, 0] * j[2, 1]) / det
    inv[2, 2] = (j[0, 0] * j[1, 1] - j[0, 1] * j[1, 0]) / det
    g = np.zeros((10, 3))
    # grad_x N_a = J^-1 grad_xi N_a
    for a in range(10):
        for i in range(3):
            g[a, i] = inv[i, 0] * dn[a, 0] + inv[i, 1] * dn[a, 1] + inv[i, 2] * dn[a, 2]
    return g, det


@njit(parallel=True, cache=True)
def _assemble(xyz, conn, dn, w, lam, mu, order, cptr, indptr, indices, data):
    """K as 3x3 blocks on the node graph. Returns the number of elements with det J <= 0."""
    bad = 0
    for c in range(cptr.shape[0] - 1):
        for t in prange(cptr[c], cptr[c + 1]):
            e = order[t]
            x = np.empty((10, 3))
            for a in range(10):
                x[a] = xyz[conn[e, a]]
            ke = np.zeros((10, 10, 3, 3))
            for q in range(dn.shape[0]):
                g, det = _jacobian(x, dn[q])
                if det <= 0.0:
                    bad += 1
                wd = w * det
                for a in range(10):
                    for b in range(10):
                        gg = g[a, 0] * g[b, 0] + g[a, 1] * g[b, 1] + g[a, 2] * g[b, 2]
                        for i in range(3):
                            for j in range(3):
                                v = lam * g[a, i] * g[b, j] + mu * g[a, j] * g[b, i]
                                if i == j:
                                    v += mu * gg
                                ke[a, b, i, j] += wd * v
            for a in range(10):
                r = conn[e, a]
                s0, s1 = indptr[r], indptr[r + 1]
                for b in range(10):
                    p = s0 + np.searchsorted(indices[s0:s1], conn[e, b])
                    for i in range(3):
                        for j in range(3):
                            data[p, i, j] += ke[a, b, i, j]
    return bad


@njit(parallel=True, cache=True)
def _bsr_matvec(indptr, indices, data, x):
    n = indptr.shape[0] - 1
    y = np.zeros((n, 3))
    for r in prange(n):
        a0 = a1 = a2 = 0.0
        for k in range(indptr[r], indptr[r + 1]):
            c = indices[k]
            b = data[k]
            a0 += b[0, 0] * x[c, 0] + b[0, 1] * x[c, 1] + b[0, 2] * x[c, 2]
            a1 += b[1, 0] * x[c, 0] + b[1, 1] * x[c, 1] + b[1, 2] * x[c, 2]
            a2 += b[2, 0] * x[c, 0] + b[2, 1] * x[c, 1] + b[2, 2] * x[c, 2]
        y[r, 0], y[r, 1], y[r, 2] = a0, a1, a2
    return y


@njit(parallel=True, cache=True)
def _eliminate(indptr, indices, data, fixed):
    """Zero the constrained rows and columns, keep their diagonal. Returns that diagonal (n, 3)."""
    n = indptr.shape[0] - 1
    diag = np.zeros((n, 3))
    for r in prange(n):
        for k in range(indptr[r], indptr[r + 1]):
            c = indices[k]
            for i in range(3):
                for j in range(3):
                    if fixed[r, i] or fixed[c, j]:
                        if r == c and i == j:
                            diag[r, i] = data[k, i, j]
                        else:
                            data[k, i, j] = 0.0
    return diag


@njit(parallel=True, cache=True)
def _upper_csr(indptr, indices, data):
    """The upper triangle of the block matrix as scalar CSR (what PARDISO's SPD mode reads)."""
    n = indptr.shape[0] - 1
    cnt = np.zeros(3 * n + 1, np.int64)
    for r in prange(n):
        k0, k1 = indptr[r], indptr[r + 1]
        later = 0
        for k in range(k0, k1):
            if indices[k] > r:
                later += 1
        cnt[3 * r + 1] = 3 * later + 3
        cnt[3 * r + 2] = 3 * later + 2
        cnt[3 * r + 3] = 3 * later + 1
    for i in range(3 * n):
        cnt[i + 1] += cnt[i]
    col = np.empty(cnt[-1], np.int64)
    val = np.empty(cnt[-1])
    for r in prange(n):
        k0, k1 = indptr[r], indptr[r + 1]
        for i in range(3):
            p = cnt[3 * r + i]
            for k in range(k0, k1):
                c = indices[k]
                if c < r:
                    continue
                for j in range(3):
                    if c == r and j < i:
                        continue
                    col[p] = 3 * c + j
                    val[p] = data[k, i, j]
                    p += 1
    return cnt, col, val


@njit(parallel=True, cache=True)
def _block_rows_csr(indptr, indices, data, lo, hi):
    """Block rows lo..hi as scalar CSR (PETSc AIJ, which GAMG needs)."""
    nb = hi - lo
    ptr = np.zeros(3 * nb + 1, np.int64)
    for r in range(nb):
        c = 3 * (indptr[lo + r + 1] - indptr[lo + r])
        ptr[3 * r + 1] = ptr[3 * r] + c
        ptr[3 * r + 2] = ptr[3 * r + 1] + c
        ptr[3 * r + 3] = ptr[3 * r + 2] + c
    col = np.empty(ptr[-1], np.int32)
    val = np.empty(ptr[-1])
    for r in prange(nb):
        for i in range(3):
            p = ptr[3 * r + i]
            for k in range(indptr[lo + r], indptr[lo + r + 1]):
                c = indices[k]
                for j in range(3):
                    col[p] = 3 * c + j
                    val[p] = data[k, i, j]
                    p += 1
    return ptr, col, val


@njit(parallel=True, cache=True)
def _recover(xyz, conn, dn, w, lam, mu, extrap, order, cptr, u, s_sum, n_sum, f_int, energy_e):
    """Gauss-point stress, element energy, nodal internal force, extrapolated nodal stress sums."""
    for c in range(cptr.shape[0] - 1):
        for t in prange(cptr[c], cptr[c + 1]):
            e = order[t]
            x = np.empty((10, 3))
            ue = np.empty((10, 3))
            for a in range(10):
                x[a] = xyz[conn[e, a]]
                ue[a] = u[conn[e, a]]
            sg = np.zeros((dn.shape[0], 6))
            fe = np.zeros((10, 3))
            en = 0.0
            for q in range(dn.shape[0]):
                g, det = _jacobian(x, dn[q])
                h = np.zeros((3, 3))          # displacement gradient h[i, j] = d u_i / d x_j
                for a in range(10):
                    for i in range(3):
                        for j in range(3):
                            h[i, j] += ue[a, i] * g[a, j]
                exx, eyy, ezz = h[0, 0], h[1, 1], h[2, 2]
                gxy, gyz, gzx = h[0, 1] + h[1, 0], h[1, 2] + h[2, 1], h[2, 0] + h[0, 2]
                tr = exx + eyy + ezz
                sxx, syy, szz = lam * tr + 2 * mu * exx, lam * tr + 2 * mu * eyy, lam * tr + 2 * mu * ezz
                sxy, syz, szx = mu * gxy, mu * gyz, mu * gzx
                sg[q, 0], sg[q, 1], sg[q, 2], sg[q, 3], sg[q, 4], sg[q, 5] = sxx, syy, szz, sxy, syz, szx
                wd = w * det
                en += 0.5 * wd * (sxx * exx + syy * eyy + szz * ezz + sxy * gxy + syz * gyz + szx * gzx)
                for a in range(10):
                    fe[a, 0] += wd * (sxx * g[a, 0] + sxy * g[a, 1] + szx * g[a, 2])
                    fe[a, 1] += wd * (sxy * g[a, 0] + syy * g[a, 1] + syz * g[a, 2])
                    fe[a, 2] += wd * (szx * g[a, 0] + syz * g[a, 1] + szz * g[a, 2])
            energy_e[e] = en
            for a in range(10):
                v = conn[e, a]
                n_sum[v] += 1.0
                for k in range(6):
                    acc = 0.0
                    for q in range(dn.shape[0]):
                        acc += extrap[a, q] * sg[q, k]
                    s_sum[v, k] += acc
                for i in range(3):
                    f_int[v, i] += fe[a, i]


# ------------------------------------------------------------------------------------------------
# the solve

@dataclass
class Result:
    u: np.ndarray          # (n, 3) nodal displacement, rows as the input xyz
    s: np.ndarray          # (n, 6) nodal stress sxx syy szz sxy syz szx (CalculiX .frd convention)
    f: np.ndarray          # (n, 3) nodal internal force: the reaction at constrained dofs (CalculiX RF)
    energy: float          # total strain energy (CalculiX ELSE, TOTALS=ONLY)
    method: str = ""
    iterations: int = 0
    residual: float = 0.0  # final ||b - K u|| / ||b|| of the eliminated system
    times: dict = field(default_factory=dict)
    rows: np.ndarray | None = None   # tag -> row lookup (indexed by tag), set by solve_meshed / solve_deck

    @property
    def vm(self) -> np.ndarray:
        sxx, syy, szz, sxy, syz, szx = self.s.T
        return np.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                       + 3 * (sxy ** 2 + syz ** 2 + szx ** 2))

    def total_force(self, nodes) -> np.ndarray:
        """Sum of the nodal reaction over node TAGS (when rows is set) or row indices."""
        idx = np.asarray(nodes, dtype=np.int64)
        return self.f[self.rows[idx] if self.rows is not None else idx].sum(axis=0)


def rigid_body_modes(xyz: np.ndarray, centre=None) -> np.ndarray:
    """(3n, 6): three translations, three rotations about the centroid (the elasticity near-null space)."""
    x = xyz - (xyz.mean(axis=0) if centre is None else centre)
    n = len(x)
    b = np.zeros((n, 3, 6))
    b[:, 0, 0] = b[:, 1, 1] = b[:, 2, 2] = 1.0
    b[:, 1, 3], b[:, 2, 3] = -x[:, 2], x[:, 1]
    b[:, 0, 4], b[:, 2, 4] = x[:, 2], -x[:, 0]
    b[:, 0, 5], b[:, 1, 5] = -x[:, 1], x[:, 0]
    return b.reshape(3 * n, 6)


@njit(parallel=True, cache=True)
def _interleave(q):
    key = np.zeros(q.shape[0], np.uint64)
    one = np.uint64(1)
    for i in prange(q.shape[0]):
        k = np.uint64(0)
        for bit in range(21):
            for ax in range(3):
                k |= ((q[i, ax] >> np.uint64(bit)) & one) << np.uint64(3 * bit + ax)
        key[i] = k
    return key


def _morton(xyz: np.ndarray) -> np.ndarray:
    """Z-order key of each point on a 2^21 grid per axis."""
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    return _interleave(((xyz - lo) / np.maximum(hi - lo, 1e-300) * (2**21 - 1)).astype(np.uint64))


def _physical_cores() -> int:
    """MPI ranks for the PETSc path: one per physical core (the solve is memory-bound; measured on an
    n2-standard-16, 16 ranks on 8 cores were no faster than 8)."""
    try:
        import psutil
        return psutil.cpu_count(logical=False) or os.cpu_count() or 1
    except ImportError:
        return os.cpu_count() or 1


def _set_threads(threads: int | None) -> int:
    import numba
    t = threads or int(os.environ.get("FAST_SOLVE_THREADS", 0)) or (os.cpu_count() or 1)
    t = min(t, numba.config.NUMBA_NUM_THREADS)
    numba.set_num_threads(t)
    return t


def _mkl():
    import ctypes
    import glob
    import site
    for root in (sys.prefix, site.USER_BASE):
        for path in sorted(glob.glob(f"{root}/lib*/**/libmkl_rt.so*", recursive=True), key=len):
            try:
                return ctypes.CDLL(path)
            except OSError:
                continue
    raise ImportError("libmkl_rt not found (pip install mkl)")


class Pardiso64:
    """MKL PARDISO through its 64-bit-index entry point (pardiso_64), called by ctypes: SPD (mtype 2),
    upper triangle, zero-based CSR, parallel nested-dissection ordering. The 32-bit interface (what
    pypardiso wraps) fails with error -2 once the factor passes 2^31 entries."""

    def __init__(self, ptr, col, val, n):
        import ctypes
        self.lib = _mkl()
        i64 = ctypes.c_int64
        self.pt = np.zeros(64, np.int64)
        self.iparm = np.zeros(64, np.int64)
        ip = self.iparm
        ip[0] = 1          # no solver defaults
        ip[1] = 3          # parallel (OpenMP) nested dissection
        ip[7] = 2          # up to two iterative-refinement steps
        ip[9] = 8          # pivot perturbation 1e-8 (unused for SPD)
        ip[23] = 1         # two-level parallel factorisation
        ip[34] = 1         # zero-based indexing
        self.ptr = np.ascontiguousarray(ptr, np.int64)
        self.col = np.ascontiguousarray(col, np.int64)
        self.val = np.ascontiguousarray(val, np.float64)
        self.n = n
        self._c = (i64(1), i64(1), i64(2), i64(n), i64(1), i64(0))   # maxfct mnum mtype n nrhs msglvl
        self.lib.pardiso_64.restype = None
        self._call(11)
        self._call(22)

    def _call(self, phase, b=None, x=None):
        import ctypes
        maxfct, mnum, mtype, n, nrhs, msg = self._c
        err = ctypes.c_int64(0)
        perm = np.zeros(1, np.int64)
        dummy = np.zeros(1)
        b = dummy if b is None else b
        x = dummy if x is None else x
        p = ctypes.c_void_p
        self.lib.pardiso_64(self.pt.ctypes.data_as(p), ctypes.byref(maxfct), ctypes.byref(mnum), ctypes.byref(mtype),
                            ctypes.byref(ctypes.c_int64(phase)), ctypes.byref(n), self.val.ctypes.data_as(p),
                            self.ptr.ctypes.data_as(p), self.col.ctypes.data_as(p), perm.ctypes.data_as(p),
                            ctypes.byref(nrhs), self.iparm.ctypes.data_as(p), ctypes.byref(msg), b.ctypes.data_as(p),
                            x.ctypes.data_as(p), ctypes.byref(err))
        if err.value != 0:
            raise RuntimeError(f"PARDISO phase {phase} failed with error {err.value}")

    def solve(self, b):
        b = np.ascontiguousarray(b, np.float64)
        x = np.zeros_like(b)
        self._call(33, b, x)
        return x

    def free(self):
        self._call(-1)


def _solve_pardiso(blocks, b):
    t0 = time.perf_counter()
    ptr, col, val = _upper_csr(*blocks)
    ps = Pardiso64(ptr, col, val, len(b))
    del ptr, col, val
    t1 = time.perf_counter()
    x = ps.solve(b)
    t2 = time.perf_counter()
    ps.free()
    return x, int(ps.iparm[6]), t1 - t0, t2 - t1      # iterations = refinement steps PARDISO took


#: PETSc options. "pmg": CG on the true (unpreconditioned) residual, preconditioned by a two-level
#: p-multigrid: Chebyshev / 3x3 point-block Jacobi on the quadratic (P2) system, the coarse correction
#: on its Galerkin projection to the linear (P1, corner-node) space by one GAMG V-cycle with the six
#: rigid-body modes. "gamg": the same CG with smoothed aggregation straight on P2 - measured worse at
#: every size (460k tets: 44 iterations but 96 s of setup, against pmg's 41 and 6 s). Both slow down
#: on meshes with slivers (the 64k-tet nist_ftc_09 deck, whose worst tet has a volume quality of 1e-5:
#: 635 pmg iterations), which is one reason small systems are factorised instead.
PETSC_COMMON = {"ksp_type": "cg", "ksp_norm_type": "unpreconditioned"}
PETSC_GAMG = {"pc_type": "gamg", "pc_gamg_type": "agg", "pc_gamg_agg_nsmooths": 1, "pc_gamg_threshold": 0.01,
              "pc_gamg_aggressive_coarsening": 0, "mg_levels_ksp_type": "chebyshev", "mg_levels_pc_type": "jacobi",
              "mg_levels_ksp_max_it": 2, "pc_gamg_coarse_eq_limit": 2000}
PETSC_PMG = {"pc_type": "mg", "pc_mg_levels": 2, "pc_mg_galerkin": "none", "pc_mg_type": "multiplicative",
             "mg_levels_ksp_type": "chebyshev", "mg_levels_pc_type": "pbjacobi", "mg_levels_ksp_max_it": 1,
             "mg_coarse_ksp_type": "preonly"} | {f"mg_coarse_{k}": v for k, v in PETSC_GAMG.items()} | {
             "mg_coarse_pc_gamg_aggressive_coarsening": 1}
# Tuned on the 460k- and 2.1M-tet decks (bench_solvers.py, DEVLOG 2026-11-19): fine Chebyshev degree
# 1 with 3x3 point-block Jacobi is as good per iteration as degree 3 and a third of the cost; the P1
# coarse operator's own GAMG hierarchy densifies level by level (45 s of Galerkin products at 2.1M
# tets) unless the first level coarsens aggressively (11 s).


def _solve_petsc(shm: Path, b, x0, xyz, rtol, maxiter, nproc, cn=None):
    """Run _petsc_worker under mpiexec on the matrix already sitting in shm (indptr/indices/data.npy)."""
    import json
    import subprocess
    np.save(shm / "b.npy", b)
    np.save(shm / "x0.npy", x0)
    np.save(shm / "xyz.npy", xyz)
    if cn is not None:      # pmg: each node's P1 parents (a corner: itself twice; a midside: its edge ends)
        par = np.empty((len(xyz), 2), np.int64)
        par[cn[:, :4].ravel()] = cn[:, :4].ravel()[:, None]
        for e, (i, j) in enumerate(EDGES):
            par[cn[:, 4 + e], 0], par[cn[:, 4 + e], 1] = cn[:, i], cn[:, j]
        np.save(shm / "parents.npy", par)
    extra = os.environ.get("FAST_SOLVE_PETSC", "").split()      # tuning: "-opt value -flag ..."
    more = {}
    for i, t in enumerate(extra):
        if t.startswith("-"):
            nxt = extra[i + 1] if i + 1 < len(extra) and not extra[i + 1].startswith("-") else ""
            more[t.lstrip("-")] = nxt
    (shm / "opts.json").write_text(json.dumps({"rtol": rtol, "maxiter": maxiter} | PETSC_COMMON
                                                 | (PETSC_GAMG if cn is None else PETSC_PMG) | more))
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMBA_NUM_THREADS="1")
    try:   # the PyPI `petsc` wheel's libpetsc finds its own libHYPRE only through the loader path
        import petsc
        lib = str(Path(petsc.get_petsc_dir()) / "lib")
        env["LD_LIBRARY_PATH"] = lib + (os.pathsep + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    except ImportError:
        pass
    cmd = ["mpiexec", "-n", str(nproc), "--bind-to", "core", "--map-by", "core", sys.executable, "-I",
           str(Path(__file__).resolve()), "--petsc-worker", str(shm)]
    if nproc > (len(os.sched_getaffinity(0)) // 2 or 1):
        cmd[3:3] = ["--use-hwthread-cpus", "--oversubscribe"]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError(f"PETSc worker failed: {(p.stderr or p.stdout)[-3000:]}")
    if os.environ.get("FAST_SOLVE_VERBOSE"):
        print(p.stdout, file=sys.stderr)
    meta = json.loads((shm / "out.json").read_text())
    if os.environ.get("FAST_SOLVE_VERBOSE"):
        print(meta, file=sys.stderr)
    # setup = everything but the CG iterations: MPI launch, matrix copy, Galerkin product, AMG setup
    return np.load(shm / "x.npy"), meta["its"], wall - meta["solve"], meta["solve"]


def _petsc_worker(shm: Path) -> int:
    """One MPI rank: a contiguous, nnz-balanced range of block rows of the eliminated K; GAMG + CG."""
    import json

    import petsc4py
    petsc4py.init([])
    from petsc4py import PETSc
    comm = PETSc.COMM_WORLD
    rank, size = comm.getRank(), comm.getSize()
    opts = json.loads((shm / "opts.json").read_text())
    profile = opts.pop("log_view", None) is not None
    if profile:
        PETSc.Log.begin()
    indptr = np.load(shm / "indptr.npy", mmap_mode="r")
    indices = np.load(shm / "indices.npy", mmap_mode="r")
    data = np.load(shm / "data.npy", mmap_mode="r")
    n = len(indptr) - 1
    cuts = np.searchsorted(indptr, np.linspace(0, indptr[-1], size + 1).astype(np.int64))
    cuts[0], cuts[-1] = 0, n
    lo, hi = int(cuts[rank]), int(cuts[rank + 1])
    ip = PETSc.IntType
    t0 = time.perf_counter()
    lip, lind, ldat = _block_rows_csr(indptr, indices, data, lo, hi)
    a = PETSc.Mat().createAIJ(size=((3 * (hi - lo), 3 * n), (3 * (hi - lo), 3 * n)), bsize=3,
                              csr=(lip.astype(ip), lind.astype(ip), ldat), comm=comm)
    del ldat, lind, lip
    tick = {"matrix": time.perf_counter() - t0}
    a.setOption(PETSc.Mat.Option.SPD, True)
    b = a.createVecLeft()
    b.setArray(np.load(shm / "b.npy", mmap_mode="r")[3 * lo:3 * hi])
    x = a.createVecRight()
    x.setArray(np.load(shm / "x0.npy", mmap_mode="r")[3 * lo:3 * hi])
    po = PETSc.Options()
    for k, v in opts.items():
        if k not in ("rtol", "maxiter"):
            po[k] = v
    ksp = PETSc.KSP().create(comm)
    ksp.setOperators(a)
    ksp.setTolerances(rtol=opts["rtol"], atol=0.0, max_it=opts["maxiter"])
    ksp.setInitialGuessNonzero(True)
    ksp.setFromOptions()
    xyz = np.load(shm / "xyz.npy", mmap_mode="r")
    centre = np.asarray(xyz).mean(axis=0)

    def near_null(mat, pts):
        """The six rigid-body modes at pts (the local rows), orthonormalised globally."""
        modes = rigid_body_modes(pts, centre=centre)
        vecs = []
        for k in range(6):
            v = mat.createVecRight()
            v.setArray(np.ascontiguousarray(modes[:, k]))
            for w in vecs:
                v.axpy(-v.dot(w), w)
            v.scale(1.0 / v.norm())
            vecs.append(v)
        mat.setNearNullSpace(PETSc.NullSpace().create(vectors=vecs, comm=comm))

    if (shm / "parents.npy").exists():
        par = np.load(shm / "parents.npy", mmap_mode="r")
        is_vertex = np.asarray(par[:, 0]) == np.arange(n)
        cidx = np.cumsum(is_vertex) - 1                   # coarse (P1) node of each vertex, fine order
        c_lo = int(is_vertex[:lo].sum())
        c_hi = c_lo + int(is_vertex[lo:hi].sum())
        lp = np.asarray(par[lo:hi])
        single = lp[:, 0] == lp[:, 1]
        cols = cidx[lp]                                   # (rows, 2)
        nnz_row = np.where(single, 1, 2)
        pi = np.zeros(3 * (hi - lo) + 1, np.int64)
        pi[1:] = np.cumsum(np.repeat(nnz_row, 3))
        pj = np.empty(pi[-1], np.int64)
        pv = np.empty(pi[-1])
        for k in range(3):
            st = pi[k:-1:3]
            pj[st] = 3 * cols[:, 0] + k
            pv[st] = np.where(single, 1.0, 0.5)
            two = ~single
            pj[st[two] + 1] = 3 * cols[two, 1] + k
            pv[st[two] + 1] = 0.5
        pmat = PETSc.Mat().createAIJ(size=((3 * (hi - lo), 3 * n), (3 * (c_hi - c_lo), 3 * int(is_vertex.sum()))),
                                     csr=(pi.astype(ip), pj.astype(ip), pv), comm=comm)
        pmat.setBlockSizes(3, 3)
        tp = time.perf_counter()
        ac = a.ptap(pmat)
        tick["ptap"] = time.perf_counter() - tp
        ac.setOption(PETSc.Mat.Option.SPD, True)
        near_null(ac, np.asarray(xyz[lo:hi])[is_vertex[lo:hi]])
        pc = ksp.getPC()
        pc.setMGInterpolation(1, pmat)
        coarse = pc.getMGCoarseSolve()
        coarse.setOperators(ac)
        pc.getMGSmoother(1).setOperators(a)
    else:
        near_null(a, np.asarray(xyz[lo:hi]))
    t1 = time.perf_counter()
    ksp.setUp()
    t2 = time.perf_counter()
    ksp.solve(b, x)
    t3 = time.perf_counter()
    if ksp.getConvergedReason() < 0:
        raise RuntimeError(f"KSP diverged: reason {ksp.getConvergedReason()}")
    parts = comm.tompi4py().gather(x.getArray().copy(), root=0)
    if rank == 0:
        np.save(shm / "x.npy", np.concatenate(parts))
        (shm / "out.json").write_text(json.dumps({"its": ksp.getIterationNumber(), "setup": t2 - t0,
                                                  "solve": t3 - t2, "ksp_setup": t2 - t1} | tick))
    if profile:
        PETSc.Log.view()
    return 0


def solve(xyz, conn, bc_rows, bc_comp, bc_val, E: float = E_MPA, nu: float = NU, *, method: str = "auto",
          rtol: float = 1e-10, maxiter: int = 2000, threads: int | None = None) -> Result:
    """Linear static solve on 10-node tets.

    xyz (n, 3); conn (m, 10) ROW indices into xyz in CalculiX C3D10 order; bc_rows / bc_comp (0, 1, 2)
    / bc_val: prescribed displacements (a later entry for the same dof wins, as in a deck). No body
    or surface loads (the bench is displacement-controlled). A node no element uses is held at its
    prescribed value (else 0) and carries no stress, as CalculiX drops it.

    method: "pardiso" (MKL, all cores, direct), "pmg" (PETSc CG + p-multigrid to ||r|| <= rtol ||b||,
    `threads` MPI ranks, default one per physical core), "gamg" (CG + plain GAMG, for comparison), or
    "auto" (pardiso up to AUTO_DIRECT_DOFS, else pmg when petsc4py and mpiexec are there)."""
    times: dict[str, float] = {}
    t0 = time.perf_counter()
    _set_threads(threads)
    xyz_all = np.ascontiguousarray(xyz, dtype=np.float64)
    conn_all = np.ascontiguousarray(conn, dtype=np.int64)
    n_all = len(xyz_all)
    # compact to the nodes elements use, renumbered along a space-filling curve: gmsh numbers the
    # midside nodes after all the corners, which scatters every element's rows across the matrix
    used = np.zeros(n_all, bool)
    used[conn_all.ravel()] = True
    keep = np.flatnonzero(used)
    keep = keep[np.argsort(_morton(xyz_all[keep]), kind="stable")]
    used_rows = keep
    new = np.full(n_all, -1, np.int64)
    new[keep] = np.arange(len(keep))
    cn = new[conn_all]
    xs = np.ascontiguousarray(xyz_all[keep])
    n = len(xs)
    uc_all = np.zeros((n_all, 3))
    fixed_all = np.zeros((n_all, 3), bool)
    bc_rows = np.asarray(bc_rows, dtype=np.int64)
    bc_comp = np.asarray(bc_comp, dtype=np.int64)
    uc_all[bc_rows, bc_comp] = np.asarray(bc_val, dtype=np.float64)   # numpy: the last duplicate wins
    fixed_all[bc_rows, bc_comp] = True
    uc, fixed = np.ascontiguousarray(uc_all[used_rows]), np.ascontiguousarray(fixed_all[used_rows])

    lam = E * nu / ((1 + nu) * (1 - 2 * nu))
    mu = E / (2 * (1 + nu))
    if method == "auto":
        method = "pardiso" if 3 * n <= AUTO_DIRECT_DOFS or not _have_petsc() else "pmg"
    nproc = threads or _physical_cores()
    shm = None
    if method in ("gamg", "pmg"):   # the matrix is built straight into shared memory for the MPI ranks
        import tempfile
        ram = "/dev/shm" if Path("/dev/shm").is_dir() else None   # noqa: S108  (mkdtemp: a private 0700 dir)
        shm = Path(tempfile.mkdtemp(prefix="fast_solve_", dir=ram))
    try:
        def alloc(name, shape, dtype):
            if shm is None:
                return np.zeros(shape, dtype)
            return np.lib.format.open_memmap(shm / f"{name}.npy", mode="w+", dtype=dtype, shape=shape)

        ne_ptr, ne_idx = _node_elements(cn, n)
        counts = np.zeros(n + 1, np.int64)
        _row_pattern(cn, ne_ptr, ne_idx, n, counts, np.empty(0, np.int64), True)
        indptr = alloc("indptr", (n + 1,), np.int64)
        np.cumsum(counts, out=indptr)
        idx_type = np.int32 if indptr[-1] < 2**31 else np.int64
        indices = alloc("indices", (int(indptr[-1]),), idx_type)
        _row_pattern(cn, ne_ptr, ne_idx, n, indptr, indices, False)
        del ne_ptr, ne_idx, counts
        col, ncol = _colour(cn, n)
        if ncol < 0:
            raise RuntimeError("element colouring needs more than 512 colours")
        order = np.argsort(col, kind="stable")
        cptr = np.searchsorted(col[order], np.arange(ncol + 1))
        times["pattern"] = time.perf_counter() - t0
        t1 = time.perf_counter()
        data = alloc("data", (int(indptr[-1]), 3, 3), np.float64)
        bad = _assemble(xs, cn, DN, WEIGHT, lam, mu, order, cptr, indptr, indices, data)
        if bad:
            raise ValueError(f"{bad} Gauss points with non-positive Jacobian")
        # elimination: b = -K u_c on the free dofs, diag * u_c on the constrained ones
        b = -_bsr_matvec(indptr, indices, data, uc)
        diag = _eliminate(indptr, indices, data, fixed)
        if np.any(diag[fixed] <= 0):
            raise ValueError("a constrained dof has no stiffness")
        b[fixed] = diag[fixed] * uc[fixed]
        b = b.ravel()
        times["assemble"] = time.perf_counter() - t1
        x0 = uc.ravel().copy()
        if method in ("gamg", "pmg"):
            data.flush()
            x, its, ts, tv = _solve_petsc(shm, b, x0, xs, rtol, maxiter, nproc, cn if method == "pmg" else None)
        elif method == "pardiso":
            x, its, ts, tv = _solve_pardiso((indptr, indices, data), b)
        else:
            raise ValueError(f"unknown method {method}")
        times["setup"], times["solve"] = ts, tv
        t2 = time.perf_counter()
        r = b - _bsr_matvec(indptr, indices, data, x.reshape(n, 3)).ravel()
        resid = float(np.linalg.norm(r) / max(np.linalg.norm(b), 1e-300))
        del data, indices
    finally:
        if shm is not None:
            import shutil
            shutil.rmtree(shm, ignore_errors=True)
    u = x.reshape(n, 3).copy()
    u[fixed] = uc[fixed]
    s_sum, n_sum = np.zeros((n, 6)), np.zeros(n)
    f_int, en = np.zeros((n, 3)), np.zeros(len(cn))
    _recover(xs, cn, DN, WEIGHT, lam, mu, EXTRAP, order, cptr, u, s_sum, n_sum, f_int, en)
    u_all, s_all, f_all = uc_all.copy(), np.zeros((n_all, 6)), np.zeros((n_all, 3))
    u_all[used_rows], s_all[used_rows], f_all[used_rows] = u, s_sum / n_sum[:, None], f_int
    times["recover"] = time.perf_counter() - t2
    times["total"] = time.perf_counter() - t0
    return Result(u_all, s_all, f_all, float(en.sum()), method, its, resid, times)


#: up to this many dofs "auto" factorises (MKL PARDISO), above it p-multigrid CG (PETSc). Measured
#: (DEVLOG 2026-11-19, fast_solve): 0.36M dofs PARDISO 5.6 s vs pmg 24 s (a mesh with slivers: 635 CG
#: iterations); 2.4M dofs PARDISO 50 s and 23 GB vs pmg 20 s and 8 GB; 8.9M dofs PARDISO needs a
#: 190 GB factor, pmg 86 s.
AUTO_DIRECT_DOFS = 1_000_000


def _have_petsc() -> bool:
    import importlib.util
    import shutil
    return importlib.util.find_spec("petsc4py") is not None and shutil.which("mpiexec") is not None


def _rows_of(tags) -> np.ndarray:
    """tag -> row, as an array indexed by tag (-1 for no such node)."""
    tags = np.asarray(tags, dtype=np.int64)
    lut = np.full(int(tags.max()) + 1, -1, np.int64)
    lut[tags] = np.arange(len(tags))
    return lut


def _expand(boundary, sets: dict, lut: np.ndarray):
    """*BOUNDARY-style entries (nodes_or_set_name, first_dof, last_dof, value) -> row, comp, value arrays."""
    rows, comps, vals = [], [], []
    for nodes, first, last, value in boundary:
        tags = np.asarray(sets[nodes] if isinstance(nodes, str) else np.atleast_1d(nodes), dtype=np.int64)
        r = lut[tags]
        if np.any(r < 0):
            raise KeyError(f"unknown node in boundary entry {nodes!r}")
        for d in range(int(first), int(last) + 1):
            rows.append(r)
            comps.append(np.full(len(r), d - 1))
            vals.append(np.broadcast_to(np.asarray(value, dtype=np.float64), r.shape))
    if not rows:
        return np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0)
    return np.concatenate(rows), np.concatenate(comps), np.concatenate(vals)


def solve_meshed(m, boundary, E: float = E_MPA, nu: float = NU, **kw) -> Result:
    """Solve a harness.Meshed (tags, xyz, conn in tags, sets) directly. `boundary` mirrors *BOUNDARY
    cards: (set name | node tag | array of tags, first dof 1-3, last dof, value or array of values per
    node). Per-node values (local.py's cut faces) go as one entry per dof: (tags, d, d, values)."""
    lut = _rows_of(m.tags)
    br, bc, bv = _expand(boundary, m.sets, lut)
    r = solve(m.xyz, lut[np.asarray(m.conn, dtype=np.int64)], br, bc, bv, E, nu, **kw)
    r.rows = lut
    return r


# ------------------------------------------------------------------------------------------------
# the deck path

@dataclass
class Deck:
    tags: np.ndarray
    xyz: np.ndarray
    conn: np.ndarray            # node tags, C3D10 order
    sets: dict                  # name -> node tags
    E: float
    nu: float
    boundary: list              # (nodes, first, last, value), deck order (pre-step first)
    print_sets: list            # *NODE PRINT NSET= names, in order


def _numbers(lines: list[str]) -> np.ndarray:
    return np.array(" ".join(lines).replace(",", " ").split(), dtype=np.float64)


def read_deck(path: str | Path) -> Deck:
    """Parse the cards harness.run_ccx and local._solve_piece write; refuse anything it cannot honour."""
    text = Path(path).read_text()
    blocks = re.split(r"^(\*[^\n]*)\n", text, flags=re.M)
    nodes, elems, sets, boundary, prints = [], [], {}, [], []
    E = nu = None
    for k in range(1, len(blocks), 2):
        head = blocks[k].upper().replace(" ", "")
        body = [ln for ln in blocks[k + 1].splitlines() if ln.strip() and not ln.startswith("**")]
        kw = head.split(",")[0]
        opts = dict(p.split("=", 1) for p in head.split(",")[1:] if "=" in p)
        if kw == "*NODE" and body:
            nodes.append(_numbers(body).reshape(-1, 4))
        elif kw == "*ELEMENT":
            if opts.get("TYPE") != "C3D10":
                raise ValueError(f"only C3D10 elements: {blocks[k]}")
            elems.append(_numbers(body).reshape(-1, 11))
        elif kw == "*NSET":
            name = re.search(r"NSET\s*=\s*([^,\s]+)", blocks[k], re.I).group(1)
            sets[name] = _numbers(body).astype(np.int64)
        elif kw == "*ELASTIC":
            E, nu = (float(v) for v in body[0].split(",")[:2])
        elif kw == "*BOUNDARY":
            for ln in body:
                p = [v.strip() for v in ln.split(",")]
                first = int(p[1])
                last = int(p[2]) if len(p) > 2 and p[2] else first
                value = float(p[3]) if len(p) > 3 and p[3] else 0.0
                target = int(p[0]) if p[0].lstrip("-").isdigit() else p[0]
                boundary.append((target, first, last, value))
        elif kw == "*NODEPRINT":
            prints.append(re.search(r"NSET\s*=\s*([^,\s]+)", blocks[k], re.I).group(1))
        elif kw in ("*CLOAD", "*DLOAD", "*DSLOAD", "*EQUATION", "*MPC", "*CONTACTPAIR", "*SPRING", "*DYNAMIC",
                    "*FREQUENCY", "*BUCKLE", "*TEMPERATURE"):
            raise ValueError(f"card not supported: {blocks[k]}")
    nd = np.vstack(nodes)
    el = np.vstack(elems)
    if E is None:
        raise ValueError("no *ELASTIC card")
    return Deck(nd[:, 0].astype(np.int64), nd[:, 1:], el[:, 1:].astype(np.int64), sets, E, nu, boundary, prints)


def solve_deck(path: str | Path, **kw) -> tuple[Result, Deck]:
    t0 = time.perf_counter()
    d = read_deck(path)
    t_read = time.perf_counter() - t0
    # a node-set name or a single node tag; set names may be given in any case in a deck
    upper = {k.upper(): v for k, v in d.sets.items()}
    bnd = [(upper[t.upper()] if isinstance(t, str) else t, a, b, v) for t, a, b, v in d.boundary]
    lut = _rows_of(d.tags)
    br, bc, bv = _expand(bnd, {}, lut)
    r = solve(d.xyz, lut[d.conn], br, bc, bv, d.E, d.nu, **kw)
    r.rows = lut
    r.times = {"read": t_read} | r.times
    return r, d


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--petsc-worker"]:
        return _petsc_worker(Path(argv[1]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("deck")
    ap.add_argument("--method", default="auto", choices=("auto", "pardiso", "pmg", "gamg"))
    ap.add_argument("--rtol", type=float, default=1e-10)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--npz", help="write tags, xyz, u, s, f here")
    a = ap.parse_args(argv)
    r, d = solve_deck(a.deck, method=a.method, rtol=a.rtol, threads=a.threads)
    for name in d.print_sets:
        fx, fy, fz = r.total_force({k.upper(): v for k, v in d.sets.items()}[name.upper()])
        print(f" total force (fx,fy,fz) for set {name}\n        {fx:.9e} {fy:.9e} {fz:.9e}")
    print(f" total internal energy\n        {r.energy:.9e}")
    print(f" method {r.method}, {r.iterations} iterations, residual {r.residual:.2e}, "
          + ", ".join(f"{k} {v:.2f}s" for k, v in r.times.items()))
    if a.npz:
        np.savez(a.npz, tags=d.tags, xyz=d.xyz, u=r.u, s=r.s, f=r.f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
