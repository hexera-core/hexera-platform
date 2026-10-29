# Responsibility: Measure a shell mesh's element quality the way shell solvers and pre-processors judge it -
# validity (a positive Jacobian at every corner), aspect ratio, warp, skew, taper, interior angles, the
# Jacobian ratio, and the share of triangles - and hold each to its limit (SHMESH-8): validity is the only
# blocker; every other figure past its limit is a note.
# Boundaries: pure numpy over engines/shell_mesh.ShellMesh. It decides no delivery. The limits are the
# design note's (section 3): MSC Nastran's GEOMCHECK defaults as we read them, and common Femap targets.
# Collaborates with: engines/gmsh/shell_adapter.py (measures every mesh into shell_quality.json),
# engines/shell_writers.py (states the figures in each deck), docs/design/shell/README.md.
"""HOW EACH FIGURE IS MEASURED (quads; triangles where it applies).

* corner Jacobian: at each corner, the cross product of its two edges, along the element's mean normal,
  over the two edge lengths (1 for a right angle, 0 for a straight one, negative when folded). VALID: every
  corner positive. The Jacobian RATIO is the smallest corner value over the largest (1 for a parallelogram).
* aspect ratio: longest edge over shortest.
* warp (degrees): the larger of the angles between the normals of the two triangles each diagonal cuts the
  quad into (0 for a flat quad).
* warp coefficient (Nastran's own, GEOMCHECK Q4_WARP): 2H / (D1 + D2), H the corners' distance from the mean
  plane (through the corners' mean, normal to both diagonals), D1 and D2 the diagonals (0 for a flat quad). A
  square whose corners stand 0.05 of a diagonal off the plane reads 0.05 here and 22.6 degrees above.
* skew (degrees): the smaller angle between the two lines joining opposite edge midpoints (90 is ideal);
  for a triangle, the smallest angle between a median and the line joining the other two edge midpoints.
* taper: the largest share by which a corner triangle (the corner and its two neighbours) departs from
  their mean, |Ti - mean| / mean (0 for a parallelogram).
* interior angles (degrees): the smallest and the largest.
"""
from __future__ import annotations

import math

import numpy as np

from meshpipeline.engines.shell_mesh import ShellMesh

#: (figure, op, limit, source). Validity is checked apart: it blocks.
LIMITS = (
    ("aspect", "<=", 5.0, "Femap / industry target"),
    ("warp_deg", "<=", 10.0, "industry target (5 is good, 10 the note)"),
    ("skew_deg", ">=", 45.0, "Femap target for quads (MSC GEOMCHECK Q4_SKEW warns under 30)"),
    ("taper", "<=", 0.5, "MSC GEOMCHECK Q4_TAPER default"),
    ("min_angle_quad", ">=", 45.0, "industry target (GEOMCHECK Q4_IAMIN 30)"),
    ("max_angle_quad", "<=", 135.0, "industry target (GEOMCHECK Q4_IAMAX 150)"),
    ("min_angle_tria", ">=", 20.0, "industry target"),
    ("max_angle_tria", "<=", 120.0, "industry target (GEOMCHECK T3_IAMAX 160)"),
    ("jacobian_ratio", ">=", 0.6, "industry target"),
)
#: the triangle share a quad-dominant mesh should keep under (a note past it)
TRIA_SHARE = 0.05
#: NASTRAN'S OWN LIMITS (SHMESH-8): the MSC Nastran GEOMCHECK defaults, past which the solver itself warns (MSC
#: Nastran Quick Reference Guide, GEOMCHECK). Every delivered element is held inside these; the tighter Femap /
#: industry targets above are notes. Q4_WARP is Nastran's warping coefficient 2H / (D1 + D2) held at 0.05, as
#: Nastran computes it (Altair's "element quality calculations, Nastran"), not an angle: the 5-degree angle held
#: here before was 4.5 times stricter than the solver. (shape, figure, op, limit, GEOMCHECK name)
NASTRAN_LIMITS = (
    ("quad", "skew_deg", ">=", 30.0, "Q4_SKEW"),
    ("quad", "taper", "<=", 0.5, "Q4_TAPER"),
    ("quad", "warp_coef", "<=", 0.05, "Q4_WARP"),
    ("quad", "min_angle_quad", ">=", 30.0, "Q4_IAMIN"),
    ("quad", "max_angle_quad", "<=", 150.0, "Q4_IAMAX"),
    ("tria", "skew_deg", ">=", 10.0, "T3_SKEW"),
    ("tria", "max_angle_tria", "<=", 160.0, "T3_IAMAX"),
)


