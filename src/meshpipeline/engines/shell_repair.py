# Responsibility: Repair a shell quad that is past a limit instead of cutting it into two triangles (SHMESH-83): move
# one of its free corners, or cut it and the element beside it again into other elements of the same kinds - each
# change kept only where NO line gets worse round it (every solver limit, every user target and outer limit, the
# user's edge lengths and their bands, the triangle count, each counted on its own; never a sum across lines).
# Boundaries: pure numpy over the clean-up's arrays (engines/shell_quality's measures). It decides no cut: the cutting
# steps (SHMESH-10, SHMESH-25) run after it on whatever is still past.
# Collaborates with: engines/gmsh/shell_adapter.py (calls it before each cutting step), engines/shell_quality.py.
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from meshpipeline.engines import shell_quality as SQ

#: SHMESH-83: the repair is on (False: the mesh exactly as before)
REPAIR = True
#: passes over the quads still past
REPAIR_ROUNDS = 2
#: a corner is tried at these shares of its star's mean edge, in 8 directions in the star's plane
STEPS = (0.25, 0.1)
#: SHMESH-83: when no one corner will do, a corner and a free node beside it move together (each at fewer spots)
PAIRS = False
PAIR_STEPS = (0.2,)
#: a node moves in a plane only when the elements round it lie in one (within this many degrees): a node on a fold
#: between two planes never moves
FLAT_DEG = 1.0


@dataclass
class Lines:
    """The lines an element is counted against, each on its own: shell_quality.line_matrix (SHMESH-84) - the one
    counting of the clean-up - over the limits in force, the user's edge lines among them (InForce.edges)."""
    limits: SQ.InForce

    def names(self) -> list[str]:
        return SQ.line_names(self.limits)

    def count(self, xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, groups: np.ndarray | None = None,
              n_groups: int = 1) -> np.ndarray:
        """(n_groups, lines) int: per group of elements (a group number per element, quads first; None: all one
        group) how many are past each line."""
        nq, nt = len(quads), len(trias)
        if groups is None:
            groups = np.zeros(nq + nt, dtype=np.int64)
        out = np.zeros((n_groups, len(self.names())), dtype=np.int64)
        for conn, g in ((quads, groups[:nq]), (trias, groups[nq:])):
            if len(conn):
                np.add.at(out, g, SQ.line_matrix(xyz, conn, self.limits).astype(np.int64))
        return out



