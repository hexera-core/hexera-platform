# Responsibility: A SURFACE FILE'S GAPS CLOSED IN THE MESH (SURF-13), the last resort after SURF-12: a place where the
# surfaces still do not meet (cad/surface_model.open_places, named by its two faces) and whose gap is narrower than
# the reach (the element size the user asked for, else the run's own, or the notes' "close gaps up to 4 mm") is
# closed in the shell mesh the way an analyst equivalences two free edges: the boundary nodes facing each other
# across the gap are joined into one node half way between them, and a node with no partner across is put into the
# edge facing it (its element cut in two or three). An edge stopping short of another face's inside (a T) is joined
# onto that face's mesh: onto a row of its nodes along the line, or, where it has none, onto the line IMPRINTED on it
# (its elements cut along the line). No node moves farther than the gap; the place is kept closed only
# when no element is folded or turned and none is left past Nastran's own limits that was not before - else the mesh
# there is as meshed and the place is told open, with why.
# Boundaries: pure numpy on the engine-neutral ShellMesh (engines/shell_mesh); no geometry, no engine, no file.
# Deterministic: the same mesh and places give the same mesh.
# Collaborates with: engines/surface_run.py (calls close() after the shell mesh is made), cad/surface_model.py
# (the open places), engines/shell_quality.py (the limits), engines/shell_mesh.py (edges, normals).
from __future__ import annotations

import math
from typing import Any

import numpy as np

from meshpipeline.engines.shell_mesh import ShellMesh

RULE = "SURF-13"
#: two nodes facing each other across the gap are one node when they lie nearer along the gap than SNAP_EDGE of the
#: shorter element edge beside them; farther apart, each is put into the other side's edge instead (no sliver)
SNAP_EDGE = 0.45
#: ... and nearer along the gap than SNAP_GAP gaps, so that the node half way between moves at most the gap
SNAP_GAP = math.sqrt(3.0)
#: a node is on the gap when it lies within REACH_GAPS gaps of the other side's free edge (the gap is measured at
#: the middle of the place's longest edge; a gap opening towards one end reads up to twice it)
REACH_GAPS = 2.0
#: an element changed by the closing keeps its normal within this many degrees of its own before
TURN_DEG = 60.0
#: a free edge is a side of the gap when it runs within ALONG_DEG of the other side's nearest edge and the way across
#: to it is at least FACING_DEG off its own direction
ALONG_DEG = 45.0
FACING_DEG = 55.0
#: a node is put into an edge only when it faces the edge's inside (this far from either end, of the edge's length):
#: one facing a corner of the other side's outline is beyond that side's run
INSIDE_EDGE = 0.1
#: nodes the joining brings within this many gaps of each other are one node
COINCIDE_GAPS = 0.05
#: ... and a node within this share of their distance of the straight run between two nodes joined
ON_RUN = 0.1


def _mid_of(face: str) -> str:
    """The mesh's mid-face name of a CAD face ("Face_12" -> "Mid_12"; cad/surface_model.record)."""
    return f"Mid_{str(face).rsplit('_', 1)[-1]}"


def _r(v: float) -> float:
    return float(f"{float(v):.4g}")