def nastran_past(mesh: ShellMesh) -> np.ndarray:
    """(elements,) bool: the element is past one of Nastran's own GEOMCHECK limits."""
    q = quad_measures(mesh.xyz, mesh.quads) if len(mesh.quads) else {}
    t = tria_measures(mesh.xyz, mesh.trias) if len(mesh.trias) else {}
    out = np.zeros(mesh.n_elements, dtype=bool)
    for shape, key, op, limit, _card in NASTRAN_LIMITS:
        d, off = (q, 0) if shape == "quad" else (t, len(mesh.quads))
        if key in d:
            v = d[key]
            out[off:off + len(v)] |= (v < limit) if op == ">=" else (v > limit)
    return out


def _quad_past(q: dict[str, np.ndarray]) -> np.ndarray:
    """(quads,) bool: the quad is past one of Nastran's own quad limits (or not valid)."""
    out = q["min_jacobian"] <= 0
    for shape, key, op, limit, _card in NASTRAN_LIMITS:
        if shape == "quad":
            out = out | ((q[key] < limit) if op == ">=" else (q[key] > limit))
    return out


def _tria_past(t: dict[str, np.ndarray]) -> np.ndarray:
    out = t["min_jacobian"] <= 0
    for shape, key, op, limit, _card in NASTRAN_LIMITS:
        if shape == "tria":
            out = out | ((t[key] < limit) if op == ">=" else (t[key] > limit))
    return out


