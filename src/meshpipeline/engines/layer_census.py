# Responsibility: Count, on any OpenFOAM polyMesh, how many near-wall prism layers each wall face actually got - an engine-neutral measure for meshers that do not report it themselves.
# Boundaries: reads an ASCII polyMesh (points, faces, owner, neighbour, boundary); it meshes nothing. Above CENSUS_MAX_CELLS, or on a binary mesh, it measures nothing and says so.
# Collaborates with: engines/cfmesh/finalize.py (cfMesh writes no layer log), the manifest's layer_coverage_pct that the reviewer and the report read.
"""HOW MANY LAYERS DID EACH WALL FACE GET?

snappyHexMesh prints its layer coverage; cfMesh does not, so a cfMesh mesh shipped with no layer
figure and the reviewer could not judge the layers it asked for. This walks from every wall face
into the mesh: a cell counts as a layer cell while it is flat against the wall (height under
FLAT_RATIO of its width) and its far face lies parallel to the face it was entered through - the
shape an extruded prism layer has and a background cell does not. Reported as snappy reports it:
the share of the requested layer cells that exist (coverage_pct), plus area shares.

On the HOME-TURF lab (2026-10-05) it read cfMesh's internal meshes at 99.9% of the wall area with a
layer and 98-99.6% with both requested layers.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: a layer cell's height against the square root of its wall face's area
FLAT_RATIO = 0.6
#: |cos| between the far face and the wall face for the cell to be a layer
PARALLEL_COS = 0.8
#: meshes above this are not walked (time and memory in the finalize step)
CENSUS_MAX_CELLS = 4_000_000

_ASCII = re.compile(rb"format\s+ascii\s*;")
_FACE = re.compile(rb"(\d+)\s*\(([^)]*)\)")


def _body(path: Path) -> bytes:
    raw = path.read_bytes()
    if not _ASCII.search(raw[:4096]):
        raise ValueError(f"{path.name}: not an ascii foam file")
    raw = re.sub(rb"/\*.*?\*/", b" ", raw, flags=re.S)
    raw = re.sub(rb"//[^\n]*", b" ", raw)
    i = raw.find(b"FoamFile")
    if i >= 0:
        raw = raw[raw.find(b"}", i) + 1:]
    return raw


def _list_start(t: bytes) -> tuple[int, int]:
    i = t.find(b"(")
    return int(t[:i].split()[-1]), i


def _labels(path: Path) -> np.ndarray:
    t = _body(path)
    n, i = _list_start(t)
    return np.array(t[i + 1:t.find(b")", i)].split(), dtype=np.int64)[:n]


def _points(path: Path) -> np.ndarray:
    t = _body(path)
    n, i = _list_start(t)
    nums = np.array(re.findall(rb"[-+0-9.eE]+", t[i:]), dtype=float)
    return nums[: 3 * n].reshape(n, 3)


def _faces(path: Path) -> tuple[np.ndarray, np.ndarray]:
    t = _body(path)
    head = path.read_bytes()[:4096]
    if b"faceCompactList" in head:
        n_off, i = _list_start(t)
        j = t.find(b")", i)
        offs = np.array(t[i + 1:j].split(), dtype=np.int64)[:n_off]
        rest = t[j + 1:]
        n_dat, k = _list_start(rest)
        return offs, np.array(rest[k + 1:].split(b")")[0].split(), dtype=np.int64)[:n_dat]
    _n, i = _list_start(t)
    toks = _FACE.findall(t[i + 1:])
    sizes = np.array([int(a) for a, _ in toks], dtype=np.int64)
    dat = np.array(b" ".join(b for _, b in toks).split(), dtype=np.int64)
    return np.concatenate([[0], np.cumsum(sizes)]), dat


def _boundary(path: Path) -> dict[str, tuple[int, int]]:
    t = _body(path).decode(errors="replace")
    out = {}
    for name, blk in re.findall(r"(\w[\w.:-]*)\s*\{([^}]*)\}", t):
        nf, sf = re.search(r"nFaces\s+(\d+)", blk), re.search(r"startFace\s+(\d+)", blk)
        if nf and sf:
            out[name] = (int(sf.group(1)), int(nf.group(1)))
    return out


def layer_census(polymesh, n_layers: int, patches) -> dict:
    """{patch: {faces, area_m2, area_with_any_layer, area_with_full_layers, mean_layers,
    coverage_pct}} for each named wall patch; {} when the mesh cannot be measured (binary,
    larger than CENSUS_MAX_CELLS, missing files) or no layers were asked for."""
    pm = Path(polymesh)
    if n_layers <= 0:
        return {}
    try:
        owner = _labels(pm / "owner")
        n_cells = int(owner.max()) + 1 if len(owner) else 0
        if n_cells > CENSUS_MAX_CELLS:
            logger.info("layer census: %d cells, above %d - not measured", n_cells,
                        CENSUS_MAX_CELLS)
            return {}
        neigh = _labels(pm / "neighbour")
        P = _points(pm / "points")
        offs, dat = _faces(pm / "faces")
        bnd = _boundary(pm / "boundary")
    except (OSError, ValueError, IndexError) as exc:
        logger.info("layer census: polyMesh not measurable (%s)", exc)
        return {}
    nF, nI = len(offs) - 1, len(neigh)
    sizes = np.diff(offs)
    fc = np.zeros((nF, 3))
    fa = np.zeros((nF, 3))
    for s in np.unique(sizes):
        ids = np.flatnonzero(sizes == s)
        pts = P[dat[offs[ids][:, None] + np.arange(s)[None, :]]]
        fc[ids] = pts.mean(axis=1)
        a = np.zeros((len(ids), 3))
        for k in range(1, int(s) - 1):
            a += 0.5 * np.cross(pts[:, k] - pts[:, 0], pts[:, k + 1] - pts[:, 0])
        fa[ids] = a
    area = np.linalg.norm(fa, axis=1)
    n_cells = max(n_cells, int(neigh.max()) + 1 if nI else 0)
    cf_cell = np.concatenate([owner, neigh])
    cf_face = np.concatenate([np.arange(nF), np.arange(nI)])
    order = np.argsort(cf_cell, kind="stable")
    cf_cell, cf_face = cf_cell[order], cf_face[order]
    cstart = np.searchsorted(cf_cell, np.arange(n_cells + 1))
    res = {}
    for name in patches:
        if name not in bnd:
            continue
        s0, nf = bnd[name]
        if nf == 0:
            continue
        wall = np.arange(s0, s0 + nf)
        n0 = fa[wall] / np.maximum(area[wall], 1e-300)[:, None]       # out of the domain
        count = np.zeros(nf, dtype=np.int64)
        cur_face, cur_cell = wall.copy(), owner[wall].copy()
        alive = np.ones(nf, dtype=bool)
        for _ in range(int(n_layers) + 2):
            if not alive.any():
                break
            idx = np.flatnonzero(alive)
            cells = cur_cell[idx]
            cnt = cstart[cells + 1] - cstart[cells]
            kmax = int(cnt.max())
            slot = cstart[cells][:, None] + np.arange(kmax)[None, :]
            valid = np.arange(kmax)[None, :] < cnt[:, None]
            faces = cf_face[np.minimum(slot, len(cf_face) - 1)]
            d = np.einsum("ijk,ik->ij", fc[faces] - fc[cur_face[idx]][:, None, :], -n0[idx])
            d = np.where(valid, d, -np.inf)
            j = np.argmax(d, axis=1)
            rows = np.arange(len(idx))
            best_f, h = faces[rows, j], d[rows, j]
            nj = fa[best_f] / np.maximum(area[best_f], 1e-300)[:, None]
            par = np.abs((nj * n0[idx]).sum(axis=1))
            flat = (h > 0) & (h < FLAT_RATIO * np.sqrt(area[cur_face[idx]])) & (par > PARALLEL_COS)
            count[idx[flat]] += 1
            alive[idx[~flat]] = False
            ok = flat & (best_f < nI)
            bf = best_f[ok]
            o, nb = owner[bf], neigh[bf]
            cc = cur_cell[idx[ok]]
            alive[idx[~ok]] = False
            cur_face[idx[ok]] = bf
            cur_cell[idx[ok]] = np.where(o == cc, nb, o)
        A = area[wall]
        capped = np.minimum(count, n_layers)
        res[name] = {"faces": int(nf), "area_m2": float(A.sum()),
                     "area_with_any_layer": round(float(A[count >= 1].sum() / A.sum()), 4),
                     "area_with_full_layers": round(float(A[count >= n_layers].sum() / A.sum()), 4),
                     "mean_layers": round(float((capped * A).sum() / A.sum()), 3),
                     "coverage_pct": round(float(100.0 * capped.sum() / (n_layers * nf)), 2)}
    return res


def requested_layers(meshdict) -> int:
    """nLayers asked for in a cfMesh meshDict (the largest, if several); 0 when none."""
    try:
        text = Path(meshdict).read_text(errors="replace")
    except OSError:
        return 0
    found = [int(n) for n in re.findall(r"nLayers\s+(\d+)\s*;", text)]
    return max(found) if found else 0


__all__ = ["CENSUS_MAX_CELLS", "layer_census", "requested_layers"]
