# Responsibility: Say WHICH gmsh surfaces' surface meshes cross each other - the place a failed
#                 volume mesh actually breaks - from the 2D mesh alone.
# Boundaries: pure numpy over node coordinates, triangles and their surface tags. It imports no
#             gmsh and no kernel, mutates nothing and repairs nothing. `mesh_probe`'s child calls it
#             on the surface mesh gmsh has just built, after the 3D stage refused it.
# Collaborates with: cad/repair/mesh_probe.py (which maps the surface tags named here onto OCC
#                    faces and stamps them as located defects).
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# WHY THIS EXISTS. gmsh 4.15's tetgen boundary recovery fails with "PLC Error: A segment and a
# facet intersect at point" and then NOTHING: no coordinates, no curve, no surface (measured on the
# wave-1 corpus, all 28 such files). Its "overlapping facets on surface A surface B" names two
# tags, but not always ones that map onto a single OCC face. Either way the part's surface mesh is
# in hand: the 2D stage succeeded. Two triangles of DIFFERENT surfaces that share no node and still
# cross, or lie on top of one another, are exactly what tetgen refused - so this finds them and
# names their surface pair. That is evidence of the failure, not an inference from body contact.

#: Kinds of clash: an edge of one surface passes through a triangle of the other, or the two lie
#: in one plane and overlap (coincident faces, typically two bodies sharing a wall unglued).
CROSSING = "crossing"
COPLANAR = "coplanar"
#: Two triangles of DIFFERENT surfaces that share an edge and lie folded onto each other (their
#: apexes on the same side, dihedral ~0): gmsh's "overlapping facets" between two surfaces.
#: Measured on abc/0059_00595955 (surfaces 370/376): three such pairs at exactly 0 degrees.
FOLD = "fold"

#: One facet meshed on two DIFFERENT surfaces: all three nodes shared. Measured on
#: abc/0059_00595955, which is what gmsh's "overlapping facets on surface 370 surface 376" was.
DUPLICATE = "duplicate"

#: The largest angle, in degrees, between two edge-sharing triangles' apex directions that is
#: still a fold. A real sharp edge is degrees; a fold is numerically flat.
FOLD_DEGREES = 0.1

#: Relative tolerances, as fractions of the mesh's diagonal (lengths) or plain numbers (barycentric
#: and parametric). An edge that merely ENDS on another surface is not a crossing: tetgen accepts
#: that when the node is shared, and the parametric margin keeps float noise at a seam out.
_PARAM_EPS = 1e-9
_PLANE_FRACTION = 1e-7
_PARALLEL_COS = 1.0 - 1e-9

#: A triangle spanning more grid cells than this per axis is tested by box against all others.
_SPAN_CAP = 4
#: Larger surface meshes are not searched: the probe's budget belongs to the mesher.
MAX_TRIANGLES = 400_000
#: At most this many candidate triangle pairs are built and tested (Greptile #495: one crowded
#: cell could build tens of millions). A search that hits the cap reports what it found in them.
MAX_CANDIDATE_PAIRS = 2_000_000
#: Candidate pairs are tested this many at a time, and the deadline is checked between chunks.
_CHUNK = 200_000


@dataclass(frozen=True, slots=True)
class Clash:
    """Two gmsh surfaces whose triangles cross: tags ascending, how many triangle pairs, one
    witness point, and whether it was a crossing or a coplanar overlap."""
    surfaces: tuple[int, int]
    hits: int
    point: tuple[float, float, float]
    kind: str


def _pairs_across_tags(members: np.ndarray, tags: np.ndarray, room: int) -> np.ndarray:
    """Pairs (i, j) of `members` whose surface tags DIFFER, at most `room` of them. Built per tag
    run, so a crowd of one surface's triangles costs nothing (Greptile #495)."""
    order = np.argsort(tags[members], kind="stable")
    members = members[order]
    run_tags = tags[members]
    starts = np.flatnonzero(np.r_[True, run_tags[1:] != run_tags[:-1]])
    ends = np.r_[starts[1:], len(members)]
    out: list[np.ndarray] = []
    made = 0
    for start, end in zip(starts, ends, strict=True):
        if end >= len(members) or made >= room:
            break
        here, later = members[start:end], members[end:]
        take = min(len(here) * len(later), room - made)
        a = np.repeat(here, len(later))[:take]
        b = np.tile(later, len(here))[:take]
        out.append(np.stack([a, b], axis=1))
        made += take
    if not out:
        return np.zeros((0, 2), dtype=np.int64)
    return np.concatenate(out)


