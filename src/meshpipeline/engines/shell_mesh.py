# Responsibility: The engine-neutral SHELL MESH every shell writer and check reads - nodes, 4-node quads and
# 3-node triangles, each element's wall, mid-face and part - with the two things done on it alone: every
# sheet's normals turned one way (SHMESH-9), and the element-edge census (free, shared, junction).
# Boundaries: pure numpy. It meshes nothing (engines/gmsh/shell_adapter.py does) and writes nothing
# (engines/shell_writers.py does).
# Collaborates with: engines/gmsh/shell_adapter.py, engines/shell_quality.py, engines/shell_writers.py.
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


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
        n = np.cross(p[2] - p[0], p[3] - p[1])
    else:
        n = np.cross(p[1] - p[0], p[2] - p[0])
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
    return any(float(np.dot(np.cross(p[(j + 1) % k] - p[j], p[(j - 1) % k] - p[j]), normal)) <= 0.0 for j in range(k))


def collapse_short(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, q_face: list[int], t_face: list[int],
                   limit: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[int], list[int], int]:
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
    short = sorted((float(np.linalg.norm(xyz[a] - xyz[b])), a, b) for (a, b), m in lim.items()
                   if float(np.linalg.norm(xyz[a] - xyz[b])) < m)
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
        for conn, _f in elems:
            c = [int(to[a]) for a in conn]
            if c == conn:
                continue
            kept = [c[i] for i in range(len(c)) if c[i] != c[(i + 1) % len(c)]]
            if len(kept) < 3 or len(set(kept)) != len(kept):
                continue
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
