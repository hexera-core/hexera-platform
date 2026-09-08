# Responsibility: Decide whether a cfMesh case is solvable enough to be worth meshing.
# Boundaries: a pre-mesh judgement.
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pyamg
import scipy.sparse.linalg as spla

logger = logging.getLogger(__name__)

_REQUIRED_REDUCTION = 1.0e-6

_MAX_ITERS = 5000


def check_solvability(workspace: Path, metrics_out: dict | None = None) -> tuple[bool, str]:
    # When a dict is supplied, residual/iters/n_cells/info are written into
    # it so the executor can persist the solvability curve, not just pass/fail.
    # cfMesh produces a native OpenFOAM polyMesh (the solver mesh). We verify it
    # with checkMesh (fatal topology) AND by assembling + solving the discrete
    # pressure-Poisson operator (FV graph Laplacian) with PCG + AMG.
    if metrics_out is None:
        metrics_out = {}
    metrics_out.update({"n_cells": None, "iters": None, "residual": None, "info": None})
    workspace = Path(workspace)
    if (workspace / "constant" / "polyMesh" / "owner").exists():
        return _check_polymesh_solvability(workspace, metrics_out)
    return False, ("[UNSOLVABLE] constant/polyMesh/owner is missing - the builder "
                   "did not produce a valid cfMesh volume mesh")


_FOAM_LIST_RE = re.compile(r"(?:^|\n)\s*(\d+)\s*\n\s*\(", re.MULTILINE)


def _read_foam_labels(path: Path):
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if "format" in text and "binary" in text.split("FoamFile", 1)[-1][:400]:
        return None  # binary polyMesh - parser only handles ASCII
    m = _FOAM_LIST_RE.search(text)
    if not m:
        return None
    start = m.end()  # just past the '('
    end = text.find(")", start)
    if end < 0:
        return None
    try:
        return np.fromstring(text[start:end], dtype=np.int64, sep=" ")
    except Exception:
        return None


def _check_polymesh_solvability(workspace: Path, metrics_out: dict) -> tuple[bool, str]:
    # One checkMesh implementation for the whole app (env-scrubbed, dict-scanned,
    # fatal-marker parsing): the shared runner helper - not a second subprocess here.
    from meshpipeline.engines.cfmesh.foam_exec import check_mesh
    try:
        q = check_mesh(workspace)
    except Exception as exc:
        return False, (
            f"[UNSOLVABLE] checkMesh could not run on the polyMesh: "
            f"{type(exc).__name__}: {exc}")

    if q.get("max_non_ortho") is not None:
        metrics_out["info"] = f"max_non_ortho={q['max_non_ortho']}"

    fatal = q.get("fatal", [])
    if fatal:
        return False, (
            f"[UNSOLVABLE] checkMesh reports fatal topology defects "
            f"({', '.join(fatal)}) in the polyMesh - no solver or scheme can "
            f"integrate over cells with these errors.")

    # : assemble + solve the FV pressure-Poisson operator  #
    pm = workspace / "constant" / "polyMesh"
    owner = _read_foam_labels(pm / "owner")
    neigh = _read_foam_labels(pm / "neighbour")
    if owner is None or neigh is None or owner.size == 0 or neigh.size == 0:
        # Could not parse connectivity (binary mesh, etc.) - fall back to the
        # checkMesh verdict rather than inventing a failure. This DEGRADES the gate
        # to fatal-topology-only, so WARN (the case controlDict should pin
        # writeFormat ascii so this path is not taken).
        logger.warning(
            "solvability(polyMesh): owner/neighbour unparseable (binary mesh?) - "
            "FV pressure-Poisson solve SKIPPED, falling back to checkMesh-only PASS")
        return True, ""

    n_cells = int(max(int(owner.max()), int(neigh.max())) + 1)
    metrics_out["n_cells"] = n_cells
    # internal faces connect owner[f] <-> neighbour[f]
    nf = int(neigh.size)
    oi = owner[:nf]
    return _solve_fv_laplacian(oi, neigh, n_cells, metrics_out)


