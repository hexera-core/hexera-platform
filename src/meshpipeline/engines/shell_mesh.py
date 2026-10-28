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
    notes: list[str] = field(default_factory=list)

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


__all__ = ["ShellMesh", "edge_census", "edge_users", "orient"]
