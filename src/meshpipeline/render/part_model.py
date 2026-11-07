# Responsibility: The parts model the console's checklist turns in 3D: the measured surface the checklist pictures are drawn from, simplified to what a browser turns smoothly, with the part (or area inside a part) every triangle belongs to, smooth normals that break at sharp corners, the sharp edges and part outlines as lines, and where each separate piece of an area sits (so a tiny hole can be ringed).
# Boundaries: numpy and scipy over arrays. No rendering, no storage, no model call; the caller stores the payload.
# Collaborates with: render/part_pictures.py (the same part of each triangle the id pictures use), application/part_matches.py (stores and serves it), ui/js/viewer/parts_view.js (draws it).
from __future__ import annotations

import base64
import math

import numpy as np

#: what the checklist's model is held to: a browser turns it smoothly beside the conversation, and it
#: stays a few megabytes on the wire. Past it the surface is merged by vertex clustering (a preview of the
#: shape, never the surface anything is measured on).
MAX_TRIANGLES = 250_000
#: two faces meeting at more than this angle keep a crisp corner and a drawn edge (the stage's value)
FEATURE_ANGLE_DEG = 35.0
#: separate pieces of one area given a ring position at most (one per hole of "6 holes")
MAX_SPOTS = 24


def _b64(a) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def part_of_face(name: str) -> str:
    """The part a labelled face belongs to ("Part_3_Face_12" -> "Part_3"; a bare face -> "Part_1")."""
    head, sep, _ = str(name).rpartition("_Face_")
    return head if sep else "Part_1"


def triangle_parts(surface, part_ids: list[str]) -> np.ndarray:
    """The index into `part_ids` of the part (or area) each triangle of `surface` belongs to; -1 for none (the grey
    rest of a part whose areas are drawn)."""
    names = list(surface.names)
    label = np.asarray(surface.label, dtype=np.int64)
    order = {p: k for k, p in enumerate(part_ids)}
    lab_part = np.array([order.get(part_of_face(n), -1) for n in names] + [-1], dtype=np.int64)
    return lab_part[np.where((label >= 0) & (label < len(names)), label, len(names))]


#: every part keeps at least this many triangles (or all it has) when the model is simplified: a bolt stays a bolt
PART_FLOOR = 240


