# Responsibility: Measure how STRUCTURED a shell mesh is - the look an engineer's hand-made mesh has: interior nodes
# with four quads round them, quads near rectangular, element edges running along the nearest boundary, and a ring of
# quads round each hole - so a change to the mesher is judged by numbers, not by eye (SHMESH-44).
# Boundaries: pure numpy (+ scipy's KD-tree). It meshes nothing and changes nothing.
# Collaborates with: engines/shell_mesh.py (the mesh), engines/gmsh/shell_adapter.py (writes it into the record).
from __future__ import annotations

import math

import numpy as np

from meshpipeline.engines.shell_mesh import ShellMesh

#: a quad is "rectangular" when every corner is within RECT_DEG of 90 degrees; an element edge is "aligned" when it
#: runs within ALIGN_DEG of the nearest boundary edge or square to it
RECT_DEG = 10.0
ALIGN_DEG = 10.0
#: a free-edge loop is a hole's circle when its nodes lie within CIRCLE_TOL of their mean distance from its centre
#: and in one plane (within the same share of the radius)
CIRCLE_TOL = 0.02
#: a hole has a RING when every element touching its circle is a quad lying on it by one edge, and the quads' far
#: corners lie on one circle (within RING_TOL of its radius)
RING_TOL = 0.10
#: a sharp edge between two elements (a fold of the sheet) counts as a boundary the mesh should run along
FOLD_DEG = 30.0


def _edges(mesh: ShellMesh) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Every element edge once: (sorted node pairs (m, 2), how many elements use each, for each element corner-edge
    its index into the pairs)."""
    parts = []
    for conn in (mesh.quads, mesh.trias):
        if len(conn):
            parts.append(np.stack([conn, np.roll(conn, -1, axis=1)], axis=2).reshape(-1, 2))
    if not parts:
        return np.zeros((0, 2), dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    raw = np.sort(np.vstack(parts), axis=1)
    pairs, inverse, counts = np.unique(raw, axis=0, return_inverse=True, return_counts=True)
    return pairs, counts, inverse.ravel()


def _corner_angles(xyz: np.ndarray, quads: np.ndarray) -> np.ndarray:
    """(q, 4) corner angles of each quad, degrees."""
    p = xyz[quads]
    a = np.roll(p, 1, axis=1) - p
    b = np.roll(p, -1, axis=1) - p
    cos = np.einsum("qki,qki->qk", a, b) / np.maximum(np.linalg.norm(a, axis=2) * np.linalg.norm(b, axis=2), 1e-300)
    return np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))


def _loops(pairs: np.ndarray) -> list[list[int]]:
    """Closed node loops of a set of edges where every node has exactly two of them (others skipped)."""
    nbr: dict[int, list[int]] = {}
    for a, b in pairs.tolist():
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)
    seen: set[int] = set()
    out: list[list[int]] = []
    for start in sorted(nbr):
        if start in seen or len(nbr[start]) != 2:
            continue
        loop, prev, cur, ok = [start], -1, start, True
        seen.add(start)
        while True:
            nxt = [v for v in nbr[cur] if v != prev]
            if len(nbr[cur]) != 2 or not nxt:
                ok = False
                break
            prev, cur = cur, nxt[0]
            if cur == start:
                break
            if cur in seen:
                ok = False
                break
            seen.add(cur)
            loop.append(cur)
        if ok and len(loop) >= 3:
            out.append(loop)
    return out


def _circle(xyz: np.ndarray) -> tuple[np.ndarray, float, np.ndarray] | None:
    """(centre, radius, normal) of points on a circle, else None."""
    c0 = xyz.mean(axis=0)
    _u, _s, vt = np.linalg.svd(xyz - c0, full_matrices=False)
    n = vt[2]
    # the centre: a least-squares circle in the loop's plane (its nodes need not be evenly spaced)
    xy = np.column_stack([(xyz - c0) @ vt[0], (xyz - c0) @ vt[1]])
    sol, *_r = np.linalg.lstsq(np.column_stack([2 * xy, np.ones(len(xy))]), (xy ** 2).sum(axis=1), rcond=None)
    c = c0 + sol[0] * vt[0] + sol[1] * vt[1]
    rel = xyz - c
    r = np.linalg.norm(rel, axis=1)
    rm = float(r.mean())
    if rm <= 0 or float(np.abs(r - rm).max()) > CIRCLE_TOL * rm or float(np.abs(rel @ n).max()) > CIRCLE_TOL * rm:
        return None
    return c, rm, n


def holes(mesh: ShellMesh, pairs: np.ndarray | None = None, counts: np.ndarray | None = None) -> list[dict]:
    """Every hole of the mesh - a free-edge loop that is a circle - with: its diameter (mm), the elements round it,
    and whether they make a RING (a washer): each a quad on the hole by one edge, nothing touching it by a corner
    alone, the quads' far corners on one circle (its diameter too)."""
    if pairs is None or counts is None:
        pairs, counts, _inv = _edges(mesh)
    free = pairs[counts == 1]
    out: list[dict] = []
    if not len(free):
        return out
    nq = len(mesh.quads)
    # each node's elements
    conns = mesh.elements()
    node_els: dict[int, list[int]] = {}
    for e, conn in enumerate(conns):
        for v in conn.tolist():
            node_els.setdefault(v, []).append(e)
    for loop in _loops(free):
        got = _circle(mesh.xyz[loop])
        if got is None:
            continue
        c, r, n = got
        on = set(loop)
        touching = sorted({e for v in loop for e in node_els.get(v, [])})
        ring = len(touching) == len(loop)
        far: list[int] = []
        for e in touching:
            conn = conns[e].tolist()
            k = sum(1 for v in conn if v in on)
            if e >= nq or k != 2:
                ring = False
                continue
            far += [v for v in conn if v not in on]
        outer = None
        if ring and far:
            rr = np.linalg.norm(mesh.xyz[sorted(set(far))] - c, axis=1)
            ring = float(rr.max() - rr.min()) <= RING_TOL * float(rr.mean())
            outer = 2.0 * float(rr.mean())
        out.append({"centre": [float(f"{x:.6g}") for x in c], "diameter_mm": float(f"{2 * r:.4g}"),
                    "around": len(loop), "elements": len(touching), "ring": bool(ring),
                    "ring_diameter_mm": float(f"{outer:.4g}") if ring and outer else None})
    return sorted(out, key=lambda h: (h["centre"], h["diameter_mm"]))


