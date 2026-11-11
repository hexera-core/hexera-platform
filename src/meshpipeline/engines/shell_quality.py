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
from dataclasses import dataclass

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


#: SHMESH-24: THE LIMITS IN FORCE while a shell mesh is tidied. `hard` is the solver's own (Nastran's, above): no
#: delivered element is left past one when a step can mend it, exactly as before. `aims` are the limits the USER
#: wrote in their notes that are tighter than ours (engines/shell_requirements.limits_in_force): rows of (shape,
#: figure, op, target, outer limit or None, allowed share). The steps aim at them - an element between a target
#: and its outer limit weighs AIM_BAND, one past an outer limit AIM_OUTER - and never trade a hard limit for one.
#: With no requirement the object is IN_FORCE: today's constants, no aims, and every step does what it always did.
AIM_BAND = 1
AIM_OUTER = 8


@dataclass(frozen=True)
class InForce:
    hard: tuple = NASTRAN_LIMITS
    aims: tuple = ()

    @property
    def extra(self) -> bool:
        """Whether a row reads a figure only the user's limits need (quad_measures(extra=True))."""
        return bool(self.aims)


IN_FORCE = InForce()


def aim_score(m: dict[str, np.ndarray], shape: str, limits: InForce, n: int) -> np.ndarray:
    """(n,) int: per element of `shape` with measures `m`, the worst over the user's limits in force - 0 inside
    every target, AIM_BAND between a target and its outer limit, AIM_OUTER past an outer limit."""
    out = np.zeros(n, dtype=np.int64)
    for row_shape, key, op, target, outer, _share in limits.aims:
        if row_shape != shape or key not in m:
            continue
        v = m[key]
        past = (v < target) if op == ">=" else (v > target)
        score = np.where(past, AIM_BAND, 0)
        if outer is not None:
            score = np.where((v < outer) if op == ">=" else (v > outer), AIM_OUTER, score)
        out = np.maximum(out, score)
    return out


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


def _quad_past(q: dict[str, np.ndarray], limits: InForce = IN_FORCE) -> np.ndarray:
    """(quads,) bool: the quad is past one of the hard quad limits in force - Nastran's own - (or not valid)."""
    out = q["min_jacobian"] <= 0
    for shape, key, op, limit, _card in limits.hard:
        if shape == "quad":
            out = out | ((q[key] < limit * (1 + MARGIN)) if op == ">=" else (q[key] > limit * (1 - MARGIN)))
    return out


def _tria_past(t: dict[str, np.ndarray], limits: InForce = IN_FORCE) -> np.ndarray:
    out = t["min_jacobian"] <= 0
    for shape, key, op, limit, _card in limits.hard:
        if shape == "tria":
            out = out | ((t[key] < limit * (1 + MARGIN)) if op == ">=" else (t[key] > limit * (1 - MARGIN)))
    return out


def _quad_aim(xyz: np.ndarray, quads: np.ndarray, limits: InForce) -> np.ndarray:
    """(quads,) int: each quad's score against the user's limits in force (aim_score); zeros with none."""
    if not limits.aims or not len(quads):
        return np.zeros(len(quads), dtype=np.int64)
    return aim_score(quad_measures(xyz, quads, extra=True), "quad", limits, len(quads))


def _tria_aim(xyz: np.ndarray, trias: np.ndarray, limits: InForce) -> np.ndarray:
    if not limits.aims or not len(trias):
        return np.zeros(len(trias), dtype=np.int64)
    return aim_score(tria_measures(xyz, trias, extra=True), "tria", limits, len(trias))


