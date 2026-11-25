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


def _cross(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """np.cross for 3-vectors (any leading shape, broadcast), written out: the same multiplications and subtractions
    in the same order as numpy's own, so the same floats, without its axis handling (np.cross took more time in the
    clean-up than the products it makes: 64,000 calls, 4.3 s on fr008)."""
    a = np.asarray(a)
    b = np.asarray(b)
    a0, a1, a2 = a[..., 0], a[..., 1], a[..., 2]
    b0, b1, b2 = b[..., 0], b[..., 1], b[..., 2]
    return np.stack(np.broadcast_arrays(a1 * b2 - a2 * b1, a2 * b0 - a0 * b2, a0 * b1 - a1 * b0), axis=-1)

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
def lay_on(project, nodes: np.ndarray, pts: np.ndarray, here: np.ndarray) -> np.ndarray:
    """project(nodes, pts) with the nodes' own places (SHMESH-82c: a projector that takes them searches from there);
    a projector of two arguments is called as it always was."""
    try:
        return project(nodes, pts, here)
    except TypeError:
        return project(nodes, pts)


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
    #: SHMESH-84: the user's edge lines (shortest, shortest less its tolerance, longest, longest plus its tolerance;
    #: mm, None where not asked) - counted as lines by line_matrix, read by no other step
    edges: tuple = ()
    #: SHMESH-86b: the faces (numbers) of grids, mapped strips and cells: no change puts a band element there
    protect: frozenset = frozenset()
    #: SHMESH-86b: the nodes of those faces' elements (the relaxing has no faces)
    protect_nodes: frozenset = frozenset()

    @property
    def extra(self) -> bool:
        """Whether a row reads a figure only the user's limits need (quad_measures(extra=True))."""
        return bool(self.aims)


IN_FORCE = InForce()


def with_edges(limits: InForce, edges: tuple) -> InForce:
    """SHMESH-84: the limits in force carrying the user's edge lines (as _line_counts reads them); unchanged when the
    user named no limit and no edge."""
    if not limits.aims or not any(e is not None for e in edges):
        return limits
    return InForce(hard=limits.hard, aims=limits.aims, edges=tuple(edges), protect=limits.protect,
                   protect_nodes=limits.protect_nodes)


def with_protect(limits: InForce, faces, nodes=()) -> InForce:
    """SHMESH-86b: the limits in force with the faces whose structure no clean-up change may spend the user's
    allowance on; unchanged with no user limit."""
    if not limits.aims:
        return limits
    return InForce(hard=limits.hard, aims=limits.aims, edges=limits.edges, protect=frozenset(int(f) for f in faces),
                   protect_nodes=frozenset(int(v) for v in nodes))


def _band_rows(xyz: np.ndarray, els: np.ndarray, limits: InForce, m: dict | None = None) -> np.ndarray:
    """(elements,) int: SHMESH-86b - 1 where the element is outside one of the user's targets but past none of their
    outer limits (in a band), else 0. `m`: the elements' measures (extra), when at hand."""
    if not len(els):
        return np.zeros(0, dtype=np.int64)
    lm = line_matrix(xyz, els, limits, m=m)
    g = line_groups(limits)
    outer = [c for c in g["severe"].tolist() if c > len(limits.hard)]
    return (lm[:, g["band"]].any(axis=1) & ~lm[:, outer].any(axis=1)).astype(np.int64)


#: SHMESH-84: the clean-up's per-line rule - a relaxing move or a mend change is kept only where no line's count
#: rises round what it touches (each solver limit, each of the user's targets and outer limits, the edge lines, the
#: triangle count, validity: each counted apart, never summed)
PER_LINE = True
#: on by default, held strictly (SHMESH-87b): on the board (60 parts, both modes, set side by side with the summed
#: rule and the ranked, on the parts each made) strict met 2 lines ranked missed and ranked none strict missed, the
#: summed rule missed 12 strict met and strict 7 it met; on a stamped prospect part it
#: left 233 triangles against the summed rule's 257 - the edge lines then rise only for a reason (SHMESH-88)
RELAX_PER_LINE = True
#: SHMESH-86: the relaxing reads the user's allowance (a line within it asks nothing of its band; a move may fill it).
#: SHMESH-86d: off - a relaxing move puts no element into a band and aims at the band's elements too. On, the moves
#: filled each line's allowance round after round and the steps after them could not take it back: on the 80 plates
#: (integration tree, against the bolt-hole base) the jacobian band went 543 -> 2,326, quad max 1,182 -> 2,451, and
#: sh0059 (quad max, quad min) and sh0077 (triangle min) went from met to missed. Off: 1,324 / 2,246, those three
#: lines met again, the triangles 7,525 -> 7,469 (the base 9,543). The mend's own allowance stays on: off, it gave
#: back the triangles (10,615) for its band (jacobian 612)
RELAX_ALLOWANCE = False
MEND_PER_LINE = True
#: SHMESH-84 / SHMESH-87: which form of the rule holds a relaxing move or a mend change when it is on - "strict" (no
#: line's count rises, a band filled within ALLOWANCE_MARGIN of its allowance) or "ranked" (line_rule_ok)
LINE_RULE = "strict"


#: SHMESH-88: an edge line of the user's that is already missed gets no worse without a reason. Under the summed rule
#: (relax and mend, per line off) a relaxing move or a mend change that puts more of the elements it touches under the
#: user's shortest edge, under it less its tolerance, over the longest or over it plus its tolerance is kept only when
#: it takes an element out past an outer or solver limit (or not valid), or out of the band of a user line that is past
#: its allowance. Without it the summed rule traded edges for nothing: on the stamped lab the elements under 0.4 G rose
#: 2107 -> 2204, and a relax round put 5 to 7 short elements back each time the mend after it had taken them off
EDGE_GUARD = True


def edge_lines(xyz: np.ndarray, els: np.ndarray, limits: InForce) -> np.ndarray:
    """(elements, 4) int: SHMESH-88 - each element (all quads or all triangles) under the user's shortest edge, under
    it less its tolerance, over the longest, over it plus its tolerance (1 / 0; a column of zeros where not asked)."""
    els = np.asarray(els, dtype=np.int64)
    out = np.zeros((len(els), 4), dtype=np.int64)
    edges = tuple(limits.edges) + (None,) * (4 - len(limits.edges))
    if not len(els) or all(e is None for e in edges[:4]):
        return out
    ed = np.linalg.norm(xyz[els] - xyz[np.roll(els, -1, axis=1)], axis=2)
    lo, hi = ed.min(axis=1), ed.max(axis=1)
    for k, lim_ in enumerate(edges[:4]):
        if lim_ is not None:
            out[:, k] = (lo < float(lim_)) if k < 2 else (hi > float(lim_))
    return out


def line_groups(limits: InForce) -> dict[str, np.ndarray]:
    """SHMESH-87: the columns of line_matrix by rank - "severe" (not valid, each solver limit, each user outer limit),
    "band" (each user target), "rest" (the edge lines and the triangle count)."""
    h, a = len(limits.hard), len(limits.aims)
    n = len(line_names(limits))
    severe = [0] + list(range(1, 1 + h)) + [1 + h + 2 * j + 1 for j in range(a)]
    band = [1 + h + 2 * j for j in range(a)]
    rest = [k for k in range(n) if k not in severe and k not in band]
    return {"severe": np.array(severe, dtype=np.int64), "band": np.array(band, dtype=np.int64),
            "rest": np.array(rest, dtype=np.int64), "triangles": np.array([n - 1], dtype=np.int64)}


#: SHMESH-88 under the per-line rule: an edge line may rise for a reason - the change takes an element out past an
#: outer or solver limit (or not valid), or out of the band of a user line past its allowance (`missed`). Held with
#: no reason at all, strict refused the fixes those lines were waiting for: on a 272-element bracket a quad taken out
#: past the Jacobian's outer limit was refused 32 times for one element coming under the shortest edge, and the line
#: ended 4 in its band against an allowance of 2
EDGE_REASON = True


def edge_cols(limits: InForce) -> np.ndarray:
    """The four edge lines' columns of line_matrix."""
    n = len(line_names(limits))
    return np.arange(n - 5, n - 1, dtype=np.int64)


#: SHMESH-88b / 86c: an element in a user line's band is NEAR its outer limit within this share of the band's width
#: (target to outer limit) from it, on the inside: 1.5 degrees for 135 / 150, 0.02 for a Jacobian of 0.6 / 0.4.
#: Such an element is one rounding (or one machine) from past the outer limit: the allowance covers the band inside
#: it, not it (SHMESH-86c, NEAR_MEND: within_allowance keeps it a cost, so the mend and the relaxing still take it
#: out where a change breaks no line), and a change that takes it back inside the target has a reason for the soft
#: edge lines to rise (SHMESH-88b: under the shortest edge, over the longest - never their tolerance lines). Free
#: under the allowance, a quad at 149.9 degrees against 150 stayed on a real column's bolt ring
NEAR_OUTER = 0.1
NEAR_MEND = True


def near_outer(xyz: np.ndarray, els: np.ndarray, limits: InForce, m: dict | None = None) -> np.ndarray:
    """(elements, aims) int: SHMESH-88b - each element (all quads or all triangles) within NEAR_OUTER of the band's
    width inside each user line's outer limit (1 / 0; zeros for a line of the other shape or with no outer limit)."""
    els = np.asarray(els, dtype=np.int64)
    out = np.zeros((len(els), len(limits.aims)), dtype=np.int64)
    if not len(els) or not limits.aims:
        return out
    shape = "quad" if els.shape[1] == 4 else "tria"
    if m is None or "min_edge" not in m:
        m = (quad_measures if shape == "quad" else tria_measures)(xyz, els, extra=True)
    for j, (s, key, op, target, outer, _share) in enumerate(limits.aims):
        if s != shape or key not in m or outer is None:
            continue
        v = m[key]
        w = NEAR_OUTER * abs(float(outer) - float(target))
        out[:, j] = ((v >= outer) & (v < outer + w)) if op == ">=" else ((v <= outer) & (v > outer - w))
    return out


def _whole_measures(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray) -> dict:
    """The measures (extra) of every quad and every triangle, as the whole-mesh counts read them."""
    return {"quad": quad_measures(xyz, quads, extra=True) if len(quads) else {},
            "tria": tria_measures(xyz, trias, extra=True) if len(trias) else {}}


def line_missed(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, limits: InForce,
                m: dict | None = None) -> np.ndarray:
    """(lines,) bool: SHMESH-88 - True on each user target whose band (outside the target, inside its outer limit) is
    past the share it allows over the whole mesh: a line missed as the mesh stands. `m`: the mesh's measures
    ({"quad": ..., "tria": ...}, extra), when at hand."""
    out = np.zeros(len(line_names(limits)), dtype=bool)
    n = len(quads) + len(trias)
    if not limits.aims or not n:
        return out
    m = m if m is not None else _whole_measures(xyz, quads, trias)
    base = 1 + len(limits.hard)
    for j, (shape, key, op, target, outer, share) in enumerate(limits.aims):
        if outer is None or key not in m[shape]:
            continue
        v = m[shape][key]
        band = int((((v < target) & (v >= outer)) if op == ">=" else ((v > target) & (v <= outer))).sum())
        out[base + 2 * j] = band > math.floor(float(share) * n + 1e-9)
    return out


#: SHMESH-89: the order, held under either form of the rule - a change that takes an element out past an outer limit or
#: a solver limit (or not valid) and puts none past one is kept whatever it does to a band (even past its allowance),
#: an edge line or the triangle count: an element in a band is a lesser fault than one past an outer line. Held to
#: the slack, strict refused it: on a flat-plate bar part (sh0042, 21960 elements) 145 relaxing moves that took a
#: triangle out past the 20 degree outer line were refused for the quad max-angle band alone, and 729 that took a quad
#: out past Nastran's taper; the part ended 59 triangles past the outer line against 11 under the summed relaxing
SEVERE_FIRST = True
#: SHMESH-89 in the mend (its own switch, to measure it apart)
MEND_SEVERE_FIRST = True
#: SHMESH-89b: each relaxing round under the per-line rule makes the moves out past an outer or solver limit first,
#: in a sweep of their own, then the rest: in one sweep the lesser moves of the nodes before them spent the allowance
RELAX_SEVERE_SWEEP = True
#: SHMESH-89c: the triangle swap (SHMESH-13) under the same order - a swap that puts more of its two triangles past
#: any one outer or solver limit is not made, whatever the summed score says. Summed, on fr008 a swap took a triangle
#: out of the triangle-min band and put one past the triangle-max outer line (the line lost against the base)
FLIP_SEVERE = True


def severe_down(rise: np.ndarray, limits: InForce) -> np.ndarray:
    """(changes,) bool: SHMESH-89 - the change lowers a count past an outer or solver limit (or not valid) and raises
    none."""
    rise = np.atleast_2d(np.asarray(rise))
    sev = line_groups(limits)["severe"]
    return (rise[:, sev] < 0).any(axis=1) & ~(rise[:, sev] > 0).any(axis=1)


def solver_distance(m: dict[str, np.ndarray], shape: str, limits: InForce, n: int) -> np.ndarray:
    """(n,) float: per element of `shape` with measures `m`, how far past the solver limits in force it is, each
    limit's excess over the limit's own size, summed (0 inside every one). SHMESH-31b ranks spots by it."""
    out = np.zeros(n)
    for s, key, op, v, _card in limits.hard:
        if s != shape or key not in m:
            continue
        x = np.asarray(m[key], dtype=float)
        out += (np.maximum(0.0, v - x) if op == ">=" else np.maximum(0.0, x - v)) / max(abs(float(v)), 1e-12)
    return out


def line_rule_ok(rise: np.ndarray, slack: np.ndarray, limits: InForce, rule: str | None = None,
                 missed: np.ndarray | None = None, near: np.ndarray | None = None,
                 severe_first: bool | None = None) -> np.ndarray:
    """(changes,) bool: SHMESH-84 / SHMESH-87 - whether each change (a row of `rise`: after minus before over what it
    touches, per line of line_matrix) is kept. "strict": no line rises beyond `slack` (what is left of ALLOWANCE_MARGIN
    of each user line's allowance; 0 elsewhere). "ranked" - the order held everywhere (valid > solver limits > the
    user's shape lines with their allowances > edges > triangles): (a) no count beyond an outer limit, past a solver
    limit or not valid rises; (b) a user line's band may rise only within its slack, and only where the change lowered
    one of those counts somewhere (or lowered the triangle count with every band rise within its slack); (c) nothing
    else rises. SHMESH-88 (EDGE_REASON), under either: the four edge lines may rise where the change lowers a count
    past an outer or solver limit (or not valid), or the band of a line `missed` marks; SHMESH-88b: the two soft edge
    lines (under the shortest edge, over the longest - not their tolerance lines) also where it lowers a line's band
    and that line's elements near its outer limit together (`near`: (changes, aims), after minus before). SHMESH-89
    (`severe_first`, SEVERE_FIRST when not given): a change that lowers a count past an outer or solver limit (or not
    valid) and raises none is kept under either form, whatever else rises."""
    rise = np.atleast_2d(np.asarray(rise))
    first = severe_down(rise, limits) if (SEVERE_FIRST if severe_first is None else severe_first) else None
    ok = _line_rule_ok(rise, slack, limits, rule, missed, near)
    return ok if first is None else (ok | first)


def _line_rule_ok(rise: np.ndarray, slack: np.ndarray, limits: InForce, rule: str | None,
                  missed: np.ndarray | None, near: np.ndarray | None) -> np.ndarray:
    """line_rule_ok without SHMESH-89."""
    rule = rule or LINE_RULE
    g = line_groups(limits)
    sev, band = g["severe"], g["band"]
    edge = edge_cols(limits) if EDGE_REASON else np.zeros(0, dtype=np.int64)
    if len(edge):
        reason = (rise[:, sev] < 0).any(axis=1)
        if missed is not None and np.any(missed):
            reason |= (rise[:, np.flatnonzero(missed)] < 0).any(axis=1)
        soft = np.zeros(len(rise), dtype=bool)
        if near is not None and len(band):
            soft = ((rise[:, band] < 0) & (np.atleast_2d(np.asarray(near)) < 0)).any(axis=1)
        hard_edge = edge[[1, 3]]                                  # (the tolerance lines)
    if rule == "strict":
        over = rise > slack[None, :]
        ok = ~over.any(axis=1)
        if len(edge):
            tol_up = over[:, hard_edge].any(axis=1)
            over[:, edge] = False
            ok |= ~over.any(axis=1) & (reason | (soft & ~tol_up))
        return ok
    rest, tri = g["rest"], g["triangles"]
    ok = ~(rise[:, sev] > 0).any(axis=1)                          # (a)
    if len(edge):
        rest_ne = np.setdiff1d(rest, edge)
        e_up = rise[:, edge] > 0
        ok &= ~(rise[:, rest_ne] > 0).any(axis=1) & (~e_up.any(axis=1) | reason
                                                     | (soft & ~(rise[:, hard_edge] > 0).any(axis=1)))  # (c), 88
    else:
        ok &= ~(rise[:, rest] > 0).any(axis=1)                    # (c)
    band_up = (rise[:, band] > 0).any(axis=1)
    within = (rise[:, band] <= slack[band][None, :]).all(axis=1)
    paid = (rise[:, sev] < 0).any(axis=1) | (rise[:, tri[0]] < 0)
    ok &= ~band_up | (within & paid)                              # (b)
    return ok


def line_names(limits: InForce) -> list[str]:
    """The columns of line_matrix, named."""
    return (["not valid"] + [str(c) for _s, _k, _o, _v, c in limits.hard]
            + [f"{s} {k} {w}" for s, k, _o, _t, _u, _sh in limits.aims for w in ("target", "outer")]
            + ["under shortest edge", "under shortest edge less tolerance", "over longest edge",
               "over longest edge plus tolerance", "triangles"])


def line_matrix(xyz: np.ndarray, els: np.ndarray, limits: InForce, m: dict | None = None) -> np.ndarray:
    """(elements, lines) bool: SHMESH-84 - each element (all quads or all triangles, by its columns) against every
    line, each apart: not valid (a corner turned over, or no area); past each solver limit in force (as the steps
    hold them, MARGIN in); outside each of the user's targets, past each outer limit (as _line_counts counts them);
    under / over each of the user's edge lines; a triangle. `m`: the elements' measures (extra), when at hand."""
    els = np.asarray(els, dtype=np.int64)
    n = len(els)
    cols: list[np.ndarray] = []
    nl = 1 + len(limits.hard) + 2 * len(limits.aims) + 5
    if not n:
        return np.zeros((0, nl), dtype=bool)
    shape = "quad" if els.shape[1] == 4 else "tria"
    if m is None or "min_edge" not in m:
        m = (quad_measures if shape == "quad" else tria_measures)(xyz, els, extra=True)
    p = xyz[els]
    c = _cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1]) if shape == "quad" else _cross(p[:, 1] - p[:, 0],
                                                                                         p[:, 2] - p[:, 0])
    cols.append((m["min_jacobian"] <= 0) | (np.linalg.norm(c, axis=1) <= 0))
    z = np.zeros(n, dtype=bool)
    for s, key, op, v, _card in limits.hard:
        cols.append(solver_past(m[key], op, v) if s == shape and key in m else z)
    for s, key, op, target, outer, _share in limits.aims:
        if s == shape and key in m:
            val = m[key]
            cols.append((val < target) if op == ">=" else (val > target))
            cols.append(((val < outer) if op == ">=" else (val > outer)) if outer is not None else z)
        else:
            cols += [z, z]
    edges = tuple(limits.edges) + (None,) * (4 - len(limits.edges))
    for lim_, side in zip(edges[:4], ("lo", "lo", "hi", "hi"), strict=True):
        if lim_ is None:
            cols.append(z)
        else:
            cols.append((m["min_edge"] < float(lim_)) if side == "lo" else (m["max_edge"] > float(lim_)))
    cols.append(np.full(n, shape == "tria"))
    return np.stack(cols, axis=1)