def _cluster(xyz: np.ndarray, tris: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One piece by vertex clustering to about `target` triangles: each corner snapped to the centre of its grid
    cell, collapsed and repeated triangles dropped; the cell is searched (halved or grown) until the count lands
    between 60 % and 100 % of the target. Returns (points, triangles, the input triangle each kept one stands for)."""
    n = len(tris)
    if n <= target or n == 0:
        used, packed = np.unique(tris, return_inverse=True)
        return xyz[used], packed.reshape(-1, 3), np.arange(n)
    lo = xyz.min(axis=0)
    a, b, c = xyz[tris[:, 0]], xyz[tris[:, 1]], xyz[tris[:, 2]]
    area = 0.5 * float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
    span = float(np.linalg.norm(xyz.max(axis=0) - lo)) or 1.0
    h = max(math.sqrt(2.0 * area / max(target, 1)), 1e-6 * span)
    small, big = 0.0, math.inf                   # cells known to give too many / too few triangles
    best = None
    for _ in range(12):
        key = np.floor((xyz - lo) / h).astype(np.int64)
        _, inverse, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
        inverse = inverse.reshape(-1)
        sums = np.zeros((len(counts), 3))
        np.add.at(sums, inverse, xyz)
        reps = sums / counts[:, None]
        faces = inverse[tris]
        idx = np.flatnonzero((faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2]))
        if len(idx):
            _, first = np.unique(np.sort(faces[idx], axis=1), axis=0, return_index=True)
            idx = idx[np.sort(first)]
        many = len(idx) > target
        if len(idx) and not many:
            used, packed = np.unique(faces[idx], return_inverse=True)
            if best is None or len(idx) > len(best[2]):
                best = (reps[used], packed.reshape(-1, 3), idx)
            if len(idx) >= 0.6 * target:
                break
        if many:
            small = h
        else:
            big = h
        h = (small + big) / 2 if (big < math.inf and small > 0) else (h * 1.6 if many else h / 1.6)
    if best is None:                              # never under the target: the piece as it is
        used, packed = np.unique(tris, return_inverse=True)
        return xyz[used], packed.reshape(-1, 3), np.arange(n)
    return best


def simplify(xyz: np.ndarray, tris: np.ndarray, part: np.ndarray | None = None,
             max_triangles: int = MAX_TRIANGLES, labels: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray,
                                                                                            np.ndarray]:
    """(points, triangles, the original triangle each kept one stands for): unchanged under the budget, else each
    solid simplified on its own (`part`: each triangle's solid), the budget shared by size with a floor per solid -
    so a big plate gives up detail and a small bolt keeps its shape. `labels` (each triangle's part or area): one
    the simplification would lose is kept on its own."""
    n = len(tris)
    if n <= max_triangles or n == 0:
        return xyz, tris, np.arange(n)
    part = np.zeros(n, dtype=np.int64) if part is None else np.asarray(part)
    slots, inv, size = np.unique(part, return_inverse=True, return_counts=True)
    floor = np.minimum(size, PART_FLOOR)
    spare = max_triangles - int(floor.sum())
    if spare > 0:
        target = floor + (size - floor) * (spare / max(int((size - floor).sum()), 1))
    else:
        target = np.maximum(size * (max_triangles / n), 4)
    pts_out, tri_out, keep_out, base = [], [], [], 0

    def take(sel, target):
        nonlocal base
        used, packed = np.unique(tris[sel], return_inverse=True)
        p, t, k = _cluster(xyz[used], packed.reshape(-1, 3), int(target))
        pts_out.append(p); tri_out.append(t + base); keep_out.append(sel[k]); base += len(p)
        return sel[k]
    for s in range(len(slots)):
        sel = np.flatnonzero(inv == s)
        kept = take(sel, target[s])
        if labels is not None:
            # AN AREA THE GRID SWALLOWED (a small hole on a big plate) comes back on its own, so it can be found
            lost = np.setdiff1d(np.unique(labels[sel]), np.unique(labels[kept]))
            for lab in lost:
                sub = sel[labels[sel] == lab]
                take(sub, min(len(sub), PART_FLOOR))
    return np.concatenate(pts_out), np.concatenate(tri_out), np.concatenate(keep_out)


def _smooth(xyz: np.ndarray, tris: np.ndarray, cos_lim: float) -> tuple[np.ndarray, np.ndarray, np.ndarray,
                                                                           np.ndarray]:
    """(points, normals, triangles over them, face normals): each corner's normal is the area-weighted average of
    the faces round its vertex that meet its own face gently (the stage skin's rule, vectorised) - so a cylinder
    reads smooth and a box keeps its edges; corners with the same vertex and normal share a point."""
    a, b, c = xyz[tris[:, 0]], xyz[tris[:, 1]], xyz[tris[:, 2]]
    cross = np.cross(b - a, c - a)
    L = np.linalg.norm(cross, axis=1)
    fn = cross / np.where(L > 0, L, 1.0)[:, None]
    cv = tris.ravel()
    cf = np.repeat(np.arange(len(tris)), 3)
    order = np.argsort(cv, kind="stable")
    sv = cv[order]
    starts = np.flatnonzero(np.r_[True, sv[1:] != sv[:-1]])
    size = np.diff(np.r_[starts, len(sv)])
    group = np.repeat(np.arange(len(starts)), size)                 # each sorted corner's vertex group
    # every pair of corners round one vertex (sum of degree squared: a few tens per vertex)
    reps = size[group]
    p = np.repeat(np.arange(len(sv)), reps)
    within = np.arange(len(p)) - np.repeat(np.cumsum(reps) - reps, reps)
    q = starts[group[p]] + within
    fp, fq = cf[order[p]], cf[order[q]]
    ok = np.einsum("ij,ij->i", fn[fp], fn[fq]) >= cos_lim
    acc = np.zeros((len(sv), 3))
    for k in range(3):
        acc[:, k] = np.bincount(p[ok], weights=cross[fq[ok], k], minlength=len(sv))
    acc /= np.where((al := np.linalg.norm(acc, axis=1)) > 0, al, 1.0)[:, None]
    corner_n = np.empty((len(cv), 3))
    corner_n[order] = acc
    key = np.column_stack([cv, np.round(corner_n * 1000).astype(np.int64)])
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return xyz[cv[first]], corner_n[first], inv.reshape(-1, 3), fn


def _edges(tris: np.ndarray, fn: np.ndarray, part: np.ndarray, cos_lim: float) -> np.ndarray:
    """The edges drawn as lines (pairs of point indices): sharp corners, open rims, and where two parts meet."""
    m = len(tris)
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    owner = np.tile(np.arange(m), 3)
    order = np.lexsort((e[:, 1], e[:, 0]))
    e, owner = e[order], owner[order]
    same_next = np.zeros(len(e), dtype=bool)
    same_next[:-1] = (e[1:, 0] == e[:-1, 0]) & (e[1:, 1] == e[:-1, 1])
    starts = np.flatnonzero(np.r_[True, ~same_next[:-1]])
    counts = np.diff(np.r_[starts, len(e)])
    f1 = owner[starts]
    f2 = np.where(counts >= 2, owner[np.minimum(starts + 1, len(e) - 1)], f1)
    sharp = counts == 1
    two = counts >= 2
    sharp |= two & (np.einsum("ij,ij->i", fn[f1], fn[f2]) < cos_lim)
    sharp |= two & (part[f1] != part[f2])
    return e[starts[sharp]]


def _spots(xyz: np.ndarray, tris: np.ndarray, part: np.ndarray, which: list[int]) -> list[tuple[int, list[float],
                                                                                                    float]]:
    """Each separate piece of the parts `which` (the areas inside a part): its centre and the radius round it."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    out: list[tuple[int, list[float], float]] = []
    if not which:
        return out
    # pieces join where their corners meet in space, whether or not the faces share point indices
    span = float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0))) or 1.0
    _, weld = np.unique(np.round(xyz / (1e-6 * span)).astype(np.int64), axis=0, return_inverse=True)
    weld = weld.reshape(-1)
    at = np.empty((int(weld.max()) + 1, 3))
    at[weld] = xyz
    for k in which:
        sel = np.flatnonzero(part == k)
        if not len(sel):
            continue
        t = weld[tris[sel]]
        verts, local = np.unique(t.ravel(), return_inverse=True)
        local = local.reshape(-1, 3)
        rows = np.concatenate([local[:, 0], local[:, 1], local[:, 2]])
        cols = np.concatenate([local[:, 1], local[:, 2], local[:, 0]])
        g = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(verts), len(verts)))
        n, lab = connected_components(g, directed=False)
        for c in range(min(n, MAX_SPOTS)):
            p = at[verts[lab == c]]
            centre = (p.min(axis=0) + p.max(axis=0)) / 2.0
            out.append((k, [float(v) for v in centre], float(np.linalg.norm(p - centre, axis=1).max())))
    return out