def split_aimed_quads(xyz: np.ndarray, quads: np.ndarray, n_elements: int, limits: InForce
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SHMESH-25: a quad past one of the USER's limits (inside Nastran's own) is cut into two triangles only when
    that mends it - both triangles inside every hard limit and every user target - and only as far as the user's
    own allowance needs: every quad past an outer limit, and of the quads between a target and its outer limit
    only those over the share the user allows, the furthest past first. A cut that leaves a triangle past a limit
    is never made (it would trade one element outside for two). Deterministic.

    Returns (the indices of the quads kept, the new triangles (k, 3), the quad each triangle came from)."""
    none = np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
    everyone = np.arange(len(quads), dtype=np.int64)
    if not limits.aims or not len(quads):
        return (everyone, *none)
    m = quad_measures(xyz, quads, extra=True)
    hard = _quad_past(m, limits)
    want = np.zeros(len(quads), dtype=bool)
    for shape, key, op, target, outer, share in limits.aims:
        if shape != "quad" or key not in m:
            continue
        v = m[key]
        over = ((v < target) if op == ">=" else (v > target)) & ~hard
        if outer is not None:
            beyond = ((v < outer) if op == ">=" else (v > outer)) & ~hard
            want |= beyond
            over &= ~beyond
        spare = int(math.floor(float(share) * n_elements + 1e-9))
        ids = np.flatnonzero(over)
        if len(ids) > spare:
            # the furthest past the target first (then the lower quad number)
            far = (target - v[ids]) if op == ">=" else (v[ids] - target)
            order = ids[np.lexsort((ids, -np.round(far, 9)))]
            want[order[:len(ids) - spare]] = True
    cand = np.flatnonzero(want)
    if not len(cand):
        return (everyone, *none)
    q = quads[cand]
    cuts = (np.stack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=1),      # diagonal 0-2
            np.stack([q[:, [0, 1, 3]], q[:, [1, 2, 3]]], axis=1))      # diagonal 1-3
    ok, least = [], []
    for c in cuts:
        t = c.reshape(-1, 3)
        tm = tria_measures(xyz, t, extra=True)
        bad = _tria_past(tm, limits) | (aim_score(tm, "tria", limits, len(t)) > 0)
        ok.append(~bad.reshape(-1, 2).any(axis=1))
        least.append(tm["min_angle_tria"].reshape(-1, 2).min(axis=1))
    second = ok[1] & (~ok[0] | (least[1] > least[0]))
    mend = ok[0] | ok[1]
    if not mend.any():
        return (everyone, *none)
    tri = np.where(second[:, None, None], cuts[1], cuts[0])[mend].reshape(-1, 3)
    cut_ids = cand[mend]
    keep = np.setdiff1d(everyone, cut_ids)
    return keep, tri.astype(np.int64), np.repeat(cut_ids, 2)


def split_past_quads(xyz: np.ndarray, quads: np.ndarray, limits: InForce = IN_FORCE
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SHMESH-10: every quad past one of Nastran's own limits is cut into two triangles on the diagonal whose
    triangles are better (fewer past the triangle limits first, then the larger smallest angle; the first
    diagonal on a tie). A triangle has no warp or taper, and the cut halves the corner that was too open.

    Returns (the indices of the quads kept, the new triangles (k, 3) in the quads' turning sense, the quad
    each triangle came from)."""
    none = np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64)
    if not len(quads):
        return (np.zeros(0, dtype=np.int64), *none)
    past = _quad_past(quad_measures(xyz, quads), limits)
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
        fails = _tria_past(m, limits).reshape(-1, 2).sum(axis=1)
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
               project=None, limits: InForce = IN_FORCE) -> tuple[np.ndarray, int]:
    """SHMESH-14: every movable node (inside a flat face, on no CAD edge) of an element past one of Nastran's own
    limits moves to the middle of its edge neighbours when its elements come out better: fewer past a limit, none
    turned over, and (on a tie) a larger smallest corner angle. The middle of coplanar neighbours lies in their
    plane, so the node stays on its face. Nodes that share no element move together (in number order, the first
    free node taken); `rounds` passes at most. Deterministic. Returns (the moved coordinates, how many moves).
    `project(nodes, points)`, when given, lays each tried point back onto its node's own face (SHMESH-14c: nodes
    inside curved faces move too, on their surface).
    `limits`: the limits in force. With the user's own limits among them (SHMESH-25) a node of an element past one
    moves too, when its elements' score against them comes down and no more of them are past a hard limit."""
    aimed = bool(limits.aims)
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

    def judge(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Per element of `ids`: past a limit (or turned over), its smallest corner angle, turned over, (quads)
        past a limit with no cut into two triangles inside the triangle limits, and its score against the user's
        limits in force (zeros with none)."""
        past = np.zeros(len(ids), dtype=bool)
        hard = np.zeros(len(ids), dtype=bool)
        least = np.full(len(ids), 180.0)
        flip = np.zeros(len(ids), dtype=bool)
        aim = np.zeros(len(ids), dtype=np.int64)
        isq = ids < nq
        if isq.any():
            m = quad_measures(X, elem[ids[isq]], extra=aimed)
            past[isq] = _quad_past(m, limits)
            least[isq] = m["min_angle_quad"]
            flip[isq] = m["min_jacobian"] <= 0
            if aimed:
                aim[isq] = aim_score(m, "quad", limits, int(isq.sum()))
            qi = np.flatnonzero(isq)[past[isq]]
            if len(qi):
                q = elem[ids[qi]]
                fails = [_tria_past(tria_measures(X, c.reshape(-1, 3)), limits).reshape(-1, 2).any(axis=1)
                         for c in (np.stack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=1),
                                   np.stack([q[:, [0, 1, 3]], q[:, [1, 2, 3]]], axis=1))]
                hard[qi] = fails[0] & fails[1]
        if (~isq).any():
            m = tria_measures(X, elem[ids[~isq], :3], extra=aimed)
            past[~isq] = _tria_past(m, limits)
            least[~isq] = m["min_angle_tria"]
            flip[~isq] = m["min_jacobian"] <= 0
            if aimed:
                aim[~isq] = aim_score(m, "tria", limits, int((~isq).sum()))
        return past, least, flip, hard, aim

    def weigh(ids: np.ndarray, hard: np.ndarray) -> np.ndarray:
        """Each element's weight when past a limit: weights[1] for a triangle; for a quad weights[0], or weights[1]
        when no cut can mend it and quads weigh less than triangles (before the cut)."""
        wq = np.where(hard & (weights[0] < weights[1]), weights[1], weights[0])
        return np.where(ids < nq, wq, weights[1])

    moves = 0
    for _ in range(rounds):
        everyone = np.arange(len(elem))
        judged = judge(everyone)
        bad_el = everyone[judged[0] | (judged[4] > 0)] if aimed else everyone[judged[0]]
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
            p0, l0, _f0, h0, a0 = judge(ids)
            before_past = np.zeros(len(S), dtype=np.int64)
            np.add.at(before_past, who, p0.astype(np.int64) * weigh(ids, h0))
            before_aim = np.zeros(len(S), dtype=np.int64)
            if aimed:
                np.add.at(before_aim, who, a0)
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
            go = (cnt > 0) & ((before_past > 0) | (before_aim > 0))
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
            best_aim = before_aim.copy()
            best_least = before_least.copy()
            moved = np.zeros(len(S), dtype=bool)
            if project is not None:
                cands = [project(S, c) for c in cands]
            for cand in cands:
                X[S[go]] = cand[go]
                p1, l1, f1, h1, a1 = judge(ids)
                after_past = np.zeros(len(S), dtype=np.int64)
                np.add.at(after_past, who, p1.astype(np.int64) * weigh(ids, h1))
                after_aim = np.zeros(len(S), dtype=np.int64)
                if aimed:
                    np.add.at(after_aim, who, a1)
                after_least = np.full(len(S), 180.0)
                np.minimum.at(after_least, who, l1)
                after_flip = np.zeros(len(S), dtype=bool)
                np.logical_or.at(after_flip, who, f1)
                # fewer past a hard limit; then (SHMESH-25) a lower score against the user's limits; then the larger
                # smallest corner angle. With no user limit both scores are 0 and this is the rule it always was
                better = go & ~after_flip & ((after_past < best_past) |
                                             ((after_past == best_past) & (after_aim < best_aim)) |
                                             ((after_past == best_past) & (after_aim == best_aim)
                                              & (after_least > best_least + 1e-9)))
                best[better] = cand[better]
                best_past[better] = after_past[better]
                best_aim[better] = after_aim[better]
                best_least[better] = after_least[better]
                moved |= better
                X[S] = old
            X[S] = best
            changed += int(moved.sum())
        moves += changed
        if not changed:
            break
    return X, moves


def _tria_score(xyz: np.ndarray, t: np.ndarray, limits: InForce = IN_FORCE) -> tuple[int, int, float]:
    """(how many of the triangles are past a hard limit, their score against the user's limits in force - 0 with
    none -, their smallest skew): lower, lower, then higher is better."""
    m = tria_measures(xyz, t, extra=limits.extra)
    aim = int(aim_score(m, "tria", limits, len(t)).sum()) if limits.aims else 0
    return int(_tria_past(m, limits).sum()), aim, float(m["skew_deg"].min())


def flip_past_trias(xyz: np.ndarray, trias: np.ndarray, t_face: list[int], *, rounds: int = 4,
                    limits: InForce = IN_FORCE) -> tuple[np.ndarray, int]:
    """SHMESH-13: a triangle past one of Nastran's limits and the triangle across its longest edge (the same face,
    that edge theirs alone) swap that edge for the other diagonal when the two new triangles are better (fewer past
    a limit, then the larger smallest skew) and turn the same way. A face's own edges never move. Deterministic:
    triangles in number order, `rounds` passes at most. Returns (the triangles, how many swaps)."""
    T = trias.copy()
    swaps = 0
    for _ in range(rounds):
        if len(T) and limits.aims:
            # SHMESH-25: and a triangle past one of the user's limits in force
            tm = tria_measures(xyz, T, extra=True)
            past = np.flatnonzero(_tria_past(tm, limits) | (aim_score(tm, "tria", limits, len(T)) > 0))
        else:
            past = np.flatnonzero(_tria_past(tria_measures(xyz, T), limits)) if len(T) else np.zeros(0, dtype=np.int64)
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
            (f_new, a_new, s_new), (f_old, a_old, s_old) = (_tria_score(xyz, new, limits),
                                                            _tria_score(xyz, T[[i, j]], limits))
            if (f_new, a_new) < (f_old, a_old) or ((f_new, a_new) == (f_old, a_old) and s_new > s_old):
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


def _good_quad(xyz: np.ndarray, q: list[int], normal: np.ndarray, limits: InForce = IN_FORCE) -> float | None:
    """The quad's smallest corner angle when it is valid, inside every Nastran quad limit (and every target of the
    user's limits in force, SHMESH-25) and turns along `normal`; else None."""
    m = quad_measures(xyz, np.array([q], dtype=np.int64), extra=limits.extra)
    if bool(_quad_past(m, limits)[0]) or (limits.aims and int(aim_score(m, "quad", limits, 1)[0]) > 0):
        return None
    p = xyz[q]
    if float(np.dot(np.cross(p[2] - p[0], p[3] - p[1]), normal)) <= 0:
        return None
    return float(m["min_angle_quad"][0])