def _rows_eval(xyz: np.ndarray, conn: np.ndarray, limits: SQ.InForce) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(line rows (elements, lines) int, unclean (elements,) bool, smallest corner angle (elements,)) of elements of
    one shape, from one measure of them."""
    conn = np.asarray(conn, dtype=np.int64)
    nl = len(SQ.line_names(limits))
    if not len(conn):
        return np.zeros((0, nl), dtype=np.int64), np.zeros(0, dtype=bool), np.zeros(0)
    quad = conn.shape[1] == 4
    m = (SQ.quad_measures if quad else SQ.tria_measures)(xyz, conn, extra=True)
    lm = SQ.line_matrix(xyz, conn, limits, m=m)
    h, a = len(limits.hard), len(limits.aims)
    cols = [0] + list(range(1, 1 + h)) + [1 + h + 2 * j for j in range(a)]
    return lm.astype(np.int64), lm[:, cols].any(axis=1), m["min_angle_quad" if quad else "min_angle_tria"]


def unclean(xyz: np.ndarray, conn: np.ndarray, lim: SQ.InForce) -> np.ndarray:
    """(elements,) bool: SHMESH-83 - the element (quads or triangles, by its columns) is not valid, past one of the
    solver's limits or outside one of the user's targets."""
    conn = np.asarray(conn, dtype=np.int64)
    if not len(conn):
        return np.zeros(0, dtype=bool)
    shape = "quad" if conn.shape[1] == 4 else "tria"
    m = (SQ.quad_measures if shape == "quad" else SQ.tria_measures)(xyz, conn, extra=bool(lim.aims))
    bad = (SQ._quad_past if shape == "quad" else SQ._tria_past)(m, lim)
    if lim.aims:
        bad = bad | (SQ.aim_score(m, shape, lim, len(conn)) > 0)
    return bad


def _normal(p: np.ndarray) -> np.ndarray:
    return np.cross(p[2] - p[0], p[3] - p[1]) if len(p) == 4 else np.cross(p[1] - p[0], p[2] - p[0])


def _sides(c: tuple[int, ...]) -> list[tuple[int, int]]:
    k = len(c)
    return [(min(c[i], c[(i + 1) % k]), max(c[i], c[(i + 1) % k])) for i in range(k)]


def _targets(xyz: np.ndarray, Q: np.ndarray, lim: SQ.InForce, stage: str, n_elements: int) -> np.ndarray:
    """The quads the cutting step after this one would cut: past a hard limit ('past', SHMESH-10), or the ones
    split_aimed_quads would cut ('aimed', SHMESH-25: past an outer limit, or over the share the user allows)."""
    if not len(Q):
        return np.zeros(0, dtype=np.int64)
    if stage == "aimed":
        _keep, _cut, came = SQ.split_aimed_quads(xyz, Q, n_elements, lim)
        return np.unique(came)
    return np.flatnonzero(SQ._quad_past(SQ.quad_measures(xyz, Q), lim))


def repair_quads(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, q_face: list[int], t_face: list[int],
                 movable: np.ndarray, pinned: np.ndarray, lines: Lines, *, stage: str = "past",
                 project: Callable | None = None, on_face: np.ndarray | None = None,
                 on_line: np.ndarray | None = None, rounds: int = REPAIR_ROUNDS, diag: dict | None = None
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int], list[int], dict]:
    """SHMESH-83: every quad the next cutting step would cut (stage 'past': past a solver limit; 'aimed': outside one
    of the user's targets) is first repaired where a repair is better on no line worse:

    * move: a free corner of it (movable, not pinned; in a plane only where the elements round it lie in one, else
      laid back by `project` on its curved face or straight CAD edge) tried at STEPS of its star's mean edge in 8
      directions in the star's plane, and where each quad round it would be a parallelogram;
    * re-pair: it and the quad beside it across an edge of their face only (a six-sided outline) cut into two quads
      the other two ways; it and a triangle beside it (five sides) into a quad and a triangle the other ways - never
      more triangles than before, no node moved, no element written twice, every new element turned as its face.

    A change is kept only where, over the elements it changes, no line's count rises (Lines.count: validity, each
    solver limit, each user target and outer limit, the edge lengths and their bands, triangles) and one falls; the
    best of a quad's kept changes is taken (most lines lowered, then the most lowered, then the larger smallest
    corner angle). An element with a pinned node (a ring's, a band's, a grid cell's, a transition cell's) is never
    changed, and a pinned node never moves. Deterministic: quads in number order, `rounds` passes at most.

    Returns (coordinates, quads, triangles, their faces, {"moved": n, "repaired": n})."""
    done = {"moved": 0, "repaired": 0}
    X = np.array(xyz, dtype=float)
    Q = np.array(Q, dtype=np.int64).reshape(-1, 4)
    T = np.array(T, dtype=np.int64).reshape(-1, 3)
    qf, tf = list(q_face), list(t_face)
    if not REPAIR or not len(Q):
        return X, Q, T, qf, tf, done
    lim = lines.limits
    failed: set[int] = set()            # quads no change could repair, and the elements changed since
    changed_last: set[int] = set()
    for _round in range(rounds):
        targets = _targets(X, Q, lim, stage, len(Q) + len(T))
        if not len(targets):
            break
        nq = len(Q)
        conn: list[tuple[int, ...]] = [tuple(c) for c in Q.tolist()] + [tuple(c) for c in T.tolist()]
        face = qf + tf
        at_node: dict[int, list[int]] = {}
        users: dict[tuple[int, int], list[int]] = {}
        for e, c in enumerate(conn):
            for v in c:
                at_node.setdefault(v, []).append(e)
            for k in _sides(c):
                users.setdefault(k, []).append(e)
        corners = {frozenset(c) for c in conn}
        used: set[int] = set()
        new_conn: dict[int, tuple[int, ...]] = {}
        changed = 0
        for qi in targets.tolist():
            if qi in used:
                continue
            if qi in failed and not any(e in changed_last for v in conn[qi] for e in at_node[v]):
                continue                # (nothing round it changed since it was tried)
            best: tuple[Any, str, Any] | None = None       # (rank, kind, payload)
            why: dict[str, Any] = {}
            # --- move one of its corners
            free = [v for v in conn[qi] if movable[v] and not pinned[v]
                    and not any(e in used for e in at_node[v])]
            spots: dict[int, np.ndarray | None] = {}
            for v in free:
                why["free"] = why.get("free", 0) + 1
                spots[v] = _spots(X, conn, at_node, v, project, on_face, on_line)
                if spots[v] is None:
                    continue
                sv = spots[v]
                if sv is None:
                    continue
                got = _best_spots(X, conn, at_node, [v], [sv], lines, qi, why)
                if got is not None and (best is None or got[0] > best[0]):
                    best = (got[0], "move", ([v], got[1]))
            if best is None and PAIRS:
                # --- no one corner will do: a corner and a free node beside it (a corner of an element round it)
                pair_spots: dict[int, np.ndarray | None] = {}
                for v in free:
                    if spots.get(v) is None:
                        continue
                    if v not in pair_spots:
                        pair_spots[v] = _spots(X, conn, at_node, v, project, on_face, on_line, PAIR_STEPS)
                    # (the nodes an edge joins to v: the quad's own neighbours of v and the ones out from it)
                    near = sorted({w for e in at_node[v] for k, w in enumerate(conn[e])
                                   if w != v and v in (conn[e][k - 1], conn[e][(k + 1) % len(conn[e])])
                                   and movable[w] and not pinned[w] and not any(x in used for x in at_node[w])})
                    for w in near:
                        if w in free and w < v:
                            continue                  # (each pair of the quad's own corners once)
                        if w not in pair_spots:
                            pair_spots[w] = _spots(X, conn, at_node, w, project, on_face, on_line, PAIR_STEPS)
                        if pair_spots[v] is None or pair_spots[w] is None:
                            continue
                        pv, pw = pair_spots[v], pair_spots[w]
                        if pv is None or pw is None:
                            continue
                        got = _best_spots(X, conn, at_node, [v, w], [pv, pw], lines, qi)
                        if got is not None and (best is None or got[0] > best[0]):
                            best = (got[0], "move", ([v, w], got[1]))
            # --- re-pair with the element across one of its edges
            if not any(pinned[v] for v in conn[qi]):
                for k in _sides(conn[qi]):
                    u = users.get(k, [])
                    if len(u) != 2:
                        continue
                    ne = u[0] if u[1] == qi else u[1]
                    if ne in used or face[ne] != face[qi] or any(pinned[v] for v in conn[ne]):
                        continue
                    got = _best_repair(X, conn, nq, qi, ne, lines, corners, users)
                    if got is not None and (best is None or got[0] > best[0]):
                        best = (got[0], "pair", (ne, got[1]))
            if best is None:
                failed.add(qi)
                if diag is not None:
                    diag[qi] = why
                continue
            if diag is not None:
                diag.pop(qi, None)
            _rank, kind, pay = best
            if kind == "move":
                vs, where_ = pay
                for v, spot in zip(vs, where_, strict=True):
                    X[v] = spot
                    used.update(at_node[v])
                done["moved"] += len(vs)
            else:
                ne, (el_a, el_b) = pay
                corners -= {frozenset(conn[qi]), frozenset(conn[ne])}
                corners |= {frozenset(el_a), frozenset(el_b)}
                for e, el in ((qi, el_a), (ne, el_b)):
                    for k in _sides(conn[e]):
                        users[k].remove(e)
                    conn[e] = el
                    new_conn[e] = el
                    for k in _sides(el):
                        users.setdefault(k, []).append(e)
                # (an element changes kind only between the two: a quad stays a quad, a triangle a triangle)
                used.update({qi, ne})
                used.update(e for vv in set(el_a) | set(el_b) for e in at_node.get(vv, []))
                done["repaired"] += 1
            changed += 1
        if new_conn:
            for e, el in new_conn.items():
                if e < nq:
                    Q[e] = el
                else:
                    T[e - nq] = el
        changed_last = set(used)
        if not changed:
            break
    return X, Q, T, qf, tf, done


def _rank(before: np.ndarray, after: np.ndarray, least: float) -> tuple | None:
    """A change's standing when no line rises and one falls: (lines lowered, how much lowered, smallest angle)."""
    if np.any(after > before) or not np.any(after < before):
        return None
    return (int((after < before).sum()), int((before - after).sum()), round(float(least), 9))


