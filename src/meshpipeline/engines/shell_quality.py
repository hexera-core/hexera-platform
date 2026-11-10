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
#: SHMESH-14b: the relaxation also tries where each quad round a node would make it a parallelogram
RELAX_PARALLELOGRAM = True
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


#: the mesh-making steps hold every element a hair inside each limit (a relative MARGIN), so that a quad made at
#: exactly the limit (taper 0.5) never reads past it once its corners are numbered the other way round (sh0039)
MARGIN = 1e-9


def _quad_past(q: dict[str, np.ndarray]) -> np.ndarray:
    """(quads,) bool: the quad is past one of Nastran's own quad limits (or not valid)."""
    out = q["min_jacobian"] <= 0
    for shape, key, op, limit, _card in NASTRAN_LIMITS:
        if shape == "quad":
            out = out | ((q[key] < limit * (1 + MARGIN)) if op == ">=" else (q[key] > limit * (1 - MARGIN)))
    return out


def _tria_past(t: dict[str, np.ndarray]) -> np.ndarray:
    out = t["min_jacobian"] <= 0
    for shape, key, op, limit, _card in NASTRAN_LIMITS:
        if shape == "tria":
            out = out | ((t[key] < limit * (1 + MARGIN)) if op == ">=" else (t[key] > limit * (1 - MARGIN)))
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
               rounds: int = 6, weights: tuple[int, int] = (1, 16), parallelogram: bool | None = None,
               project=None) -> tuple[np.ndarray, int]:
    """SHMESH-14: every movable node (inside a flat face, on no CAD edge) of an element past one of Nastran's own
    limits moves to the middle of its edge neighbours when its elements come out better: fewer past a limit, none
    turned over, and (on a tie) a larger smallest corner angle. The middle of coplanar neighbours lies in their
    plane, so the node stays on its face. Nodes that share no element move together (in number order, the first
    free node taken); `rounds` passes at most. Deterministic. Returns (the moved coordinates, how many moves).
    `project(nodes, points)`, when given, lays each tried point back onto its node's own face (SHMESH-14c: nodes
    inside curved faces move too, on their surface)."""
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

    def judge(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Per element of `ids`: past a limit (or turned over), its smallest corner angle, turned over, and (quads)
        past a limit with no cut into two triangles inside the triangle limits."""
        past = np.zeros(len(ids), dtype=bool)
        hard = np.zeros(len(ids), dtype=bool)
        least = np.full(len(ids), 180.0)
        flip = np.zeros(len(ids), dtype=bool)
        isq = ids < nq
        if isq.any():
            m = quad_measures(X, elem[ids[isq]])
            past[isq] = _quad_past(m)
            least[isq] = m["min_angle_quad"]
            flip[isq] = m["min_jacobian"] <= 0
            qi = np.flatnonzero(isq)[past[isq]]
            if len(qi):
                q = elem[ids[qi]]
                fails = [_tria_past(tria_measures(X, c.reshape(-1, 3))).reshape(-1, 2).any(axis=1)
                         for c in (np.stack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=1),
                                   np.stack([q[:, [0, 1, 3]], q[:, [1, 2, 3]]], axis=1))]
                hard[qi] = fails[0] & fails[1]
        if (~isq).any():
            m = tria_measures(X, elem[ids[~isq], :3])
            past[~isq] = _tria_past(m)
            least[~isq] = m["min_angle_tria"]
            flip[~isq] = m["min_jacobian"] <= 0
        return past, least, flip, hard

    def weigh(ids: np.ndarray, hard: np.ndarray) -> np.ndarray:
        """Each element's weight when past a limit: weights[1] for a triangle; for a quad weights[0], or weights[1]
        when no cut can mend it and quads weigh less than triangles (before the cut)."""
        wq = np.where(hard & (weights[0] < weights[1]), weights[1], weights[0])
        return np.where(ids < nq, wq, weights[1])

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
            # a triangle past a limit stays past; a quad past one is cut into two triangles that mostly are not:
            # a move never trades a quad for a triangle (one past triangle outweighs every quad round a node)
            # (a quad no cut can mend counts as a triangle)
            p0, l0, _f0, h0 = judge(ids)
            before_past = np.zeros(len(S), dtype=np.int64)
            np.add.at(before_past, who, p0.astype(np.int64) * weigh(ids, h0))
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
            mean = np.where(cnt[:, None] > 0, acc / np.maximum(cnt, 1.0)[:, None], old)
            cands = [mean]
            if (RELAX_PARALLELOGRAM if parallelogram is None else parallelogram) and nq:
                # SHMESH-14b: where each quad round the node would make it a parallelogram (b + d - c, b and d its
                # neighbours in the quad, c across), averaged; and half-way to each - the middle alone left
                # tapered quads tapered
                iso = np.zeros((len(S), 3))
                icnt = np.zeros(len(S))
                for j in range(4):
                    at = np.isin(quads[:, j], S)
                    if at.any():
                        qq = quads[at]
                        ii = np.array([pos[int(v)] for v in qq[:, j]], dtype=np.int64)
                        np.add.at(iso, ii, X[qq[:, (j + 1) % 4]] + X[qq[:, (j + 3) % 4]] - X[qq[:, (j + 2) % 4]])
                        np.add.at(icnt, ii, 1.0)
                iso = np.where(icnt[:, None] > 0, iso / np.maximum(icnt, 1.0)[:, None], mean)
                cands += [iso, 0.5 * (old + mean), 0.5 * (old + iso), 0.5 * (mean + iso)]
            best = old.copy()
            best_past = before_past.copy()
            best_least = before_least.copy()
            moved = np.zeros(len(S), dtype=bool)
            if project is not None:
                cands = [project(S, c) for c in cands]
            for cand in cands:
                X[S[go]] = cand[go]
                p1, l1, f1, h1 = judge(ids)
                after_past = np.zeros(len(S), dtype=np.int64)
                np.add.at(after_past, who, p1.astype(np.int64) * weigh(ids, h1))
                after_least = np.full(len(S), 180.0)
                np.minimum.at(after_least, who, l1)
                after_flip = np.zeros(len(S), dtype=bool)
                np.logical_or.at(after_flip, who, f1)
                better = go & ~after_flip & ((after_past < best_past) |
                                             ((after_past == best_past) & (after_least > best_least + 1e-9)))
                best[better] = cand[better]
                best_past[better] = after_past[better]
                best_least[better] = after_least[better]
                moved |= better
                X[S] = old
            X[S] = best
            changed += int(moved.sum())
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


def _cycle(a: tuple[int, ...], b: tuple[int, ...]) -> list[int] | None:
    """The outline (in their turning sense) of two elements that share exactly one edge, run opposite ways by the
    two; None when they share no edge, more than one, run it the same way, or the outline is not one simple loop."""
    da = [(a[i], a[(i + 1) % len(a)]) for i in range(len(a))]
    db = [(b[i], b[(i + 1) % len(b)]) for i in range(len(b))]
    sa = {(min(u, v), max(u, v)) for u, v in da}
    shared = [e for e in ((min(u, v), max(u, v)) for u, v in db) if e in sa]
    if len(shared) != 1:
        return None
    s = shared[0]
    rest = [(u, v) for u, v in da + db if (min(u, v), max(u, v)) != s]
    if sum(1 for u, v in da if (min(u, v), max(u, v)) == s and (u, v) in db) or len(rest) != len(da) + len(db) - 2:
        return None
    nxt: dict[int, int] = {}
    for u, v in rest:
        if u in nxt:
            return None
        nxt[u] = v
    start = rest[0][0]
    loop = [start]
    while True:
        nv = nxt.get(loop[-1])
        if nv is None:
            return None
        if nv == start:
            break
        if nv in loop:
            return None
        loop.append(nv)
    return loop if len(loop) == len(rest) else None


def _good_quad(xyz: np.ndarray, q: list[int], normal: np.ndarray) -> float | None:
    """The quad's smallest corner angle when it is valid, inside every Nastran quad limit and turns along `normal`;
    else None."""
    m = quad_measures(xyz, np.array([q], dtype=np.int64))
    if bool(_quad_past(m)[0]):
        return None
    p = xyz[q]
    if float(np.dot(np.cross(p[2] - p[0], p[3] - p[1]), normal)) <= 0:
        return None
    return float(m["min_angle_quad"][0])


def _turns_along(xyz: np.ndarray, t: list[int], normal: np.ndarray) -> bool:
    p = xyz[t]
    return float(np.dot(np.cross(p[1] - p[0], p[2] - p[0]), normal)) > 0


def walk_trias(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int], *,
               depth: int = 8, targets: int = 6) -> tuple[np.ndarray, np.ndarray, list[int], list[int], int]:
    """SHMESH-18: a triangle is walked through the quads of its face to the nearest other triangle of that face and
    the two become one quad. Each step re-cuts the triangle and the quad ahead (a pentagon) into a quad and a
    triangle that touches the next element of the path; the last step joins the two triangles. A walk is kept only
    when every quad it leaves is valid, turns as the face does and is inside every one of Nastran's own quad limits;
    the path's outline (and every face's own edges, and every node) stays as it was. Paths of at most `depth` quads,
    the `targets` nearest triangles tried; triangles in number order. Deterministic.

    Returns (quads, triangles, their faces, how many pairs of triangles became quads)."""
    elems: dict[int, tuple[int, ...]] = {}
    face: dict[int, int] = {}
    for i, c in enumerate(quads.tolist()):
        elems[i] = tuple(c)
        face[i] = q_face[i]
    base = len(elems)
    for i, c in enumerate(trias.tolist()):
        elems[base + i] = tuple(c)
        face[base + i] = t_face[i]
    users: dict[tuple[int, int], set[int]] = {}

    def edges(c: tuple[int, ...]):
        return [(min(c[i], c[(i + 1) % len(c)]), max(c[i], c[(i + 1) % len(c)])) for i in range(len(c))]

    sides: dict[int, list[tuple[int, int]]] = {}
    for e, c in elems.items():
        sides[e] = edges(c)
        for k in sides[e]:
            users.setdefault(k, set()).add(e)

    def across(e: int) -> list[tuple[int, tuple[int, int]]]:
        out = []
        for k in sides[e]:
            u = users[k]
            if len(u) == 2:
                o = min(u - {e}) if len(u - {e}) == 1 else None
                if o is not None and face[o] == face[e]:
                    out.append((o, k))
        return sorted(out)

    def remove(e: int) -> None:
        for k in sides.pop(e):
            users[k].discard(e)
        del elems[e]

    nxt_id = [base + len(trias)]

    def add(c: tuple[int, ...], f: int) -> int:
        e = nxt_id[0]
        nxt_id[0] += 1
        elems[e] = c
        face[e] = f
        sides[e] = edges(c)
        for k in sides[e]:
            users.setdefault(k, set()).add(e)
        return e

    def _n(c: tuple[int, ...]) -> np.ndarray:
        p = xyz[list(c)]
        return np.cross(p[2] - p[0], p[3] - p[1]) if len(c) == 4 else np.cross(p[1] - p[0], p[2] - p[0])

    def simulate(tri: tuple[int, ...], path: list[int], goal: int):
        """The quads that replace the path and the two triangles, or None."""
        if not path:
            loop = _cycle(tri, elems[goal])
            if loop is None or len(loop) != 4:
                return None
            return [loop] if _good_quad(xyz, loop, _n(elems[goal])) is not None else None
        q = elems[path[0]]
        normal = _n(q)
        loop = _cycle(tri, q)
        if loop is None or len(loop) != 5:
            return None
        ahead = elems[path[1]] if len(path) > 1 else elems[goal]
        e_next = set(edges(q)) & set(edges(ahead))
        if len(e_next) != 1:
            return None
        u, v = next(iter(e_next))
        options = []
        for i in range(5):
            t = [loop[i - 1], loop[i], loop[(i + 1) % 5]]
            if not ({u, v} <= set(t) and ({u, v} == {t[0], t[1]} or {u, v} == {t[1], t[2]})):
                continue
            quad = [loop[(i + 1) % 5], loop[(i + 2) % 5], loop[(i + 3) % 5], loop[(i + 4) % 5]]
            if not _turns_along(xyz, t, normal):
                continue
            g = _good_quad(xyz, quad, normal)
            if g is not None:
                options.append((-round(g, 9), i, quad, tuple(t)))
        for _g, _i, quad, tt in sorted(options):
            rest = simulate(tt, path[1:], goal)
            if rest is not None:
                return [quad] + rest
        return None

    walked = 0
    for start in range(base, base + len(trias)):
        if start not in elems:
            continue
        f0 = face[start]
        c0 = elems[start]
        # breadth first through the face's quads to its nearest triangles
        parent = {start: -1}
        level = [start]
        found: list[int] = []
        for _d in range(depth + 1):
            nxt_level = []
            for e in level:
                for o, _k in across(e):
                    if o in parent:
                        continue
                    parent[o] = e
                    if len(elems[o]) == 3:
                        found.append(o)
                    else:
                        nxt_level.append(o)
            if len(found) >= targets or not nxt_level:
                break
            level = nxt_level
        for goal in found[:targets]:
            path = []
            e = parent[goal]
            while e != start:
                path.append(e)
                e = parent[e]
            path.reverse()
            got = simulate(c0, path, goal)
            if got is None:
                continue
            for e in [start, goal] + path:
                remove(e)
            for qd in got:
                add(tuple(qd), f0)
            walked += 1
            break
    Q = [(e, c) for e, c in sorted(elems.items()) if len(c) == 4]
    T = [(e, c) for e, c in sorted(elems.items()) if len(c) == 3]
    return (np.array([c for _e, c in Q], dtype=np.int64).reshape(-1, 4),
            np.array([c for _e, c in T], dtype=np.int64).reshape(-1, 3),
            [face[e] for e, _c in Q], [face[e] for e, _c in T], walked)


def pair_trias(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, t_face: list[int]
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SHMESH-17: two triangles of one face across an edge that is theirs alone become one quad when that quad is
    valid and inside every one of Nastran's own quad limits. Pairs are taken best first (the larger smallest corner
    angle, then the lower triangle numbers), each triangle once. Deterministic.

    Returns (the new quads (k, 4) in the first triangle's turning sense, the indices of the triangles kept, the
    triangle each new quad's face comes from)."""
    none = np.zeros((0, 4), dtype=np.int64), np.arange(len(trias)), np.zeros(0, dtype=np.int64)
    if len(trias) < 2:
        return none
    users: dict[tuple[int, int], list[int]] = {}
    for i, c in enumerate(quads.tolist()):
        for k in range(4):
            a, b = c[k], c[(k + 1) % 4]
            users.setdefault((min(a, b), max(a, b)), []).append(-1 - i)
    for i, c in enumerate(trias.tolist()):
        for k in range(3):
            a, b = c[k], c[(k + 1) % 3]
            users.setdefault((min(a, b), max(a, b)), []).append(i)
    cand_q, cand_t = [], []
    for i, c in enumerate(trias.tolist()):
        for k in range(3):
            a, b, p = c[k], c[(k + 1) % 3], c[(k + 2) % 3]        # i runs a -> b, p opposite
            u = users[(min(a, b), max(a, b))]
            if len(u) != 2 or min(u) < 0:
                continue
            j = u[0] if u[1] == i else u[1]
            if j <= i or t_face[j] != t_face[i]:
                continue
            rest = [x for x in trias[j].tolist() if x not in (a, b)]
            if len(rest) != 1:
                continue
            cand_q.append([a, rest[0], b, p])                     # a -> s -> b -> p keeps i's turning sense
            cand_t.append((i, j))
    if not cand_q:
        return none
    Q = np.array(cand_q, dtype=np.int64)
    m = quad_measures(xyz, Q)
    ok = ~_quad_past(m)
    # the quad must turn the way both its triangles do
    n_q = np.cross(xyz[Q[:, 2]] - xyz[Q[:, 0]], xyz[Q[:, 3]] - xyz[Q[:, 1]])
    for col in (0, 1):
        t = trias[np.array([ct[col] for ct in cand_t])]
        n_t = np.cross(xyz[t[:, 1]] - xyz[t[:, 0]], xyz[t[:, 2]] - xyz[t[:, 0]])
        ok &= np.einsum("mi,mi->m", n_q, n_t) > 0
    order = sorted(np.flatnonzero(ok).tolist(), key=lambda r: (-round(float(m["min_angle_quad"][r]), 9), cand_t[r]))
    used: set[int] = set()
    new_q, came = [], []
    for r in order:
        i, j = cand_t[r]
        if i in used or j in used:
            continue
        used |= {i, j}
        new_q.append(cand_q[r])
        came.append(i)
    keep = np.array([i for i in range(len(trias)) if i not in used], dtype=np.int64)
    return np.array(new_q, dtype=np.int64).reshape(-1, 4), keep, np.array(came, dtype=np.int64)