def _turns_along(xyz: np.ndarray, t: list[int], normal: np.ndarray) -> bool:
    p = xyz[t]
    return float(np.dot(np.cross(p[1] - p[0], p[2] - p[0]), normal)) > 0


def walk_trias(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int], *,
               depth: int = 8, targets: int = 6, limits: InForce = IN_FORCE
               ) -> tuple[np.ndarray, np.ndarray, list[int], list[int], int]:
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
            return [loop] if _good_quad(xyz, loop, _n(elems[goal]), limits) is not None else None
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
            g = _good_quad(xyz, quad, normal, limits)
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


def pair_trias(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, t_face: list[int],
               limits: InForce = IN_FORCE) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
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
    m = quad_measures(xyz, Q, extra=limits.extra)
    ok = ~_quad_past(m, limits)
    if limits.aims:
        # SHMESH-25: and the quad must score no worse against the user's limits than its two triangles do
        t_aim = _tria_aim(xyz, trias, limits)
        pairs_aim = np.array([t_aim[i] + t_aim[j] for i, j in cand_t], dtype=np.int64)
        ok &= aim_score(m, "quad", limits, len(Q)) <= pairs_aim
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


def _past_one(xyz: np.ndarray, c: list[int], limits: InForce = IN_FORCE) -> bool:
    a = np.array([c], dtype=np.int64)
    if len(c) == 4:
        return bool(_quad_past(quad_measures(xyz, a), limits)[0])
    return bool(_tria_past(tria_measures(xyz, a), limits)[0])