def measure(mesh: ShellMesh) -> dict:
    """SHMESH-44: the regularity of a shell mesh.
    - valence4: the share of interior nodes (every edge round them shared by exactly two elements, every element round
      them a quad) with exactly four quads round them - 1.0 is a perfect grid;
    - rect: the share of quads with every corner within RECT_DEG of 90 degrees;
    - aligned: the share of element edges running within ALIGN_DEG of the nearest boundary edge (a free edge, a
      junction, a fold of the sheet past FOLD_DEG) or square to it;
    - holes: per hole (a free-edge circle) whether it has a ring of quads, and how many elements round it."""
    pairs, counts, inv = _edges(mesh)
    xyz = mesh.xyz
    out: dict = {"nodes": int(len(xyz)), "quads": int(len(mesh.quads)), "trias": int(len(mesh.trias))}
    if not len(pairs):
        return out
    # boundary edges: free, junction (3+), and folds (two elements meeting at a sharp angle)
    sizes = [4] * len(mesh.quads) + [3] * len(mesh.trias)
    el_of_corner = np.repeat(np.arange(mesh.n_elements), sizes)
    two = np.flatnonzero(counts == 2)
    order = np.argsort(inv, kind="stable")
    starts = np.searchsorted(inv[order], np.arange(len(pairs) + 1))
    normals = np.zeros((mesh.n_elements, 3))
    for k, conn in enumerate((mesh.quads, mesh.trias)):
        if not len(conn):
            continue
        p = xyz[conn]
        nv = np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1]) if conn.shape[1] == 4 else np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        nv /= np.maximum(np.linalg.norm(nv, axis=1), 1e-300)[:, None]
        if k == 0:
            normals[: len(conn)] = nv
        else:
            normals[len(mesh.quads):] = nv
    fold = np.zeros(len(pairs), dtype=bool)
    cos_fold = math.cos(math.radians(FOLD_DEG))
    for i in two.tolist():
        e1, e2 = el_of_corner[order[starts[i]:starts[i + 1]]]
        fold[i] = abs(float(np.dot(normals[e1], normals[e2]))) < cos_fold
    bound = (counts != 2) | fold
    # interior nodes: on no boundary edge, every element round them a quad
    on_bound = np.zeros(len(xyz), dtype=bool)
    on_bound[pairs[bound].ravel()] = True
    in_tria = np.zeros(len(xyz), dtype=bool)
    if len(mesh.trias):
        in_tria[mesh.trias.ravel()] = True
    used = np.zeros(len(xyz), dtype=bool)
    used[pairs.ravel()] = True
    interior = used & ~on_bound & ~in_tria
    valence = np.bincount(mesh.quads.ravel(), minlength=len(xyz)) if len(mesh.quads) else np.zeros(len(xyz), dtype=int)
    n_int = int(interior.sum())
    out["interior_nodes"] = n_int
    out["valence4"] = float(f"{(valence[interior] == 4).sum() / n_int:.4f}") if n_int else None
    hist = np.bincount(valence[interior], minlength=7)[:7] if n_int else np.zeros(7, dtype=int)
    out["valence_hist"] = {str(k): int(hist[k]) for k in range(3, 7) if hist[k]}
    if len(mesh.quads):
        ang = _corner_angles(xyz, mesh.quads)
        out["rect"] = float(f"{(np.abs(ang - 90.0) <= RECT_DEG).all(axis=1).mean():.4f}")
    # alignment: each element edge against the nearest boundary edge (by midpoint)
    bpairs = pairs[bound]
    if len(bpairs):
        from scipy.spatial import cKDTree

        bmid = 0.5 * (xyz[bpairs[:, 0]] + xyz[bpairs[:, 1]])
        bdir = xyz[bpairs[:, 1]] - xyz[bpairs[:, 0]]
        bdir /= np.maximum(np.linalg.norm(bdir, axis=1), 1e-300)[:, None]
        inner = pairs[~bound]
        if len(inner):
            mid = 0.5 * (xyz[inner[:, 0]] + xyz[inner[:, 1]])
            d = xyz[inner[:, 1]] - xyz[inner[:, 0]]
            d /= np.maximum(np.linalg.norm(d, axis=1), 1e-300)[:, None]
            _dist, near = cKDTree(bmid).query(mid)
            c = np.abs(np.einsum("mi,mi->m", d, bdir[near]))
            a = np.degrees(np.arccos(np.clip(c, 0.0, 1.0)))          # 0..90
            folded = np.minimum(a, 90.0 - a)
            out["aligned"] = float(f"{(folded <= ALIGN_DEG).mean():.4f}")
    hs = holes(mesh, pairs, counts)
    out["holes"] = len(hs)
    out["holes_ringed"] = sum(1 for h in hs if h["ring"])
    out["hole_list"] = hs
    return out


__all__ = ["measure", "holes"]