def _seg_nearest(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For points p (n, 3) and segments a-b (m, 3): the distance to the nearest segment, its index, and the
    parameter (0..1) along it."""
    ab = b - a
    ll = np.maximum(np.einsum("mi,mi->m", ab, ab), 1e-300)
    t = np.clip(np.einsum("nmi,mi->nm", p[:, None, :] - a[None, :, :], ab) / ll[None, :], 0.0, 1.0)
    q = a[None, :, :] + t[:, :, None] * ab[None, :, :]
    d = np.linalg.norm(p[:, None, :] - q, axis=2)
    k = np.argmin(d, axis=1)
    rows = np.arange(len(p))
    return d[rows, k], k, t[rows, k]


def _paths(edges: list[tuple[int, int]]) -> list[list[int]]:
    """The edges joined end to end into paths (node lists); a node met by more than two edges ends its paths."""
    nbr: dict[int, list[int]] = {}
    for a, b in edges:
        nbr.setdefault(a, []).append(b)
        nbr.setdefault(b, []).append(a)
    used: set[tuple[int, int]] = set()
    out: list[list[int]] = []

    def walk(start: int, nxt: int) -> list[int]:
        path = [start]
        prev, cur = start, nxt
        while True:
            used.add((min(prev, cur), max(prev, cur)))
            path.append(cur)
            if len(nbr[cur]) != 2:
                return path
            follow = [x for x in nbr[cur] if x != prev and (min(cur, x), max(cur, x)) not in used]
            if not follow:
                return path
            prev, cur = cur, follow[0]

    ends = sorted(n for n, v in nbr.items() if len(v) != 2)
    for s in ends + sorted(nbr):
        for x in sorted(nbr[s]):
            if (min(s, x), max(s, x)) not in used:
                out.append(walk(s, x))
    return out


def _chain_params(chain_xyz: np.ndarray, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Points measured along a polyline: (distance to it, arc length of the nearest point, that point, whether it
    falls inside a segment - not on a corner of the polyline, where a point beyond the corner lands too)."""
    a, b = chain_xyz[:-1], chain_xyz[1:]
    d, k, t = _seg_nearest(pts, a, b)
    seg = np.linalg.norm(b - a, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    s = cum[k] + t * seg[k]
    q = a[k] + t[:, None] * (b[k] - a[k])
    return d, s, q, (t > INSIDE_EDGE) & (t < 1.0 - INSIDE_EDGE)


def _normal(p: np.ndarray) -> np.ndarray:
    if len(p) == 4:
        n = np.cross(p[2] - p[0], p[3] - p[1])
    else:
        n = np.cross(p[1] - p[0], p[2] - p[0])
    return n / max(float(np.linalg.norm(n)), 1e-300)


def _pieces(poly: list[int], xyz: np.ndarray) -> list[list[int]]:
    """A polygon (an element with nodes put into its edges, in its own turning sense) cut into a quad and triangles
    or into triangles, the cut whose worst element is best: inside Nastran's limits first, then the largest least
    angle; ties by a fixed order."""
    from meshpipeline.engines import shell_quality as SQ

    n = len(poly)
    if n <= 3:
        return [poly]
    rot = [poly[k:] + poly[:k] for k in range(n)]
    options: list[list[list[int]]] = [[poly]] if n == 4 else []
    for c in rot:
        options.append([c[0:3]] + [[c[0], c[j], c[j + 1]] for j in range(2, n - 1)])      # a fan from a corner
        if n == 5:
            options.append([c[:4], [c[3], c[4], c[0]]])                                  # a quad and a triangle
        if n == 6:
            options.append([c[:4], [c[3], c[4], c[5], c[0]]])                            # two quads
    best, score = None, None
    for opt in options:
        ok = all(len(set(e)) == len(e) for e in opt)
        if not ok:
            continue
        areas = [float(np.linalg.norm(np.cross(xyz[e[1]] - xyz[e[0]], xyz[e[-1]] - xyz[e[0]]))) for e in opt]
        if min(areas) <= 1e-12:
            continue
        past = sum(int(SQ._past_one(xyz, e)) for e in opt)
        least = min(SQ._least_angle(xyz, e) for e in opt)
        key = (past, -least, len(opt))
        if score is None or key < score:
            best, score = opt, key
    return best if best is not None else [poly]


class _Mesh:
    """A ShellMesh taken apart into editable lists: every element with the attributes it carries."""

    def __init__(self, mesh: ShellMesh) -> None:
        self.src = mesh
        self.xyz = mesh.xyz.astype(float).copy()
        self.conn: list[list[int]] = [list(map(int, c)) for c in mesh.elements()]
        self.parent: list[int] = list(range(mesh.n_elements))

    def users(self) -> dict[tuple[int, int], list[int]]:
        out: dict[tuple[int, int], list[int]] = {}
        for e, c in enumerate(self.conn):
            for i in range(len(c)):
                a, b = c[i], c[(i + 1) % len(c)]
                out.setdefault((a, b) if a < b else (b, a), []).append(e)
        return out

    def copy(self) -> _Mesh:
        other = _Mesh.__new__(_Mesh)
        other.src = self.src
        other.xyz = self.xyz.copy()
        other.conn = [list(c) for c in self.conn]
        other.parent = list(self.parent)
        return other

    def _node_wall(self) -> np.ndarray:
        """The source's node walls, a node an imprint added taking its element's wall."""
        base = np.asarray(self.src.node_wall, dtype=float)
        out = np.empty(len(self.xyz), dtype=float)
        out[:len(base)] = base
        walls = np.asarray(self.src.wall, dtype=float)
        for c, p in zip(self.conn, self.parent, strict=True):
            for n in c:
                if n >= len(base):
                    out[n] = walls[p]
        return out

    def build(self) -> ShellMesh:
        """Back to a ShellMesh: quads first, then triangles; unused nodes dropped; every attribute the parent's."""
        m = self.src
        quads = [(c, p) for c, p in zip(self.conn, self.parent, strict=True) if len(c) == 4]
        trias = [(c, p) for c, p in zip(self.conn, self.parent, strict=True) if len(c) == 3]
        order = quads + trias
        used = np.unique(np.array([n for c, _p in order for n in c], dtype=np.int64))
        remap = -np.ones(len(self.xyz), dtype=np.int64)
        remap[used] = np.arange(len(used))
        par = np.array([p for _c, p in order], dtype=np.int64)
        q = np.array([[remap[n] for n in c] for c, _p in quads], dtype=np.int64).reshape(-1, 4)
        t = np.array([[remap[n] for n in c] for c, _p in trias], dtype=np.int64).reshape(-1, 3)
        new_of: dict[int, list[int]] = {}
        for i, p in enumerate(par.tolist()):
            new_of.setdefault(p, []).append(i)
        groups = {}
        for name, idx in m.groups.items():
            got = sorted(i for old in np.asarray(idx).tolist() for i in new_of.get(int(old), []))
            groups[name] = np.array(got, dtype=np.int64)
        out = ShellMesh(xyz=self.xyz[used], quads=q, trias=t, wall=np.asarray(m.wall)[par],
                        mid=[m.mid[p] for p in par.tolist()], part=[m.part[p] for p in par.tolist()], groups=groups,
                        node_wall=None if m.node_wall is None else self._node_wall()[used],
                        varies=None if m.varies is None else np.asarray(m.varies)[par],
                        zoffs=None if m.zoffs is None else np.asarray(m.zoffs)[par],
                        notes=list(m.notes), part_names=dict(m.part_names))
        return out


def _side_free(mm: _Mesh, users: dict, mids: set[str]) -> list[tuple[int, int]]:
    mid = mm.src.mid
    return [k for k, us in users.items() if len(us) == 1 and mid[mm.parent[us[0]]] in mids]


def _near(mm: _Mesh, edges: list[tuple[int, int]], other: list[tuple[int, int]], lim: float) -> list[tuple[int, int]]:
    """The edges of `edges` that face `other` across the gap: both ends within `lim` of a segment of `other`, running
    alongside the nearest one (within ALONG_DEG) and looking across at it (the way to it at least FACING_DEG off the
    edge's own direction) - a face's short end, or two outlines running on in one line, are no gap's sides."""
    if not edges or not other:
        return []
    nodes = sorted({n for e in edges for n in e})
    a = mm.xyz[[e[0] for e in other]]
    b = mm.xyz[[e[1] for e in other]]
    d, _k, _t = _seg_nearest(mm.xyz[nodes], a, b)
    ok = {n for n, x in zip(nodes, d.tolist(), strict=True) if x <= lim}
    cand = [e for e in edges if e[0] in ok and e[1] in ok]
    if not cand:
        return []
    p0, p1 = mm.xyz[[e[0] for e in cand]], mm.xyz[[e[1] for e in cand]]
    mid = 0.5 * (p0 + p1)
    dm, km, tm = _seg_nearest(mid, a, b)
    own = p1 - p0
    own /= np.maximum(np.linalg.norm(own, axis=1, keepdims=True), 1e-300)
    theirs = b[km] - a[km]
    theirs /= np.maximum(np.linalg.norm(theirs, axis=1, keepdims=True), 1e-300)
    across = (a[km] + tm[:, None] * (b[km] - a[km])) - mid
    al = np.abs(np.einsum("mi,mi->m", own, theirs))
    look = np.abs(np.einsum("mi,mi->m", own, across)) / np.maximum(np.linalg.norm(across, axis=1), 1e-300)
    keep = (al >= math.cos(math.radians(ALONG_DEG))) & ((look <= math.cos(math.radians(FACING_DEG))) | (dm <= 1e-9))
    return [e for e, k in zip(cand, keep.tolist(), strict=True) if k]


def _join_pair(mm: _Mesh, mids_a: set[str], mids_b: set[str], gap: float, reach: float,
               onto_b: bool = False) -> dict[str, Any]:
    """One side of a place closed in `mm` (changed in place): face A's free edges facing face B's across `gap` (met
    half way; with `onto_b`, B's kept where they are). A free edge stopping short of the other face's inside (a T) is
    brought onto that face (_t_line)."""
    users = mm.users()
    lim = REACH_GAPS * gap + 1e-9
    fa, fb = _side_free(mm, users, mids_a), _side_free(mm, users, mids_b)
    ea, eb = _near(mm, fa, fb, lim), _near(mm, fb, fa, lim)
    out: dict[str, Any] = {"joined": 0, "put_in": 0, "moved_mm": 0.0, "open_mm": 0.0}
    remap: dict[int, int] = {}
    newpos: dict[int, np.ndarray] = {}
    inserts: dict[tuple[int, int], list[tuple[float, int]]] = {}
    taken: set[int] = set()
    orig = mm.xyz.copy()
    tee = not ea or not eb
    short: list[tuple[int, int]] = []
    other: set[str] = set()
    if tee:
        # A T: one side's free edge stops short of the other's INSIDE (a wall's foot short of a panel) - the panel's
        # mesh has no free edge there. Its nodes along the foot's line are moved onto that line, and the foot's
        # nodes onto the panel, then joined as two free edges are
        sa_ = _near_surface(mm, fa, mids_b, lim)
        short, other = (sa_, mids_b) if sa_ else (_near_surface(mm, fb, mids_a, lim), mids_a)
        tee = bool(short)
    if tee:
        runs_b = _t_line(mm, short, other, gap, lim, out)
        if not runs_b:
            out["why"] = out.get("why") or "the mesh has no free edges facing each other there"
            return out
        out["open_mm"] = _r(sum(float(np.linalg.norm(mm.xyz[a] - mm.xyz[b])) for a, b in short))
        # the panel stays where it is: the foot comes to it
        _pair_runs(mm, _paths(short), runs_b, gap, lim, set(), taken, remap, newpos, inserts, out, onto_b=True)
    elif not ea or not eb:
        out["why"] = "the mesh has no free edges facing each other there"
        return out
    else:
        out["open_mm"] = _r(sum(float(np.linalg.norm(mm.xyz[a] - mm.xyz[b])) for a, b in ea + eb))
        # a node on both sides is already one node (the two faces sewn there): it stays where it is and is no partner
        # of any other; and a node joined or put in once is not joined again from another run of the gap
        shared = {n for e in ea for n in e} & {n for e in eb for n in e}
        _pair_runs(mm, _paths(ea), _paths(eb), gap, lim, shared, taken, remap, newpos, inserts, out, onto_b=onto_b)
    got = _apply(mm, remap, newpos, inserts, out, mids_a, mids_b, gap, lim)
    if tee:
        # every node's move, from where it was meshed (the line's nodes and the foot's moved before the joining)
        got["moved_mm"] = _r(float(np.max(np.linalg.norm(mm.xyz[:len(orig)] - orig, axis=1))) if len(orig) else 0.0)
    return got


def _on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> tuple[float, np.ndarray]:
    """The nearest point to p on triangles a-b-c (m, 3): (its distance, the point)."""
    n = np.cross(b - a, c - a)
    nn = np.maximum(np.einsum("mi,mi->m", n, n), 1e-300)
    t = np.einsum("mi,mi->m", p[None, :] - a, n) / nn
    q = p[None, :] - t[:, None] * n
    # inside: the point's barycentric weights all >= 0
    w0 = np.einsum("mi,mi->m", np.cross(b - q, c - q), n)
    w1 = np.einsum("mi,mi->m", np.cross(c - q, a - q), n)
    w2 = np.einsum("mi,mi->m", np.cross(a - q, b - q), n)
    inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
    d = np.where(inside, np.linalg.norm(q - p[None, :], axis=1), np.inf)
    best_d, best_q = float(np.min(d)) if len(d) else np.inf, q[int(np.argmin(d))] if len(d) else p
    for s, e in ((a, b), (b, c), (c, a)):
        ds, k, ts = _seg_nearest(p[None, :], s, e)
        if float(ds[0]) < best_d:
            best_d, best_q = float(ds[0]), s[int(k[0])] + float(ts[0]) * (e[int(k[0])] - s[int(k[0])])
    return best_d, np.asarray(best_q, dtype=float)


def _triangles(mm: _Mesh, mids: set[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The elements of the faces `mids` as triangles (a quad as two): their corners a, b, c (m, 3)."""
    mid = mm.src.mid
    elems = [c for e, c in enumerate(mm.conn) if mid[mm.parent[e]] in mids]
    tris = [(c[0], c[1], c[2]) for c in elems] + [(c[0], c[2], c[3]) for c in elems if len(c) == 4]
    if not tris:
        z = np.zeros((0, 3))
        return z, z, z
    return mm.xyz[[t[0] for t in tris]], mm.xyz[[t[1] for t in tris]], mm.xyz[[t[2] for t in tris]]


def _near_surface(mm: _Mesh, edges: list[tuple[int, int]], other: set[str], lim: float) -> list[tuple[int, int]]:
    """SURF-13, A T: the free edges of `edges` lying along the faces `other`'s mesh: both ends within `lim` of it and
    running along it (within ALONG_DEG of the nearest element's own plane)."""
    ta, tb, tc = _triangles(mm, other)
    if not len(ta) or not edges:
        return []
    lo_t = np.minimum(np.minimum(ta, tb), tc)
    hi_t = np.maximum(np.maximum(ta, tb), tc)
    near_of: dict[int, tuple[float, np.ndarray] | None] = {}
    for n in sorted({n for e in edges for n in e}):
        p = mm.xyz[n]
        k = np.where(np.all((lo_t <= p + lim) & (hi_t >= p - lim), axis=1))[0]
        if not len(k):
            near_of[n] = None
            continue
        d, q = _on_triangles(p, ta[k], tb[k], tc[k])
        near_of[n] = (d, q) if d <= lim else None
    out = []
    for a, b in edges:
        if near_of[a] is None or near_of[b] is None:
            continue
        own = mm.xyz[b] - mm.xyz[a]
        m = 0.5 * (mm.xyz[a] + mm.xyz[b])
        k = np.where(np.all((lo_t <= m + lim) & (hi_t >= m - lim), axis=1))[0]
        if not len(k):
            continue
        # the plane of the element nearest the edge's middle
        cen = (ta[k] + tb[k] + tc[k]) / 3.0
        j = k[int(np.argmin(np.linalg.norm(cen - m, axis=1)))]
        nv = np.cross(tb[j] - ta[j], tc[j] - ta[j])
        sin = abs(float(np.dot(own, nv))) / max(float(np.linalg.norm(own) * np.linalg.norm(nv)), 1e-300)
        if sin <= math.sin(math.radians(ALONG_DEG)):
            out.append((a, b))
    return out


def _t_line(mm: _Mesh, short: list[tuple[int, int]], other: set[str], gap: float, lim: float,
            out: dict[str, Any]) -> list[list[int]]:
    """SURF-13, A T: the free edges `short` (a face stopping short of another's inside) brought onto the faces
    `other`: each node of theirs moved to the nearest point of the other faces' mesh (within `lim`), and along the line
    they then draw, the other mesh's own nodes within the gap of it moved onto it - a row of that mesh's edges along
    the line, which the short side's nodes are then joined to. Where the other mesh has no such row (its nodes farther
    from the line than the gap: small gaps on large elements), the line is IMPRINTED on it (_imprint: its elements cut
    along the line, as a pre-processor imprints an edge on a mesh) and the row is the new nodes. Changes mm in place;
    returns the rows (node lists), [] when there is none (why in `out`)."""
    mid = mm.src.mid

    def graph():
        elems = [e for e, c in enumerate(mm.conn) if mid[mm.parent[e]] in other]
        tris = [(c[0], c[1], c[2]) for e in elems for c in [mm.conn[e]]] + \
            [(c[0], c[2], c[3]) for e in elems for c in [mm.conn[e]] if len(c) == 4]
        nbr: dict[int, dict[int, float]] = {}
        for e in elems:
            c = mm.conn[e]
            for i in range(len(c)):
                p0, p1 = c[i], c[(i + 1) % len(c)]
                w = float(np.linalg.norm(mm.xyz[p0] - mm.xyz[p1]))
                nbr.setdefault(p0, {})[p1] = w
                nbr.setdefault(p1, {})[p0] = w
        return elems, tris, nbr

    elems, tris, nbr = graph()
    if not elems:
        return []
    ta = mm.xyz[[t[0] for t in tris]]
    tb = mm.xyz[[t[1] for t in tris]]
    tc = mm.xyz[[t[2] for t in tris]]
    rows: list[list[int]] = []
    moves: dict[int, np.ndarray] = {}
    for run in _paths(short):
        foot: list[np.ndarray | None] = []
        for n in run:
            lo, hi = mm.xyz[n] - lim, mm.xyz[n] + lim
            near = np.where(np.all((np.minimum(np.minimum(ta, tb), tc) <= hi) &
                                   (np.maximum(np.maximum(ta, tb), tc) >= lo), axis=1))[0]
            if not len(near):
                foot.append(None)
                continue
            d, q = _on_triangles(mm.xyz[n], ta[near], tb[near], tc[near])
            foot.append(q if d <= lim else None)
        # the longest stretch of the run with every node within reach
        best: tuple[int, int] = (0, 0)
        i = 0
        while i < len(run):
            if foot[i] is None:
                i += 1
                continue
            j = i
            while j + 1 < len(run) and foot[j + 1] is not None:
                j += 1
            if j - i > best[1] - best[0]:
                best = (i, j + 1)
            i = j + 1
        if best[1] - best[0] < 2:
            continue
        nodes = run[best[0]:best[1]]
        line = np.array([foot[k] for k in range(best[0], best[1])], dtype=float)
        path = _row_near(mm, nbr, line, gap)
        if path is None:
            # no row of the other mesh's nodes along the line: the line imprinted on it
            path = _imprint(mm, other, line, gap)
            if path is None:
                continue
            elems, tris, nbr = graph()
            ta = mm.xyz[[t[0] for t in tris]]
            tb = mm.xyz[[t[1] for t in tris]]
            tc = mm.xyz[[t[2] for t in tris]]
            out["imprinted"] = int(out.get("imprinted") or 0) + len(path)
        else:
            _d, _k, _t = _seg_nearest(mm.xyz[path], line[:-1], line[1:])
            for n, k, t in zip(path, _k.tolist(), _t.tolist(), strict=True):
                moves[n] = line[k] + t * (line[k + 1] - line[k])
        for n, q in zip(nodes, line, strict=True):
            moves[n] = q
        rows.append(path)
    if not rows:
        out["why"] = "the mesh has no row of nodes along the line the edge should meet"
        return []
    # nothing turned over by the moves
    touched = [e for e, c in enumerate(mm.conn) if any(n in moves for n in c)]
    before = {e: _normal(mm.xyz[mm.conn[e]]) for e in touched}
    for n, q in moves.items():
        mm.xyz[n] = q
    for e in touched:
        if float(np.dot(_normal(mm.xyz[mm.conn[e]]), before[e])) < math.cos(math.radians(TURN_DEG)):
            out["why"] = "joining the mesh there would turn an element over"
    return rows


def _row_near(mm: _Mesh, nbr: dict[int, dict[int, float]], line: np.ndarray, gap: float) -> list[int] | None:
    """The other mesh's row of nodes along `line`: the shortest way through its nodes within the gap of the line,
    from the node nearest one end to the node nearest the other, kept close to the line; None when there is none."""
    import heapq

    other_nodes = np.array(sorted(nbr), dtype=np.int64)
    dn, _k, _t = _seg_nearest(mm.xyz[other_nodes], line[:-1], line[1:])
    ok = {int(n) for n, x in zip(other_nodes.tolist(), dn.tolist(), strict=True) if x <= gap}
    if not ok:
        return None
    ends: list[int] = []
    for end in (line[0], line[-1]):
        far = {m: float(np.linalg.norm(mm.xyz[m] - end)) for m in ok}
        ends.append(min(sorted(ok), key=far.__getitem__))
    if ends[0] == ends[1]:
        return None
    dist_of = {int(n): float(x) for n, x in zip(other_nodes.tolist(), dn.tolist(), strict=True)}
    best_d: dict[int, float] = {ends[0]: 0.0}
    prev: dict[int, int] = {}
    heap = [(0.0, ends[0])]
    while heap:
        dd, u = heapq.heappop(heap)
        if u == ends[1]:
            break
        if dd > best_d.get(u, np.inf):
            continue
        for v, w in sorted(nbr[u].items()):
            if v not in ok:
                continue
            nd = dd + w + 3.0 * dist_of[v]
            if nd < best_d.get(v, np.inf) - 1e-12:
                best_d[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    if ends[1] not in prev:
        return None
    path = [ends[1]]
    while path[-1] != ends[0]:
        path.append(prev[path[-1]])
    path.reverse()
    return path


#: SURF-13, an imprint: where the line crosses an element edge within this share of the edge's length of one of its
#: nodes - and within half the gap of it - that node is moved onto the line instead of a new node put beside it
IMPRINT_SNAP = 0.15


def _imprint(mm: _Mesh, other: set[str], line: np.ndarray, gap: float) -> list[int] | None:
    """SURF-13: THE LINE IMPRINTED ON A MESH: every element of the faces `other` the polyline `line` (on their mesh)
    crosses is cut along it - a new node where the line crosses each element edge (an edge node within IMPRINT_SNAP
    of it, and half the gap, moved onto the line instead), the element cut in two along the line between them and
    each part made a quad or triangles (_pieces) - and the elements beside take the new nodes into their edges. The
    other mesh then has a row of nodes along the line. Changes mm in place; returns the row (node list, in the line's
    order) or None when the line crosses no element edge (nothing changed)."""
    mid = mm.src.mid
    elems = [e for e, c in enumerate(mm.conn) if mid[mm.parent[e]] in other]
    lo_l, hi_l = line.min(axis=0) - gap, line.max(axis=0) + gap
    seg_len = np.linalg.norm(np.diff(line, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    hits: dict[tuple[int, int], tuple[float, float, np.ndarray]] = {}       # edge -> (s along it, s along the line, x)
    for e in elems:
        c = mm.conn[e]
        p = mm.xyz[c]
        if np.any(p.min(axis=0) > hi_l) or np.any(p.max(axis=0) < lo_l):
            continue
        n = _normal(p)
        e1 = p[1] - p[0]
        e1 /= max(float(np.linalg.norm(e1)), 1e-300)
        e2 = np.cross(n, e1)

        def flat(x: np.ndarray, p0=p[0], e1=e1, e2=e2) -> np.ndarray:
            return np.stack([(x - p0) @ e1, (x - p0) @ e2], axis=-1)

        lf = flat(line)
        for i in range(len(c)):
            u, v = c[i], c[(i + 1) % len(c)]
            key = (min(u, v), max(u, v))
            if key in hits:
                continue
            a, b = mm.xyz[key[0]], mm.xyz[key[1]]
            fa, fb = flat(a[None, :])[0], flat(b[None, :])[0]
            d = fb - fa
            for j in range(len(line) - 1):
                g = lf[j + 1] - lf[j]
                den = d[0] * g[1] - d[1] * g[0]
                if abs(den) <= 1e-14 * max(1.0, float(np.dot(d, d) + np.dot(g, g))):
                    continue
                w = lf[j] - fa
                s = (w[0] * g[1] - w[1] * g[0]) / den
                t = (w[0] * d[1] - w[1] * d[0]) / den
                if not (0.0 <= s <= 1.0 and 0.0 <= t <= 1.0):
                    continue
                x = line[j] + t * (line[j + 1] - line[j])
                # the line lies on the mesh: the crossing in 3D, not a projection of a far stretch
                if float(np.linalg.norm(x - (a + s * (b - a)))) > max(0.1 * float(np.linalg.norm(b - a)), 1e-9):
                    continue
                hits[key] = (float(s), float(cum[j] + t * seg_len[j]), x)
                break
    if not hits:
        return None
    # each crossing: an end node moved onto the line, or a new node in the edge
    at_line: dict[int, float] = {}                 # node -> its place along the line
    inserts: dict[tuple[int, int], int] = {}
    for key in sorted(hits):
        s, sl, x = hits[key]
        length = float(np.linalg.norm(mm.xyz[key[1]] - mm.xyz[key[0]]))
        end = key[0] if s <= 0.5 else key[1]
        near = min(s, 1.0 - s) * length
        if min(s, 1.0 - s) <= IMPRINT_SNAP and near <= 0.5 * gap:
            if end not in at_line:
                mm.xyz[end] = x
                at_line[end] = sl
            continue
        nid = len(mm.xyz)
        mm.xyz = np.vstack([mm.xyz, x[None, :]])
        inserts[key] = nid
        at_line[nid] = sl
    # the elements: new nodes into their edges, those the line crosses cut in two along it
    new_conn: list[list[int]] = []
    new_parent: list[int] = []
    for e, c in enumerate(mm.conn):
        keys = [(min(c[i], c[(i + 1) % len(c)]), max(c[i], c[(i + 1) % len(c)])) for i in range(len(c))]
        if mid[mm.parent[e]] not in other or not (any(k in inserts for k in keys) or
                                                   sum(1 for n in c if n in at_line) >= 2):
            new_conn.append(c)
            new_parent.append(mm.parent[e])
            continue
        poly: list[int] = []
        for i in range(len(c)):
            poly.append(c[i])
            if keys[i] in inserts:
                poly.append(inserts[keys[i]])
        on = [i for i, n in enumerate(poly) if n in at_line]
        parts = [poly]
        if len(on) == 2 and (on[1] - on[0]) % len(poly) not in (1, len(poly) - 1):
            i0, i1 = on
            parts = [poly[i0:i1 + 1], poly[i1:] + poly[:i0 + 1]]
        for part in parts:
            for piece in _pieces(part, mm.xyz):
                new_conn.append(piece)
                new_parent.append(mm.parent[e])
    mm.conn, mm.parent = new_conn, new_parent
    row = sorted(at_line, key=lambda n: (at_line[n], n))
    # the row as a run of edges: consecutive nodes the cut joined; the longest stretch that is
    users = mm.users()
    best: tuple[int, int] = (0, 1)
    i = 0
    while i < len(row) - 1:
        j = i
        while j + 1 < len(row) and (min(row[j], row[j + 1]), max(row[j], row[j + 1])) in users:
            j += 1
        if j - i > best[1] - 1 - best[0]:
            best = (i, j + 1)
        i = j + 1 if j > i else i + 1
    got = row[best[0]:best[1]]
    return got if len(got) >= 2 else None


def _pair_runs(mm: _Mesh, runs_a: list[list[int]], runs_b: list[list[int]], gap: float, lim: float,
               shared: set[int], taken: set[int], remap: dict[int, int], newpos: dict[int, np.ndarray],
               inserts: dict[tuple[int, int], list[tuple[float, int]]], out: dict[str, Any],
               onto_b: bool = False) -> None:
    """The runs of nodes of side A paired with those of side B along the gap: nodes facing each other joined (half
    way; at B's node with `onto_b`), a node with no partner put into the other side's edge it faces. Fills `remap`,
    `newpos`, `inserts`, `out`."""
    for cb in runs_b:
        bx = mm.xyz[cb]
        if len(cb) < 2:
            continue
        sb = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(bx, axis=0), axis=1))])
        for ca in runs_a:
            d, sa, qa, ina = _chain_params(bx, mm.xyz[ca])
            if float(np.median(d)) > lim:
                continue
            if len(ca) > 1 and not (np.all(np.diff(sa) >= -1e-9) or np.all(np.diff(sa) <= 1e-9)):
                continue                                     # folds back along the gap: not one row of nodes
            if len(ca) > 1 and sa[-1] < sa[0]:
                ca, d, sa, qa, ina = ca[::-1], d[::-1], sa[::-1], qa[::-1], ina[::-1]
            # the local element edge at each node (the shorter of its two chain edges)
            def local(xs: np.ndarray) -> np.ndarray:
                seg = np.linalg.norm(np.diff(xs, axis=0), axis=1)
                if not len(seg):
                    return np.array([np.inf])
                left = np.concatenate([[np.inf], seg])
                right = np.concatenate([seg, [np.inf]])
                return np.minimum(left, right)
            la, lb = local(mm.xyz[ca]), local(bx)
            # the pairs: nearest along the gap first, never crossing
            cand = []
            for i, a in enumerate(ca):
                for j, b in enumerate(cb):
                    if a == b:
                        cand.append((-1.0, i, j))
                        continue
                    if a in shared or b in shared or a in taken or b in taken:
                        continue
                    ds = abs(float(sa[i]) - float(sb[j]))
                    snap = min(SNAP_GAP * gap, SNAP_EDGE * min(float(la[i]), float(lb[j])))
                    if ds <= snap and float(np.linalg.norm(mm.xyz[a] - mm.xyz[b])) <= lim:
                        cand.append((ds, i, j))
            cand.sort()
            pairs: list[tuple[int, int]] = []
            for _ds, i, j in cand:
                if any(i == p or j == q or (i < p) != (j < q) for p, q in pairs):
                    continue
                pairs.append((i, j))
            pairs.sort()
            paired_a = {i for i, _j in pairs}
            paired_b = {j for _i, j in pairs}
            for i, j in pairs:
                a, b = ca[i], cb[j]
                if a == b:
                    continue
                m = mm.xyz[b].copy() if onto_b else 0.5 * (mm.xyz[a] + mm.xyz[b])
                remap[a] = b
                newpos[b] = m
                taken |= {a, b}
                out["joined"] += 1
                out["moved_mm"] = max(out["moved_mm"], 0.5 * float(np.linalg.norm(mm.xyz[a] - mm.xyz[b])))
            # a node with no partner goes into the other side's edge it faces (inside the other side's run only)
            for i, a in enumerate(ca):
                if i in paired_a or a in shared or a in taken or not (sb[0] < sa[i] < sb[-1]) or float(d[i]) > lim \
                        or not bool(ina[i]):
                    continue
                j = int(np.searchsorted(sb, sa[i]) - 1)
                key = (min(cb[j], cb[j + 1]), max(cb[j], cb[j + 1]))
                taken.add(a)
                newpos[a] = qa[i].copy() if onto_b else 0.5 * (mm.xyz[a] + qa[i])
                inserts.setdefault(key, []).append((float(sa[i]), a))
                out["put_in"] += 1
                out["moved_mm"] = max(out["moved_mm"], 0.5 * float(d[i]))
            if len(ca) > 1:
                db, sab, qb, inb = _chain_params(mm.xyz[ca], bx)
                ca_s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(mm.xyz[ca], axis=0), axis=1))])
                for j, b in enumerate(cb):
                    if j in paired_b or b in shared or b in taken or not (ca_s[0] < sab[j] < ca_s[-1]) \
                            or float(db[j]) > lim or not bool(inb[j]):
                        continue
                    i = int(np.searchsorted(ca_s, sab[j]) - 1)
                    key = (min(ca[i], ca[i + 1]), max(ca[i], ca[i + 1]))
                    taken.add(b)
                    newpos[b] = mm.xyz[b].copy() if onto_b else 0.5 * (mm.xyz[b] + qb[j])
                    inserts.setdefault(key, []).append((float(sab[j]), b))
                    out["put_in"] += 1
                    out["moved_mm"] = max(out["moved_mm"], 0.5 * float(db[j]))


