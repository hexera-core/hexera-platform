# Responsibility: Measure every cell of an OpenFOAM polyMesh the way checkMesh does - non-orthogonality, skewness, aspect ratio, determinant, volume, orthogonal quality - so a user's limit that allows a share of cells past it ("non-ortho under 65 for 99% of cells") is measured as a share, not only as the worst cell.
# Boundaries: pure numpy over the ascii polyMesh files (points, faces, owner, neighbour); it writes nothing and runs no OpenFOAM. A mesh it cannot read (binary, missing, past the size it measures) gives None, and the caller says the share was not measured.
# Collaborates with: engines/cfd_requirements.py (the user's limits, asked against delivered), engines/snappy/polymesh_surface.py (the same ascii format).
"""HOW EACH FIGURE IS MEASURED (OpenFOAM's primitiveMeshTools definitions):

* face centres and areas by triangles fanned from the face's mean point; cell volumes and centres by pyramids from
  the mean of the cell's face centres.
* non-orthogonality (degrees): on each internal face, the angle between the face normal and the line joining the two
  cell centres; a cell's figure is the worst of its faces (checkMesh reports the worst face).
* skewness: the distance from the face centre to where the line between the cell centres crosses the face, over the
  face's own size in that direction (checkMesh's measure); boundary faces too. A cell's figure is its worst face.
* aspect ratio: the larger of the ratio of the cell's projected areas and (1/6) x its face area over volume^(2/3)
  (1 for a cube).
* determinant: det(sum over the faces of S S / |S|) over 8 x (sum |S| / 6)^3 (1 for a cube; near 0 for a flat cell).
* orthogonal quality (Fluent's): the smallest cosine, over a cell's faces, between the face normal and the lines
  from the cell centre to the face centre and to the next cell centre (1 is perfect).
* volume: the cell volume, m^3.
"""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: the most cells measured cell by cell (beyond it the worst figures checkMesh reports are the measure, and said)
MAX_CELLS = 6_000_000
_ASCII = re.compile(rb"format\s+ascii\s*;")
_CHUNK = 2_000_000


def _body(path: Path) -> bytes | None:
    """The list of a foam file (from its count to its closing parenthesis), or None when it is not ascii."""
    raw = path.read_bytes()
    head = raw[:4096]
    if b"FoamFile" in head and not _ASCII.search(head):
        return None
    end = raw.find(b"}", raw.find(b"FoamFile")) + 1 if b"FoamFile" in head else 0
    m = re.compile(rb"\n\s*(\d+)\s*\n\s*\(").search(raw, end)
    if m is None:
        return None
    return raw[m.end():raw.rfind(b")")]


def _floats(path: Path, width: int) -> np.ndarray | None:
    b = _body(path)
    if b is None:
        return None
    a = np.array(b.replace(b"(", b" ").replace(b")", b" ").split(), dtype=float)
    return a.reshape(-1, width) if width > 1 else a


def _ints(path: Path) -> np.ndarray | None:
    b = _body(path)
    if b is None:
        return None
    return np.array(b.replace(b"(", b" ").replace(b")", b" ").split(), dtype=np.int64)


def read(case) -> dict | None:
    """{"points", "verts" (every face's vertices end to end), "start" (each face's first), "size", "owner",
    "neighbour", "n_cells"} of the case's constant/polyMesh, or None when it cannot be read."""
    pm = Path(case) / "constant" / "polyMesh"
    try:
        pts = _floats(pm / "points", 3)
        tok = _ints(pm / "faces")
        own = _ints(pm / "owner")
        nei = _ints(pm / "neighbour")
    except (OSError, ValueError):
        return None
    if pts is None or tok is None or own is None or nei is None or not len(own):
        return None
    n_faces = len(own)
    start = np.empty(n_faces, dtype=np.int64)
    size = np.empty(n_faces, dtype=np.int64)
    pos = 0
    t = tok.tolist() if n_faces < 50_000 else tok      # a short list walks faster as Python ints
    for f in range(n_faces):
        k = int(t[pos])
        start[f], size[f] = pos + 1, k
        pos += k + 1
    first = np.cumsum(size) - size
    verts = tok[np.repeat(start, size) + (np.arange(int(size.sum())) - np.repeat(first, size))]
    return {"points": pts, "verts": verts, "start": first, "size": size, "owner": own, "neighbour": nei,
            "n_cells": int(max(int(own.max()), int(nei.max()) if len(nei) else -1)) + 1}