def _candidate_pairs(lo: np.ndarray, hi: np.ndarray, *, tags: np.ndarray,
                     groups: np.ndarray, max_pairs: int = 0) -> np.ndarray:
    """Triangle index pairs (i < j) whose boxes may meet, via a uniform grid - only pairs of
    DIFFERENT surfaces in ONE volume component (>= 0), filtered while they are built and never
    after, and at most `max_pairs` of them (MAX_CANDIDATE_PAIRS when 0). Below the cap a superset of
    the meeting pairs; at the cap, the first `max_pairs` in grid order and no more."""
    cap = int(max_pairs) or MAX_CANDIDATE_PAIRS
    count = len(lo)
    if count < 2:
        return np.zeros((0, 2), dtype=np.int64)
    extent = (hi - lo).max(axis=1)
    cell = float(np.percentile(extent, 75))
    if cell <= 0.0:
        cell = max(float((hi.max(axis=0) - lo.min(axis=0)).max()), 1e-12)
    origin = lo.min(axis=0)
    first = np.floor((lo - origin) / cell).astype(np.int64)
    last = np.floor((hi - origin) / cell).astype(np.int64)
    span = last - first + 1
    live = groups >= 0
    small = live & (span <= _SPAN_CAP).all(axis=1)
    big = np.nonzero(live & ~small)[0]

    keys: list[np.ndarray] = []
    owners: list[np.ndarray] = []
    dims = last.max(axis=0) + 2
    for dx in range(_SPAN_CAP):
        for dy in range(_SPAN_CAP):
            for dz in range(_SPAN_CAP):
                offset = np.array([dx, dy, dz])
                mask = small & (span > offset).all(axis=1)
                if not mask.any():
                    continue
                c = first[mask] + offset
                keys.append((c[:, 0] * dims[1] + c[:, 1]) * dims[2] + c[:, 2])
                owners.append(np.nonzero(mask)[0])
    pairs: list[np.ndarray] = []
    made = 0
    if keys:
        key = np.concatenate(keys)
        owner = np.concatenate(owners)
        group = groups[owner]
        order = np.lexsort((owner, group, key))
        key, group, owner = key[order], group[order], owner[order]
        bounds = np.flatnonzero((np.diff(key) != 0) | (np.diff(group) != 0)) + 1
        for members in np.split(owner, bounds):
            if made >= cap:
                break
            if len(members) < 2:
                continue
            found = _pairs_across_tags(members, tags, cap - made)
            if len(found):
                pairs.append(found)
                made += len(found)
    for i in big:
        if made >= cap:
            break
        meets = np.nonzero((lo <= hi[i]).all(axis=1) & (hi >= lo[i]).all(axis=1)
                           & (groups == groups[i]) & (tags != tags[i]))[0][:cap - made]
        if len(meets):
            pairs.append(np.stack([np.full(len(meets), i), meets], axis=1))
            made += len(meets)
    if not pairs:
        return np.zeros((0, 2), dtype=np.int64)
    found = np.sort(np.concatenate(pairs), axis=1)
    return np.unique(found, axis=0)[:cap]


def _edge_hits(coords: np.ndarray, tris: np.ndarray, a: np.ndarray, b: np.ndarray
               ) -> tuple[np.ndarray, np.ndarray]:
    """For each pair (a[k], b[k]): does an edge of triangle a[k] pass through the interior of
    triangle b[k] (Moller-Trumbore, ends excluded)? Returns (hit mask, a witness point per pair)."""
    hit = np.zeros(len(a), dtype=bool)
    point = np.zeros((len(a), 3))
    b0 = coords[tris[b, 0]]
    e1 = coords[tris[b, 1]] - b0
    e2 = coords[tris[b, 2]] - b0
    for i, j in ((0, 1), (1, 2), (2, 0)):
        p0 = coords[tris[a, i]]
        d = coords[tris[a, j]] - p0
        h = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, h)
        ok = np.abs(det) > 1e-300
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = p0 - b0
        u = inv * np.einsum("ij,ij->i", s, h)
        q = np.cross(s, e1)
        v = inv * np.einsum("ij,ij->i", d, q)
        t = inv * np.einsum("ij,ij->i", e2, q)
        now = (ok & (u >= -_PARAM_EPS) & (v >= -_PARAM_EPS) & (u + v <= 1 + _PARAM_EPS)
               & (t > _PARAM_EPS) & (t < 1 - _PARAM_EPS))
        fresh = now & ~hit
        point[fresh] = p0[fresh] + t[fresh, None] * d[fresh]
        hit |= now
    return hit, point