def split_past_quads(xyz: np.ndarray, quads: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SHMESH-10: every quad past one of Nastran's own limits is cut into two triangles on the diagonal whose
    triangles are better (fewer past the triangle limits first, then the larger smallest angle; the first
    diagonal on a tie). A triangle has no warp or taper, and the cut halves the corner that was too open.

    Returns (the indices of the quads kept, the new triangles (k, 3) in the quads' turning sense, the quad
    each triangle came from)."""
    none = np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
    if not len(quads):
        return (np.zeros(0, dtype=np.int64), *none)
    past = _quad_past(quad_measures(xyz, quads))
    bad = np.flatnonzero(past)
    keep = np.flatnonzero(~past)
    if not len(bad):
        return (keep, *none)
    q = quads[bad]
    cuts = (np.stack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=1),      # diagonal 0-2
            np.stack([q[:, [0, 1, 3]], q[:, [1, 2, 3]]], axis=1))      # diagonal 1-3
    score = []
    for c in cuts:
        m = tria_measures(xyz, c.reshape(-1, 3))
        fails = _tria_past(m).reshape(-1, 2).sum(axis=1)
        least = m["min_angle_tria"].reshape(-1, 2).min(axis=1)
        score.append((fails, least))
    second = (score[1][0] < score[0][0]) | ((score[1][0] == score[0][0]) & (score[1][1] > score[0][1]))
    tri = np.where(second[:, None, None], cuts[1], cuts[0]).reshape(-1, 3)
    return keep, tri.astype(np.int64), np.repeat(bad, 2)


def _past_count(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(quads past a limit or not valid, triangles past a limit or not valid), as bool arrays."""
    q = _quad_past(quad_measures(xyz, quads)) if len(quads) else np.zeros(0, dtype=bool)
    t = _tria_past(tria_measures(xyz, trias)) if len(trias) else np.zeros(0, dtype=bool)
    return q, t


def relax_past(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, movable: np.ndarray, *,
               rounds: int = 6) -> tuple[np.ndarray, int]:
    """SHMESH-14: every movable node (inside a flat face, on no CAD edge) of an element past one of Nastran's own
    limits moves to the middle of its edge neighbours when its elements come out better: fewer past a limit, none
    turned over, and (on a tie) a larger smallest corner angle. The middle of coplanar neighbours lies in their
    plane, so the node stays on its face. Nodes that share no element move together (in number order, the first
    free node taken); `rounds` passes at most. Deterministic. Returns (the moved coordinates, how many moves)."""
    X = xyz.copy()
    nq = len(quads)
    elem = np.full((nq + len(trias), 4), -1, dtype=np.int64)
    elem[:nq] = quads
    elem[nq:, :3] = trias
    size = np.where(np.arange(len(elem)) < nq, 4, 3)
    # each node's elements, as one sorted table
    flat = elem.ravel()
    owner = np.repeat(np.arange(len(elem)), 4)
    ok = flat >= 0
    flat, owner = flat[ok], owner[ok]
    order = np.argsort(flat, kind="stable")
    flat, owner = flat[order], owner[order]
    start = np.searchsorted(flat, np.arange(len(X) + 1))
    # edges (each element's sides), for the neighbours' middle
    a_l, b_l = [], []
    for k in (3, 4):
        e = elem[size == k][:, :k]
        for j in range(k):
            a_l.append(e[:, j])
            b_l.append(e[:, (j + 1) % k])
    ea = np.concatenate(a_l) if a_l else np.zeros(0, dtype=np.int64)
    eb = np.concatenate(b_l) if b_l else np.zeros(0, dtype=np.int64)
    lo, hi = np.minimum(ea, eb), np.maximum(ea, eb)
    edges = np.unique(np.stack([lo, hi], axis=1), axis=0) if len(ea) else np.zeros((0, 2), dtype=np.int64)

    def judge(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per element of `ids`: past a limit (or turned over), its smallest corner angle, turned over."""
        past = np.zeros(len(ids), dtype=bool)
        least = np.full(len(ids), 180.0)
        flip = np.zeros(len(ids), dtype=bool)
        isq = ids < nq
        if isq.any():
            m = quad_measures(X, elem[ids[isq]])
            past[isq] = _quad_past(m)
            least[isq] = m["min_angle_quad"]
            flip[isq] = m["min_jacobian"] <= 0
        if (~isq).any():
            m = tria_measures(X, elem[ids[~isq], :3])
            past[~isq] = _tria_past(m)
            least[~isq] = m["min_angle_tria"]
            flip[~isq] = m["min_jacobian"] <= 0
        return past, least, flip

    moves = 0
    for _ in range(rounds):
        everyone = np.arange(len(elem))
        bad_el = everyone[judge(everyone)[0]]
        if not len(bad_el):
            break
        cand = np.unique(elem[bad_el].ravel())
        cand = cand[(cand >= 0) & movable[cand]]
        if not len(cand):
            break
        changed = 0
        todo = cand.tolist()
        while todo:
            taken: list[int] = []
            used: set[int] = set()
            rest: list[int] = []
            for v in todo:
                mine = owner[start[v]:start[v + 1]].tolist()
                if used.isdisjoint(mine):
                    taken.append(v)
                    used.update(mine)
                else:
                    rest.append(v)
            todo = rest
            S = np.array(taken, dtype=np.int64)
            counts = start[S + 1] - start[S]
            ids = np.concatenate([owner[start[v]:start[v + 1]] for v in taken])
            who = np.repeat(np.arange(len(S)), counts)
            p0, l0, _f0 = judge(ids)
            before_past = np.zeros(len(S), dtype=np.int64)
            np.add.at(before_past, who, p0.astype(np.int64))
            before_least = np.full(len(S), 180.0)
            np.minimum.at(before_least, who, l0)
            # the neighbours' middle of every node in S
            pos = {int(v): i for i, v in enumerate(S)}
            sel_a = np.isin(edges[:, 0], S)
            sel_b = np.isin(edges[:, 1], S)
            acc = np.zeros((len(S), 3))
            cnt = np.zeros(len(S))
            ia = np.array([pos[int(v)] for v in edges[sel_a, 0]], dtype=np.int64)
            ib = np.array([pos[int(v)] for v in edges[sel_b, 1]], dtype=np.int64)
            if len(ia):
                np.add.at(acc, ia, X[edges[sel_a, 1]])
                np.add.at(cnt, ia, 1.0)
            if len(ib):
                np.add.at(acc, ib, X[edges[sel_b, 0]])
                np.add.at(cnt, ib, 1.0)
            go = (cnt > 0) & (before_past > 0)
            old = X[S].copy()
            X[S[go]] = acc[go] / cnt[go, None]
            p1, l1, f1 = judge(ids)
            after_past = np.zeros(len(S), dtype=np.int64)
            np.add.at(after_past, who, p1.astype(np.int64))
            after_least = np.full(len(S), 180.0)
            np.minimum.at(after_least, who, l1)
            after_flip = np.zeros(len(S), dtype=bool)
            np.logical_or.at(after_flip, who, f1)
            better = go & ~after_flip & ((after_past < before_past) |
                                         ((after_past == before_past) & (after_least > before_least + 1e-9)))
            X[S[~better]] = old[~better]
            changed += int(better.sum())
        moves += changed
        if not changed:
            break
    return X, moves


def _tria_score(xyz: np.ndarray, t: np.ndarray) -> tuple[int, float]:
    """(how many of the triangles are past a limit, their smallest skew): lower first is better, then higher."""
    m = tria_measures(xyz, t)
    return int(_tria_past(m).sum()), float(m["skew_deg"].min())


def flip_past_trias(xyz: np.ndarray, trias: np.ndarray, t_face: list[int], *, rounds: int = 4) -> tuple[np.ndarray, int]:
    """SHMESH-13: a triangle past one of Nastran's limits and the triangle across its longest edge (the same face,
    that edge theirs alone) swap that edge for the other diagonal when the two new triangles are better (fewer past
    a limit, then the larger smallest skew) and turn the same way. A face's own edges never move. Deterministic:
    triangles in number order, `rounds` passes at most. Returns (the triangles, how many swaps)."""
    T = trias.copy()
    swaps = 0
    for _ in range(rounds):
        past = np.flatnonzero(_tria_past(tria_measures(xyz, T))) if len(T) else np.zeros(0, dtype=np.int64)
        if not len(past):
            break
        users: dict[tuple[int, int], list[int]] = {}
        for i, c in enumerate(T.tolist()):
            for k in range(3):
                a, b = c[k], c[(k + 1) % 3]
                users.setdefault((min(a, b), max(a, b)), []).append(i)
        done = set()
        changed = 0
        for i in past.tolist():
            if i in done:
                continue
            c = T[i].tolist()
            lens = [float(np.linalg.norm(xyz[c[k]] - xyz[c[(k + 1) % 3]])) for k in range(3)]
            k = int(np.argmax(lens))
            a, b = c[k], c[(k + 1) % 3]
            others = [j for j in users.get((min(a, b), max(a, b)), []) if j != i]
            if len(others) != 1 or others[0] in done or t_face[others[0]] != t_face[i]:
                continue
            j = others[0]
            p = c[(k + 2) % 3]                                  # i runs a -> b -> p
            d = [x for x in T[j].tolist() if x not in (a, b)]
            if len(d) != 1:
                continue
            q = d[0]
            new = np.array([[a, q, p], [q, b, p]], dtype=np.int64)  # same turning sense as (a, b, p)
            n_old = np.cross(xyz[b] - xyz[a], xyz[p] - xyz[a])
            n_new = [np.cross(xyz[t[1]] - xyz[t[0]], xyz[t[2]] - xyz[t[0]]) for t in new]
            if any(float(np.dot(n, n_old)) <= 0 for n in n_new):
                continue
            (f_new, s_new), (f_old, s_old) = _tria_score(xyz, new), _tria_score(xyz, T[[i, j]])
            if f_new < f_old or (f_new == f_old and s_new > s_old):
                T[i], T[j] = new[0], new[1]
                done |= {i, j}
                changed += 1
        swaps += changed
        if not changed:
            break
    return T, swaps


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-300)


def _angles(p: np.ndarray) -> np.ndarray:
    """Interior angles (degrees) of polygons p (m, k, 3)."""
    k = p.shape[1]
    prev = p[:, [(i - 1) % k for i in range(k)]] - p
    nxt = p[:, [(i + 1) % k for i in range(k)]] - p
    c = np.einsum("mki,mki->mk", _unit(prev), _unit(nxt))
    return np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))


def quad_measures(xyz: np.ndarray, quads: np.ndarray) -> dict[str, np.ndarray]:
    p = xyz[quads]                                         # (m, 4, 3)
    n = _unit(np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1]))
    e_next = p[:, [1, 2, 3, 0]] - p
    e_prev = p[:, [3, 0, 1, 2]] - p
    cross = np.cross(e_next, e_prev)
    jac = np.einsum("mki,mi->mk", cross, n) / np.maximum(
        np.linalg.norm(e_next, axis=2) * np.linalg.norm(e_prev, axis=2), 1e-300)
    lengths = np.linalg.norm(e_next, axis=2)
    n1 = _unit(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]))
    n2 = _unit(np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]))
    n3 = _unit(np.cross(p[:, 1] - p[:, 0], p[:, 3] - p[:, 0]))
    n4 = _unit(np.cross(p[:, 2] - p[:, 1], p[:, 3] - p[:, 1]))
    warp = np.maximum(np.degrees(np.arccos(np.clip(np.einsum("mi,mi->m", n1, n2), -1, 1))),
                      np.degrees(np.arccos(np.clip(np.einsum("mi,mi->m", n3, n4), -1, 1))))
    d1 = np.linalg.norm(p[:, 2] - p[:, 0], axis=1)
    d2 = np.linalg.norm(p[:, 3] - p[:, 1], axis=1)
    h = np.abs(np.einsum("mki,mi->mk", p - p.mean(axis=1, keepdims=True), n)).max(axis=1)
    warp_coef = 2.0 * h / np.maximum(d1 + d2, 1e-300)
    mids = 0.5 * (p + p[:, [1, 2, 3, 0]])
    a = _unit(mids[:, 2] - mids[:, 0])
    b = _unit(mids[:, 3] - mids[:, 1])
    ang = np.degrees(np.arccos(np.clip(np.abs(np.einsum("mi,mi->m", a, b)), 0, 1)))
    corner = 0.5 * np.linalg.norm(cross, axis=2)
    mean = corner.mean(axis=1)
    taper = np.max(np.abs(corner - mean[:, None]), axis=1) / np.maximum(mean, 1e-300)
    angles = _angles(p)
    jmax = jac.max(axis=1)
    return {"min_jacobian": jac.min(axis=1), "jacobian_ratio": jac.min(axis=1) / np.where(jmax > 0, jmax, 1.0),
            "aspect": lengths.max(axis=1) / np.maximum(lengths.min(axis=1), 1e-300), "warp_deg": warp,
            "warp_coef": warp_coef,
            "skew_deg": ang, "taper": taper, "min_angle_quad": angles.min(axis=1),
            "max_angle_quad": angles.max(axis=1)}


def tria_measures(xyz: np.ndarray, trias: np.ndarray) -> dict[str, np.ndarray]:
    p = xyz[trias]
    lengths = np.linalg.norm(p[:, [1, 2, 0]] - p, axis=2)
    area2 = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    angles = _angles(p)
    # skew: the smallest angle between a median and the mid-line opposite it, over the three corners
    skews = []
    for i in range(3):
        j, k = (i + 1) % 3, (i + 2) % 3
        med = _unit(0.5 * (p[:, j] + p[:, k]) - p[:, i])
        mid = _unit(0.5 * (p[:, i] + p[:, k]) - 0.5 * (p[:, i] + p[:, j]))
        skews.append(np.degrees(np.arccos(np.clip(np.abs(np.einsum("mi,mi->m", med, mid)), 0, 1))))
    return {"min_jacobian": area2 / np.maximum(lengths.max(axis=1) ** 2, 1e-300),
            "aspect": lengths.max(axis=1) / np.maximum(lengths.min(axis=1), 1e-300),
            "skew_deg": np.min(np.stack(skews), axis=0), "min_angle_tria": angles.min(axis=1),
            "max_angle_tria": angles.max(axis=1)}


def _stat(v: np.ndarray, worst: str) -> dict:
    if not len(v):
        return {}
    return {"worst": float(f"{float(v.min() if worst == 'min' else v.max()):.6g}"),
            "median": float(f"{float(np.median(v)):.6g}")}


def measure(mesh: ShellMesh) -> dict:
    """The figures, their worst and median, how many elements are past each limit, validity and the notes."""
    q = quad_measures(mesh.xyz, mesh.quads) if len(mesh.quads) else {}
    t = tria_measures(mesh.xyz, mesh.trias) if len(mesh.trias) else {}
    n = mesh.n_elements
    bad = int((q.get("min_jacobian", np.ones(0)) <= 0).sum() + (t.get("min_jacobian", np.ones(0)) <= 0).sum())
    out: dict = {"elements": n, "quads": int(len(mesh.quads)), "trias": int(len(mesh.trias)),
                 "tria_share": float(f"{len(mesh.trias) / n:.4g}") if n else 0.0,
                 "invalid": bad, "valid": bad == 0 and n > 0, "figures": {}, "past_limits": {}, "notes": []}
    for key, op, limit, source in LIMITS:
        vals = np.concatenate([d[key] for d in (q, t) if key in d]) if any(key in d for d in (q, t)) else np.zeros(0)
        if not len(vals):
            continue
        worst = "min" if op == ">=" else "max"
        out["figures"][key] = _stat(vals, worst)
        past = int((vals < limit).sum() if op == ">=" else (vals > limit).sum())
        out["past_limits"][key] = past
        if past:
            out["notes"].append(f"{past} of {len(vals)} elements are past the {key} limit ({op} {limit}; {source}).")
    out["nastran_past"] = {}
    for shape, key, op, limit, card in NASTRAN_LIMITS:
        d = q if shape == "quad" else t
        if key in d:
            past = int((d[key] < limit).sum() if op == ">=" else (d[key] > limit).sum())
            if past:
                out["nastran_past"][card] = past
    out["within_nastran"] = bool(out["valid"]) and not out["nastran_past"]
    if out["nastran_past"]:
        out["notes"].insert(0, "Past Nastran's own GEOMCHECK limits: " + ", ".join(
            f"{v} elements by {k}" for k, v in out["nastran_past"].items()) + ".")
    if n and len(mesh.trias) / n > TRIA_SHARE:
        out["notes"].append(f"{100 * len(mesh.trias) / n:.1f}% of elements are triangles (target under "
                            f"{100 * TRIA_SHARE:.0f}%).")
    if bad:
        out["notes"].insert(0, f"{bad} elements are folded or flat (a corner Jacobian not positive): the mesh is "
                               "not valid.")
    return out


def element_angle_deg(radius: float, size: float) -> float:
    """The angle one element of `size` turns through on a curve of `radius` (degrees): the curvature check."""
    return math.degrees(size / radius) if radius > 0 else 0.0


__all__ = ["LIMITS", "NASTRAN_LIMITS", "TRIA_SHARE", "measure", "nastran_past", "quad_measures",
           "flip_past_trias", "split_past_quads", "tria_measures"]