def _aim_one(xyz: np.ndarray, c: list[int], limits: InForce) -> int:
    a = np.array([c], dtype=np.int64)
    return int((_quad_aim if len(c) == 4 else _tria_aim)(xyz, a, limits)[0])


def _least_angle(xyz: np.ndarray, c: list[int]) -> float:
    return float(_angles(xyz[np.array([c])])[0].min())


def absorb_caps(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                face_wall: np.ndarray, *, rounds: int = 4, limits: InForce = IN_FORCE):
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
        tm = tria_measures(xyz, np.array(T, dtype=np.int64), extra=limits.extra)
        cap_past = _tria_past(tm, limits)
        if limits.aims:
            # SHMESH-25: and a cap past one of the user's OUTER limits (their "none beyond")
            cap_past = cap_past | (aim_score(tm, "tria", limits, len(T)) >= AIM_OUTER)
        caps = np.flatnonzero(cap_past & (tm["max_angle_tria"] >= CAP_MIN_DEG)).tolist()
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
            before_aim = (_aim_one(xyz, c, limits) + _aim_one(xyz, e, limits)) if limits.aims else 0
            for cut in _splits(poly):
                if any(_past_one(xyz, el, limits) or float(_elem_normal(xyz, el) @ n_e) <= 0 for el in cut):
                    continue
                if limits.aims and sum(_aim_one(xyz, el, limits) for el in cut) > before_aim:
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