#: SHMESH-86: the share of the user's allowance a step may fill (the band left for later steps)
ALLOWANCE_MARGIN = 0.8


def line_slack(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, limits: InForce,
               m: dict | None = None, margin: float | None = None) -> np.ndarray:
    """(lines,) int: SHMESH-86 - how many more elements each line of line_matrix may take: on each user line's target
    (into its band, never past its outer limit), what is left of ALLOWANCE_MARGIN (or `margin`) of the share it allows
    over the whole mesh; 0 on every other line. `m`: the mesh's measures, when at hand."""
    margin = ALLOWANCE_MARGIN if margin is None else float(margin)
    out = np.zeros(len(line_names(limits)), dtype=np.int64)
    n = len(quads) + len(trias)
    if not limits.aims or not n:
        return out
    m = m if m is not None else _whole_measures(xyz, quads, trias)
    base = 1 + len(limits.hard)
    for j, (shape, key, op, target, outer, share) in enumerate(limits.aims):
        if outer is None or key not in m[shape]:
            continue
        v = m[shape][key]
        band = int((((v < target) & (v >= outer)) if op == ">=" else ((v > target) & (v <= outer))).sum())
        out[base + 2 * j] = max(0, int(math.floor(float(share) * margin * n + 1e-9)) - band)
    return out


#: SHMESH-86g: a mend change that takes an element back under the user's longest edge (raising no count past an outer
#: or solver limit) may fill a band up to the user's whole allowance, not only ALLOWANCE_MARGIN of it: the longest
#: edge is a line the user allows no element over, and the margin kept for later steps cannot be worth an element over
#: it. On sh0042 (the relaxing's allowance off, SHMESH-86d) the mend's moves back under the longest edge each put a
#: quad into the quad-max band; the margin's share ran out after 8 of them and 20 elements stayed over the edge
#: (2 with the allowance on); with the whole allowance, 2 (the quad-max band 165 -> 183 of 215 allowed)
LONG_FULL_ALLOWANCE = True


def line_counts(xyz: np.ndarray, els: np.ndarray, shape: str, lim: InForce, edges=None) -> np.ndarray:
    """SHMESH-51o45 (moved here from the adapter, its one counting helper): of these elements of one shape, per line -
    each of the user's limits (outside its target, past its outer line), then the edges (under the user's shortest
    edge, under its band; over the longest, over its band) - the counts, as one array."""
    n_rows = len(lim.aims)
    out = np.zeros(2 * n_rows + 4, dtype=np.int64)
    els = np.asarray(els, dtype=np.int64)
    if not len(els):
        return out
    m = (quad_measures if shape == "quad" else tria_measures)(xyz, els, extra=lim.extra)
    for j, (row_shape, key, op, target, outer, _share) in enumerate(lim.aims):
        if row_shape != shape or key not in m:
            continue
        v = m[key]
        out[2 * j] = int(((v < target) if op == ">=" else (v > target)).sum())
        if outer is not None:
            out[2 * j + 1] = int(((v < outer) if op == ">=" else (v > outer)).sum())
    if edges is not None:
        ed = np.linalg.norm(xyz[els] - xyz[np.roll(els, -1, axis=1)], axis=2)
        lo, hi = ed.min(axis=1), ed.max(axis=1)
        for k, (lim_, side) in enumerate(zip(edges, ("lo", "lo", "hi", "hi"), strict=True)):
            if lim_ is not None:
                out[2 * n_rows + k] = int(((lo < float(lim_)) if side == "lo" else (hi > float(lim_))).sum())
    return out


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
            out[off:off + len(v)] |= solver_past(v, op, limit)
    return out


#: the mesh-making steps hold every element a hair inside each limit (a relative MARGIN), so that a quad made at
#: exactly the limit (taper 0.5) never reads past it once its corners are numbered the other way round (sh0039).
#: SHMESH-89e: one margin, 1e-6 of the limit, shared by every step and the product's own check (nastran_past,
#: measure): an element within it of a solver limit is past for both - the steps take it inside, and none is told
#: inside that a deck reader (coordinates to 10 digits) could read past. With 1e-9 in the steps and none in the
#: check, a quad at exactly taper 0.500 was past for the steps and inside in the record
MARGIN = 1e-6


def solver_past(v: np.ndarray, op: str, limit: float) -> np.ndarray:
    """SHMESH-89e: past a solver limit, MARGIN (relative) in: within it of the limit counts as past."""
    tol = MARGIN * abs(float(limit))
    return (v < limit + tol) if op == ">=" else (v > limit - tol)


def _quad_past(q: dict[str, np.ndarray], limits: InForce = IN_FORCE) -> np.ndarray:
    """(quads,) bool: the quad is past one of the hard quad limits in force - Nastran's own - (or not valid)."""
    out = q["min_jacobian"] <= 0
    for shape, key, op, limit, _card in limits.hard:
        if shape == "quad":
            out = out | solver_past(q[key], op, limit)
    return out


def _tria_past(t: dict[str, np.ndarray], limits: InForce = IN_FORCE) -> np.ndarray:
    out = t["min_jacobian"] <= 0
    for shape, key, op, limit, _card in limits.hard:
        if shape == "tria":
            out = out | solver_past(t[key], op, limit)
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


#: SHMESH-86: the in-band quads cut for the user's allowance are chosen together over every line - each one cut takes
#: itself out of every band it is in, so the quad in the most bands over their allowance goes first (then the furthest
#: past), and only quads a cut mends are chosen; each line is cut to AIMED_MARGIN of its allowance (later steps change
#: the element count). Cut line by line, a quad over 135 degrees, under 45 and under 0.6 was counted three times
AIMED_COVER = True
AIMED_MARGIN = 0.8


def _clean_cut(xyz: np.ndarray, quads: np.ndarray, limits: InForce) -> np.ndarray:
    """(quads,) bool: SHMESH-86 - a cut into two triangles inside every hard limit and every user target exists."""
    q = np.asarray(quads, dtype=np.int64)
    out = np.zeros(len(q), dtype=bool)
    for c in (np.stack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=1), np.stack([q[:, [0, 1, 3]], q[:, [1, 2, 3]]], axis=1)):
        t = c.reshape(-1, 3)
        tm = tria_measures(xyz, t, extra=True)
        bad = _tria_past(tm, limits) | (aim_score(tm, "tria", limits, len(t)) > 0)
        out |= ~bad.reshape(-1, 2).any(axis=1)
    return out


