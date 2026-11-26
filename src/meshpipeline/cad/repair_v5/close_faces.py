# Responsibility: the close_faces class's arithmetic (v5 step 7): the facet sag, the segments that keep it under the
#                 gap, whether two facet sets cross, and the whole-part scan's sweep and time cap. Pure: no OCCT, no
#                 pool, no gmsh.
# Owns: `sag_mm`, `segments_for_gap`, `tri_tri_intersect`, `count_crossings`, `candidate_pairs`, `examine`,
#       `PREDICTED_MEMBERS`.
# Boundaries: the kernel side (the scan, the predictor's tessellation) is close_faces_kernel.py, the predictor's
#             gmsh child close_faces_gmsh.py; the class rule is diagnose.py. Nothing here tunes a mesher.
"""Will two close faces' facets cross at the shipped mesh size?

A mesher discretises each boundary curve first and the surface from it; on a coarse curved face the triangles are
chords whose SAG (radius x (1 - cos(pi/n)) for n segments round a circle) can exceed the gap to a nearby face of
another body or of the same thin wall. When it does, the two surface meshes intersect and 3D boundary recovery fails
(gmsh "overlapping facets", HXT "3D mesh failed"). The PREDICTOR takes the faces' facets from the shipped mesher's
own surface stage (close_faces_gmsh: each gmsh member's 2D mesh of the pair, with that member's own options) and
counts triangle pairs that intersect.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

Vec = tuple[float, float, float]
Tri = tuple[Vec, Vec, Vec]

#: the members the predictor meshes: the three gmsh members (their `mesh_child` profiles). Netgen is not predicted:
#: its surface mesher is not gmsh's, and a None is "not measured", never "no crossing".
PREDICTED_MEMBERS: dict[str, str] = {"gmsh_prod": "prod", "gmsh_default": "default", "gmsh_alt": "alt"}


def sag_mm(radius: float, segments_full_circle: int) -> float:
    """The chord's sag on a circle of `radius` cut into `segments_full_circle` equal segments."""
    return radius * (1.0 - math.cos(math.pi / max(1, segments_full_circle))) if radius > 0 else 0.0


def segments_for_gap(radius: float, gap_mm: float) -> int | None:
    """The fewest equal segments round a full circle of `radius` whose sag stays under HALF the gap (so the two
    faces' sags together cannot bridge it). None without a radius or a gap."""
    if not radius or radius <= 0 or not gap_mm or gap_mm <= 0:
        return None
    c = 1.0 - gap_mm / (2.0 * radius)
    if c <= -1.0:
        return 2
    return max(2, math.ceil(math.pi / math.acos(c) - 1e-9))


# --- triangle intersection (separating axis theorem) ----------------------------------------------------------


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def tri_tri_intersect(p: Tri, q: Tri, eps: float = 0.0) -> bool:
    """Do triangles `p` and `q` intersect (share any point, within `eps` mm)? Separating axis theorem over both
    normals, the nine edge-edge cross products and (for coplanar pairs) the six in-plane edge normals."""
    ep = [_sub(p[1], p[0]), _sub(p[2], p[1]), _sub(p[0], p[2])]
    eq = [_sub(q[1], q[0]), _sub(q[2], q[1]), _sub(q[0], q[2])]
    np_, nq = _cross(ep[0], ep[1]), _cross(eq[0], eq[1])
    axes = [np_, nq, *(_cross(a, b) for a in ep for b in eq), *(_cross(np_, e) for e in ep),
            *(_cross(nq, e) for e in eq)]
    scale = max(1e-300, max(abs(c) for v in (*p, *q) for c in v))
    for axis in axes:
        norm = math.sqrt(_dot(axis, axis))
        if norm <= 1e-12 * scale * scale:
            continue   # degenerate axis (parallel edges): the other axes decide
        pa = [_dot(v, axis) / norm for v in p]
        qa = [_dot(v, axis) / norm for v in q]
        if min(pa) > max(qa) + eps or min(qa) > max(pa) + eps:
            return False
    return True


def _box(t: Tri) -> tuple[float, float, float, float, float, float]:
    xs, ys, zs = zip(*t, strict=True)
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


def count_crossings(a: Sequence[Tri], b: Sequence[Tri], *, eps: float = 0.0, cap: int = 1_000,
                    nodes_a: Sequence[tuple[int, ...]] | None = None,
                    nodes_b: Sequence[tuple[int, ...]] | None = None) -> int:
    """How many (triangle of `a`, triangle of `b`) pairs intersect, stopping at `cap`. Sweep-and-prune on x. With
    `nodes_a`/`nodes_b` (each triangle's mesh node ids) a pair sharing a node is never counted: it touches there by
    construction, it does not cross."""
    order = sorted(range(len(b)), key=lambda k: _box(b[k])[0])
    boxes_b = [(_box(b[k]), b[k], set(nodes_b[k]) if nodes_b is not None else set()) for k in order]
    starts = [bx[0] for bx, _, _ in boxes_b]
    import bisect

    found = 0
    for i, t in enumerate(a):
        ba = _box(t)
        own = set(nodes_a[i]) if nodes_a is not None else set()
        hi = bisect.bisect_right(starts, ba[3] + eps)
        for bb, u, theirs in boxes_b[:hi]:
            if own & theirs:
                continue
            if (bb[3] < ba[0] - eps or bb[1] > ba[4] + eps or bb[4] < ba[1] - eps or bb[2] > ba[5] + eps
                    or bb[5] < ba[2] - eps):
                continue
            if tri_tri_intersect(t, u, eps):
                found += 1
                if found >= cap:
                    return found
    return found


# --- the whole-part scan's pure half: candidate pairs, then the time-capped exact measurement -------------------

Box = tuple[float, float, float, float, float, float]


def candidate_pairs(boxes: Sequence[Box], shared: Sequence[set[int]]) -> list[tuple[float, int, int]]:
    """(box separation, a, b) for every pair of faces (1-based) whose boxes meet and whose `shared` sets (their
    edges and vertices) are disjoint, nearest boxes first. Sweep-and-prune on x."""
    order = sorted(range(len(boxes)), key=lambda k: boxes[k][0])
    out: list[tuple[float, int, int]] = []
    active: list[int] = []
    for k in order:
        b = boxes[k]
        active = [j for j in active if boxes[j][3] >= b[0]]
        for j in active:
            c = boxes[j]
            if c[1] > b[4] or c[4] < b[1] or c[2] > b[5] or c[5] < b[2] or shared[j] & shared[k]:
                continue
            sep = math.sqrt(sum(max(0.0, max(b[n], c[n]) - min(b[n + 3], c[n + 3])) ** 2 for n in range(3)))
            out.append((sep, min(j, k) + 1, max(j, k) + 1))
        active.append(k)
    return sorted(out)


def examine(candidates: Sequence[tuple[float, int, int]], measure: Callable[[int, int], dict | None], *,
            budget_s: float, clock: Callable[[], float]) -> tuple[list[dict], int, bool]:
    """(the measured pairs `measure` kept, how many were examined, truncated?) - candidates in order until
    `budget_s` of `clock` is spent. `measure(a, b)` returns the pair's row or None to drop it."""
    deadline = clock() + budget_s
    found, examined = [], 0  # type: ignore[var-annotated]
    for _sep, a, b in candidates:
        if clock() > deadline:
            return found, examined, True
        examined += 1
        row = measure(a, b)
        if row is not None:
            found.append(row)
    return found, examined, False