def _faces(m: dict) -> tuple[np.ndarray, np.ndarray]:
    """(face centres, face area vectors) by triangles fanned from the mean point (OpenFOAM's makeFaceCentresAndAreas)."""
    P, V, first, size = m["points"], m["verts"], m["start"], m["size"]
    nf = len(size)
    fid = np.repeat(np.arange(nf), size)
    est = np.add.reduceat(P[V], first, axis=0) / size[:, None]
    nxt = np.arange(len(V)) + 1
    last = first + size - 1
    nxt[last] = first
    a, b, c = P[V], P[V[nxt]], est[fid]
    n = np.cross(b - a, c - a)
    tri_c = a + b + c
    mag = np.linalg.norm(n, axis=1)
    sum_n = np.add.reduceat(n, first, axis=0)
    sum_a = np.add.reduceat(mag, first)
    sum_ac = np.add.reduceat(mag[:, None] * tri_c, first, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ctr = np.where(sum_a[:, None] > 0, sum_ac / (3.0 * sum_a[:, None]), est)
    return ctr, 0.5 * sum_n


def measure(case) -> dict | None:
    """Every cell's figures ({name: array over cells}, plus "n_cells"), or None when the mesh cannot be read or is
    past MAX_CELLS (the worst figures are then the measure)."""
    try:
        m = read(case)
    except Exception:  # noqa: BLE001 - a mesh that cannot be read is unmeasured, never a failed run
        logger.warning("cell measures: the polyMesh could not be read", exc_info=True)
        return None
    if m is None or m["n_cells"] > MAX_CELLS:
        return None
    try:
        return _measure(m)
    except (MemoryError, ValueError, IndexError):
        logger.warning("cell measures: not measured", exc_info=True)
        return None


def _measure(m: dict) -> dict:
    nc, own, nei = m["n_cells"], m["owner"], m["neighbour"]
    nf, ni = len(own), len(nei)
    Cf, Sf = _faces(m)
    magS = np.linalg.norm(Sf, axis=1)
    # cell centres and volumes (makeCellCentresAndVols)
    nfc = np.bincount(own, minlength=nc) + np.bincount(nei, minlength=nc)
    cEst = (np.stack([np.bincount(own, Cf[:, k], nc) + np.bincount(nei, Cf[:ni, k], nc) for k in range(3)], 1)
            / np.maximum(nfc, 1)[:, None])
    pv_o = np.einsum("ij,ij->i", Sf, Cf - cEst[own]) / 3.0
    pv_n = np.einsum("ij,ij->i", Sf[:ni], cEst[nei] - Cf[:ni]) / 3.0
    pc_o = 0.75 * Cf + 0.25 * cEst[own]
    pc_n = 0.75 * Cf[:ni] + 0.25 * cEst[nei]
    vol = np.bincount(own, pv_o, nc) + np.bincount(nei, pv_n, nc)
    cc = np.stack([np.bincount(own, pv_o * pc_o[:, k], nc) + np.bincount(nei, pv_n * pc_n[:, k], nc)
                   for k in range(3)], 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cc = np.where(np.abs(vol)[:, None] > 0, cc / vol[:, None], cEst)

    def worst(face_vals: np.ndarray, faces_own: np.ndarray, faces_nei: np.ndarray | None, *, low: bool) -> np.ndarray:
        out = np.full(nc, np.inf if low else -np.inf)
        op = np.minimum if low else np.maximum
        op.at(out, faces_own, face_vals[:len(faces_own)])
        if faces_nei is not None:
            op.at(out, faces_nei, face_vals[:len(faces_nei)])
        return out

    # non-orthogonality, internal faces
    d = cc[nei] - cc[own[:ni]]
    with np.errstate(divide="ignore", invalid="ignore"):
        cos = np.einsum("ij,ij->i", Sf[:ni], d) / (magS[:ni] * np.linalg.norm(d, axis=1))
    ortho = np.degrees(np.arccos(np.clip(np.nan_to_num(cos, nan=1.0), -1.0, 1.0)))
    non_ortho = worst(ortho, own[:ni], nei, low=False)
    non_ortho[~np.isfinite(non_ortho)] = 0.0

    # skewness, every face (faceSkewness / boundaryFaceSkewness)
    Cpf = Cf - cc[own]
    dd = np.empty_like(Cf)
    dd[:ni] = d
    nb = Sf[ni:] / np.maximum(magS[ni:], 1e-300)[:, None]
    dd[ni:] = nb * np.einsum("ij,ij->i", nb, Cpf[ni:])[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        sv = Cpf - (np.einsum("ij,ij->i", Sf, Cpf) / (np.einsum("ij,ij->i", Sf, dd) + 1e-300))[:, None] * dd
    msv = np.linalg.norm(sv, axis=1)
    svHat = sv / (msv + 1e-300)[:, None]
    P, V, first, size = m["points"], m["verts"], m["start"], m["size"]
    fid = np.repeat(np.arange(nf), size)
    reach = np.abs(np.einsum("ij,ij->i", svHat[fid], P[V] - Cf[fid]))
    fd = np.maximum(0.2 * np.linalg.norm(dd, axis=1), np.maximum.reduceat(reach, first)) + 1e-300
    skew = worst(msv / fd, own, nei, low=False)

    # aspect ratio (cellClosedness), determinant, orthogonal quality
    absS = np.abs(Sf)
    smc = np.stack([np.bincount(own, absS[:, k], nc) + np.bincount(nei, absS[:ni, k], nc) for k in range(3)], 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ar = np.maximum(smc.max(1) / np.maximum(smc.min(1), 1e-300),
                        (smc.sum(1) / 6.0) / np.power(np.maximum(np.abs(vol), 1e-300), 2.0 / 3.0))
    T = np.zeros((nc, 3, 3))
    w = (Sf[:, :, None] * Sf[:, None, :]) / np.maximum(magS, 1e-300)[:, None, None]
    for a in range(3):
        for b in range(a, 3):
            v = np.bincount(own, w[:, a, b], nc) + np.bincount(nei, w[:ni, a, b], nc)
            T[:, a, b] = T[:, b, a] = v
    sumA = np.bincount(own, magS, nc) + np.bincount(nei, magS[:ni], nc)
    with np.errstate(divide="ignore", invalid="ignore"):
        det = np.abs(np.linalg.det(T)) / (8.0 * np.power(np.maximum(sumA / 6.0, 1e-300), 3))
    fo = Cf - cc[own]
    with np.errstate(divide="ignore", invalid="ignore"):
        q_own = np.einsum("ij,ij->i", Sf, fo) / (magS * np.linalg.norm(fo, axis=1))
        fn = Cf[:ni] - cc[nei]
        q_nei = np.einsum("ij,ij->i", -Sf[:ni], fn) / (magS[:ni] * np.linalg.norm(fn, axis=1))
    q_d = np.nan_to_num(cos, nan=1.0)
    oq = worst(np.minimum(np.nan_to_num(q_own, nan=1.0), np.r_[q_d, np.ones(nf - ni)]), own, None, low=True)
    np.minimum.at(oq, nei, np.minimum(np.nan_to_num(q_nei, nan=1.0), q_d))
    oq[~np.isfinite(oq)] = 1.0
    return {"n_cells": nc, "non_orthogonality": non_ortho, "skewness": skew, "aspect_ratio": ar,
            "determinant": np.nan_to_num(det, nan=0.0), "min_volume": vol, "orthogonal_quality": oq,
            "cell_size_m": np.cbrt(np.abs(vol))}


def worst_of(fig: dict | None) -> dict:
    """The worst of each figure (the checkMesh line it matches): max non-orthogonality, max skewness, max aspect
    ratio, min determinant, min volume, min orthogonal quality."""
    if not fig:
        return {}
    out = {}
    for k, low in (("non_orthogonality", False), ("skewness", False), ("aspect_ratio", False),
                   ("determinant", True), ("min_volume", True), ("orthogonal_quality", True)):
        v = fig.get(k)
        if v is not None and len(v):
            x = float(v.min() if low else v.max())
            out[k] = float(f"{x:.6g}") if math.isfinite(x) else None
    return out


__all__ = ["MAX_CELLS", "measure", "read", "worst_of"]