def _folded(coords: np.ndarray, tris: np.ndarray, a: np.ndarray, b: np.ndarray,
            same: np.ndarray) -> np.ndarray:
    """For pairs (a[k], b[k]) sharing exactly one edge: are they folded onto each other - the
    apexes, seen across the shared edge, within FOLD_DEGREES of the same direction?"""
    apex_a = tris[a][~same.any(axis=2)]                           # the node of a not in b
    apex_b = tris[b][~same.any(axis=1)]                           # the node of b not in a
    edge = tris[a][same.any(axis=2)].reshape(-1, 2)
    e0, e1 = coords[edge[:, 0]], coords[edge[:, 1]]
    axis = e1 - e0
    axis /= np.maximum(np.linalg.norm(axis, axis=1), 1e-300)[:, None]

    def across(apex: np.ndarray) -> np.ndarray:
        v = coords[apex] - e0
        v -= np.einsum("ij,ij->i", v, axis)[:, None] * axis
        return v / np.maximum(np.linalg.norm(v, axis=1), 1e-300)[:, None]

    cos = np.einsum("ij,ij->i", across(apex_a), across(apex_b))
    return cos >= np.cos(np.radians(FOLD_DEGREES))


def _inside_2d(p: np.ndarray, tri: np.ndarray) -> bool:
    """Is 2D point p strictly inside 2D triangle tri?"""
    def side(a, b, c) -> float:
        return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    s1, s2, s3 = side(tri[0], tri[1], p), side(tri[1], tri[2], p), side(tri[2], tri[0], p)
    eps = 1e-12 * max(1.0, float(np.abs(tri).max()) ** 2)
    return (s1 > eps and s2 > eps and s3 > eps) or (s1 < -eps and s2 < -eps and s3 < -eps)


def _segments_cross_2d(p1, p2, q1, q2) -> bool:
    def side(a, b, c) -> float:
        return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    eps = 1e-12 * max(1.0, float(np.abs(np.array([p1, p2, q1, q2])).max()) ** 2)
    d1, d2 = side(q1, q2, p1), side(q1, q2, p2)
    d3, d4 = side(p1, p2, q1), side(p1, p2, q2)
    return ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and \
           ((d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps))


def _coplanar_overlap(ta: np.ndarray, tb: np.ndarray, normal: np.ndarray) -> bool:
    """Two coplanar triangles (3x3 each) overlap in a region of positive area?"""
    axis = int(np.argmax(np.abs(normal)))
    keep = [k for k in range(3) if k != axis]
    # RELATIVE TO THE TRIANGLE, so the tolerances below follow its size and not where the part
    # sits (Greptile #495: the same overlap translated to x = 1e6 was missed).
    local = ta[0]
    a2, b2 = (ta - local)[:, keep], (tb - local)[:, keep]
    if _inside_2d(a2.mean(axis=0), b2) or _inside_2d(b2.mean(axis=0), a2):
        return True
    if any(_inside_2d(p, b2) for p in a2) or any(_inside_2d(p, a2) for p in b2):
        return True
    return any(_segments_cross_2d(a2[i], a2[(i + 1) % 3], b2[j], b2[(j + 1) % 3])
               for i in range(3) for j in range(3))