def _assemble_pinned_laplacian(owner, neigh, n_cells: int):
    """The FV graph Laplacian L = D - A (A[i,j] = A[j,i] = 1 per internal face, duplicates
    summed) with cell 0 pinned as the reference pressure: row 0 and column 0 cleared, L[0,0] = 1,
    so the pure-Neumann operator is SPD. Every other diagonal keeps its full degree.

    Assembled ONCE, straight into CSR with 32-bit indices. The previous route went COO -> CSR ->
    +diag -> CSR -> LIL -> CSR, and the LIL copy - two Python lists per row, ~50 M Python objects
    on shell_tube_bundle_009's 7.3 M-cell mesh - is what took the worker past its 8 GiB limit;
    the kernel killed it (WorkerLostError) and the job sat 'running' for four hours.
    """
    import scipy.sparse as sp

    idx = np.int32 if n_cells < 2**31 - 1 else np.int64
    owner = np.asarray(owner, dtype=np.int64)
    neigh = np.asarray(neigh, dtype=np.int64)
    # Degree counts every internal face, including those on cell 0's row/column: pinning clears
    # cell 0's couplings, not its neighbours' diagonals.
    deg = (np.bincount(owner, minlength=n_cells) + np.bincount(neigh, minlength=n_cells)).astype(np.float64)
    deg[0] = 1.0
    keep = (owner != 0) & (neigh != 0)
    o = owner[keep].astype(idx, copy=False)
    n = neigh[keep].astype(idx, copy=False)
    diag = np.arange(n_cells, dtype=idx)
    rows = np.concatenate([o, n, diag])
    cols = np.concatenate([n, o, diag])
    del o, n, diag
    data = np.concatenate([np.full(rows.size - n_cells, -1.0), deg])
    del deg
    L = sp.csr_matrix((data, (rows, cols)), shape=(n_cells, n_cells))
    del rows, cols, data
    L.sum_duplicates()
    return L


def _solve_fv_laplacian(owner, neigh, n_cells: int, metrics_out: dict) -> tuple[bool, str]:
    import scipy.sparse as sp

    if n_cells < 2:
        return True, ""  # trivial mesh; nothing to solve
    # cfMesh/OpenFOAM may store `neighbour` as length-nFaces with -1 marking
    # boundary faces (which create no cell-cell coupling). Keep only faces whose
    # owner AND neighbour are real interior cells in [0, n_cells); otherwise a -1
    # index detonates coo_matrix ("negative axis index").
    owner = np.asarray(owner, dtype=np.int64)
    neigh = np.asarray(neigh, dtype=np.int64)
    valid = (owner >= 0) & (neigh >= 0) & (owner < n_cells) & (neigh < n_cells)
    if not bool(valid.all()):
        owner = owner[valid]
        neigh = neigh[valid]
    if owner.size == 0:
        return True, ""  # no internal connectivity to test (degenerate)
    L = _assemble_pinned_laplacian(owner, neigh, n_cells)
    del owner, neigh

    rng = np.random.default_rng(0)
    b = rng.random(n_cells)
    b[0] = 0.0
    b_norm = float(np.linalg.norm(b))
    if b_norm <= 0.0:
        return False, "[UNSOLVABLE] internal error: zero RHS norm"

    try:
        ml = pyamg.smoothed_aggregation_solver(L)
        M = ml.aspreconditioner()
    except Exception as exc:
        logger.exception("solvability(polyMesh): pyamg setup failed")
        return False, (
            f"[UNSOLVABLE] AMG preconditioner setup failed on the FV pressure "
            f"operator: {type(exc).__name__}: {exc}. The cell-connectivity graph "
            f"is too degenerate to coarsen - typically a disconnected/orphan region.")

    iters_count = [0]
    try:
        # A singular operator (disconnected region) makes CG diverge - silence the
        # expected overflow/invalid-value warnings; we detect non-convergence below.
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            x, info = spla.cg(L, b, rtol=_REQUIRED_REDUCTION, maxiter=_MAX_ITERS, M=M,
                              callback=lambda _xk: iters_count.__setitem__(0, iters_count[0] + 1))
            final_residual = float(np.linalg.norm(L @ x - b) / b_norm)
    except Exception as exc:
        logger.exception("solvability(polyMesh): PCG raised")
        return False, f"[UNSOLVABLE] PCG raised on the FV pressure operator: {type(exc).__name__}: {exc}"

    iters = iters_count[0]
    if not np.isfinite(final_residual):
        final_residual = 1e30  # JSON-safe sentinel for a diverged (singular) solve
    converged = (info == 0) and (final_residual < _REQUIRED_REDUCTION * 10)
    metrics_out.update({"iters": iters, "residual": final_residual, "info": int(info)})

    if converged:
        logger.info("solvability(polyMesh): PASS - n_cells=%d iters=%d residual=%.2e",
                    n_cells, iters, final_residual)
        return True, ""
    logger.info("solvability(polyMesh): REJECT - n_cells=%d iters=%d residual=%.2e info=%d",
                n_cells, iters, final_residual, info)
    return False, (
        f"[UNSOLVABLE] The discrete pressure-Poisson operator assembled from the "
        f"polyMesh did not converge under PCG+AMG (residual {final_residual:.2e}, "
        f"target {_REQUIRED_REDUCTION:.0e}, {iters} iters over {n_cells} cells). "
        f"This usually means a disconnected region or near-singular connectivity. "
        f"Check for orphan cells / multiple mesh regions and rebuild with a single "
        f"connected domain.")