def _apply(mm: _Mesh, remap: dict[int, int], newpos: dict[int, np.ndarray],
           inserts: dict[tuple[int, int], list[tuple[float, int]]], out: dict[str, Any], mids_a: set[str],
           mids_b: set[str], gap: float, lim: float) -> dict[str, Any]:
    """The joins and put-ins applied to `mm` (in place): nodes moved, joined nodes made one, elements cut where nodes
    were put into their edges; `out` filled with what was done and what is left."""
    if not remap and not inserts:
        out["why"] = "no nodes face each other across the gap"
        return out
    # a node lying on the straight run between two nodes that are joined (a third face's short edge across the end of
    # the gap, the stub between two corners) is joined with them
    near_of: dict[int, set[int]] = {}
    for c in mm.conn:
        if any(n in remap for n in c):
            for n in c:
                near_of.setdefault(n, set()).update(c)
    for a, b in sorted(remap.items()):
        xa, xb = mm.xyz[a], mm.xyz[b]
        span = float(np.linalg.norm(xb - xa))
        if span <= 0.0:
            continue
        for n in sorted((near_of.get(a, set()) | near_of.get(b, set())) - {a, b} - set(remap) - set(newpos)):
            dn, _k, tn = _seg_nearest(mm.xyz[[n]], xa[None, :], xb[None, :])
            if float(dn[0]) <= ON_RUN * span and 0.0 < float(tn[0]) < 1.0:
                remap[n] = b
                out["moved_mm"] = max(out["moved_mm"], float(np.linalg.norm(mm.xyz[n] - 0.5 * (xa + xb))))
    # the node moves, then the elements: nodes put into their edges, the joined nodes made one
    before = {e: _normal(mm.xyz[c]) for e, c in enumerate(mm.conn)
              if any(n in remap or n in newpos for n in c) or any(
                  (min(c[i], c[(i + 1) % len(c)]), max(c[i], c[(i + 1) % len(c)])) in inserts for i in range(len(c)))}
    for n, at in newpos.items():
        mm.xyz[n] = at
    for a, b in remap.items():
        if a not in newpos:
            mm.xyz[a] = mm.xyz[b]
    # nodes that the joining lands on one another are one node: a third face's short edge across the end of the gap
    # (the stub between two corners joined half way) collapses with them
    parent = {n: n for c in before for e in [mm.conn[c]] for n in e}

    def root(n: int) -> int:
        while parent.get(n, n) != n:
            n = parent[n]
        return n

    for a, b in remap.items():
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[ra] = rb
    tiny = COINCIDE_GAPS * gap
    for e in before:
        c = mm.conn[e]
        for i in range(len(c)):
            p, q = c[i], c[(i + 1) % len(c)]
            if float(np.linalg.norm(mm.xyz[p] - mm.xyz[q])) <= tiny:
                rp, rq = root(p), root(q)
                if rp != rq:
                    parent[max(rp, rq)] = min(rp, rq)
    remap = {n: root(n) for n in parent if root(n) != n}
    new_conn: list[list[int]] = []
    new_parent: list[int] = []
    changed: list[int] = []
    for e, c in enumerate(mm.conn):
        if e not in before:
            new_conn.append(c)
            new_parent.append(mm.parent[e])
            continue
        poly: list[int] = []
        for i in range(len(c)):
            p, q = c[i], c[(i + 1) % len(c)]
            poly.append(p)
            key = (min(p, q), max(p, q))
            if key in inserts:
                put = sorted(inserts[key])
                # in the element's own direction along the edge
                sp = float(np.dot(mm.xyz[q] - mm.xyz[p], mm.xyz[put[-1][1]] - mm.xyz[put[0][1]])) if len(put) > 1 else 1.0
                seq = [n for _s, n in put]
                poly += seq if sp >= 0 else seq[::-1]
        poly = [remap.get(n, n) for n in poly]
        kept = [poly[i] for i in range(len(poly)) if poly[i] != poly[(i + 1) % len(poly)]]
        if len(kept) < 3:
            changed.append(-1)                         # collapsed to an edge: it was the stub across the gap
            continue
        if len(set(kept)) != len(kept):
            out["why"] = "joining the mesh there would turn an element over"
            changed.append(-1)
            continue
        for piece in _pieces(kept, mm.xyz):
            new_conn.append(piece)
            new_parent.append(mm.parent[e])
            changed.append(len(new_conn) - 1)
            if float(np.dot(_normal(mm.xyz[piece]), before[e])) < math.cos(math.radians(TURN_DEG)):
                out["why"] = "joining the mesh there would turn an element over"
    mm.conn, mm.parent = new_conn, new_parent
    out["changed"] = [c for c in changed if c >= 0]
    out["dropped"] = sum(1 for c in changed if c < 0)
    users = mm.users()
    fa, fb = _side_free(mm, users, mids_a), _side_free(mm, users, mids_b)
    left = _near(mm, fa, fb, lim) + _near(mm, fb, fa, lim)
    out["left_mm"] = _r(sum(float(np.linalg.norm(mm.xyz[a] - mm.xyz[b])) for a, b in left))
    out["moved_mm"] = _r(out["moved_mm"])
    return out