def clashing_surfaces(coords: np.ndarray, tris: np.ndarray, tags: np.ndarray, *,
                      groups: np.ndarray | None = None, max_pairs: int = 8,
                      deadline: float | None = None) -> tuple[Clash, ...]:
    """The surface pairs whose triangles cross or overlap, most hits first, at most `max_pairs`.

    `coords` is (N, 3); `tris` (M, 3) indexes rows of `coords`; `tags` (M,) is each triangle's
    gmsh surface. Triangles that share a node are never compared (a conformal seam), nor two of one
    surface. `groups` (M,), when given, is each triangle's volume component: only triangles of ONE
    component (>= 0) are compared, because gmsh tetrahedralises each connected group of volumes on
    its own and a crossing between two groups never reaches the same tetgen call.

    BOUNDED: at most MAX_CANDIDATE_PAIRS candidates are built, they are tested _CHUNK at a time,
    and a `deadline` (time.monotonic()) that passes ends the search with what it has found so far -
    so a crowded mesh can never cost the caller the gmsh failure it already holds (Greptile #495).
    Empty for an empty mesh or one larger than MAX_TRIANGLES. Pure; never raises on a well-formed
    input.
    """
    import time

    coords = np.asarray(coords, dtype=float)
    tris = np.asarray(tris, dtype=np.int64)
    tags = np.asarray(tags, dtype=np.int64)
    group = (np.zeros(len(tags), dtype=np.int64) if groups is None
             else np.asarray(groups, dtype=np.int64))
    if len(tris) < 2 or len(tris) > MAX_TRIANGLES:
        return ()
    if deadline is not None and time.monotonic() >= deadline:
        return ()
    corners = coords[tris]                       # (M, 3, 3)
    lo, hi = corners.min(axis=1), corners.max(axis=1)
    diagonal = float(np.linalg.norm(coords.max(axis=0) - coords.min(axis=0))) or 1.0
    pad = diagonal * _PLANE_FRACTION
    pairs = _candidate_pairs(lo - pad, hi + pad, tags=tags, groups=group)
    if not len(pairs):
        return ()
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    unit = normals / np.where(lengths > 0, lengths, 1.0)[:, None]

    found: dict[tuple[int, int], list] = {}

    def record(i: int, j: int, where: np.ndarray, kind: str) -> None:
        key = (int(min(tags[i], tags[j])), int(max(tags[i], tags[j])))
        entry = found.setdefault(key, [0, tuple(float(x) for x in where), kind])
        entry[0] += 1

    for start in range(0, len(pairs), _CHUNK):
        if deadline is not None and time.monotonic() >= deadline:
            break
        a, b = pairs[start:start + _CHUNK, 0], pairs[start:start + _CHUNK, 1]
        meets = ((lo[a] <= hi[b] + pad) & (lo[b] <= hi[a] + pad)).all(axis=1)
        a, b = a[meets], b[meets]
        same = tris[a][:, :, None] == tris[b][:, None, :]          # (K, 3, 3)
        shared = same.any(axis=2).sum(axis=1)
        for k in np.nonzero(shared == 3)[0]:
            record(int(a[k]), int(b[k]), corners[a[k]].mean(axis=0), DUPLICATE)
        edge = shared == 2
        if edge.any():
            fa, fb = a[edge], b[edge]
            for k in np.nonzero(_folded(coords, tris, fa, fb, same[edge]))[0]:
                record(int(fa[k]), int(fb[k]), corners[fa[k]].mean(axis=0), FOLD)
        a, b = a[shared == 0], b[shared == 0]
        if not len(a):
            continue
        cos = np.abs(np.einsum("ij,ij->i", unit[a], unit[b]))
        offset = np.abs(np.einsum("ij,ij->i", corners[a, 0] - corners[b, 0], unit[b]))
        coplanar = (cos >= _PARALLEL_COS) & (offset <= pad)
        ca, cb = a[~coplanar], b[~coplanar]
        hit_ab, point_ab = _edge_hits(coords, tris, ca, cb)
        hit_ba, point_ba = _edge_hits(coords, tris, cb, ca)
        for k in np.nonzero(hit_ab | hit_ba)[0]:
            record(int(ca[k]), int(cb[k]), point_ab[k] if hit_ab[k] else point_ba[k], CROSSING)
        for i, j in zip(a[coplanar], b[coplanar], strict=True):
            if _coplanar_overlap(corners[i], corners[j], unit[i]):
                record(int(i), int(j), corners[i].mean(axis=0), COPLANAR)

    ranked = sorted(found.items(), key=lambda item: (-item[1][0], item[0]))[:max(0, max_pairs)]
    return tuple(Clash(surfaces=key, hits=int(v[0]),
                       point=(v[1][0], v[1][1], v[1][2]), kind=str(v[2]))
                 for key, v in ranked)


def surface_components(owners: dict[int, list[int]]) -> dict[int, int]:
    """Each surface's volume component: volumes sharing a surface are one component, as gmsh
    groups them for the 3D stage. A surface no volume owns is -1 (never tetrahedralised). Pure."""
    parent: dict[int, int] = {}

    def find(v: int) -> int:
        parent.setdefault(v, v)
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    for volumes in owners.values():
        for v in volumes[1:]:
            ra, rb = find(int(volumes[0])), find(int(v))
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        for v in volumes[:1]:
            find(int(v))
    return {int(s): (find(int(v[0])) if v else -1) for s, v in owners.items()}
