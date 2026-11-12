# Responsibility: A SURFACE FILE'S GAPS CLOSED IN THE MESH (SURF-13), the last resort after SURF-12: a place where the
# surfaces still do not meet (cad/surface_model.open_places, named by its two faces) and whose gap is narrower than
# the reach (the element size the user asked for, else the run's own, or the notes' "close gaps up to 4 mm") is
# closed in the shell mesh the way an analyst equivalences two free edges: the boundary nodes facing each other
# across the gap are joined into one node half way between them, and a node with no partner across is put into the
# edge facing it (its element cut in two or three). No node moves farther than the gap; the place is kept closed only
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
                        node_wall=None if m.node_wall is None else np.asarray(m.node_wall)[used],
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


def _join_pair(mm: _Mesh, mids_a: set[str], mids_b: set[str], gap: float, reach: float) -> dict[str, Any]:
    """One side of a place closed in `mm` (changed in place): face A's free edges facing face B's across `gap`."""
    users = mm.users()
    lim = REACH_GAPS * gap + 1e-9
    fa, fb = _side_free(mm, users, mids_a), _side_free(mm, users, mids_b)
    ea, eb = _near(mm, fa, fb, lim), _near(mm, fb, fa, lim)
    out: dict[str, Any] = {"joined": 0, "put_in": 0, "moved_mm": 0.0, "open_mm": 0.0}
    if not ea or not eb:
        out["why"] = "the mesh has no free edges facing each other there"
        return out
    out["open_mm"] = _r(sum(float(np.linalg.norm(mm.xyz[a] - mm.xyz[b])) for a, b in ea + eb))
    remap: dict[int, int] = {}
    newpos: dict[int, np.ndarray] = {}
    inserts: dict[tuple[int, int], list[tuple[float, int]]] = {}
    # a node on both sides is already one node (the two faces sewn there): it stays where it is and is no partner of
    # any other; and a node joined or put in once is not joined again from another run of the gap
    shared = {n for e in ea for n in e} & {n for e in eb for n in e}
    taken: set[int] = set()
    for cb in _paths(eb):
        bx = mm.xyz[cb]
        if len(cb) < 2:
            continue
        sb = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(bx, axis=0), axis=1))])
        for ca in _paths(ea):
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
                m = 0.5 * (mm.xyz[a] + mm.xyz[b])
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
                newpos[a] = 0.5 * (mm.xyz[a] + qa[i])
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
                    newpos[b] = 0.5 * (mm.xyz[b] + qb[j])
                    inserts.setdefault(key, []).append((float(sab[j]), b))
                    out["put_in"] += 1
                    out["moved_mm"] = max(out["moved_mm"], 0.5 * float(db[j]))
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
        trial = mm.copy()
        total = {"joined": 0, "put_in": 0, "moved_mm": 0.0, "open_mm": 0.0, "left_mm": 0.0}
        why = ""
        for fa, fb in row["faces"]:
            if fa == fb:
                continue
            got = _join_pair(trial, {_mid_of(fa)}, {_mid_of(fb)}, max(gap, 1e-9), reach_mm)
            for k in ("joined", "put_in"):
                total[k] += int(got.get(k) or 0)
            total["moved_mm"] = max(total["moved_mm"], float(got.get("moved_mm") or 0.0))
            total["open_mm"] += float(got.get("open_mm") or 0.0)
            total["left_mm"] += float(got.get("left_mm") or 0.0)
            why = why or str(got.get("why") or "")
        # the elements the closing changed: new ones, and those on a node it moved
        moved = np.linalg.norm(trial.xyz - mm.xyz, axis=1) > 0.0
        old = {tuple(c) for c in mm.conn}
        changed = {e for e, c in enumerate(trial.conn) if tuple(c) not in old or bool(moved[c].any())}
        row.update({k: _r(v) if isinstance(v, float) else v for k, v in total.items()})
        if not total["joined"] and not total["put_in"]:
            row["why"] = why or "no nodes face each other across the gap"
            rows.append(row)
            continue
        folded, past = _bad(trial, sorted(changed))
        new = {tuple(c) for c in trial.conn}
        _f0, past0 = _bad(mm, [e for e, c in enumerate(mm.conn) if tuple(c) not in new or bool(moved[c].any())])
        if why.startswith("joining the mesh there would turn") or folded:
            row["why"] = "joining the mesh there would fold an element"
        elif past > past0:
            row["why"] = "joining the mesh there would leave elements past the solver's limits"
        elif total["moved_mm"] > gap * (1.0 + 1e-6) + 1e-9:
            row["why"] = "joining the mesh there would move a node farther than the gap"
        elif total["left_mm"] > max(0.25 * total["open_mm"], 2.0 * gap):
            row["why"] = "the mesh's edges there do not face each other along the gap"
        else:
            row["closed"] = True
            mm = trial
            done += 1
        rows.append(row)
    record = {"rule": RULE, "reach_mm": _r(reach_mm), "reach_from": reach_from, "closed": done,
              "left": sum(1 for r in rows if not r["closed"]), "places": rows}
    if not done:
        return mesh, record
    out = mm.build()
    orient(out)
    return out, record


__all__ = ["REACH_GAPS", "RULE", "SNAP_EDGE", "SNAP_GAP", "close"]