def _flat(X: np.ndarray, conn: list[tuple[int, ...]], star: list[int]) -> bool:
    nrm = []
    for e in star:
        n = _normal(X[list(conn[e])])
        nn = float(np.linalg.norm(n))
        if nn > 0:
            nrm.append(n / nn)
    return bool(nrm) and all(abs(float(a @ nrm[0])) >= math.cos(math.radians(FLAT_DEG)) for a in nrm)


def _spots(X, conn, at_node, v, project, on_face, on_line, steps=STEPS) -> np.ndarray | None:
    """Where node v may be tried (k, 3): `steps` of its star's mean edge in 8 directions in the star's plane, and where
    each quad round it would be a parallelogram (and half-way there), laid back by `project` on a curved face or a
    straight CAD edge; None when v may not move (a node on a fold between two planes never moves in a plane)."""
    star = at_node[v]
    curved = on_face is not None and v < len(on_face) and int(on_face[v]) >= 0
    sliding = on_line is not None and v < len(on_line) and bool(on_line[v])
    if curved or sliding:
        if project is None:
            return None           # (laid back on its surface or its straight CAD edge, or not moved)
    elif not _flat(X, conn, star):
        return None
    p0 = X[v].copy()
    elems = [conn[e] for e in star]
    nrm = np.zeros(3)
    for c in elems:
        n = _normal(X[list(c)])
        nn = float(np.linalg.norm(n))
        if nn > 0:
            nrm += n / nn
    nn = float(np.linalg.norm(nrm))
    if nn <= 0:
        return None
    nrm /= nn
    others = sorted({w for c in elems for w in c if w != v})
    h = float(np.mean(np.linalg.norm(X[others] - p0, axis=1)))
    e1 = X[others[0]] - p0
    e1 -= nrm * float(e1 @ nrm)
    if h <= 0 or float(np.linalg.norm(e1)) <= 0:
        return None
    e1 /= float(np.linalg.norm(e1))
    e2 = np.cross(nrm, e1)
    cands = [p0 + s * h * (dx * e1 + dy * e2) for s in steps
             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (0.7071, 0.7071), (0.7071, -0.7071),
                            (-0.7071, 0.7071), (-0.7071, -0.7071))]
    for c in elems:
        if len(c) == 4:
            j = c.index(v)
            iso = X[c[(j + 1) % 4]] + X[c[(j + 3) % 4]] - X[c[(j + 2) % 4]]
            cands += [iso, 0.5 * (p0 + iso)]
    P = np.array(cands)
    if project is not None:
        P = SQ.lay_on(project, np.full(len(P), v, dtype=np.int64), P, np.repeat(p0[None], len(P), axis=0))
    return P