#: SHMESH-22: a cap is a triangle past a limit whose widest corner is at least this open (its apex near its longest
#: edge); the element it is absorbed into must face the same way within CAP_NORMAL_DEG
CAP_MIN_DEG = 120.0
CAP_NORMAL_DEG = 5.0


def _splits(poly: list[int]) -> list[list[list[int]]]:
    """The ways to cut a polygon of 4 or 5 corners (in order) into a quad and the rest, or into triangles: lists of
    elements, each in the polygon's turning sense, in a fixed order."""
    n = len(poly)
    r = [poly[k:] + poly[:k] for k in range(n)]
    if n == 4:
        return [[poly], [r[0][:3], [r[0][0], r[0][2], r[0][3]]], [r[1][:3], [r[1][0], r[1][2], r[1][3]]]]
    out = [[c[:4], [c[3], c[4], c[0]]] for c in r]                       # a quad and a triangle
    out += [[c[:3], [c[0], c[2], c[3]], [c[0], c[3], c[4]]] for c in r]   # three triangles, fanned from a corner
    return out


def _elem_normal(xyz: np.ndarray, c: list[int]) -> np.ndarray:
    p = xyz[c]
    if len(c) == 4:
        return np.cross(p[2] - p[0], p[3] - p[1])
    return np.cross(p[1] - p[0], p[2] - p[0])