def _cover(xyz: np.ndarray, quads: np.ndarray, limits: InForce, want: np.ndarray,
           bands: list[tuple[np.ndarray, int, np.ndarray]]) -> np.ndarray:
    """SHMESH-86: the in-band quads to cut so every line's band is within its allowance - greedy: the cuttable quad
    in the most lines still over first, then the furthest past (summed over those lines' targets' shares), then the
    lower number. Quads already wanted (past an outer limit) count as gone."""
    n = len(quads)
    over = np.stack([b for b, _s, _f in bands], axis=1) & ~want[:, None]       # (quads, lines)
    left = np.array([int(over[:, j].sum()) - s for j, (_b, s, _f) in enumerate(bands)])
    pick = np.zeros(n, dtype=bool)
    if not (left > 0).any():
        return pick
    cand = np.flatnonzero(over[:, left > 0].any(axis=1))
    able = np.zeros(n, dtype=bool)
    able[cand] = _clean_cut(xyz, quads[cand], limits)
    far = np.stack([np.where(b, np.maximum(f, 0.0), 0.0) for b, _s, f in bands], axis=1)
    while (left > 0).any():
        live = left > 0
        hits = (over[:, live] & able[:, None] & ~pick[:, None]).sum(axis=1)
        if not hits.max():
            break
        best = np.flatnonzero(hits == hits.max())
        score = (far[best][:, live] / np.maximum(far[:, live].max(axis=0), 1e-12)).sum(axis=1)
        i = int(best[np.lexsort((best, -np.round(score, 9)))][0])
        pick[i] = True
        left -= over[i].astype(np.int64)
    return pick


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
    bands: list[tuple[np.ndarray, int, np.ndarray]] = []       # (in the band, allowed, how far past) per line
    for shape, key, op, target, outer, share in limits.aims:
        if shape != "quad" or key not in m:
            continue
        v = m[key]
        over = ((v < target) if op == ">=" else (v > target)) & ~hard
        if outer is not None:
            beyond = ((v < outer) if op == ">=" else (v > outer)) & ~hard
            want |= beyond
            over &= ~beyond
        far = (target - v) if op == ">=" else (v - target)
        if AIMED_COVER:
            spare = int(math.floor(float(share) * AIMED_MARGIN * n_elements + 1e-9))
            bands.append((over, spare, far))
            continue
        spare = int(math.floor(float(share) * n_elements + 1e-9))
        ids = np.flatnonzero(over)
        if len(ids) > spare:
            # the furthest past the target first (then the lower quad number)
            order = ids[np.lexsort((ids, -np.round(far[ids], 9)))]
            want[order[:len(ids) - spare]] = True
    if AIMED_COVER and bands:
        want |= _cover(xyz, quads, limits, want, bands)
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

    seen_m: dict = {}                       # (the measures judge last made: "q" quads, "t" triangles)

    def judge(ids: np.ndarray, whole: dict | None = None
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Per element of `ids`: past a limit (or turned over), its smallest corner angle, turned over, (quads)
        past a limit with no cut into two triangles inside the triangle limits, and its score against the user's
        limits in force (zeros with none). `whole`: with `ids` every element, the measures of the whole mesh as it
        stands ({"quad": ..., "tria": ...}, extra), read instead of made again."""
        past = np.zeros(len(ids), dtype=bool)
        hard = np.zeros(len(ids), dtype=bool)
        least = np.full(len(ids), 180.0)
        flip = np.zeros(len(ids), dtype=bool)
        aim = np.zeros(len(ids), dtype=np.int64)
        isq = ids < nq
        seen_m.clear()
        if isq.any():
            m = whole["quad"] if whole is not None else quad_measures(X, elem[ids[isq]], extra=aimed)
            seen_m["q"] = m
            past[isq] = _quad_past(m, limits)
            least[isq] = m["min_angle_quad"]
            flip[isq] = m["min_jacobian"] <= 0
            if aimed:
                aim[isq] = aim_score(m, "quad", lim_round[0], int(isq.sum()))
                held = guarded[ids[isq]]
                if held.any():
                    # (SHMESH-86b: an element of a grid, a mapped strip or a band keeps every line in full)
                    aim[np.flatnonzero(isq)[held]] = aim_score(m, "quad", limits, int(isq.sum()))[held]
            qi = np.flatnonzero(isq)[past[isq]]
            if len(qi):
                q = elem[ids[qi]]
                fails = [_tria_past(tria_measures(X, c.reshape(-1, 3)), limits).reshape(-1, 2).any(axis=1)
                         for c in (np.stack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=1),
                                   np.stack([q[:, [0, 1, 3]], q[:, [1, 2, 3]]], axis=1))]
                hard[qi] = fails[0] & fails[1]
        if (~isq).any():
            m = whole["tria"] if whole is not None else tria_measures(X, elem[ids[~isq], :3], extra=aimed)
            seen_m["t"] = m
            past[~isq] = _tria_past(m, limits)
            least[~isq] = m["min_angle_tria"]
            flip[~isq] = m["min_jacobian"] <= 0
            if aimed:
                aim[~isq] = aim_score(m, "tria", lim_round[0], int((~isq).sum()))
                held = guarded[ids[~isq]]
                if held.any():
                    aim[np.flatnonzero(~isq)[held]] = aim_score(m, "tria", limits, int((~isq).sum()))[held]
        return past, least, flip, hard, aim

    def weigh(ids: np.ndarray, hard: np.ndarray) -> np.ndarray:
        """Each element's weight when past a limit: weights[1] for a triangle; for a quad weights[0], or weights[1]
        when no cut can mend it and quads weigh less than triangles (before the cut)."""
        wq = np.where(hard & (weights[0] < weights[1]), weights[1], weights[0])
        return np.where(ids < nq, wq, weights[1])

    per_line = PER_LINE and RELAX_PER_LINE and aimed
    # SHMESH-86: per round, the user's lines within their allowance aim at nothing in their band (lim_round), and
    # each may take what is left of its allowance (slack) - a move may put an element into a band only while that
    # line stays within ALLOWANCE_MARGIN of the share the user allows
    lim_round = [limits]
    # SHMESH-86b: the elements on a protected node (a grid's, a mapped strip's, a band's): their lines stay in full
    guarded = np.zeros(len(elem), dtype=bool)
    if limits.protect_nodes:
        pn = np.zeros(len(X) + 1, dtype=bool)
        at_ = np.fromiter(sorted(v for v in limits.protect_nodes if v < len(X)), dtype=np.int64)
        pn[at_] = True
        guarded = pn[np.where(elem >= 0, elem, len(X))].any(axis=1)
    slack = np.zeros(0, dtype=np.int64)
    missed_r = None

    def lines_round(ids: np.ndarray, who: np.ndarray, n_s: int, judged: bool = False) -> np.ndarray:
        """SHMESH-84: (nodes, lines): round each node, how many of its elements are past each line. `judged`: the
        coordinates are those judge(ids) last measured - its measures are read, not made again."""
        isq = ids < nq
        out_l = np.zeros((n_s, len(line_names(limits))), dtype=np.int64)
        if isq.any():
            np.add.at(out_l, who[isq], line_matrix(X, elem[ids[isq]], limits,
                                                   m=seen_m.get("q") if judged else None).astype(np.int64))
        if (~isq).any():
            np.add.at(out_l, who[~isq], line_matrix(X, elem[ids[~isq], :3], limits,
                                                    m=seen_m.get("t") if judged else None).astype(np.int64))
        return out_l

    def near_round(ids: np.ndarray, who: np.ndarray, n_s: int) -> np.ndarray:
        """SHMESH-88b: (nodes, aims): round each node, how many of its elements are near each line's outer limit (the
        coordinates judge(ids) last measured)."""
        isq = ids < nq
        out_n = np.zeros((n_s, len(limits.aims)), dtype=np.int64)
        if isq.any():
            np.add.at(out_n, who[isq], near_outer(X, elem[ids[isq]], limits, m=seen_m.get("q")))
        if (~isq).any():
            np.add.at(out_n, who[~isq], near_outer(X, elem[ids[~isq], :3], limits, m=seen_m.get("t")))
        return out_n

    # SHMESH-88: under the summed rule, a move that puts more elements round the node under / over an edge line is
    # kept only for a reason (fewer past an outer or solver limit, or fewer in the band of a line past its allowance)
    edge_guard = EDGE_GUARD and aimed and not per_line and any(e is not None for e in limits.edges)

    def edges_round(ids: np.ndarray, who: np.ndarray, n_s: int, past: np.ndarray, aim: np.ndarray
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """SHMESH-88: round each node, (its elements under / over each edge line, its elements past an outer or a
        solver limit, its elements in the band of a line past its allowance)."""
        isq = ids < nq
        e_l = np.zeros((n_s, 4), dtype=np.int64)
        if isq.any():
            np.add.at(e_l, who[isq], edge_lines(X, elem[ids[isq]], limits))
        if (~isq).any():
            np.add.at(e_l, who[~isq], edge_lines(X, elem[ids[~isq], :3], limits))
        sev = past | (aim >= AIM_OUTER)
        s_n = np.zeros(n_s, dtype=np.int64)
        np.add.at(s_n, who, sev.astype(np.int64))
        b_n = np.zeros(n_s, dtype=np.int64)
        np.add.at(b_n, who, ((aim == AIM_BAND) & ~sev).astype(np.int64))
        return e_l, s_n, b_n

    def sweep(cand: np.ndarray, only_severe: bool) -> int:
        """One pass over the candidate nodes (sets sharing no element, in number order). `only_severe` (SHMESH-89b):
        a move is kept only where it takes an element out past an outer or solver limit and puts none past one.
        Returns how many nodes moved."""
        nonlocal slack
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
            before_lines = lines_round(ids, who, len(S), judged=True) if per_line else None
            before_near = near_round(ids, who, len(S)) if per_line and EDGE_REASON else None
            before_edges = edges_round(ids, who, len(S), p0, a0) if edge_guard else None
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
            elif per_line:
                # SHMESH-84: held to every line apart, a node may come only part of the way to the middle
                cands += [0.5 * (old + mean)]
            if per_line:
                cands += [0.75 * old + 0.25 * mean]
            best = old.copy()
            best_past = before_past.copy()
            best_aim = before_aim.copy()
            best_least = before_least.copy()
            moved = np.zeros(len(S), dtype=bool)
            if project is not None:
                cands = [lay_on(project, S, c, old) for c in cands]
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
                if before_lines is not None:
                    # SHMESH-84: and no line's count rises round the node (summed, a move traded one line for another)
                    rise_c = lines_round(ids, who, len(S), judged=True) - before_lines
                    better &= line_rule_ok(rise_c, slack, limits, missed=missed_r,
                                           near=None if before_near is None else near_round(ids, who, len(S))
                                           - before_near)
                    if only_severe:
                        # SHMESH-89b: this sweep makes only the moves that take an element out past an outer or a
                        # solver limit and put none past one
                        better &= severe_down(rise_c, limits)
                if before_edges is not None:
                    # SHMESH-88: an edge line rises round the node only for a reason
                    e1, s1, b1 = edges_round(ids, who, len(S), p1, a1)
                    better &= ~(e1 > before_edges[0]).any(axis=1) | (s1 < before_edges[1]) | (b1 < before_edges[2])
                best[better] = cand[better]
                best_past[better] = after_past[better]
                best_aim[better] = after_aim[better]
                best_least[better] = after_least[better]
                moved |= better
                X[S] = old
            X[S] = best
            if before_lines is not None and moved.any():
                # SHMESH-86: the band each kept move fills comes off its line's slack, in node order; a move the
                # slack left cannot cover goes back
                rise = lines_round(ids, who, len(S)) - before_lines
                # (SHMESH-89: a move out past an outer or solver limit spends the slack, and is never sent back by it)
                first = severe_down(rise, limits) if SEVERE_FIRST else np.zeros(len(S), dtype=bool)
                if EDGE_REASON:
                    rise[:, edge_cols(limits)] = 0       # (SHMESH-88: an edge line has no slack; its reason was held)
                for i in np.flatnonzero(moved & (rise > 0).any(axis=1)).tolist():
                    if first[i] or (rise[i] <= slack).all():
                        slack = np.maximum(slack - np.maximum(rise[i], 0), 0)
                    else:
                        best[i] = old[i]
                        moved[i] = False
                X[S] = best
            changed += int(moved.sum())
        return changed

    moves = 0
    everyone = np.arange(len(elem))
    # SHMESH-89b: per round, first a sweep that makes only the moves taking an element out past an outer or solver
    # limit (putting none past one), then the ordinary sweep - so lesser moves never spend a line's allowance first
    sweeps = (True, False) if per_line and RELAX_SEVERE_SWEEP else (False,)
    for _ in range(rounds):
        whole = None
        if aimed and (RELAX_ALLOWANCE or per_line):
            # (one measure of the whole mesh a round, read by every whole-mesh count below)
            whole = {"quad": quad_measures(X, quads, extra=True) if len(quads) else {},
                     "tria": tria_measures(X, trias, extra=True) if len(trias) else {}}
        if aimed and RELAX_ALLOWANCE:
            lim_round[0] = within_allowance(X, quads, trias, limits, m=whole)
        if per_line:
            slack = line_slack(X, quads, trias, limits, m=whole) if RELAX_ALLOWANCE else np.zeros(
                len(line_names(limits)), dtype=np.int64)
            missed_r = line_missed(X, quads, trias, limits, m=whole) if EDGE_REASON else None
        changed = 0
        for only_severe in sweeps:
            if only_severe or not changed:
                judged = judge(everyone, whole)
            else:
                judged = judge(everyone)
            if only_severe:
                bad_el = everyone[judged[0] | (judged[4] >= AIM_OUTER)]
            else:
                bad_el = everyone[judged[0] | (judged[4] > 0)] if aimed else everyone[judged[0]]
            if not len(bad_el):
                continue
            cand = np.unique(elem[bad_el].ravel())
            cand = cand[(cand >= 0) & movable[cand]]
            if not len(cand):
                continue
            changed += sweep(cand, only_severe)
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
    tiny = _tiny_area(xyz, np.arange(len(xyz))) if len(T) else 0.0
    # SHMESH-89c: under the user's lines (the per-line rule on), a swap is held to SHMESH-89's order line by line
    sev = line_groups(limits)["severe"] if FLIP_SEVERE and PER_LINE and limits.aims else None
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
            if TWIN_GUARD and (min(p, q), max(p, q)) in users:
                continue                # SHMESH-80: the other diagonal is an edge already (its triangles would be twins)
            new = np.array([[a, q, p], [q, b, p]], dtype=np.int64)  # same turning sense as (a, b, p)
            n_old = _cross(xyz[b] - xyz[a], xyz[p] - xyz[a])
            n_new = [_cross(xyz[t[1]] - xyz[t[0]], xyz[t[2]] - xyz[t[0]]) for t in new]
            if any(float(np.dot(n, n_old)) <= 0 for n in n_new):
                continue
            if TWIN_GUARD and any(0.5 * float(np.linalg.norm(n)) <= tiny for n in n_new):
                continue                # SHMESH-81: a swap never makes a triangle with no area (its corners on a line)
            (f_new, a_new, s_new), (f_old, a_old, s_old) = (_tria_score(xyz, new, limits),
                                                            _tria_score(xyz, T[[i, j]], limits))
            if sev is not None and (line_matrix(xyz, new, limits)[:, sev].sum(axis=0)
                                    > line_matrix(xyz, T[[i, j]], limits)[:, sev].sum(axis=0)).any():
                continue                # (SHMESH-89c: no swap puts a triangle past an outer or solver limit, line by line)
            if (f_new, a_new) < (f_old, a_old) or ((f_new, a_new) == (f_old, a_old) and s_new > s_old):
                T[i], T[j] = new[0], new[1]
                done |= {i, j}
                if TWIN_GUARD:
                    users[(min(p, q), max(p, q))] = [i, j]
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


#: a quad whose corner angle is past one of the solver's own angle limits by more than QUICK_DEG is known past it from
#: a plain-float reading, without the full measure (the full measure would find it past too: the margin is a
#: thousand times any rounding between the two readings)
QUICK_DEG = 0.01


def _angle_bounds(limits: InForce) -> tuple[float, float] | None:
    """(the least, the most) quad corner angle the hard limits in force allow; None when they hold no such limit."""
    lo = [v for s, k, op, v, _c in limits.hard if s == "quad" and k == "min_angle_quad" and op == ">="]
    hi = [v for s, k, op, v, _c in limits.hard if s == "quad" and k == "max_angle_quad" and op == "<="]
    return (max(lo), min(hi)) if lo and hi else None


def _clearly_past(p: list[list[float]], bounds: tuple[float, float]) -> bool:
    """Whether a quad (its corners, plain floats) has a corner angle past `bounds` by more than QUICK_DEG, or an edge
    of no length."""
    for i in range(4):
        a, b, c = p[i - 1], p[i], p[(i + 1) % 4]
        u = (a[0] - b[0], a[1] - b[1], a[2] - b[2])
        v = (c[0] - b[0], c[1] - b[1], c[2] - b[2])
        nu = math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2])
        nv = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        if nu <= 0.0 or nv <= 0.0:
            return False                     # left to the full measure
        cosv = max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / (nu * nv)))
        ang = math.degrees(math.acos(cosv))
        if ang < bounds[0] - QUICK_DEG or ang > bounds[1] + QUICK_DEG:
            return True
    return False


def _good_quad(xyz: np.ndarray, q: list[int], normal: np.ndarray, limits: InForce = IN_FORCE) -> float | None:
    """The quad's smallest corner angle when it is valid, inside every Nastran quad limit (and every target of the
    user's limits in force, SHMESH-25) and turns along `normal`; else None. A quad clearly past the solver's own
    angle limits is turned down from a plain-float reading first (the same answer, faster: the walks try hundreds of
    thousands of quads on a large part)."""
    bounds = _angle_bounds(limits)
    if bounds is not None and _clearly_past(xyz[q].tolist(), bounds):
        return None
    m = quad_measures(xyz, np.array([q], dtype=np.int64), extra=limits.extra)
    if bool(_quad_past(m, limits)[0]) or (limits.aims and int(aim_score(m, "quad", limits, 1)[0]) > 0):
        return None
    p = xyz[q]
    if float(np.dot(_cross(p[2] - p[0], p[3] - p[1]), normal)) <= 0:
        return None
    return float(m["min_angle_quad"][0])


def _turns_along(xyz: np.ndarray, t: list[int], normal: np.ndarray) -> bool:
    p = xyz[t]
    return float(np.dot(_cross(p[1] - p[0], p[2] - p[0]), normal)) > 0


#: SHMESH-18b: the walk holds the user's lines (per line, within their allowance). Without it a walk spent the bands
#: freely and the mend after it, held per line, could not take them back: a 6247-element flange's aspect band went
#: 55 -> 116 at the walk against an allowance of 62 and ended at 80; a bracket's quad-angle band 13 -> 27 against 9
WALK_LINES = True


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
        return _cross(p[2] - p[0], p[3] - p[1]) if len(c) == 4 else _cross(p[1] - p[0], p[2] - p[0])

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
    # SHMESH-18b: with the user's limits, a walk is held to the per-line rule as a mend change is - no line's count
    # rises over the elements it replaces beyond what is left of its allowance (spent walk by walk); the triangles it
    # takes out count as its gain
    held = WALK_LINES and PER_LINE and bool(limits.aims)
    if held:
        slack_w = line_slack(xyz, quads, trias, limits)
        missed_w = line_missed(xyz, quads, trias, limits) if EDGE_REASON else None
        e_cols = edge_cols(limits) if EDGE_REASON else np.zeros(0, dtype=np.int64)

        def lines_of(els: list) -> np.ndarray:
            out_l = np.zeros(len(line_names(limits)), dtype=np.int64)
            for k in (4, 3):
                rows_ = [list(c) for c in els if len(c) == k]
                if rows_:
                    out_l += line_matrix(xyz, np.array(rows_, dtype=np.int64), limits).astype(np.int64).sum(axis=0)
            return out_l

        def near_of(els: list) -> np.ndarray:
            out_n = np.zeros(len(limits.aims), dtype=np.int64)
            for k in (4, 3):
                rows_ = [list(c) for c in els if len(c) == k]
                if rows_:
                    out_n += near_outer(xyz, np.array(rows_, dtype=np.int64), limits).sum(axis=0)
            return out_n
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
            if held:
                news, olds = [tuple(q_) for q_ in got], [elems[e] for e in [start, goal] + path]
                rise = lines_of(news) - lines_of(olds)
                near_w = near_of(news) - near_of(olds) if EDGE_REASON else None
                if not bool(line_rule_ok(rise, slack_w, limits, missed=missed_w, near=near_w)[0]):
                    continue
                spend = np.maximum(rise, 0)
                spend[e_cols] = 0
                slack_w = slack_w - spend
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


#: SHMESH-17b: the pairing holds the user's lines (per line, within their allowance). Its own check summed the
#: scores (a quad in one band for two triangles in another passed), and on a 6247-element flange the 97 quads it made
#: took the aspect band from 53 to 108 against an allowance of 62; the mend held per line could not take it back.
#: Off: held, the flange met its aspect and quad-angle lines but kept 178 more triangles, past the triangle lines
#: (15 past the outer one), and a bracket lost its triangle-angle line - a pair refused leaves its band in triangles
PAIR_LINES = False


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
    n_q = _cross(xyz[Q[:, 2]] - xyz[Q[:, 0]], xyz[Q[:, 3]] - xyz[Q[:, 1]])
    for col in (0, 1):
        t = trias[np.array([ct[col] for ct in cand_t])]
        n_t = _cross(xyz[t[:, 1]] - xyz[t[:, 0]], xyz[t[:, 2]] - xyz[t[:, 0]])
        ok &= np.einsum("mi,mi->m", n_q, n_t) > 0
    order = sorted(np.flatnonzero(ok).tolist(), key=lambda r: (-round(float(m["min_angle_quad"][r]), 9), cand_t[r]))
    used: set[int] = set()
    new_q, came = [], []
    # SHMESH-17b: with the user's limits, a pair is held to the per-line rule as a mend change is (each line apart,
    # within what is left of its allowance, spent pair by pair; the two triangles it takes out count as its gain)
    held = PAIR_LINES and PER_LINE and bool(limits.aims) and bool(order)
    if held:
        lq = line_matrix(xyz, Q, limits, m=m if limits.extra else None).astype(np.int64)
        lt = line_matrix(xyz, trias, limits).astype(np.int64)
        slack_p = line_slack(xyz, quads, trias, limits)
        missed_p = line_missed(xyz, quads, trias, limits) if EDGE_REASON else None
        e_cols = edge_cols(limits) if EDGE_REASON else np.zeros(0, dtype=np.int64)
    for r in order:
        i, j = cand_t[r]
        if i in used or j in used:
            continue
        if held:
            rise = lq[r] - lt[i] - lt[j]
            if not bool(line_rule_ok(rise, slack_p, limits, missed=missed_p)[0]):
                continue
            spend = np.maximum(rise, 0)
            spend[e_cols] = 0
            slack_p = slack_p - spend
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
        return _cross(p[2] - p[0], p[3] - p[1])
    return _cross(p[1] - p[0], p[2] - p[0])


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


#: SHMESH-80: no clean-up step writes the same element twice (the swap, the mend, the sliver merge; the cap and the
#: triangle re-cut are guarded on the bolt-hole branch, SHMESH-51o53/51o54), and what is left is dropped (drop_twins)
TWIN_GUARD = True


def absorb_caps(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                face_wall: np.ndarray, *, rounds: int = 4, limits: InForce = IN_FORCE, quads_too: bool = True):
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
    `quads_too` False (an all-triangle shell, STD-4): the polygon is cut into triangles only - no quad is ever made.

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
        # SHMESH-51o54: the node sets of the elements standing, so a cut never makes one already there (on the
        # 334-part board a bent bracket's deck was invalid for one duplicated element, made here)
        have = {tuple(sorted(x)) for x in conn}
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
                if not quads_too and any(len(el) == 4 for el in cut):
                    continue
                if any(_past_one(xyz, el, limits) or float(_elem_normal(xyz, el) @ n_e) <= 0 for el in cut):
                    continue
                if limits.aims and sum(_aim_one(xyz, el, limits) for el in cut) > before_aim:
                    continue
                if any(len(set(el)) != len(el) or tuple(sorted(el)) in have - {tuple(sorted(c)), tuple(sorted(e))}
                       for el in cut):
                    continue        # (SHMESH-51o54: a corner twice, or the corners of an element already there)
                key = (len(cut), -min(_least_angle(xyz, el) for el in cut))
                if best is None or key < best[0]:
                    best = (key, cut)
            if best is None:
                continue
            dead |= {ki, ke}
            have -= {tuple(sorted(c)), tuple(sorted(e))}
            have |= {tuple(sorted(el)) for el in best[1]}
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
#: SHMESH-28: where a triangle's corner is tried, as its height over the edge across it in that edge's lengths: 1.37
#: (the corner then reads 40 degrees), 1 (53) and 0.7 (71)
MEND_HEIGHTS = (1.37, 1.0, 0.7)
#: SHMESH-28b: how much a line's worst may move (in the line's own units) before it counts as worse
WORST_SLACK = 1e-9
#: node kinds (the mesher's): inside a face, on a CAD edge, at a CAD vertex
INSIDE, ON_EDGE, AT_VERTEX = 0, 1, 2


@dataclass(frozen=True)
class ShortGate:
    """SHMESH-28e: the short-edge mend's gate. `low`: the user's shortest edge (mm); `forced_curves`: the CAD edges
    along which a mesh edge under it is forced by the part (an open CAD edge shorter than it, a round edge too small
    for HOLE_PER_TURN of it); `tri_faces`: the faces that may take more triangles (None: every face; empty: none)."""
    low: float
    forced_curves: frozenset = frozenset()
    tri_faces: frozenset | None = frozenset()


def _node_curves(v: int, node_kind: np.ndarray, node_edge: np.ndarray, vertex_edges: dict[int, set[int]]) -> set[int]:
    k = int(node_kind[v])
    if k == ON_EDGE:
        return {int(node_edge[v])}
    if k == AT_VERTEX:
        return set(vertex_edges.get(v, ()))
    return set()


def edge_forced(a: int, b: int, node_kind: np.ndarray, node_edge: np.ndarray, vertex_edges: dict[int, set[int]],
                forced_curves: frozenset) -> bool:
    """SHMESH-28e: whether a mesh edge a-b under the user's shortest edge is the part's doing, which no mend can
    lengthen: both ends on the CAD's edges and (a) on two CAD edges and none in common (across a gap or a corner
    narrower than the shortest edge), (b) along a CAD edge `forced_curves` holds (shorter than it, or a hole too small
    for HOLE_PER_TURN of it), or (c) the two ends of one CAD edge (no node between them to move)."""
    if int(node_kind[a]) == INSIDE or int(node_kind[b]) == INSIDE:
        return False
    ca = _node_curves(a, node_kind, node_edge, vertex_edges)
    cb = _node_curves(b, node_kind, node_edge, vertex_edges)
    common = ca & cb
    if not common:
        return True
    if common & forced_curves:
        return True
    return int(node_kind[a]) == AT_VERTEX and int(node_kind[b]) == AT_VERTEX


def short_ours(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, node_kind: np.ndarray, node_edge: np.ndarray,
               vertex_edges: dict[int, set[int]], gate: ShortGate) -> np.ndarray:
    """(elements,) bool, quads then triangles: SHMESH-28e - the element has an edge under the user's shortest edge that
    is not forced by the part (edge_forced): ours to mend."""
    out = []
    for els in (quads, trias):
        els = np.asarray(els, dtype=np.int64)
        ours = np.zeros(len(els), dtype=bool)
        if len(els):
            nxt = np.roll(els, -1, axis=1)
            short = np.linalg.norm(xyz[els] - xyz[nxt], axis=2) < float(gate.low)
            for i, j in zip(*np.nonzero(short), strict=True):
                if not ours[i] and not edge_forced(int(els[i, j]), int(nxt[i, j]), node_kind, node_edge,
                                                   vertex_edges, gate.forced_curves):
                    ours[i] = True
        out.append(ours)
    return np.concatenate(out)


def mend_short_local(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                     node_kind: np.ndarray, node_edge: np.ndarray, vertex_edges: dict[int, set[int]], *,
                     gate: ShortGate, movable: np.ndarray | None = None, focus: np.ndarray | None = None, **kw
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int], list[int], dict]:
    """SHMESH-28e: mend(short_gate=gate) on the part of the mesh round our own short elements only - the elements two
    rings of nodes out from them; a node they share with the rest of the mesh stays where it is (as a CAD vertex's:
    never merged, never moved), so the rest is untouched and the mesh stays whole. A round reads only the elements on
    the nodes of the elements it mends: the same changes, measured on far fewer elements. Returns as mend does; the
    elements outside the part come first, in their order. `focus` (nodes, bool): only our short elements on one of
    them are mended (a round after one that changed nothing round an element judges it the same: the next round is
    held to what the last one changed)."""
    Q = np.asarray(quads, dtype=np.int64).reshape(-1, 4)
    T = np.asarray(trias, dtype=np.int64).reshape(-1, 3)
    ours = short_ours(xyz, Q, T, node_kind, node_edge, vertex_edges, gate)
    if focus is not None:
        ours &= np.concatenate([focus[Q].any(axis=1) if len(Q) else np.zeros(0, dtype=bool),
                                focus[T].any(axis=1) if len(T) else np.zeros(0, dtype=bool)])
    if not ours.any():
        return xyz, Q, T, list(q_face), list(t_face), {"merged": 0, "recut": 0, "moved": 0}
    nq = len(Q)
    n = len(xyz)

    def touching(nodes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return (nodes[Q].any(axis=1) if nq else np.zeros(0, dtype=bool),
                nodes[T].any(axis=1) if len(T) else np.zeros(0, dtype=bool))

    def nodes_of(sq_: np.ndarray, st_: np.ndarray) -> np.ndarray:
        out = np.zeros(n, dtype=bool)
        out[Q[sq_].ravel()] = True
        out[T[st_].ravel()] = True
        return out

    sel_q, sel_t = ours[:nq], ours[nq:]
    for _ in range(2):
        sel_q, sel_t = touching(nodes_of(sel_q, sel_t))
    held = nodes_of(sel_q, sel_t) & nodes_of(~sel_q, ~sel_t)   # (the nodes the part shares with the rest)
    kind_l = np.asarray(node_kind).copy()
    kind_l[held] = AT_VERTEX
    ve_l = {v: s for v, s in vertex_edges.items() if not held[v]}
    mov_l = None
    if movable is not None:
        mov_l = np.asarray(movable).copy()
        mov_l[held] = False
    qf = np.asarray(list(q_face), dtype=np.int64)
    tf = np.asarray(list(t_face), dtype=np.int64)
    X, Q2, T2, qf2, tf2, done = mend(xyz, Q[sel_q], T[sel_t], qf[sel_q].tolist(), tf[sel_t].tolist(), kind_l,
                                     node_edge, ve_l, movable=mov_l, short_gate=gate, **kw)
    return (X, np.vstack([Q[~sel_q], Q2]).astype(np.int64), np.vstack([T[~sel_t], T2]).astype(np.int64),
            qf[~sel_q].tolist() + list(qf2), tf[~sel_t].tolist() + list(tf2), done)


def _short_gate_ok(cands: list, conn: list, face: list, nq: int, lines_now: np.ndarray, lines_new_q: np.ndarray,
                   lines_new_t: np.ndarray, limits: InForce, gate: ShortGate, protect: set) -> np.ndarray:
    """(candidates,) bool: SHMESH-28e - each change judged face by face. On every face it touches no count of
    line_matrix rises (not valid, each solver limit, each user target and outer limit, the four edge lines, the
    triangles - a face outside `gate.tri_faces` takes no triangle more), and the elements under the shortest edge
    (or under it less its tolerance) fall over the faces it touches. A merge or re-cut never touches a protected face
    (a grid, a mapped strip, a ring's: structure ranks over the edges); a move may (the structure stays)."""
    n_c = len(cands)
    ok = np.ones(n_c, dtype=bool)
    if not n_c:
        return ok
    nl = lines_now.shape[1]
    now = lines_now.astype(np.int64, copy=True)
    now[:, -1] = 0
    now[nq:, -1] = 1                                   # (the triangle column: the mend's own rows have it zeroed)
    new_rows = np.vstack([np.asarray(lines_new_q, dtype=np.int64).reshape(-1, nl),
                          np.asarray(lines_new_t, dtype=np.int64).reshape(-1, nl)])
    n_newq = len(np.asarray(lines_new_q).reshape(-1, nl))
    new_rows[:, -1] = 0
    new_rows[n_newq:, -1] = 1
    keys: dict[tuple[int, int], int] = {}
    k_new: list[int] = []
    p_new: list[int] = []
    k_old: list[int] = []
    p_old: list[int] = []
    iq = it = 0
    for ci, (_e, touched, new, kind, _block, _move) in enumerate(cands):
        if kind != "moved" and protect and (any(face[t] in protect for t in touched)
                                            or any(f_ in protect for _el, f_, _o in new)):
            ok[ci] = False
        for el, f_, _o in new:
            k = keys.setdefault((ci, int(f_)), len(keys))
            if len(el) == 4:
                p_new.append(iq)
                iq += 1
            else:
                p_new.append(n_newq + it)
                it += 1
            k_new.append(k)
        for t in touched:
            k_old.append(keys.setdefault((ci, int(face[t])), len(keys)))
            p_old.append(int(t))
    k_cand = np.fromiter((ci for ci, _f in keys), dtype=np.int64, count=len(keys))
    k_face = np.fromiter((f_ for _ci, f_ in keys), dtype=np.int64, count=len(keys))
    rise = np.zeros((len(keys), nl), dtype=np.int64)
    if k_new:
        np.add.at(rise, np.asarray(k_new, dtype=np.int64), new_rows[np.asarray(p_new, dtype=np.int64)])
    if k_old:
        np.subtract.at(rise, np.asarray(k_old, dtype=np.int64), now[np.asarray(p_old, dtype=np.int64)])
    up = rise > 0
    if gate.tri_faces is None:
        up[:, -1] = False
    elif gate.tri_faces:
        up[np.isin(k_face, np.fromiter(gate.tri_faces, dtype=np.int64)), -1] = False
    bad = np.zeros(n_c, dtype=np.int64)
    np.add.at(bad, k_cand, up.any(axis=1).astype(np.int64))
    short_c = edge_cols(limits)[:2]
    fell = np.zeros(n_c, dtype=np.int64)
    np.add.at(fell, k_cand, rise[:, short_c].sum(axis=1))
    return ok & (bad == 0) & (fell < 0)


def _hex_splits(poly: list[int]) -> list[list[list[int]]]:
    """The three ways to cut a six-cornered outline into two quads."""
    return [[[poly[k], poly[(k + 1) % 6], poly[(k + 2) % 6], poly[(k + 3) % 6]],
             [poly[(k + 3) % 6], poly[(k + 4) % 6], poly[(k + 5) % 6], poly[k]]] for k in range(3)]


def _costs(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, limits: InForce,
           edges: tuple[float | None, float | None] = (None, None), bad: list | None = None,
           m: dict | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(cost per quad, cost per triangle, smallest corner angle per quad, per triangle): MEND_INVALID when not valid,
    MEND_HARD past a hard limit, else the score against the user's limits in force (0 inside every target) plus
    AIM_BAND for an edge under the user's shortest edge and for one over their longest (`edges`, mm; None: none).
    `bad`, when given, is filled with [quads', triangles'] badness against every line (_badness). `m`: the
    elements' measures ({"quad": ..., "tria": ...}, extra), when at hand (SHMESH-28c: the same figures, made once)."""
    lo, hi = edges
    sized = lo is not None or hi is not None
    given = m or {}

    def one(m: dict, past: np.ndarray, shape: str, n: int) -> np.ndarray:
        c = aim_score(m, shape, limits, n)
        if lo is not None:
            c = c + AIM_BAND * (m["min_edge"] < lo)
        if hi is not None:
            c = c + AIM_BAND * (m["max_edge"] > hi)
        c = np.where(past, MEND_HARD, c)
        return np.where(m["min_jacobian"] <= 0, MEND_INVALID, c).astype(np.int64)

    nl = len(_lines(limits))
    if bad is not None:
        bad[:] = [np.zeros((len(quads), nl)), np.zeros((len(trias), nl))]
    if len(quads):
        m = given.get("quad") or quad_measures(xyz, quads, extra=limits.extra or sized)
        cq, aq = one(m, _quad_past(m, limits), "quad", len(quads)), m["min_angle_quad"]
        if bad is not None:
            bad[0] = _badness(m, "quad", limits, len(quads))
    else:
        cq, aq = np.zeros(0, dtype=np.int64), np.zeros(0)
    if len(trias):
        m = given.get("tria") or tria_measures(xyz, trias, extra=limits.extra or sized)
        ct, at = one(m, _tria_past(m, limits), "tria", len(trias)), m["min_angle_tria"]
        if bad is not None:
            bad[1] = _badness(m, "tria", limits, len(trias))
    else:
        ct, at = np.zeros(0, dtype=np.int64), np.zeros(0)
    return cq, ct, aq, at


def _lines(limits: InForce) -> list[tuple[str, str, str, float]]:
    """SHMESH-28b: every line an element is held to - the user's targets and the solver's own limits - as (shape,
    figure, op, the value past which it is outside)."""
    return ([(s, k, op, float(target)) for s, k, op, target, _outer, _share in limits.aims]
            + [(s, k, op, float(v)) for s, k, op, v, _card in limits.hard])


def _badness(m: dict[str, np.ndarray], shape: str, limits: InForce, n: int) -> np.ndarray:
    """(n, lines): how far each element is past each line of _lines (0 inside it, or for a line of the other
    shape)."""
    lines = _lines(limits)
    out = np.zeros((n, len(lines)))
    for j, (s, key, op, v) in enumerate(lines):
        if s != shape or key not in m:
            continue
        out[:, j] = np.maximum(0.0, (v - m[key]) if op == ">=" else (m[key] - v))
    return out


def mend(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
         node_kind: np.ndarray, node_edge: np.ndarray, vertex_edges: dict[int, set[int]], *,
         limits: InForce = IN_FORCE, rounds: int = MEND_ROUNDS, quads_too: bool = True, triangles_too: bool = True,
         edges: tuple[float | None, float | None] = (None, None), suppress: float | None = None,
         movable: np.ndarray | None = None, project=None, hard_long: float | None = None,
         short_gate: ShortGate | None = None
         ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int], list[int], dict]:
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
    * move (with `movable`): one of its nodes that may move (inside a face; on a straight CAD edge, along it) goes
      to where the element would be well shaped - a triangle's corner to MEND_HEIGHTS times the edge across it
      above that edge's middle (a needle standing on a short CAD edge gets its far corner pulled in), a quad's
      corner to where it would be a parallelogram and half-way there - each point laid back by `project` (onto
      the node's own surface or CAD edge).

    Merges and re-cuts move no node. Candidates of a round are judged together and taken best first, each element
    changed at most once a round; `rounds` passes at most. Deterministic.
    `node_kind` (INSIDE / ON_EDGE / AT_VERTEX), `node_edge` (the CAD edge of an ON_EDGE node) and `vertex_edges`
    (the CAD edges that end at an AT_VERTEX node) come from the mesher. `quads_too` False: no change makes a quad
    (the user asked for triangles); `triangles_too` False: no change leaves more triangles than it found (they
    asked for quads). `edges`: the user's (shortest, longest) edge, mm - an element with an edge outside them costs
    AIM_BAND more, so a merge that only trades a limit for an over-long edge is not made.
    `short_gate` (SHMESH-28e): the short-edge mend - only our own elements under the user's shortest edge
    (short_ours) are mended, each change kept only where _short_gate_ok holds (face by face nothing gets worse).

    Returns (the coordinates, quads, triangles, their faces, {"merged": nodes merged, "recut": outlines cut
    again, "moved": nodes moved})."""
    Q = [tuple(map(int, c)) for c in quads.tolist()]
    T = [tuple(map(int, c)) for c in trias.tolist()]
    qf, tf = list(q_face), list(t_face)
    done = {"merged": 0, "recut": 0, "moved": 0}
    X = xyz.copy()
    for _round in range(rounds):
        got = _mend_round(X, Q, T, qf, tf, node_kind, node_edge, vertex_edges, limits, quads_too, triangles_too,
                          done, edges, suppress, movable, project, hard_long, short_gate)
        if got is None:
            break
        Q, qf, T, tf = got
    return (X, np.array(Q, dtype=np.int64).reshape(-1, 4), np.array(T, dtype=np.int64).reshape(-1, 3), qf, tf, done)


#: SHMESH-86: the mend asks nothing of a line whose band is within the user's allowance
MEND_ALLOWANCE = True


def within_allowance(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, limits: InForce,
                     faces: np.ndarray | None = None, m: dict | None = None) -> InForce:
    """SHMESH-86: the limits in force with every user line whose band (elements between its target and its outer
    limit) is within the share it allows read at its outer limit alone (its band no cost). With `faces` (each
    element's face, quads then triangles) a line is so read only when every face is within that share of its own
    elements too: the structured steps judge a grid on its own face's elements, and a band left where one face
    gathers it refused a channel web's grid. `m`: the mesh's measures ({"quad": ..., "tria": ...}, extra), when at
    hand."""
    if not limits.aims:
        return limits
    n = len(quads) + len(trias)
    nq = len(quads)
    f_all = np.asarray(faces, dtype=np.int64) if faces is not None else None
    per_face = np.bincount(f_all) if f_all is not None and len(f_all) else None
    m = m if m is not None else _whole_measures(xyz, quads, trias)
    mq, mt = m["quad"], m["tria"]
    aims = []
    for row in limits.aims:
        shape, key, op, target, outer, share = row
        m = mq if shape == "quad" else mt
        if outer is not None and key in m:
            v = m[key]
            band = ((v < target) & (v >= outer)) if op == ">=" else ((v > target) & (v <= outer))
            ok = int(band.sum()) <= math.floor(float(share) * n + 1e-9)
            if ok and per_face is not None and f_all is not None:
                f_shape = f_all[:nq] if shape == "quad" else f_all[nq:]
                in_f = np.bincount(f_shape[band], minlength=len(per_face))
                ok = bool((in_f <= np.floor(float(share) * per_face + 1e-9)).all())
            if ok:
                # SHMESH-86c: the band asks nothing but its outer NEAR_OUTER, which the steps still aim out of
                at = float(outer) + (float(target) - float(outer)) * NEAR_OUTER if NEAR_MEND else outer
                row = (shape, key, op, at, outer, share)
        aims.append(row)
    return InForce(hard=limits.hard, aims=tuple(aims), edges=limits.edges, protect=limits.protect,
                   protect_nodes=limits.protect_nodes)


#: SHMESH-28c: the mend judges a round's candidates in one batch (sums, maxima and minima over their new elements
#: and what they touch at once) - the same choices, made as the loop made them one element at a time (that loop
#: stands for the summed rule's edge guard and for an element with no area, whose turn it reads from the change)
MEND_BATCH = True
#: SHMESH-86e: on a protected face (SHMESH-86b) the mend may add a band element when the change takes an element back
#: under the user's longest edge and raises no count past an outer or solver limit (the longest-edge line allows no
#: element over it: held to SHMESH-86b alone, the mend left elements over it on plates whose every face holds a ring)
PROTECT_LONG = True
#: SHMESH-86d (lab): a mend change that takes no element out past an outer or solver limit (or not valid) makes no
#: triangle more than it found. Measured on the 80 plates with the relaxing's allowance off: with the mend's allowance
#: off too, 7,256 triangles and jacobian band 1,100, but 16 lines lost against the base where the relaxing alone loses
#: 9 (sh0059's two quad lines and fr018's warpage among them); with it on, fr018's warpage lost. Off
MEND_TRI_GUARD = False


def _judge_batch(xyz, cands, conn, face, cost, worst_now, lines_now, near_now, band_now, protect, band,
                 nq_c, nt_c, na_q, na_t, held_new, lines_new_q, lines_new_t, near_new_q, near_new_t,
                 band_new_q, band_new_t, nrm_new_q, nrm_new_t, long_reason=None):
    """SHMESH-28c: _mend_round's judging of its candidates, all at once. Returns (checks so far: valid, turned as
    before, the protected faces' bands; rise per line or None; rise near each outer limit or None; the worst check;
    after minus before; the smallest corner angle) per candidate - or None where an element with no area to speak of
    stands behind a new one (its turn is read from the change: the loop reads it)."""
    n_c = len(cands)
    el_c, el_q, el_old, el_f, t_c, t_i = [], [], [], [], [], []
    for ci, cand in enumerate(cands):
        for el, f_, old in cand[2]:
            el_c.append(ci)
            el_q.append(len(el) == 4)
            el_old.append(old)
            el_f.append(f_)
        t_c += [ci] * len(cand[1])
        t_i += list(cand[1])
    el_c_a = np.array(el_c, dtype=np.int64)
    isq = np.array(el_q, dtype=bool)
    n_el = len(el_c_a)
    # (the turn each new element must keep: its old element's normal - the loop's own arithmetic, element by element)
    normal: dict[int, np.ndarray] = {}
    for e in set(el_old):
        p = xyz[list(conn[e])]
        n_e = _elem_normal(xyz, list(conn[e]))
        span = float(np.linalg.norm(p - p.mean(axis=0), axis=1).max())
        if float(np.linalg.norm(n_e)) <= 1e-6 * span * span:
            return None
        normal[e] = n_e

    def stream(a_q, a_t, width=None, dtype=float):
        out = np.zeros((n_el,) if width is None else (n_el, width), dtype=dtype)
        if a_q is not None and len(a_q):
            out[isq] = a_q
        if a_t is not None and len(a_t):
            out[~isq] = a_t
        return out

    c_el = stream(nq_c, nt_c, dtype=np.int64)
    nl = worst_now.shape[1]
    nrm = stream(nrm_new_q, nrm_new_t, 3)
    turned = np.array([float(nrm[i] @ normal[el_old[i]]) <= 0.0 for i in range(n_el)], dtype=bool)
    bad_el = (c_el >= MEND_INVALID) | turned
    ok = np.ones(n_c, dtype=bool)
    if n_el:
        hit = np.zeros(n_c, dtype=np.int64)
        np.add.at(hit, el_c_a, bad_el.astype(np.int64))
        ok = hit == 0
    after = np.zeros(n_c, dtype=np.int64)
    np.add.at(after, el_c_a, c_el)
    least = np.full(n_c, 180.0)
    np.fmin.at(least, el_c_a, stream(na_q, na_t))            # (as min(least, x) reads a figure that is not a number)
    worst_after = np.zeros((n_c, nl))
    np.maximum.at(worst_after, el_c_a, stream(held_new[0], held_new[1], nl))
    t_c_a = np.array(t_c, dtype=np.int64)
    t_i_a = np.array(t_i, dtype=np.int64)
    before = np.zeros(n_c, dtype=np.int64)
    np.add.at(before, t_c_a, cost[t_i_a])
    worst_before = np.full((n_c, nl), -np.inf)
    np.maximum.at(worst_before, t_c_a, worst_now[t_i_a])
    if protect:
        prot = np.array([f_ in protect for f_ in el_f], dtype=bool)
        pb = np.zeros(n_c, dtype=np.int64)
        np.add.at(pb, el_c_a, (prot & (stream(band_new_q, band_new_t, dtype=np.int64) != 0)).astype(np.int64))
        sev_after = np.zeros(n_c, dtype=np.int64)
        np.add.at(sev_after, el_c_a, (c_el >= AIM_OUTER).astype(np.int64))
        band_arr = np.asarray(band_now, dtype=np.int64)
        prot_t = np.array([face[t] in protect for t in t_i], dtype=bool)
        pb_now = np.zeros(n_c, dtype=np.int64)
        np.add.at(pb_now, t_c_a, np.where(prot_t, band_arr[t_i_a], 0))
        sev_before = np.zeros(n_c, dtype=np.int64)
        np.add.at(sev_before, t_c_a, (cost[t_i_a] >= AIM_OUTER).astype(np.int64))
    rise = near_rise = None
    if lines_now is not None:
        la = np.zeros((n_c, lines_now.shape[1]), dtype=np.int64)
        np.add.at(la, el_c_a, stream(lines_new_q, lines_new_t, lines_now.shape[1], dtype=np.int64))
        lb = np.zeros_like(la)
        np.add.at(lb, t_c_a, lines_now[t_i_a])
        rise = la - lb
    if protect:
        # SHMESH-86b: no more in-band elements on a gridded or mapped face than the change found there - unless it
        # takes an element out past an outer limit or a solver limit; SHMESH-86e: or back under the user's longest
        # edge, raising no count past an outer or solver limit
        held_ = ok & (pb > pb_now)
        why_ = sev_after < sev_before
        if long_reason is not None and rise is not None:
            over_c, sev_c = long_reason
            why_ = why_ | ((rise[:, over_c] < 0).any(axis=1) & ~(rise[:, sev_c] > 0).any(axis=1))
        ok = np.where(held_, why_, ok)
    if lines_now is not None:
        if near_now is not None:
            na_ = np.zeros((n_c, near_now.shape[1]), dtype=np.int64)
            np.add.at(na_, el_c_a, stream(near_new_q, near_new_t, near_now.shape[1], dtype=np.int64))
            nb_ = np.zeros_like(na_)
            np.add.at(nb_, t_c_a, near_now[t_i_a])
            near_rise = na_ - nb_
    worst_ok = np.all(worst_after <= np.maximum(worst_before, band[None, :]) + WORST_SLACK, axis=1)
    return ok, rise, near_rise, worst_ok, after - before, least


def _mend_round(xyz, Q, T, qf, tf, node_kind, node_edge, vertex_edges, limits, quads_too, triangles_too, done,
                edges=(None, None), suppress=None, movable=None, project=None, hard_long=None, short_gate=None):
    """One pass of mend: (quads, their faces, triangles, their faces) as lists, or None when nothing changed. A
    node that moves is moved in `xyz` itself."""
    def on_edge(v: int, c: int) -> bool:
        k = int(node_kind[v])
        return (k == ON_EDGE and int(node_edge[v]) == c) or (k == AT_VERTEX and c in vertex_edges.get(v, ()))

    Qa = np.array(Q, dtype=np.int64).reshape(-1, 4)
    Ta = np.array(T, dtype=np.int64).reshape(-1, 3)
    lines_limits = limits
    per_line = PER_LINE and MEND_PER_LINE and bool(lines_limits.aims)
    # (one measure of the whole mesh a round, read by every whole-mesh count below)
    whole = _whole_measures(xyz, Qa, Ta) if lines_limits.aims and (MEND_ALLOWANCE or per_line) else None
    if MEND_ALLOWANCE:
        # SHMESH-86: a line whose band is within the share the user allows asks nothing of the mend (its band is
        # no cost): the mend cut in-band quads into triangles the aimed cut had left on purpose
        limits = within_allowance(xyz, Qa, Ta, limits, m=whole)
    protect = set(lines_limits.protect) if MEND_ALLOWANCE else set()
    band_now: list[int] = []
    # (SHMESH-28c: the whole mesh's measures, made once a round, read by every count of it)
    wq = whole["quad"] if whole is not None and len(Qa) else None
    wt = whole["tria"] if whole is not None and len(Ta) else None
    if protect:
        # SHMESH-86b: each element in a band of the user's (outside a target, inside its outer limit), as it stands
        bq = _band_rows(xyz, Qa, lines_limits, m=wq)
        bt = _band_rows(xyz, Ta, lines_limits, m=wt)
        band_now = bq.tolist() + bt.tolist()

    held: list = []
    cq, ct, _aq, _at = _costs(xyz, Qa, Ta, limits, edges, bad=held, m=whole)
    worst_now = np.vstack(held)                      # (elements, lines): how far each is past each line
    # SHMESH-84: each element against every line, apart
    lines_now =(np.vstack([line_matrix(xyz, Qa, lines_limits, m=wq),
                            line_matrix(xyz, Ta, lines_limits, m=wt)]).astype(np.int64) if per_line else None)
    if lines_now is not None:
        lines_now[:, -1] = 0                         # (the triangle count is the mend's own switches' business)
    near_now = (np.vstack([near_outer(xyz, Qa, lines_limits, m=wq), near_outer(xyz, Ta, lines_limits, m=wt)])
                if lines_now is not None and EDGE_REASON else None)       # (SHMESH-88b)
    # SHMESH-86: what each user line's band may still take (its allowance), spent change by change
    slack_m = (line_slack(xyz, Qa, Ta, lines_limits, m=whole) if lines_now is not None and MEND_ALLOWANCE
               else np.zeros(0 if lines_now is None else lines_now.shape[1], dtype=np.int64))
    # SHMESH-86g: the whole allowance, for the changes back under the user's longest edge
    slack_f = (line_slack(xyz, Qa, Ta, lines_limits, m=whole, margin=1.0)
               if LONG_FULL_ALLOWANCE and lines_now is not None and MEND_ALLOWANCE else None)
    long_of: set[int] = set()
    rise_of: dict[int, np.ndarray] = {}
    first_of: set[int] = set()
    missed_m = line_missed(xyz, Qa, Ta, lines_limits, m=whole) if lines_now is not None and EDGE_REASON else None
    # an element may come into a user's band (it costs, and the share allows some) but no further than the elements
    # it replaces were past that line; past a solver limit, never
    band = np.array([abs(float(outer) - float(target)) if outer is not None else 0.0
                     for _s, _k, _op, target, outer, _sh in limits.aims] + [0.0] * len(limits.hard))
    nq = len(Q)
    cost = np.concatenate([cq, ct])
    bad = np.flatnonzero(cost > 0)
    if short_gate is not None:
        # SHMESH-28e: only our own elements under the shortest edge are mended here (the part's own short edges are
        # skipped; the lines were the mend's before this pass)
        bad = np.flatnonzero(short_ours(xyz, Qa, Ta, node_kind, node_edge, vertex_edges, short_gate))
    if not len(bad):
        return None
    # SHMESH-88: under the summed rule a change puts no more elements under / over an edge line without a reason
    edge_guard = (EDGE_GUARD and lines_now is None and bool(lines_limits.aims)
                  and any(e_ is not None for e_ in lines_limits.edges))
    if edge_guard:
        e_now = np.vstack([edge_lines(xyz, Qa, lines_limits), edge_lines(xyz, Ta, lines_limits)])
        sev_now = cost >= AIM_OUTER
        miss_now = (np.concatenate([_quad_aim(xyz, Qa, limits), _tria_aim(xyz, Ta, limits)]) == AIM_BAND) & ~sev_now
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

    def along(a: int, b: int) -> bool:
        """Whether the mesh edge a-b runs along a CAD edge: a free edge, a junction, or a seam between two faces
        (an edge two elements of ONE face share is a chord inside that face)."""
        u = users.get((a, b) if a < b else (b, a), ())
        return len(u) != 2 or face[u[0]] != face[u[1]]

    def may_merge(x: int, y: int) -> bool:
        """Whether node x may be merged into node y (an edge of the mesh joins them)."""
        kx = int(node_kind[x])
        if kx == AT_VERTEX:
            # only when the user asked for small edges to be ignored (SHMESH-29): into its neighbour along a CAD edge
            # shorter than the length they gave
            if suppress is None or not along(x, y) or not any(on_edge(y, c) for c in vertex_edges.get(x, ())):
                return False
            if float(np.linalg.norm(xyz[x] - xyz[y])) >= suppress:
                return False
        if kx == ON_EDGE:
            c = int(node_edge[x])
            if not on_edge(y, c) or not along(x, y):
                return False
            other = [z for z in nbrs.get(x, ()) if z != y and on_edge(z, c) and along(x, z)]
            if len(other) != 1:
                return False
            z = other[0]
            line = xyz[z] - xyz[y]
            span = float(np.linalg.norm(line))
            if span <= 0.0:
                return False
            off = float(np.linalg.norm(_cross(xyz[x] - xyz[y], line))) / span
            if off > MEND_CHORD * span and (suppress is None or off >= suppress):
                return False
        # the mesh stays a surface: every node next to both is a corner of an element that holds both
        both = set(at_node.get(x, ())) & set(at_node.get(y, ()))
        shared = {v for e in both for v in conn[e]}
        return (nbrs.get(x, set()) & nbrs.get(y, set())) <= shared

    # (the bad element, the elements that go, the new ones as (corners, face, the element each stands for),
    #  what it is, the elements no other change of this round may touch, for a move: (the node, where to))
    cands: list[tuple] = []
    points: list[np.ndarray] = []
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
                            break
                        new.append((tuple(v for v in el if v != x), face[t], t))
                    else:
                        new.append((tuple(y if v == x else v for v in el), face[t], t))
                if ok and (triangles_too or sum(len(el) == 3 for el, _f, _o in new)
                           <= sum(len(conn[t]) == 3 for t in touched)):
                    cands.append((e, touched, new, "merged", frozenset(at_node[x]) | frozenset(at_node[y]), None))
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
                cands.append((e, pair, [(tuple(el), face[e], e) for el in cut], "recut", frozenset(pair), None))
        if movable is None:
            continue
        # move one of its nodes: a triangle's corner towards (or away from) the edge across it, to where the
        # triangle would stand MEND_HEIGHTS times that edge high; a quad's corner to where the quad would be a
        # parallelogram, and half-way there
        for j in range(k):
            v = c[j]
            if not movable[v]:
                continue
            here = xyz[v]
            if k == 3:
                a, b = c[(j + 1) % 3], c[(j + 2) % 3]
                mid = 0.5 * (xyz[a] + xyz[b])
                base = float(np.linalg.norm(xyz[a] - xyz[b]))
                up = here - mid
                high = float(np.linalg.norm(up))
                if high <= 0.0 or base <= 0.0:
                    continue
                spots = [mid + up * (f * base / high) for f in MEND_HEIGHTS if abs(f * base - high) > 0.05 * high]
            else:
                iso = xyz[c[(j + 1) % 4]] + xyz[c[(j + 3) % 4]] - xyz[c[(j + 2) % 4]]
                spots = [iso, 0.5 * (here + iso)]
            star = tuple(sorted(set(at_node[v])))
            for spot in spots:
                if project is not None:
                    spot = lay_on(project, np.array([v], dtype=np.int64), spot[None], xyz[v][None])[0]
                tmp = len(xyz) + len(points)
                points.append(spot)
                cands.append((e, star, [(tuple(tmp if w == v else w for w in conn[t]), face[t], t) for t in star],
                              "moved", frozenset(star), (v, spot)))
    if not cands:
        return None
    # judge every new element of every candidate at once (a moved node stands at the end of the coordinates)
    here_all = xyz
    if points:
        xyz = np.vstack([xyz, np.array(points)])
    new_q = [el for cand in cands for el, _f, _o in cand[2] if len(el) == 4]
    new_t = [el for cand in cands for el, _f, _o in cand[2] if len(el) == 3]
    held_new: list = []
    new_qa = np.array(new_q, dtype=np.int64).reshape(-1, 4)
    new_ta = np.array(new_t, dtype=np.int64).reshape(-1, 3)
    # (SHMESH-28c: the new elements measured once, read by every count below - the same figures each made apart)
    mn = ({"quad": quad_measures(xyz, new_qa, extra=True) if len(new_qa) else None,
           "tria": tria_measures(xyz, new_ta, extra=True) if len(new_ta) else None}
          if limits.extra or any(e_ is not None for e_ in edges) else None)
    mnq = mn["quad"] if mn is not None else None
    mnt = mn["tria"] if mn is not None else None
    nq_c, nt_c, na_q, na_t = _costs(xyz, new_qa, new_ta, limits, edges, bad=held_new, m=mn)
    band_new_q = band_new_t = np.zeros(0, dtype=np.int64)
    if protect:
        # (SHMESH-86b: each new element in a band, measured together)
        band_new_q = _band_rows(xyz, new_qa, lines_limits, m=mnq)
        band_new_t = _band_rows(xyz, new_ta, lines_limits, m=mnt)
    lines_new_q = lines_new_t = None
    if lines_now is not None:
        lines_new_q = line_matrix(xyz, new_qa, lines_limits, m=mnq).astype(np.int64)
        lines_new_t = line_matrix(xyz, new_ta, lines_limits, m=mnt).astype(np.int64)
        lines_new_q[:, -1] = 0
        lines_new_t[:, -1] = 0
    near_new_q = near_new_t = None
    if near_now is not None:
        near_new_q = near_outer(xyz, new_qa, lines_limits, m=mnq)
        near_new_t = near_outer(xyz, new_ta, lines_limits, m=mnt)
    if edge_guard:
        nqa = np.array(new_q, dtype=np.int64).reshape(-1, 4)
        nta = np.array(new_t, dtype=np.int64).reshape(-1, 3)
        e_new_q, e_new_t = edge_lines(xyz, nqa, lines_limits), edge_lines(xyz, nta, lines_limits)
        a_new_q, a_new_t = _quad_aim(xyz, nqa, limits), _tria_aim(xyz, nta, limits)
    # every new element's normal at once, as _elem_normal makes it (the same arithmetic, element by element)
    pq = xyz[np.array(new_q, dtype=np.int64).reshape(-1, 4)]
    pt = xyz[np.array(new_t, dtype=np.int64).reshape(-1, 3)]
    nrm_new_q = _cross(pq[:, 2] - pq[:, 0], pq[:, 3] - pq[:, 1]) if len(pq) else np.zeros((0, 3))
    nrm_new_t = _cross(pt[:, 1] - pt[:, 0], pt[:, 2] - pt[:, 0]) if len(pt) else np.zeros((0, 3))
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
    judged: list[tuple] = []
    prot_band = 0
    severe_after = 0
    batch = None
    # SHMESH-86e: the longest-edge lines' columns and the severe ones (a change back under the user's longest edge may
    # put a band element on a protected face: that line allows no element over it)
    long_reason = ((edge_cols(lines_limits)[2:4], line_groups(lines_limits)["severe"])
                   if PROTECT_LONG and lines_now is not None else None)
    if MEND_BATCH and not edge_guard:
        batch = _judge_batch(xyz, cands, conn, face, cost, worst_now, lines_now, near_now, band_now, protect, band,
                             nq_c, nt_c, na_q, na_t, held_new, lines_new_q, lines_new_t, near_new_q, near_new_t,
                             band_new_q, band_new_t, nrm_new_q, nrm_new_t, long_reason)
    for ci, (e, touched, new, _kind, _block, _move) in enumerate(cands if batch is None else ()):
        before = int(cost[list(touched)].sum())
        after, least, ok = 0, 180.0, True
        worst_after = np.zeros(worst_now.shape[1])
        lines_after = None if lines_now is None else np.zeros(lines_now.shape[1], dtype=np.int64)
        eg_after = np.zeros(4, dtype=np.int64)
        sg_after = mg_after = 0
        near_after = None if near_now is None else np.zeros(near_now.shape[1], dtype=np.int64)
        for el, _f, old in new:
            if len(el) == 4:
                after += int(nq_c[iq])
                ok = ok and int(nq_c[iq]) < MEND_INVALID
                least = min(least, float(na_q[iq]))
                worst_after = np.maximum(worst_after, held_new[0][iq])
                if lines_after is not None and lines_new_q is not None:
                    lines_after += lines_new_q[iq]
                if near_after is not None and near_new_q is not None:
                    near_after += near_new_q[iq]
                if edge_guard:
                    eg_after += e_new_q[iq]
                    s_ = int(nq_c[iq]) >= AIM_OUTER
                    sg_after += s_
                    mg_after += int(a_new_q[iq]) == AIM_BAND and not s_
                iq += 1
            else:
                after += int(nt_c[it])
                ok = ok and int(nt_c[it]) < MEND_INVALID
                least = min(least, float(na_t[it]))
                worst_after = np.maximum(worst_after, held_new[1][it])
                if lines_after is not None and lines_new_t is not None:
                    lines_after += lines_new_t[it]
                if near_after is not None and near_new_t is not None:
                    near_after += near_new_t[it]
                if edge_guard:
                    eg_after += e_new_t[it]
                    s_ = int(nt_c[it]) >= AIM_OUTER
                    sg_after += s_
                    mg_after += int(a_new_t[it]) == AIM_BAND and not s_
                it += 1
            n_el = nrm_new_q[iq - 1] if len(el) == 4 else nrm_new_t[it - 1]
            if ok and float(n_el @ turned_as(old, touched)) <= 0.0:
                ok = False
            if ok and protect and _f in protect and (band_new_q[iq - 1] if len(el) == 4 else band_new_t[it - 1]):
                prot_band += 1
            if protect:
                severe_after += int((nq_c[iq - 1] if len(el) == 4 else nt_c[it - 1]) >= AIM_OUTER)
        if ok and protect and prot_band > sum(band_now[t] for t in touched if face[t] in protect):
            # SHMESH-86b: no more in-band elements on a gridded or mapped face than the change found there - unless
            # the change takes an element out past an outer limit or a solver limit (that always comes first: held
            # here, a 6.8 degree sliver on a stamped part's strip stayed)
            severe_before = int((cost[list(touched)] >= AIM_OUTER).sum())
            ok = severe_after < severe_before
            if not ok and long_reason is not None and lines_after is not None and lines_now is not None:
                # (SHMESH-86e: or back under the user's longest edge, raising no count past an outer or solver limit)
                r_ = lines_after - lines_now[list(touched)].sum(axis=0)
                ok = bool((r_[long_reason[0]] < 0).any() and not (r_[long_reason[1]] > 0).any())
        severe_after = 0
        prot_band = 0
        if ok and edge_guard and (eg_after > e_now[list(touched)].sum(axis=0)).any():
            # SHMESH-88: more under / over an edge line round the change - kept only for a reason
            ok = (sg_after < int(sev_now[list(touched)].sum())) or (mg_after < int(miss_now[list(touched)].sum()))
        rise_ = near_rise = None
        if lines_after is not None and lines_now is not None:
            # SHMESH-84: no line's count rises over what the change touches (summed costs traded one line for
            # another: a quad of 36.6 degrees in a paved cell whose own mesh had nothing under 44.5) - judged below,
            # every candidate at once (SHMESH-28c: the rule reads each row alone)
            rise_ = lines_after - lines_now[list(touched)].sum(axis=0)
            near_rise = (None if near_after is None or near_now is None
                         else near_after - near_now[list(touched)].sum(axis=0))
        # SHMESH-28b: the worst element under each line never gets worse - a change that lowers the total by
        # trading several elements in a band for one further out is not made (a welded part at 10 mm: one
        # triangle at 9.1 degrees where the elements it came from were at 21.4 at the worst)
        worst_ok = bool(np.all(worst_after <= np.maximum(worst_now[list(touched)].max(axis=0), band) + WORST_SLACK))
        judged.append((ok, rise_, near_rise, worst_ok,
                       after < before and len({frozenset(el) for el, _f, _o in new}) == len(new),
                       (after - before, len(new), -round(least, 9), e, ci)))
    if batch is not None:
        # (SHMESH-28c: the batch's rows as the loop's - per candidate: its checks so far, its rise per line, its rise
        # near each outer limit, the worst check, whether it lowers the cost, its rank)
        ok_b, rise_b, near_b, worst_b, gain_b, least_b = batch
        judged = [(bool(ok_b[ci]), None if rise_b is None else rise_b[ci], None if near_b is None else near_b[ci],
                   bool(worst_b[ci]), bool(gain_b[ci] < 0) and bool(ok_b[ci]) and bool(worst_b[ci])
                   and len({frozenset(el) for el, _f, _o in cands[ci][2]}) == len(cands[ci][2]),
                   (int(gain_b[ci]), len(cands[ci][2]), -round(float(least_b[ci]), 9), cands[ci][0], ci))
                  for ci in range(len(cands))]
    rule_ok = sev_down = None
    rows_ = [j for j, x in enumerate(judged) if x[1] is not None]
    if rows_:
        rise_all = np.array([judged[j][1] for j in rows_])
        near_all = (None if judged[rows_[0]][2] is None else np.array([judged[j][2] for j in rows_]))
        rule_ok = np.zeros(len(judged), dtype=bool)
        rule_ok[rows_] = line_rule_ok(rise_all, slack_m, lines_limits, missed=missed_m, near=near_all,
                                      severe_first=MEND_SEVERE_FIRST)
        if slack_f is not None:
            # SHMESH-86g: back under the longest edge, no count past an outer or solver limit raised: the whole
            # allowance
            longs = ((rise_all[:, edge_cols(lines_limits)[2:4]] < 0).any(axis=1)
                     & ~(rise_all[:, line_groups(lines_limits)["severe"]] > 0).any(axis=1))
            if longs.any():
                ok_f = line_rule_ok(rise_all, slack_f, lines_limits, missed=missed_m, near=near_all,
                                    severe_first=MEND_SEVERE_FIRST)
                rule_ok[np.asarray(rows_)[longs]] = ok_f[longs]
                long_of = {rows_[i] for i in np.flatnonzero(longs).tolist()}
        sev_down = np.zeros(len(judged), dtype=bool)
        if MEND_SEVERE_FIRST or MEND_TRI_GUARD:
            sev_down[rows_] = severe_down(rise_all, lines_limits)
    e_cols = edge_cols(lines_limits) if EDGE_REASON else None
    gate_ok = None
    if short_gate is not None:
        g_now, g_q, g_t = lines_now, lines_new_q, lines_new_t
        if g_now is None:
            g_now = np.vstack([line_matrix(xyz, Qa, lines_limits, m=wq), line_matrix(xyz, Ta, lines_limits, m=wt)])
            g_q = line_matrix(xyz, new_qa, lines_limits, m=mnq)
            g_t = line_matrix(xyz, new_ta, lines_limits, m=mnt)
        gate_ok = _short_gate_ok(cands, conn, face, nq, g_now, g_q, g_t, lines_limits, short_gate,
                                 set(lines_limits.protect))

    # (SHMESH-70's guard reads each element's longest edge once a round: the nodes stand still while the changes are
    # judged, so every candidate that holds an element reads the same float as before; s33 spent a third of its time
    # recomputing it, 4 million times)
    longest_at: dict[tuple, float] = {}

    def longest(c_) -> float:
        k_ = tuple(c_)
        got = longest_at.get(k_)
        if got is None:
            p_ = xyz[list(c_)]
            got = longest_at[k_] = float(np.linalg.norm(p_ - np.roll(p_, -1, axis=0), axis=1).max())
        return got

    for ci, (ok, rise_, _near_rise, worst_ok, better, rank) in enumerate(judged):
        if rise_ is not None:
            ok = ok and bool(rule_ok[ci])
            if ok and MEND_TRI_GUARD and not bool(sev_down[ci]) and (
                    sum(len(el) == 3 for el, _f, _o in cands[ci][2]) > sum(len(conn[t]) == 3 for t in cands[ci][1])):
                ok = False      # (SHMESH-86d: no triangle more without taking an element out past a limit)
            if ok and (rise_ > 0).any():
                rise_of[ci] = np.maximum(rise_, 0)
                if e_cols is not None:
                    rise_of[ci][e_cols] = 0         # (SHMESH-88: no slack on an edge; its reason was held)
                if MEND_SEVERE_FIRST and bool(sev_down[ci]):
                    first_of.add(ci)                # (SHMESH-89: spends the allowance, never stopped by it)
        ok = ok and worst_ok
        if gate_ok is not None:
            ok = ok and bool(gate_ok[ci])
        if ok and hard_long is not None:
            # SHMESH-70: no change leaves an element with an edge over the user's longest edge (with its band) where
            # the elements it replaces had none that long (a merge of a short edge left 8 to 12 mm edges at 4 to 6 mm)
            # (INT4: judged after the SHMESH-28c batch, on the candidate's own elements)
            touched_, new_ = cands[ci][1], cands[ci][2]
            was = max(longest(conn[t_]) for t_ in touched_)
            ok = all(longest(el) <= max(hard_long, was) + 1e-9 for el, _f, _o in new_)
        if ok and better:
            ranked.append(rank)
    if not ranked:
        return None
    used: set[int] = set()
    gone: set[int] = set()
    add: list[tuple[tuple[int, ...], int]] = []
    # SHMESH-80: the corners of the elements round the bad ones and how many elements hold each of their edges (every
    # element on an edge a change can make is among them: each such edge has a node of a bad element)
    near_sets = {frozenset(conn[x]) for x in near.tolist()} if TWIN_GUARD else set()
    ecount = {k: len(v) for k, v in users.items()} if TWIN_GUARD else {}

    def _stays_surface(touched: tuple[int, ...], new: list) -> dict | None:
        """SHMESH-80: the change in how many elements hold each edge when the change writes no element on the corners
        of another, and leaves no edge held by more elements than max(2, as many as now); else None."""
        old_sets = {frozenset(conn[t]) for t in touched}
        if any(frozenset(el) in near_sets and frozenset(el) not in old_sets for el, _f, _o in new):
            return None
        delta: dict[tuple[int, int], int] = {}
        for els, sign in (([conn[t] for t in touched], -1), ([el for el, _f, _o in new], 1)):
            for el in els:
                for m in range(len(el)):
                    a_, b_ = el[m], el[(m + 1) % len(el)]
                    k_ = (a_, b_) if a_ < b_ else (b_, a_)
                    delta[k_] = delta.get(k_, 0) + sign
        for k_, d in delta.items():
            if d > 0 and ecount.get(k_, 0) + d > max(ecount.get(k_, 0), 2):
                return None
        return delta

    for _gain, _n, _least, _e, ci in sorted(ranked):
        _e2, touched, new, kind, block, move = cands[ci]
        if block & used:
            continue
        if ci in rise_of:
            room_ = slack_f if (slack_f is not None and ci in long_of) else slack_m
            if ci not in first_of and not bool(np.all(rise_of[ci] <= room_)):
                continue                    # (SHMESH-86: the allowance is spent)
            slack_m = np.maximum(slack_m - rise_of[ci], 0)
            if slack_f is not None:
                slack_f = np.maximum(slack_f - rise_of[ci], 0)
        if TWIN_GUARD and move is None:
            delta_ = _stays_surface(touched, new)
            if delta_ is None:
                continue
            near_sets -= {frozenset(conn[t]) for t in touched}
            near_sets |= {frozenset(el) for el, _f, _o in new}
            for k_, d in delta_.items():
                ecount[k_] = ecount.get(k_, 0) + d
        used |= block
        done[kind] += 1
        if move is not None:
            here_all[move[0]] = move[1]          # the elements stay; their node stands somewhere else
            continue
        gone |= set(touched)
        add += [(el, f) for el, f, _o in new]
    if not used:
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
    n = _unit(_cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1]))
    e_next = p[:, [1, 2, 3, 0]] - p
    e_prev = p[:, [3, 0, 1, 2]] - p
    cross = _cross(e_next, e_prev)
    jac = np.einsum("mki,mi->mk", cross, n) / np.maximum(
        np.linalg.norm(e_next, axis=2) * np.linalg.norm(e_prev, axis=2), 1e-300)
    lengths = np.linalg.norm(e_next, axis=2)
    n1 = _unit(_cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]))
    n2 = _unit(_cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]))
    n3 = _unit(_cross(p[:, 1] - p[:, 0], p[:, 3] - p[:, 0]))
    n4 = _unit(_cross(p[:, 2] - p[:, 1], p[:, 3] - p[:, 1]))
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
                dets.append(np.einsum("mi,mi->m", _cross(d_xi, d_eta), n))
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
    area2 = np.linalg.norm(_cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
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
    folded = np.concatenate([q.get("min_jacobian", np.ones(0)) <= 0, t.get("min_jacobian", np.ones(0)) <= 0])
    # SHMESH-81: an element on the same corners as another (written twice) and one with no area to speak of are not
    # valid either, as a deck reader counts them
    twin = twins(mesh.quads, mesh.trias) if VALID_TWINS else np.zeros(n, dtype=bool)
    flat = zero_area(mesh.xyz, mesh.quads, mesh.trias) if VALID_TWINS else np.zeros(n, dtype=bool)
    bad = int((folded | twin | flat).sum())
    out: dict = {"elements": n, "quads": int(len(mesh.quads)), "trias": int(len(mesh.trias)),
                 "tria_share": float(f"{len(mesh.trias) / n:.4g}") if n else 0.0,
                 "invalid": bad, "valid": bad == 0 and n > 0, "figures": {}, "past_limits": {}, "notes": []}
    if VALID_TWINS:
        out["folded"] = int(folded.sum())
        out["duplicates"] = int(twin.sum())
        out["zero_area"] = int(flat.sum())
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
            past = int(solver_past(d[key], op, limit).sum())       # (SHMESH-89e: the steps' own margin)
            if past:
                out["nastran_past"][card] = past
    out["within_nastran"] = bool(out["valid"]) and not out["nastran_past"]
    if out["nastran_past"]:
        out["notes"].insert(0, "Past Nastran's own GEOMCHECK limits: " + ", ".join(
            f"{v} elements by {k}" for k, v in out["nastran_past"].items()) + ".")
    if n and len(mesh.trias) / n > TRIA_SHARE:
        out["notes"].append(f"{100 * len(mesh.trias) / n:.1f}% of elements are triangles (target under "
                            f"{100 * TRIA_SHARE:.0f}%).")
    if bad and not VALID_TWINS:
        out["notes"].insert(0, f"{bad} elements are folded or flat (a corner Jacobian not positive): the mesh is "
                               "not valid.")
    elif bad:
        why = [f"{v} {k}" for k, v in (("folded or flat (a corner Jacobian not positive)", out["folded"]),
                                       ("written twice (the same corners as another)", out["duplicates"]),
                                       ("with no area", out["zero_area"])) if v]
        out["notes"].insert(0, f"{bad} elements are not valid ({'; '.join(why)}): the mesh is not valid.")
    return out


#: SHMESH-81: the validity counts elements written twice and elements with no area (as the deck reader does)
VALID_TWINS = True
#: an element's area is none to speak of under this share of the square of the mesh's box diagonal (the deck
#: reader's own threshold)
ZERO_AREA = 1e-12


def twins(quads: np.ndarray, trias: np.ndarray) -> np.ndarray:
    """(quads + trias,) bool: SHMESH-81 - the element is on the same corners as an earlier one of its shape (each
    copy after the first)."""
    out = []
    for conn in (quads, trias):
        m = np.zeros(len(conn), dtype=bool)
        if len(conn) > 1:
            s = np.sort(np.asarray(conn, dtype=np.int64), axis=1)
            _u, first = np.unique(s, axis=0, return_index=True)
            m[:] = True
            m[first] = False
        out.append(m)
    return np.concatenate(out)


def drop_twins(quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int]
               ) -> tuple[np.ndarray, np.ndarray, list[int], list[int], int]:
    """SHMESH-80: elements on the same corners as another (two faces whose meshes fold onto each other along a sliver
    between them - gmsh writes the same chord triangle in both -, two quads round a node only they share cut along
    the same diagonal) are left once or not at all, whichever leaves fewer free edges round them, then fewer edges
    held by three or more elements (on a tie: once). A fold of two faces closes up (the elements beside meet along
    the chord); a copy written in place goes. Returns (quads, trias, their faces, how many elements went)."""
    groups: list[tuple[int, list[int]]] = []          # (shape 4 / 3, the indices of the copies)
    for shape, conn in ((4, quads), (3, trias)):
        if len(conn) < 2:
            continue
        s = np.sort(np.asarray(conn, dtype=np.int64), axis=1)
        _u, inv, cnt = np.unique(s, axis=0, return_inverse=True, return_counts=True)
        inv = np.asarray(inv).ravel()
        for g in np.flatnonzero(cnt > 1).tolist():
            groups.append((shape, np.flatnonzero(inv == g).tolist()))
    if not groups:
        return quads, trias, list(q_face), list(t_face), 0
    held: dict[tuple[int, int], int] = {}
    for conn in (quads, trias):
        k = conn.shape[1] if len(conn) else 0
        for c in np.asarray(conn).tolist():
            for m in range(k):
                a, b = c[m], c[(m + 1) % k]
                key = (a, b) if a < b else (b, a)
                held[key] = held.get(key, 0) + 1
    gone_q: set[int] = set()
    gone_t: set[int] = set()

    def sides(shape: int, ids: list[int]) -> list[tuple[int, int]]:
        c = (quads if shape == 4 else trias)[ids[0]].tolist()
        return [(min(c[m], c[(m + 1) % shape]), max(c[m], c[(m + 1) % shape])) for m in range(shape)]

    # twins beside twins are a region two faces both cover (an overlap): it is left once, never opened up
    group_at: dict[tuple[int, int], set[int]] = {}
    for g, (shape, ids) in enumerate(groups):
        for e in sides(shape, ids):
            group_at.setdefault(e, set()).add(g)
    for shape, ids in groups:
        edges = sides(shape, ids)
        others = {e: held[e] - len(ids) for e in edges}

        def score(kept: int, others=others, edges=edges) -> tuple[int, int]:
            after = [others[e] + kept for e in edges]
            return sum(1 for v in after if v == 1), sum(1 for v in after if v > 2)

        beside = any(len(group_at[e]) > 1 for e in edges)
        keep = 1 if beside or score(1) <= score(0) else 0
        for e in edges:
            held[e] = others[e] + keep
        (gone_q if shape == 4 else gone_t).update(ids[keep:])
    kq = [i for i in range(len(quads)) if i not in gone_q]
    kt = [i for i in range(len(trias)) if i not in gone_t]
    return (np.asarray(quads)[kq].reshape(-1, 4), np.asarray(trias)[kt].reshape(-1, 3), [q_face[i] for i in kq],
            [t_face[i] for i in kt], len(gone_q) + len(gone_t))


def _tiny_area(xyz: np.ndarray, *conns: np.ndarray) -> float:
    """SHMESH-81: the area under which an element has none to speak of - ZERO_AREA of the squared box diagonal of the
    nodes the elements use."""
    used = np.unique(np.concatenate([np.asarray(c, dtype=np.int64).ravel() for c in conns] or [np.zeros(0, int)]))
    if not len(used):
        return 0.0
    p = xyz[used]
    return ZERO_AREA * max(float(np.linalg.norm(p.max(axis=0) - p.min(axis=0))), 1e-9) ** 2


def zero_area(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray) -> np.ndarray:
    """(quads + trias,) bool: SHMESH-81 - the element's area is at most ZERO_AREA of the box diagonal squared (a
    quad's area: half its diagonals' cross product)."""
    if not (len(quads) + len(trias)):
        return np.zeros(0, dtype=bool)
    used = np.unique(np.concatenate([np.asarray(quads).ravel(), np.asarray(trias).ravel()]).astype(np.int64))
    p = xyz[used]
    diag = float(np.linalg.norm(p.max(axis=0) - p.min(axis=0))) if len(p) else 0.0
    tiny = ZERO_AREA * max(diag, 1e-9) ** 2
    out = []
    if len(quads):
        pq = xyz[quads]
        out.append(0.5 * np.linalg.norm(_cross(pq[:, 2] - pq[:, 0], pq[:, 3] - pq[:, 1]), axis=1) <= tiny)
    if len(trias):
        pt = xyz[trias]
        out.append(0.5 * np.linalg.norm(_cross(pt[:, 1] - pt[:, 0], pt[:, 2] - pt[:, 0]), axis=1) <= tiny)
    return np.concatenate(out)


def per_face(mesh: ShellMesh, low_mm: float | None = None, high_mm: float | None = None,
             aims: tuple = ()) -> dict[str, dict[str, int]]:
    """SHMESH-51o12: per mid-face, its triangles and its elements past each limit (LIMITS) - only the faces with any,
    so a run's record says where its triangles and past elements sit."""
    q = quad_measures(mesh.xyz, mesh.quads) if len(mesh.quads) else {}
    t = tria_measures(mesh.xyz, mesh.trias) if len(mesh.trias) else {}
    nq = len(mesh.quads)
    mids = np.asarray([str(m) for m in mesh.mid], dtype=object)
    out: dict[str, dict[str, int]] = {}

    def add(names: np.ndarray, key: str) -> None:
        for m, c in zip(*np.unique(names, return_counts=True), strict=True):
            out.setdefault(str(m), {})[key] = int(c)

    if len(mesh.trias):
        add(mids[nq:], "trias")
    for key, op, limit, _src in LIMITS:
        for d, names in ((q, mids[:nq]), (t, mids[nq:])):
            if key in d and len(names):
                bad = d[key] < limit if op == ">=" else d[key] > limit
                if bad.any():
                    add(names[bad], key)
    # (SHMESH-51o24: and every element with an edge under the user's shortest or over their longest edge, each edge
    # its straight length, as a deck reader measures it)
    for els, names in ((mesh.quads, mids[:nq]), (mesh.trias, mids[nq:])):
        if not len(els) or (low_mm is None and high_mm is None):
            continue
        p_ = mesh.xyz[els]
        ed = np.linalg.norm(p_ - np.roll(p_, -1, axis=1), axis=2)
        if low_mm is not None and (ed.min(axis=1) < float(low_mm)).any():
            add(names[ed.min(axis=1) < float(low_mm)], "edge_under_shortest")
        if high_mm is not None and (ed.max(axis=1) > float(high_mm)).any():
            add(names[ed.max(axis=1) > float(high_mm)], "edge_over_longest")
    # (SHMESH-51o34: and each line of the user's - its target - with the elements of its shape outside it, keyed
    # "<figure>_outside_target": a triangle at 24 degrees against a 30 degree target counts, as a deck reader sees it)
    for shape, key, op, target, _outer, _share in aims:
        d, names = (q, mids[:nq]) if shape == "quad" else (t, mids[nq:])
        if key not in d or not len(names):
            continue
        bad = d[key] < target if op == ">=" else d[key] > target
        if bad.any():
            add(names[bad], f"{key}_outside_target")
    return {k: out[k] for k in sorted(out)}


def element_angle_deg(radius: float, size: float) -> float:
    """The angle one element of `size` turns through on a curve of `radius` (degrees): the curvature check."""
    return math.degrees(size / radius) if radius > 0 else 0.0


__all__ = ["AIM_BAND", "AIM_OUTER", "IN_FORCE", "InForce", "LIMITS", "NASTRAN_LIMITS", "TRIA_SHARE", "aim_score",
           "measure", "nastran_past", "quad_measures", "absorb_caps", "flip_past_trias", "split_aimed_quads",
           "split_past_quads", "tria_measures"]