def _best_spots(X, conn, at_node, nodes: list[int], spots: list[np.ndarray], lines: Lines, target: int | None,
                why: dict | None = None):
    """The best placing of `nodes` (each at one of its `spots` or where it stands, every combination with one at
    least moved) by _rank, where the quad `target` comes out clean and every element round them turns as before;
    None if there is none. Returns (rank, [the spot of each node])."""
    nodes = list(nodes)
    star = sorted({e for v in nodes for e in at_node[v]})
    elems = [conn[e] for e in star]
    lists = [np.vstack([X[v][None], P]) for v, P in zip(nodes, spots, strict=True)]
    sizes = [len(L) for L in lists]
    grid = np.stack(np.meshgrid(*[np.arange(n) for n in sizes], indexing="ij"), axis=-1).reshape(-1, len(nodes))
    grid = grid[(grid > 0).any(axis=1)]
    n_c = len(grid)
    if not n_c:
        return None
    base = len(X)
    offs = np.cumsum([0] + sizes[:-1])
    XX = np.vstack([X] + lists)
    where = {v: k for k, v in enumerate(nodes)}

    def rows(cs: list[tuple[int, ...]], width: int) -> np.ndarray:
        if not cs:
            return np.zeros((0, width), dtype=np.int64)
        out = np.empty((n_c, len(cs), width), dtype=np.int64)
        for m, c in enumerate(cs):
            for col, w in enumerate(c):
                out[:, m, col] = (base + offs[where[w]] + grid[:, where[w]]) if w in where else w
        return out.reshape(-1, width)

    qs = [c for c in elems if len(c) == 4]
    ts = [c for c in elems if len(c) == 3]
    q_rows, t_rows = rows(qs, 4), rows(ts, 3)
    nl = len(lines.names())
    after = np.zeros((n_c, nl), dtype=np.int64)
    before = np.zeros(nl, dtype=np.int64)
    ok = np.ones(n_c, dtype=bool)
    least = np.full(n_c, 180.0)
    clean = np.ones(n_c, dtype=bool)
    for r_, src, k in ((q_rows, qs, 4), (t_rows, ts, 3)):
        if not len(src):
            continue
        n_old = np.array([_normal(X[list(c)]) for c in src])
        pr = XX[r_]
        n_new = (np.cross(pr[:, 2] - pr[:, 0], pr[:, 3] - pr[:, 1]) if k == 4
                 else np.cross(pr[:, 1] - pr[:, 0], pr[:, 2] - pr[:, 0]))
        dots = np.einsum("mi,mi->m", n_new, np.tile(n_old, (n_c, 1)))
        ok &= (dots.reshape(n_c, len(src)) > 0).all(axis=1)
        lm, bad, ang = _rows_eval(XX, r_, lines.limits)
        g = np.repeat(np.arange(n_c), len(src))
        np.add.at(after, g, lm)
        np.minimum.at(least, g, ang)
        before += _rows_eval(X, np.array(src, dtype=np.int64), lines.limits)[0].sum(axis=0)
        if target is not None and k == 4:
            # (the target quad is one of the star's quads: its rows say whether it comes out clean)
            j = next(m_ for m_, c in enumerate(src) if c == conn[target])
            clean &= ~bad.reshape(n_c, len(src))[:, j]
    if target is not None:
        if why is not None:
            why["clean_spot"] = why.get("clean_spot", 0) + int((ok & clean).any())
            cand = np.flatnonzero(ok & clean)
            if len(cand):
                k_ = cand[int(np.argmin((after[cand] > before[None, :]).sum(axis=1)))]
                why.setdefault("rises", []).extend(np.flatnonzero(after[k_] > before).tolist())
        ok &= clean
    worse = (after > before[None, :]).any(axis=1)
    lower = (after < before[None, :]).any(axis=1)
    cand = np.flatnonzero(ok & ~worse & lower).tolist()
    if not cand:
        return None
    keys = {i: (int((after[i] < before).sum()), int((before - after[i]).sum()), round(float(least[i]), 9))
            for i in cand}
    i = max(cand, key=lambda m: (keys[m], -m))
    return keys[i], [lists[k][grid[i, k]].copy() for k in range(len(nodes))]