def _past_one(xyz: np.ndarray, c: list[int]) -> bool:
    a = np.array([c], dtype=np.int64)
    return bool(_quad_past(quad_measures(xyz, a))[0]) if len(c) == 4 else bool(_tria_past(tria_measures(xyz, a))[0])


def _least_angle(xyz: np.ndarray, c: list[int]) -> float:
    return float(_angles(xyz[np.array([c])])[0].min())


def absorb_caps(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                face_wall: np.ndarray, *, rounds: int = 4):
    """SHMESH-22: a cap - a triangle past one of Nastran's limits whose apex lies near its longest edge (its widest
    corner over CAP_MIN_DEG) - left once every other step is done. Its three corners are on CAD edges (a face's
    corner that closes almost flat, two edges that run close): no node of it can move and no swap inside its face
    mends it. So the longest edge goes:
    - across an edge that only it and one other element hold (inside its face, or a seam between two faces of the
      same wall facing the same way within CAP_NORMAL_DEG): the cap and that element are taken as one polygon (the
      element with the apex added between the edge's ends) and cut again into the fewest elements all inside
      Nastran's limits (a quad and a triangle, or two triangles; the largest smallest corner first), on the other
      element's face. The seam then runs through the apex: off its curve by the cap's height, under a tenth of the
      edge;
    - on a free edge, its two other edges held by one more element each: the cap is dropped, and the free edge
      runs through the apex (the area lost is the cap's, a sliver).
    Never across a junction edge (three or more elements). Deterministic: caps in number order, `rounds` passes.

    Returns (quads, trias, q_face, t_face, how many caps went)."""
    Q = [list(map(int, c)) for c in quads.tolist()]
    T = [list(map(int, c)) for c in trias.tolist()]
    qf, tf = list(q_face), list(t_face)
    gone_total = 0
    for _ in range(rounds):
        if not T:
            break
        tm = tria_measures(xyz, np.array(T, dtype=np.int64))
        caps = np.flatnonzero(_tria_past(tm) & (tm["max_angle_tria"] >= CAP_MIN_DEG)).tolist()
        if not caps:
            break
        conn = Q + T
        face = qf + tf
        users: dict[tuple[int, int], list[int]] = {}
        for k, c in enumerate(conn):
            for i in range(len(c)):
                a, b = c[i], c[(i + 1) % len(c)]
                users.setdefault((min(a, b), max(a, b)), []).append(k)
        dead: set[int] = set()
        new: list[tuple[list[int], int]] = []
        for i in caps:
            ki = len(Q) + i
            if ki in dead:
                continue
            c = T[i]
            L = [float(np.linalg.norm(xyz[c[k]] - xyz[c[(k + 1) % 3]])) for k in range(3)]
            k = int(np.argmax(L))
            a, b, p = c[k], c[(k + 1) % 3], c[(k + 2) % 3]           # the cap runs a -> b -> p
            others = [u for u in users[(min(a, b), max(a, b))] if u != ki]
            if not others:
                side = [users[(min(x, y), max(x, y))] for x, y in ((b, p), (p, a))]
                if all(len(u) == 2 and not (set(u) & dead) for u in side):
                    dead.add(ki)
                continue
            if len(others) != 1 or others[0] in dead:
                continue
            ke = others[0]
            e = conn[ke]
            if p in e or abs(float(face_wall[face[ke]]) - float(face_wall[face[ki]])) > 1e-9 * max(
                    1.0, float(face_wall[face[ki]])):
                continue
            # the faces are not yet turned alike: an element of another face may run a -> b; it is read the other
            # way round (b -> a, the cap's turning sense) and its new elements turned back to its own sense
            back = any(e[m] == a and e[(m + 1) % len(e)] == b for m in range(len(e)))
            if back:
                e = e[::-1]
            n_c, n_e = _elem_normal(xyz, c), _elem_normal(xyz, e)
            cosv = float(n_c @ n_e) / max(float(np.linalg.norm(n_c) * np.linalg.norm(n_e)), 1e-300)
            if cosv < np.cos(np.radians(CAP_NORMAL_DEG)):
                continue
            # the element now runs b -> a: the apex goes between them
            j = next((m for m in range(len(e)) if e[m] == b and e[(m + 1) % len(e)] == a), None)
            if j is None:
                continue
            poly = e[:j + 1] + [p] + e[j + 1:]
            best = None
            for cut in _splits(poly):
                if any(_past_one(xyz, el) or float(_elem_normal(xyz, el) @ n_e) <= 0 for el in cut):
                    continue
                key = (len(cut), -min(_least_angle(xyz, el) for el in cut))
                if best is None or key < best[0]:
                    best = (key, cut)
            if best is None:
                continue
            dead |= {ki, ke}
            new += [(el[::-1] if back else el, face[ke]) for el in best[1]]
        if not dead:
            break
        gone_total += sum(1 for k in dead if k >= len(Q))
        keep_q = [k for k in range(len(Q)) if k not in dead]
        keep_t = [k for k in range(len(T)) if len(Q) + k not in dead]
        Q, qf, T, tf = ([Q[k] for k in keep_q] + [el for el, _f in new if len(el) == 4],
                        [qf[k] for k in keep_q] + [f for el, f in new if len(el) == 4],
                        [T[k] for k in keep_t] + [el for el, _f in new if len(el) == 3],
                        [tf[k] for k in keep_t] + [f for el, f in new if len(el) == 3])
    return (np.array(Q, dtype=np.int64).reshape(-1, 4), np.array(T, dtype=np.int64).reshape(-1, 3), qf, tf,
            gone_total)


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
           "absorb_caps", "flip_past_trias", "split_past_quads", "tria_measures"]