#: SHMESH-28: in the mend, an element past a hard limit (or not valid) costs this; one between a user's target and
#: its outer limit AIM_BAND, one past an outer limit AIM_OUTER. No number of aims outweighs a hard limit
MEND_HARD = 1000
#: ... and an element that is not valid (a corner turned over, no area) is never made
MEND_INVALID = 1000000
#: SHMESH-28: a node on a CAD edge may be merged into its neighbour along that edge only when the edge stays in
#: place: the node's distance from the line through its two neighbours along the edge, as a share of that line
#: (0 on a straight edge; 0.033 on an arc meshed at 15 degrees a segment)
MEND_CHORD = 0.01
MEND_ROUNDS = 8
#: node kinds (the mesher's): inside a face, on a CAD edge, at a CAD vertex
INSIDE, ON_EDGE, AT_VERTEX = 0, 1, 2


def _hex_splits(poly: list[int]) -> list[list[list[int]]]:
    """The three ways to cut a six-cornered outline into two quads."""
    return [[[poly[k], poly[(k + 1) % 6], poly[(k + 2) % 6], poly[(k + 3) % 6]],
             [poly[(k + 3) % 6], poly[(k + 4) % 6], poly[(k + 5) % 6], poly[k]]] for k in range(3)]


def _costs(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, limits: InForce
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(cost per quad, cost per triangle, smallest corner angle per quad, per triangle): MEND_INVALID when not valid,
    MEND_HARD past a hard limit, else the score against the user's limits in force (0 inside every target)."""
    if len(quads):
        m = quad_measures(xyz, quads, extra=limits.extra)
        cq = np.where(_quad_past(m, limits), MEND_HARD, aim_score(m, "quad", limits, len(quads)))
        cq = np.where(m["min_jacobian"] <= 0, MEND_INVALID, cq)
        aq = m["min_angle_quad"]
    else:
        cq, aq = np.zeros(0, dtype=np.int64), np.zeros(0)
    if len(trias):
        m = tria_measures(xyz, trias, extra=limits.extra)
        ct = np.where(_tria_past(m, limits), MEND_HARD, aim_score(m, "tria", limits, len(trias)))
        ct = np.where(m["min_jacobian"] <= 0, MEND_INVALID, ct)
        at = m["min_angle_tria"]
    else:
        ct, at = np.zeros(0, dtype=np.int64), np.zeros(0)
    return cq.astype(np.int64), ct.astype(np.int64), aq, at


def mend(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
         node_kind: np.ndarray, node_edge: np.ndarray, vertex_edges: dict[int, set[int]], *,
         limits: InForce = IN_FORCE, rounds: int = MEND_ROUNDS, quads_too: bool = True, triangles_too: bool = True
         ) -> tuple[np.ndarray, np.ndarray, list[int], list[int], dict]:
    """SHMESH-28: the last clean-up. Every element past a hard limit, and every element outside one of the user's
    limits in force, is mended by the best of the local changes that leave its neighbourhood better - a lower total
    cost (MEND_HARD past a hard limit, AIM_OUTER past a user's outer limit, AIM_BAND between target and outer limit),
    every new element valid and turned as before:

    * merge: one end of one of its edges is merged into the other. A node inside a face may go anywhere; a node on
      a CAD edge only into its neighbour along that same edge, and only when the edge stays in place (MEND_CHORD);
      a node at a CAD vertex never goes - the CAD's own edges and corners all stay. The mesh must stay a surface
      (the two ends share no other neighbour).
    * re-cut: the element and the one across one of its edges (the same face, that edge theirs alone) are taken
      as one outline and cut again: a triangle and a triangle into a quad or along the other diagonal, a triangle and
      a quad into another quad and triangle (or three triangles), two quads into two quads the other two ways.

    No node moves, so no node leaves its surface or its CAD edge. Candidates of a round are judged together and
    taken best first, each element changed at most once a round; `rounds` passes at most. Deterministic.
    `node_kind` (INSIDE / ON_EDGE / AT_VERTEX), `node_edge` (the CAD edge of an ON_EDGE node) and `vertex_edges`
    (the CAD edges that end at an AT_VERTEX node) come from the mesher. `quads_too` False: no change makes a quad
    (the user asked for triangles); `triangles_too` False: no change leaves more triangles than it found (they
    asked for quads).

    Returns (quads, triangles, their faces, {"merged": nodes merged, "recut": outlines cut again})."""
    Q = [tuple(map(int, c)) for c in quads.tolist()]
    T = [tuple(map(int, c)) for c in trias.tolist()]
    qf, tf = list(q_face), list(t_face)
    done = {"merged": 0, "recut": 0}

    for _round in range(rounds):
        got = _mend_round(xyz, Q, T, qf, tf, node_kind, node_edge, vertex_edges, limits, quads_too, triangles_too,
                          done)
        if got is None:
            break
        Q, qf, T, tf = got
    return (np.array(Q, dtype=np.int64).reshape(-1, 4), np.array(T, dtype=np.int64).reshape(-1, 3), qf, tf, done)


def _mend_round(xyz, Q, T, qf, tf, node_kind, node_edge, vertex_edges, limits, quads_too, triangles_too, done):
    """One pass of mend: (quads, their faces, triangles, their faces) as lists, or None when nothing changed."""
    def on_edge(v: int, c: int) -> bool:
        k = int(node_kind[v])
        return (k == ON_EDGE and int(node_edge[v]) == c) or (k == AT_VERTEX and c in vertex_edges.get(v, ()))

    Qa = np.array(Q, dtype=np.int64).reshape(-1, 4)
    Ta = np.array(T, dtype=np.int64).reshape(-1, 3)
    cq, ct, _aq, _at = _costs(xyz, Qa, Ta, limits)
    nq = len(Q)
    cost = np.concatenate([cq, ct])
    bad = np.flatnonzero(cost > 0)
    if not len(bad):
        return None
    conn = Q + T
    face = qf + tf
    # the elements round the bad ones (every element on a node of a bad element)
    hot = np.zeros(len(xyz), dtype=bool)
    for e in bad.tolist():
        hot[list(conn[e])] = True
    near = np.flatnonzero(np.concatenate([hot[Qa].any(axis=1) if nq else np.zeros(0, dtype=bool),
                                          hot[Ta].any(axis=1) if len(T) else np.zeros(0, dtype=bool)]))
    at_node: dict[int, list[int]] = {}
    users: dict[tuple[int, int], list[int]] = {}
    nbrs: dict[int, set[int]] = {}
    for e in near.tolist():
        c = conn[e]
        k = len(c)
        for i in range(k):
            a, b = c[i], c[(i + 1) % k]
            at_node.setdefault(a, []).append(e)
            users.setdefault((a, b) if a < b else (b, a), []).append(e)
            nbrs.setdefault(a, set()).add(b)
            nbrs.setdefault(b, set()).add(a)

    def may_merge(x: int, y: int) -> bool:
        """Whether node x may be merged into node y (an edge of the mesh joins them)."""
        kx = int(node_kind[x])
        if kx == AT_VERTEX:
            return False
        if kx == ON_EDGE:
            c = int(node_edge[x])
            if not on_edge(y, c):
                return False
            other = [z for z in nbrs.get(x, ()) if z != y and on_edge(z, c)
                     and len(users.get((x, z) if x < z else (z, x), ())) != 2]
            if len(other) != 1:
                return False
            z = other[0]
            line = xyz[z] - xyz[y]
            span = float(np.linalg.norm(line))
            if span <= 0.0:
                return False
            off = float(np.linalg.norm(np.cross(xyz[x] - xyz[y], line))) / span
            if off > MEND_CHORD * span:
                return False
        # the mesh stays a surface: every node next to both is a corner of an element that holds both
        both = set(at_node.get(x, ())) & set(at_node.get(y, ()))
        shared = {v for e in both for v in conn[e]}
        return (nbrs.get(x, set()) & nbrs.get(y, set())) <= shared

    # (the bad element, the elements that go, the new ones as (corners, face, the element each stands for),
    #  what it is, the elements no other change of this round may touch)
    cands: list[tuple[int, tuple[int, ...], list[tuple[tuple[int, ...], int, int]], str, frozenset]] = []
    seen: set = set()
    for e in bad.tolist():
        c = conn[e]
        k = len(c)
        for i in range(k):
            a, b = c[i], c[(i + 1) % k]
            # merge one end of this edge into the other
            for x, y in ((a, b), (b, a)):
                if ("m", x, y) in seen:
                    continue
                seen.add(("m", x, y))
                if not may_merge(x, y):
                    continue
                touched = tuple(sorted(set(at_node[x])))
                new, ok = [], True
                for t in touched:
                    el = conn[t]
                    if y in el:
                        if len(el) == 3:
                            continue                                  # a triangle on the edge goes
                        j = el.index(x)
                        if el[(j + 1) % 4] != y and el[(j - 1) % 4] != y:
                            ok = False                                # across a quad: it would fold
                            return None
                        new.append((tuple(v for v in el if v != x), face[t], t))
                    else:
                        new.append((tuple(y if v == x else v for v in el), face[t], t))
                if ok and (triangles_too or sum(len(el) == 3 for el, _f, _o in new)
                           <= sum(len(conn[t]) == 3 for t in touched)):
                    cands.append((e, touched, new, "merged", frozenset(at_node[x]) | frozenset(at_node[y])))
            # cut the outline of this element and the one across this edge again
            u = users[(a, b) if a < b else (b, a)]
            if len(u) != 2:
                continue
            n = u[0] if u[1] == e else u[1]
            if face[n] != face[e] or ("r", min(e, n), max(e, n)) in seen:
                continue
            seen.add(("r", min(e, n), max(e, n)))
            poly = _cycle(c, conn[n])
            if poly is None:
                continue
            options = _hex_splits(poly) if len(poly) == 6 else _splits(poly)
            now = {frozenset(c), frozenset(conn[n])}
            for cut in options:
                if {frozenset(el) for el in cut} == now:
                    continue
                n_tri = sum(len(el) == 3 for el in cut)
                if (not quads_too and n_tri != len(cut)) or (
                        not triangles_too and n_tri > (len(c) == 3) + (len(conn[n]) == 3)):
                    continue
                pair = (min(e, n), max(e, n))
                cands.append((e, pair, [(tuple(el), face[e], e) for el in cut], "recut", frozenset(pair)))
    if not cands:
        return None
    # judge every new element of every candidate at once
    new_q = [el for cand in cands for el, _f, _o in cand[2] if len(el) == 4]
    new_t = [el for cand in cands for el, _f, _o in cand[2] if len(el) == 3]
    nq_c, nt_c, na_q, na_t = _costs(xyz, np.array(new_q, dtype=np.int64).reshape(-1, 4),
                                    np.array(new_t, dtype=np.int64).reshape(-1, 3), limits)
    normal: dict[int, np.ndarray] = {}

    def turned_as(e: int, touched: tuple[int, ...]) -> np.ndarray:
        """The normal a new element standing for element e must turn along: e's own; for an element with no
        area to speak of, the sum over the changed elements of its face."""
        if e not in normal:
            p = xyz[list(conn[e])]
            n_e = _elem_normal(xyz, list(conn[e]))
            span = float(np.linalg.norm(p - p.mean(axis=0), axis=1).max())
            if float(np.linalg.norm(n_e)) <= 1e-6 * span * span:
                n_e = sum((_elem_normal(xyz, list(conn[t])) for t in touched if face[t] == face[e]), np.zeros(3))
            normal[e] = n_e
        return normal[e]

    iq = it = 0
    ranked = []
    for ci, (e, touched, new, _kind, _block) in enumerate(cands):
        before = int(cost[list(touched)].sum())
        after, least, ok = 0, 180.0, True
        for el, _f, old in new:
            if len(el) == 4:
                after += int(nq_c[iq])
                ok = ok and int(nq_c[iq]) < MEND_INVALID
                least = min(least, float(na_q[iq]))
                iq += 1
            else:
                after += int(nt_c[it])
                ok = ok and int(nt_c[it]) < MEND_INVALID
                least = min(least, float(na_t[it]))
                it += 1
            if ok and float(_elem_normal(xyz, list(el)) @ turned_as(old, touched)) <= 0.0:
                ok = False
        if ok and after < before and len({frozenset(el) for el, _f, _o in new}) == len(new):
            ranked.append((after - before, len(new), -round(least, 9), e, ci))
    if not ranked:
        return None
    used: set[int] = set()
    gone: set[int] = set()
    add: list[tuple[tuple[int, ...], int]] = []
    for _gain, _n, _least, _e, ci in sorted(ranked):
        _e2, touched, new, kind, block = cands[ci]
        if block & used:
            continue
        used |= block
        gone |= set(touched)
        add += [(el, f) for el, f, _o in new]
        done[kind] += 1
    if not gone:
        return None
    return ([c for i, c in enumerate(Q) if i not in gone] + [el for el, _f in add if len(el) == 4],
            [f for i, f in enumerate(qf) if i not in gone] + [f for el, f in add if len(el) == 4],
            [c for i, c in enumerate(T) if nq + i not in gone] + [el for el, _f in add if len(el) == 3],
            [f for i, f in enumerate(tf) if nq + i not in gone] + [f for el, f in add if len(el) == 3])


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-300)


