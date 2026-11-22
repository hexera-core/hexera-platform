# Responsibility: The engine-neutral SHELL MESH every shell writer and check reads - nodes, 4-node quads and
# 3-node triangles, each element's wall, mid-face and part - with the two things done on it alone: every
# sheet's normals turned one way (SHMESH-9), and the element-edge census (free, shared, junction).
# Boundaries: pure numpy. It meshes nothing (engines/gmsh/shell_adapter.py does) and writes nothing
# (engines/shell_writers.py does).
# Collaborates with: engines/gmsh/shell_adapter.py, engines/shell_quality.py, engines/shell_writers.py.
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def _cross3(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """np.cross for 3-vectors (any leading shape, broadcast), written out: the same multiplications and subtractions
    in the same order as numpy's own, so the same floats, without its axis handling (np.cross took more time in the
    clean-up than the products it makes: 64,000 calls, 4.3 s on fr008)."""
    a = np.asarray(a)
    b = np.asarray(b)
    a0, a1, a2 = a[..., 0], a[..., 1], a[..., 2]
    b0, b1, b2 = b[..., 0], b[..., 1], b[..., 2]
    return np.stack(np.broadcast_arrays(a1 * b2 - a2 * b1, a2 * b0 - a0 * b2, a0 * b1 - a1 * b0), axis=-1)


@dataclass
class ShellMesh:
    """A shell mesh, millimetres. Elements are numbered quads first, then triangles (1-based in the files)."""
    xyz: np.ndarray                          # (n, 3) float
    quads: np.ndarray                        # (q, 4) int, 0-based node indices, counter-clockwise about the normal
    trias: np.ndarray                        # (r, 3) int
    wall: np.ndarray                         # (q + r,) float: each element's wall (mm)
    mid: list[str]                           # (q + r,) the mid-face each element came from ("Mid_k")
    part: list[str]                          # (q + r,) its body ("Part_k")
    groups: dict[str, np.ndarray] = field(default_factory=dict)   # name -> element indices (0-based)
    node_wall: np.ndarray | None = None      # (n,) the wall at each node (mm), where a wall varies (SHELL-9)
    varies: np.ndarray | None = None         # (q + r,) bool: the element's wall varies (written per corner)
    zoffs: np.ndarray | None = None          # (q + r,) mm: the element's true mid-plane off its nodes' plane, along
    #                                          its normal (SHELL-7 steps and laps: Nastran ZOFFS, Abaqus OFFSET)
    notes: list[str] = field(default_factory=list)
    #: SHWRITE-1: the CAD's own name of a part ("PEDESTAL FOOTPAD GUSSET.57"), by its id; a part not in it is its id
    part_names: dict[str, str] = field(default_factory=dict)

    @property
    def n_elements(self) -> int:
        return int(len(self.quads) + len(self.trias))

    def element_nodes(self, e: int) -> np.ndarray:
        q = len(self.quads)
        return self.quads[e] if e < q else self.trias[e - q]

    def elements(self) -> list[np.ndarray]:
        return [self.quads[i] for i in range(len(self.quads))] + [self.trias[i] for i in range(len(self.trias))]


def _normal(xyz: np.ndarray, conn: np.ndarray) -> np.ndarray:
    p = xyz[conn]
    if len(conn) == 4:
        n = _cross3(p[2] - p[0], p[3] - p[1])
    else:
        n = _cross3(p[1] - p[0], p[2] - p[0])
    return n / max(float(np.linalg.norm(n)), 1e-300)


def _edges_of(conn) -> list[tuple[int, int]]:
    k = len(conn)
    return [(int(conn[i]), int(conn[(i + 1) % k])) for i in range(k)]


def edge_users(mesh: ShellMesh) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """Every element edge (sorted node pair) -> [(element, +1 if the element runs it a->b else -1)]."""
    users: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for e, conn in enumerate(mesh.elements()):
        for a, b in _edges_of(conn):
            key = (a, b) if a < b else (b, a)
            users.setdefault(key, []).append((e, 1 if a < b else -1))
    return users


def orient(mesh: ShellMesh, reference: dict[str, np.ndarray] | None = None) -> int:
    """SHMESH-9: turn every element so the normals of each connected sheet agree - neighbours across an edge two
    elements share run it in opposite directions; a junction edge (three or more elements) joins nothing here.
    Each sheet's first element (lowest number) sets its sheet's side: along `reference[mid]` (a direction per
    mid-face) when given, else as meshed. Returns how many elements were turned."""
    users = edge_users(mesh)
    n = mesh.n_elements
    flip = np.zeros(n, dtype=bool)
    seen = np.zeros(n, dtype=bool)
    nbrs: list[list[tuple[int, bool]]] = [[] for _ in range(n)]
    for us in users.values():
        if len(us) == 2:
            (e1, s1), (e2, s2) = us
            # consistent when they run the edge in opposite directions: same relative flip
            nbrs[e1].append((e2, s1 == s2))
            nbrs[e2].append((e1, s1 == s2))
    for seed in range(n):
        if seen[seed]:
            continue
        seen[seed] = True
        if reference and mesh.mid[seed] in reference:
            nrm = _normal(mesh.xyz, mesh.element_nodes(seed))
            flip[seed] = float(np.dot(nrm, reference[mesh.mid[seed]])) < 0
        todo = [seed]
        while todo:
            e = todo.pop()
            for o, opposite_needed in nbrs[e]:
                want = flip[e] ^ opposite_needed
                if not seen[o]:
                    seen[o] = True
                    flip[o] = want
                    todo.append(o)
    q = len(mesh.quads)
    if flip[:q].any():
        mesh.quads[flip[:q]] = mesh.quads[flip[:q]][:, ::-1]
    if flip[q:].any():
        mesh.trias[flip[q:]] = mesh.trias[flip[q:]][:, ::-1]
    return int(flip.sum())


def edge_census(mesh: ShellMesh) -> dict:
    """The element edges: free (one element), shared (two), junction (three or more), and how many shared edges
    two elements run the same way (normals that disagree; 0 after orient, except across a Moebius-like sheet)."""
    users = edge_users(mesh)
    free = [k for k, u in users.items() if len(u) == 1]
    junction = sum(1 for u in users.values() if len(u) > 2)
    clash = sum(1 for u in users.values() if len(u) == 2 and u[0][1] == u[1][1])
    return {"edges": len(users), "free": len(free), "shared": sum(1 for u in users.values() if len(u) == 2),
            "junction": junction, "normals_disagree": clash, "free_edges": free}


#: SHMESH-12: an element edge shorter than the floor (FLOOR_WALLS times the wall: below what a shell carries) and
#: either shorter than SHORT_SHARE of the longest edge of every element on it, or than SHORT_SHARE of the floor
#: itself, is a CAD sliver (a 0.002 mm edge where two kayak panels nearly meet; a 0.2 mm step meshed among 1.5 mm
#: edges): its two ends become one node
SHORT_SHARE = 0.2
CORNER_GUARD = True
#: a fold the corner guard finds is undone at the folded element alone, not its whole cluster (sh0083: undoing whole
#: clusters kept 2,600 sliver edges and doubled its triangles); the last of FOLD_ROUNDS checks undoes whole clusters
FOLD_LOCAL = True
FOLD_ROUNDS = 6


def _corners_turn(xyz: np.ndarray, conn: list[int], normal: np.ndarray) -> bool:
    """Whether any corner of the element turns against `normal` (a corner Jacobian not positive)."""
    p = xyz[conn]
    k = len(conn)
    return any(float(np.dot(_cross3(p[(j + 1) % k] - p[j], p[(j - 1) % k] - p[j]), normal)) <= 0.0 for j in range(k))


#: SHMESH-80: no clean-up step writes the same element twice (two elements on the same corners)
TWIN_GUARD = True


def _twins_after(elems: list[tuple[list[int], int]], to: np.ndarray) -> list[list[int]]:
    """SHMESH-80: the groups of elements (indices into `elems`) that the merge `to` would leave on the same corners,
    each group holding at least one element the merge changes (twins the input already held are not the merge's)."""
    seen: dict[frozenset, list[int]] = {}
    moved: set[int] = set()
    for i, (conn, _f) in enumerate(elems):
        c = [int(to[a]) for a in conn]
        kept = [c[k] for k in range(len(c)) if c[k] != c[(k + 1) % len(c)]]
        if len(kept) < 3 or len(set(kept)) != len(kept):
            continue
        seen.setdefault(frozenset(kept), []).append(i)
        if c != conn:
            moved.add(i)
    return [ids for ids in seen.values() if len(ids) > 1 and moved.intersection(ids)]


#: SHMESH-80b: a merge that would write two elements on the same corners is made, and the copies taken out as
#: shell_quality.drop_twins takes them (left once or not at all), where that leaves no more free edges and no more
#: edges held by three or more elements than the mesh had; only elsewhere is the merge undone (TWIN_GUARD). Undone
#: everywhere, the guard left a 0.12 mm edge standing between two strips of a stamped sheet's fillet: 8 needle
#: triangles under 20 degrees (worst 4.8) that no later step could take out, where the merge with its two twins
#: dropped left none
TWIN_DROP = True


def _edge_load(quads: np.ndarray, trias: np.ndarray) -> tuple[int, int]:
    """(edges held by one element, edges held by three or more)."""
    held: dict[tuple[int, int], int] = {}
    for conn in (quads, trias):
        c = np.asarray(conn, dtype=np.int64)
        if not len(c):
            continue
        k = c.shape[1]
        for m in range(k):
            a, b = c[:, m], c[:, (m + 1) % k]
            for x, y in zip(np.minimum(a, b).tolist(), np.maximum(a, b).tolist(), strict=True):
                held[(x, y)] = held.get((x, y), 0) + 1
    return sum(1 for v in held.values() if v == 1), sum(1 for v in held.values() if v > 2)


#: SHMESH-12d: two triangles of one face back to back whose far corners lie closer than an edge of theirs would have
#: to be to merge (the shared edge read as their longest) are a quad folded flat: their far corners are merged as a
#: short edge's ends are, and the two triangles go (the twins the merge writes, SHMESH-80b). gmsh left such a pair on
#: sh0010 (corners 0.09 mm apart across a 2.88 mm edge, both triangles at 177 degrees): no edge of theirs is short,
#: and a swap only makes two other needles, so no step took them out and the plate stayed past Nastran's limits
FOLDED_PAIRS = True


def _folded_pairs(xyz: np.ndarray, trias: np.ndarray, t_face: list[int], limit: np.ndarray
                  ) -> list[tuple[float, int, int, int, int]]:
    """SHMESH-12d: (distance, a, b, i, j) for the far corners a < b of each pair of triangles i, j of one face that
    share an edge (theirs alone) and whose far corners are closer than the merge limit for an edge as long as the
    shared one."""
    users: dict[tuple[int, int], list[int]] = {}
    T = np.asarray(trias, dtype=np.int64)
    for i, c in enumerate(T.tolist()):
        for k in range(3):
            a, b = c[k], c[(k + 1) % 3]
            users.setdefault((min(a, b), max(a, b)), []).append(i)
    out = []
    for (a, b), us in users.items():
        if len(us) != 2 or t_face[us[0]] != t_face[us[1]]:
            continue
        p, q = (next(v for v in T[u].tolist() if v not in (a, b)) for u in us)
        if p == q:
            continue
        f = t_face[us[0]]
        d = float(np.linalg.norm(xyz[p] - xyz[q]))
        m = max(SHORT_SHARE * float(limit[f]), min(float(limit[f]), SHORT_SHARE * float(np.linalg.norm(xyz[a] - xyz[b]))))
        if d < m:
            out.append((d, min(p, q), max(p, q), us[0], us[1]))
    return out


def collapse_short(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                   limit: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[int], list[int], int]:
    """SHMESH-12 (_collapse_short), with SHMESH-80b: the twins a merge writes are dropped where the surface stays as
    clean (no more free edges, no more edges held by three or more), and the merge undone only elsewhere."""
    if TWIN_GUARD and TWIN_DROP:
        from meshpipeline.engines.shell_quality import drop_twins

        got = _collapse_short(xyz, quads, trias, q_face, t_face, limit, guard=False)
        q2, t2, fq, ft, merged = got
        if merged:
            q3, t3, fq3, ft3, gone = drop_twins(q2, t2, fq, ft)
            if not gone:
                return got
            free0, over0 = _edge_load(quads, trias)
            free1, over1 = _edge_load(q3, t3)
            if free1 <= free0 and over1 <= over0:
                return q3, t3, fq3, ft3, merged
    return _collapse_short(xyz, quads, trias, q_face, t_face, limit, guard=TWIN_GUARD)


def _collapse_short(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                    limit: np.ndarray, *, guard: bool = True) -> tuple[np.ndarray, np.ndarray, list[int], list[int], int]:
    """SHMESH-12: merge the two ends of every element edge shorter than `limit[face]` (the smaller of its elements'
    faces' floors) and either than SHORT_SHARE of the longest edge of each element on it or than SHORT_SHARE of
    that floor. A cluster of merged nodes keeps the one most faces meet at (a junction or a corner holds its
    place), the lowest numbered on a tie, where it is. A triangle left with two corners one is dropped; a quad
    with one pair merged becomes a triangle, with more is dropped. Returns (quads, trias, their faces, how many
    nodes merged); the nodes themselves stay (unused ones are dropped by the caller)."""
    n = len(xyz)
    elems = [(q, f) for q, f in zip(quads.tolist(), q_face, strict=True)] + \
            [(t, f) for t, f in zip(trias.tolist(), t_face, strict=True)]
    lim: dict[tuple[int, int], float] = {}
    faces_at: list[set[int]] = [set() for _ in range(n)]
    for conn, f in elems:
        p = xyz[conn]
        longest = float(np.linalg.norm(p[list(range(1, len(conn))) + [0]] - p, axis=1).max())
        for i, a in enumerate(conn):
            faces_at[a].add(f)
            b = conn[(i + 1) % len(conn)]
            key = (a, b) if a < b else (b, a)
            lim[key] = min(lim.get(key, np.inf), max(SHORT_SHARE * float(limit[f]),
                                                      min(float(limit[f]), SHORT_SHARE * longest)))
    # (each edge's length measured once: the same float the filter and the sort read)
    short = sorted((d_, a, b) for (a, b), m in lim.items()
                   for d_ in (float(np.linalg.norm(xyz[a] - xyz[b])),) if d_ < m)
    folded: dict[int, tuple[int, int]] = {}       # (SHMESH-12d: each folded pair's triangles -> its far corners)
    if FOLDED_PAIRS:
        pairs = _folded_pairs(xyz, trias, t_face, limit)
        short = sorted(set(short) | {(d, a, b) for d, a, b, _i, _j in pairs})
        for _d, a, b, i, j in pairs:
            folded[len(quads) + i] = folded[len(quads) + j] = (a, b)
    if not short:
        return quads, trias, list(q_face), list(t_face), 0
    parent = list(range(n))

    def root(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def better(a: int, b: int) -> bool:
        return (len(faces_at[a]), -a) > (len(faces_at[b]), -b)

    for _d, a, b in short:
        ra, rb = root(a), root(b)
        if ra == rb:
            continue
        if better(ra, rb):
            parent[rb] = ra
        else:
            parent[ra] = rb
    to = np.array([root(a) for a in range(n)], dtype=np.int64)
    # a merge that would turn an element over (its normal against its own before) is undone, its whole cluster; a merge that only folds a corner in is undone at that element's own moved nodes, which puts
    # the element back exactly as it was while the rest of its cluster still merges
    for round_ in range(FOLD_ROUNDS):
        bad_roots: set[int] = set()
        bad_nodes: set[int] = set()
        if guard:
            # SHMESH-80: a merge that would make two elements one and the same (the same corners: two faces folded
            # onto each other at a seam) is undone as a folded corner is - at the elements' own moved nodes, the last
            # round their whole clusters
            for ids in _twins_after(elems, to):
                if FOLD_LOCAL and round_ < FOLD_ROUNDS - 1:
                    bad_nodes |= {a for i in ids for a in elems[i][0] if int(to[a]) != a}
                else:
                    bad_roots |= {int(to[a]) for i in ids for a in elems[i][0]}
        for e_i, (conn, _f) in enumerate(elems):
            c = [int(to[a]) for a in conn]
            if c == conn:
                continue
            kept = [c[i] for i in range(len(c)) if c[i] != c[(i + 1) % len(c)]]
            if len(kept) < 3 or len(set(kept)) != len(kept):
                continue
            if e_i in folded and to[folded[e_i][0]] == to[folded[e_i][1]]:
                continue        # (SHMESH-12d: a folded pair's triangle closes onto its twin; the two go together)
            before = _normal(xyz, np.array(conn))
            if float(np.dot(_normal(xyz, np.array(kept)), before)) <= 0.0:
                bad_roots |= {int(to[a]) for a in conn}
            elif CORNER_GUARD and _corners_turn(xyz, kept, before) and not _corners_turn(xyz, conn, before):
                # a corner folded in (a quad can keep its normal with one corner turned back)
                if FOLD_LOCAL and round_ < FOLD_ROUNDS - 1:
                    bad_nodes |= {a for a in conn if int(to[a]) != a}
                else:
                    bad_roots |= {int(to[a]) for a in conn}
        if not bad_roots and not bad_nodes:
            break
        if bad_roots:
            undo = np.isin(to, np.array(sorted(bad_roots), dtype=np.int64))
            to[undo] = np.flatnonzero(undo)
        for a in sorted(bad_nodes):
            to[a] = a
    if guard:
        # SHMESH-80: the last round's undoing is checked too - whole clusters undone until no two elements are one
        for _ in range(n):
            groups = _twins_after(elems, to)
            if not groups:
                break
            roots = {int(to[a]) for ids in groups for i in ids for a in elems[i][0]}
            undo = np.isin(to, np.array(sorted(roots), dtype=np.int64))
            to[undo] = np.flatnonzero(undo)
    merged = int((to != np.arange(n)).sum())
    if not merged:
        return quads, trias, list(q_face), list(t_face), 0
    out_q, out_t, fq, ft = [], [], [], []
    for conn, f in elems:
        c = [int(to[a]) for a in conn]
        # drop repeats next to each other (round the element)
        kept = [c[i] for i in range(len(c)) if c[i] != c[(i + 1) % len(c)]]
        if len(set(kept)) != len(kept):
            continue
        if len(kept) == 4:
            out_q.append(kept)
            fq.append(f)
        elif len(kept) == 3:
            out_t.append(kept)
            ft.append(f)
    Q = np.array(out_q, dtype=np.int64).reshape(-1, 4)
    T = np.array(out_t, dtype=np.int64).reshape(-1, 3)
    return Q, T, fq, ft, merged

__all__ = ["ShellMesh", "edge_census", "edge_users", "collapse_short", "orient"]