def _bad(mesh_conn: _Mesh, idx: list[int]) -> tuple[int, int]:
    """(folded, past Nastran's own limits) among the elements `idx`."""
    from meshpipeline.engines import shell_quality as SQ

    folded = past = 0
    for e in idx:
        c = mesh_conn.conn[e]
        p = mesh_conn.xyz[c]
        n = _normal(p)
        k = len(c)
        if any(float(np.dot(np.cross(p[(j + 1) % k] - p[j], p[(j - 1) % k] - p[j]), n)) <= 0.0 for j in range(k)):
            folded += 1
        past += int(SQ._past_one(mesh_conn.xyz, c))
    return folded, past


def _attempt(mm: _Mesh, faces: list, gap: float, reach_mm: float, onto: str) -> tuple[_Mesh, dict, str]:
    """One try at closing a place on a copy of `mm`: each pair of its faces joined (`onto` "": half way; "b": the
    second face kept where it is; "a": the first). Returns (the copy, the totals, the first reason given)."""
    trial = mm.copy()
    total: dict[str, Any] = {"joined": 0, "put_in": 0, "moved_mm": 0.0, "open_mm": 0.0, "left_mm": 0.0}
    why = ""
    for fa, fb in faces:
        if fa == fb:
            continue
        a, b = ({_mid_of(fb)}, {_mid_of(fa)}) if onto == "a" else ({_mid_of(fa)}, {_mid_of(fb)})
        got = _join_pair(trial, a, b, max(gap, 1e-9), reach_mm, onto_b=bool(onto))
        for k in ("joined", "put_in"):
            total[k] += int(got.get(k) or 0)
        total["moved_mm"] = max(total["moved_mm"], float(got.get("moved_mm") or 0.0))
        total["open_mm"] += float(got.get("open_mm") or 0.0)
        total["left_mm"] += float(got.get("left_mm") or 0.0)
        why = why or str(got.get("why") or "")
    return trial, total, why