def _angles(p: np.ndarray) -> np.ndarray:
    """Interior angles (degrees) of polygons p (m, k, 3)."""
    k = p.shape[1]
    prev = p[:, [(i - 1) % k for i in range(k)]] - p
    nxt = p[:, [(i + 1) % k for i in range(k)]] - p
    c = np.einsum("mki,mki->mk", _unit(prev), _unit(nxt))
    return np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))


#: the 2 x 2 Gauss points of a 4-node quad
_GAUSS = 1.0 / math.sqrt(3.0)


def quad_measures(xyz: np.ndarray, quads: np.ndarray, *, extra: bool = False) -> dict[str, np.ndarray]:
    """The quads' figures. `extra` adds the figures only a user's limits read (engines/shell_requirements):
    "jacobian" (the smallest over the largest determinant of the Jacobian at the 2 x 2 Gauss points, in the
    element's mean plane), "min_edge" and "max_edge" (mm), and "skew_pre" (90 minus skew_deg: 0 is ideal)."""
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
    out = {"min_jacobian": jac.min(axis=1), "jacobian_ratio": jac.min(axis=1) / np.where(jmax > 0, jmax, 1.0),
           "aspect": lengths.max(axis=1) / np.maximum(lengths.min(axis=1), 1e-300), "warp_deg": warp,
           "warp_coef": warp_coef,
           "skew_deg": ang, "taper": taper, "min_angle_quad": angles.min(axis=1),
           "max_angle_quad": angles.max(axis=1)}
    if extra:
        dets = []
        for xi in (-_GAUSS, _GAUSS):
            for eta in (-_GAUSS, _GAUSS):
                d_xi = 0.25 * ((1 - eta) * (p[:, 1] - p[:, 0]) + (1 + eta) * (p[:, 2] - p[:, 3]))
                d_eta = 0.25 * ((1 - xi) * (p[:, 3] - p[:, 0]) + (1 + xi) * (p[:, 2] - p[:, 1]))
                dets.append(np.einsum("mi,mi->m", np.cross(d_xi, d_eta), n))
        det = np.stack(dets, axis=1)
        dmax = det.max(axis=1)
        out.update(jacobian=det.min(axis=1) / np.where(dmax > 0, dmax, 1.0), min_edge=lengths.min(axis=1),
                   max_edge=lengths.max(axis=1), skew_pre=90.0 - ang)
    return out