def _best_repair(X, conn, nq, qi, ne, lines: Lines, corners: set, users: dict):
    """The best re-pair of quad qi and the element ne across their shared edge by _rank, or None:
    ((rank), (qi's new corners, ne's new corners))."""
    poly = SQ._cycle(conn[qi], conn[ne])
    if poly is None:
        return None
    if len(poly) == 6:
        options = list(SQ._hex_splits(poly))
    elif len(poly) == 5:
        options = [cut for cut in SQ._splits(poly) if len(cut) == 2]
    else:
        return None
    now = {frozenset(conn[qi]), frozenset(conn[ne])}
    rim = {(min(poly[m], poly[(m + 1) % len(poly)]), max(poly[m], poly[(m + 1) % len(poly)]))
           for m in range(len(poly))}
    n_old = _normal(X[list(conn[qi])])
    keep = []
    for cut in options:
        if {frozenset(el) for el in cut} == now:
            continue
        quads = [el for el in cut if len(el) == 4]
        trias = [el for el in cut if len(el) == 3]
        if len(trias) > (len(conn[ne]) == 3):
            continue
        if any(frozenset(el) in corners for el in cut):
            continue                     # (SHMESH-80: never an element already there)
        if any(k not in rim and users.get(k) for el in cut for k in _sides(tuple(el))):
            continue                     # (nor an edge inside the outline another element holds)
        if any(float(_normal(X[el]) @ n_old) <= 0 for el in cut):
            continue
        keep.append((quads, trias))
    if not keep:
        return None
    lim_ = lines.limits
    old_q = np.array([c for c in (conn[qi], conn[ne]) if len(c) == 4], dtype=np.int64).reshape(-1, 4)
    old_t = np.array([c for c in (conn[qi], conn[ne]) if len(c) == 3], dtype=np.int64).reshape(-1, 3)
    (lq, bq, _a), (lt, bt, _b) = _rows_eval(X, old_q, lim_), _rows_eval(X, old_t, lim_)
    before = lq.sum(axis=0) + lt.sum(axis=0)
    old_bad = int(bq.sum() + bt.sum())
    # every option at once: its quads, then its triangles, each row tagged with its option
    all_q = [el for quads, _t in keep for el in quads]
    all_t = [el for _q, trias in keep for el in trias]
    gq = np.array([k for k, (quads, _t) in enumerate(keep) for _el in quads], dtype=np.int64)
    gt = np.array([k for k, (_q, trias) in enumerate(keep) for _el in trias], dtype=np.int64)
    (lq, bq, aq), (lt, bt, at) = (_rows_eval(X, np.array(all_q, dtype=np.int64).reshape(-1, 4), lim_),
                                  _rows_eval(X, np.array(all_t, dtype=np.int64).reshape(-1, 3), lim_))
    afters = np.zeros((len(keep), len(before)), dtype=np.int64)
    bads = np.zeros(len(keep), dtype=np.int64)
    leasts = np.full(len(keep), 180.0)
    for g_, l_, b_, a_ in ((gq, lq, bq, aq), (gt, lt, bt, at)):
        if len(g_):
            np.add.at(afters, g_, l_)
            np.add.at(bads, g_, b_.astype(np.int64))
            np.minimum.at(leasts, g_, a_)
    best = None
    for k_, (quads, trias) in enumerate(keep):
        if int(bads[k_]) >= old_bad:
            continue
        r = _rank(before, afters[k_], float(leasts[k_]))
        if r is None:
            continue
        # qi keeps a quad; ne keeps its kind
        el_q = tuple(quads[0])
        el_n = tuple(quads[1]) if len(quads) == 2 else tuple(trias[0])
        if best is None or r > best[0]:
            best = (r, (el_q, el_n))
    return best


def pinned_nodes(n: int, quads: np.ndarray, q_face: list[int], made_from: int | None, band_nodes,
                 grid_faces: set[int]) -> np.ndarray:
    """(n,) bool: SHMESH-83 - the nodes no repair moves or re-cuts round: every node of an element with a node the
    mesher made itself (a washer's ring, a box band, a transition cell: numbered from `made_from` on), every band node,
    and every node of an element of a grid cell or mapped face (`grid_faces`, face numbers)."""
    out = np.zeros(n, dtype=bool)
    if band_nodes:
        out[np.fromiter(sorted(int(x) for x in band_nodes), dtype=np.int64)] = True
    if len(quads):
        own = (quads >= made_from).any(axis=1) if made_from is not None else np.zeros(len(quads), dtype=bool)
        if grid_faces:
            own |= np.isin(np.asarray(q_face, dtype=np.int64), np.fromiter(sorted(grid_faces), dtype=np.int64))
        out[np.unique(quads[own])] = True
    return out


__all__ = ["Lines", "REPAIR", "pinned_nodes", "repair_quads"]