def close(mesh: ShellMesh, places: list[dict], *, reach_mm: float, reach_from: str) -> tuple[ShellMesh, dict]:
    """SURF-13: the open `places` ([{"at_mm", "gap_mm", "faces": [[face a, face b], ...]}], in mm, as
    cad/surface_model names them) closed in `mesh` (mm) where the gap is under `reach_mm`. Returns (the mesh, the
    record {"reach_mm", "reach_from", "places": [one row per place: at_mm, gap_mm, faces, closed, joined, put_in,
    moved_mm, why]}). The mesh returned is `mesh` itself when nothing was closed."""
    from meshpipeline.engines.shell_mesh import orient

    mm = _Mesh(mesh)
    rows: list[dict] = []
    done = 0
    for p in sorted(places, key=lambda x: (float(x.get("gap_mm") or 0.0), list(x.get("at_mm") or []))):
        gap = float(p.get("gap_mm") or 0.0)
        row: dict[str, Any] = {"at_mm": list(p.get("at_mm") or []), "gap_mm": _r(gap),
                               "faces": [list(f) for f in p.get("faces") or []], "closed": False}
        if gap > reach_mm:
            row["why"] = "wider than the reach"
            rows.append(row)
            continue
        # met half way first; where that folds an element or leaves one past the limits, one side is kept where it is
        # and the other brought the whole gap to it (each side in turn): the smaller elements are not squeezed by half
        # the gap from both sides
        first: dict[str, Any] | None = None
        for onto in ("", "b", "a"):
            trial, total, why = _attempt(mm, row["faces"], gap, reach_mm, onto)
            verdict = dict(row)
            verdict.update({k: _r(v) if isinstance(v, float) else v for k, v in total.items()})
            if not total["joined"] and not total["put_in"]:
                verdict["why"] = why or "no nodes face each other across the gap"
                first = first or verdict
                break
            # (nodes an imprint added are new: moved)
            moved = np.concatenate([np.linalg.norm(trial.xyz[:len(mm.xyz)] - mm.xyz, axis=1) > 0.0,
                                    np.ones(len(trial.xyz) - len(mm.xyz), dtype=bool)])
            old = {tuple(c) for c in mm.conn}
            # the elements the closing changed: new ones, and those on a node it moved
            changed = {e for e, c in enumerate(trial.conn) if tuple(c) not in old or bool(moved[c].any())}
            folded, past = _bad(trial, sorted(changed))
            new = {tuple(c) for c in trial.conn}
            _f0, past0 = _bad(mm, [e for e, c in enumerate(mm.conn) if tuple(c) not in new or bool(moved[c].any())])
            if why.startswith("joining the mesh there would turn") or folded:
                verdict["why"] = "joining the mesh there would fold an element"
            elif past > past0:
                verdict["why"] = "joining the mesh there would leave elements past the solver's limits"
            elif total["moved_mm"] > gap * (1.0 + 1e-6) + 1e-9:
                verdict["why"] = "joining the mesh there would move a node farther than the gap"
            elif total["left_mm"] > max(0.25 * total["open_mm"], 2.0 * gap):
                verdict["why"] = "the mesh's edges there do not face each other along the gap"
            else:
                verdict["closed"] = True
                mm = trial
                done += 1
                first = verdict
                break
            first = first or verdict
            if "fold" not in verdict["why"] and "limits" not in verdict["why"]:
                break
        rows.append(first if first is not None else dict(row, why="no nodes face each other across the gap"))
    record = {"rule": RULE, "reach_mm": _r(reach_mm), "reach_from": reach_from, "closed": done,
              "left": sum(1 for r in rows if not r["closed"]), "places": rows}
    if not done:
        return mesh, record
    out = mm.build()
    orient(out)
    return out, record


__all__ = ["REACH_GAPS", "RULE", "SNAP_EDGE", "SNAP_GAP", "close"]