def model_payload(surface, part_ids: list[str], *, inside: dict[str, str] | None = None,
                  max_triangles: int = MAX_TRIANGLES) -> dict:
    """The parts model as the console draws it: `points_b64`/`normals_b64` (float32 x3), `tris_b64` (uint32 x3),
    `part_b64` (uint16 per triangle: the index into `order` plus one, 0 for the grey rest), `edges_b64` (uint32
    pairs over the same points), `order` (the part ids), `spots` (each piece of an area: part, centre, radius)
    and, when it was simplified, `simplified_from`. `inside`: the part each area is in (an area is simplified
    with its part, so the two never part at the seam)."""
    xyz = np.asarray(surface.xyz, dtype=float)
    tris = np.asarray(surface.tris, dtype=np.int64).reshape(-1, 3)
    if not len(tris):
        raise ValueError("no triangles to draw")
    part_all = triangle_parts(surface, part_ids)
    # THE SOLID each triangle is in: an area goes with its part; on a single part, everything is that part
    index = {p: k for k, p in enumerate(part_ids)}
    solids = [k for k, p in enumerate(part_ids) if not str(p).startswith("Feature_")]
    to_solid = np.arange(len(part_ids) + 1) - 1                    # slot k+1 -> k; the rest (-1) stays -1
    for f, p in (inside or {}).items():
        if f in index and p in index:
            to_solid[index[f] + 1] = index[p]
    solid = to_solid[part_all + 1]
    if len(solids) == 1:
        solid = np.full_like(solid, solids[0])
    pts, t, kept = simplify(xyz, tris, solid, max_triangles, labels=part_all)
    part = part_all[kept]
    cos_lim = math.cos(math.radians(FEATURE_ANGLE_DEG))
    out_pts, out_nrm, out_tris, fn = _smooth(pts, t, cos_lim)
    # the edges over the smooth points: a corner split in two keeps one of its copies for the line
    edges = _edges(t, fn, part, cos_lim)
    first_copy = np.full(len(pts), -1, dtype=np.int64)
    first_copy[t.ravel()[::-1]] = out_tris.ravel()[::-1]
    lines = first_copy[edges] if len(edges) else np.zeros((0, 2), dtype=np.int64)
    features = [k for k, p in enumerate(part_ids) if str(p).startswith("Feature_")]
    spots = _spots(pts, t, part, features)
    payload: dict = {
        "kind": "parts_model", "order": list(part_ids),
        "points_b64": _b64(out_pts.astype(np.float32)), "normals_b64": _b64(out_nrm.astype(np.float32)),
        "tris_b64": _b64(out_tris.astype(np.uint32)),
        "part_b64": _b64(np.clip(part + 1, 0, 65535).astype(np.uint16)),
        "edges_b64": _b64(lines.astype(np.uint32)),
        "spots": [{"part": part_ids[k], "c": [round(v, 6) for v in c], "r": round(r, 6)} for k, c, r in spots],
        "triangles": int(len(out_tris)),
    }
    if len(t) < len(tris):
        payload["simplified_from"] = int(len(tris))
    return payload


__all__ = ["MAX_TRIANGLES", "model_payload", "part_of_face", "simplify", "triangle_parts"]