def tria_measures(xyz: np.ndarray, trias: np.ndarray, *, extra: bool = False) -> dict[str, np.ndarray]:
    """The triangles' figures. `extra` adds the ones only a user's limits read: "jacobian" (1: a flat 3-node
    triangle has one Jacobian), "min_edge" and "max_edge" (mm), and "skew_pre" (90 minus skew_deg)."""
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
    out = {"min_jacobian": area2 / np.maximum(lengths.max(axis=1) ** 2, 1e-300),
           "aspect": lengths.max(axis=1) / np.maximum(lengths.min(axis=1), 1e-300),
           "skew_deg": np.min(np.stack(skews), axis=0), "min_angle_tria": angles.min(axis=1),
           "max_angle_tria": angles.max(axis=1)}
    if extra:
        out.update(jacobian=np.ones(len(p)), min_edge=lengths.min(axis=1), max_edge=lengths.max(axis=1),
                   skew_pre=90.0 - out["skew_deg"])
    return out


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


__all__ = ["AIM_BAND", "AIM_OUTER", "IN_FORCE", "InForce", "LIMITS", "NASTRAN_LIMITS", "TRIA_SHARE", "aim_score",
           "measure", "nastran_past", "quad_measures", "absorb_caps", "flip_past_trias", "split_aimed_quads",
           "split_past_quads", "tria_measures"]
