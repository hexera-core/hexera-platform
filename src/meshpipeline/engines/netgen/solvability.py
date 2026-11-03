# Responsibility: Decide whether a Netgen polyMesh is solvable: checkMesh's fatal classes, then the FV pressure-Poisson operator solved with PCG + AMG, one reference pressure per connected passage.
# Boundaries: cfMesh's solve, reused; what this adds is the passage count read off the mesh itself - Netgen meshes every separate passage of a staged boundary in one fill (a left and a right carotid tree), where cfMesh meshes each as its own case and records them in passages.json.
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_ENTRY_BODY = re.compile(r"^\s*([A-Za-z_][^\s{}]*)\s*\n\s*\{([^}]*)\}", re.M)


def _patches(pm: Path) -> list[tuple[str, str, int, int]]:
    """(name, type, startFace, nFaces) of every boundary patch."""
    out = []
    for m in _ENTRY_BODY.finditer((pm / "boundary").read_text(errors="replace")):
        name, body = m.group(1), m.group(2)
        if name == "FoamFile":
            continue
        t = re.search(r"\btype\s+(\w+)\s*;", body)
        s = re.search(r"\bstartFace\s+(\d+)\s*;", body)
        n = re.search(r"\bnFaces\s+(\d+)\s*;", body)
        if s and n:
            out.append((name, t.group(1) if t else "", int(s.group(1)), int(n.group(1))))
    return out


def passages_of(owner: np.ndarray, neigh: np.ndarray, n_cells: int, pm: Path) -> tuple[int, int]:
    """(connected regions, regions with no opening) of the mesh: a region every boundary face of
    which is wall has no flow through it - an orphan, which no reference pressure makes solvable."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    nf = int(neigh.size)
    g = coo_matrix((np.ones(nf), (owner[:nf], neigh)), shape=(n_cells, n_cells))
    n, lab = connected_components(g, directed=False)
    if n <= 1:
        return n, 0
    opened = np.zeros(n, dtype=bool)
    for _name, ptype, start, count in _patches(pm):
        if ptype == "wall" or count <= 0:
            continue
        opened[lab[owner[start:start + count]]] = True
    return int(n), int((~opened).sum())


def check_solvability(workspace, metrics_out: dict | None = None) -> tuple[bool, str]:
    from meshpipeline.engines.cfmesh import solvability as S
    from meshpipeline.engines.cfmesh.foam_exec import check_mesh
    if metrics_out is None:
        metrics_out = {}
    metrics_out.update({"n_cells": None, "iters": None, "residual": None, "info": None})
    ws = Path(workspace)
    pm = ws / "constant" / "polyMesh"
    if not (pm / "owner").exists():
        return False, "[UNSOLVABLE] constant/polyMesh/owner is missing - Netgen produced no mesh"
    q = check_mesh(ws)
    if q.get("fatal"):
        return False, (f"[UNSOLVABLE] checkMesh reports fatal topology defects "
                       f"({', '.join(q['fatal'])}) in the polyMesh.")
    owner = S._read_foam_labels(pm / "owner")
    neigh = S._read_foam_labels(pm / "neighbour")
    if owner is None or neigh is None or owner.size == 0 or neigh.size == 0:
        logger.warning("netgen solvability: owner/neighbour unparseable - checkMesh-only PASS")
        return True, ""
    owner = np.asarray(owner, dtype=np.int64)
    neigh = np.asarray(neigh, dtype=np.int64)
    n_cells = int(max(int(owner.max()), int(neigh.max())) + 1)
    metrics_out["n_cells"] = n_cells
    regions, orphans = passages_of(owner, neigh, n_cells, pm)
    metrics_out["passages"] = regions
    if orphans:
        return False, (f"[UNSOLVABLE] {orphans} of the mesh's {regions} separate regions have no "
                       "inlet or outlet - fluid shut in a closed pocket has no flow and no pressure "
                       "level, so the pressure equation cannot be solved there.")
    return S._solve_fv_laplacian(owner[:neigh.size], neigh, n_cells, metrics_out,
                                 passages=max(1, regions))
