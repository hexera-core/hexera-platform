# Responsibility: Hold a triangle surface read from any mesh format, measure it, and write it as the canonical STL.
# Owns: the SurfaceMesh value, its cleaning (finite coordinates, no collapsed triangles), its measurements, and the canonical STL encodings.
# Boundaries: numpy over arrays; it reads no source format and decides nothing about meshing.
# Collaborates with: cad/ingest/readers.py (fills it), cad/ingest/canonical.py (writes it), cad/regions.py (what counts as a meaningful name).
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

#: The solid name triangles that belong to no named group are written under, when others are named.
UNNAMED_SOLID = "surface"
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


class SurfaceError(ValueError):
    """A surface that cannot be meshed from: nothing in it, or coordinates that are not numbers."""


@dataclass
class SurfaceMesh:
    points: np.ndarray                     # (n, 3) float64, the file's own unit
    triangles: np.ndarray                  # (m, 3) int64 indices into points
    #: (m,) int, index into names; -1 = no named group. None on construction = all unnamed.
    group: Any = None
    names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.points = np.asarray(self.points, dtype=np.float64).reshape(-1, 3)
        self.triangles = np.asarray(self.triangles, dtype=np.int64).reshape(-1, 3)
        if self.group is None:
            self.group = np.full(len(self.triangles), -1, dtype=np.int64)
        self.group = np.asarray(self.group, dtype=np.int64).reshape(-1)
        if len(self.group) != len(self.triangles):
            raise SurfaceError("internal: one group per triangle is required")

    @property
    def n_triangles(self) -> int:
        return int(len(self.triangles))

    def corners(self) -> np.ndarray:
        """(m, 3, 3) triangle corner coordinates."""
        return self.points[self.triangles]


def concat(parts: list[SurfaceMesh]) -> SurfaceMesh:
    """One surface from several, each part's group names kept (equal names merge into one group)."""
    names: list[str] = []
    pts: list[np.ndarray] = []
    tris: list[np.ndarray] = []
    groups: list[np.ndarray] = []
    notes: list[str] = []
    offset = 0
    for p in parts:
        remap = np.full(len(p.names) + 1, -1, dtype=np.int64)
        for i, n in enumerate(p.names):
            if n not in names:
                names.append(n)
            remap[i] = names.index(n)
        pts.append(p.points)
        tris.append(p.triangles + offset)
        groups.append(np.where(p.group >= 0, remap[np.clip(p.group, 0, None)], -1)
                      if len(p.names) else np.full(len(p.triangles), -1, dtype=np.int64))
        notes.extend(n for n in p.notes if n not in notes)
        offset += len(p.points)
    if not parts:
        return SurfaceMesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), names=[])
    return SurfaceMesh(np.vstack(pts), np.vstack(tris), np.concatenate(groups), names, notes)


def clean(mesh: SurfaceMesh) -> SurfaceMesh:
    """Refuse what cannot be meshed from, then the SAFE cleanup (`tidy`): nothing it does moves a
    point or changes the shape."""
    if mesh.n_triangles == 0 or len(mesh.points) == 0:
        raise SurfaceError("the file holds no surface triangles")
    if not np.isfinite(mesh.points).all():
        raise SurfaceError("the file has coordinates that are not numbers (NaN or infinity)")
    t = mesh.triangles
    if t.min() < 0 or t.max() >= len(mesh.points):
        raise SurfaceError("the file's faces point at vertices that do not exist")
    tidied, _report = tidy(mesh)
    return tidied


@dataclass(frozen=True)
class TidyReport:
    """What the safe cleanup changed. Every item leaves the shape exactly as it was."""

    merged_points: int = 0          # corners that sat on the same spot, made one vertex
    zero_area: int = 0              # triangles collapsed to a line or a point, left out
    duplicates: int = 0             # second copies of a triangle in the same group, left out
    turned: int = 0                 # triangles turned to face the same way as their neighbours

    @property
    def changed(self) -> bool:
        return bool(self.zero_area or self.duplicates or self.turned)

    def notes(self) -> list[str]:
        out = []
        if self.duplicates:
            out.append(f"{self.duplicates} doubled triangle(s) were left out: second copies of "
                       "a face, or zero-thickness slivers made of two copies")
        if self.zero_area:
            out.append(f"{self.zero_area} zero-area triangle(s) were left out")
        if self.turned:
            out.append(f"{self.turned} triangle(s) were turned to face the same way as their "
                       "neighbours")
        return out


def tidy(mesh: SurfaceMesh) -> tuple[SurfaceMesh, TidyReport]:
    """The shape-preserving cleanup of a canonical surface, and what it changed.

    1. corners at exactly the same coordinates become one vertex (nothing moves);
    2. triangles collapsed to a line or a point are left out (they enclose nothing);
    3. a triangle written more than once within one group is resolved by what its edges say
       (`_doubled_to_drop`): a loose sliver goes, a face written twice keeps one copy, a wall two
       regions share keeps both; copies in different groups are never touched;
    4. triangles are turned so that every edge two of them share is walked in opposite
       directions - per connected piece, the way the majority already faces wins, so a file that
       was consistent is left exactly as it was.
    Nothing is filled, smoothed, cut or moved."""
    corners = mesh.points[mesh.triangles]
    pts, inv = np.unique(corners.reshape(-1, 3), axis=0, return_inverse=True)
    t = inv.reshape(-1, 3).astype(np.int64)
    merged = int(len(np.unique(mesh.triangles)) - len(pts))
    group = np.asarray(mesh.group, dtype=np.int64)

    c = pts[t]
    area2 = np.linalg.norm(np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]), axis=1)
    keep = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2]) & (area2 > 0.0)
    zero_area = int((~keep).sum())
    if not keep.any():
        raise SurfaceError("every triangle in the file has zero area")
    t, group = t[keep], group[keep]

    drop = _doubled_to_drop(t, group)
    duplicates = int(drop.sum())
    if duplicates:
        t, group = t[~drop], group[~drop]

    flip = _consistent_flips(t)
    turned = int(flip.sum())
    if turned:
        t = np.where(flip[:, None], t[:, ::-1], t)
    report = TidyReport(max(merged, 0), zero_area, duplicates, turned)
    notes = list(mesh.notes) + [n for n in report.notes() if n not in mesh.notes]
    return SurfaceMesh(pts, t, group, list(mesh.names), notes), report


def _doubled_to_drop(t: np.ndarray, group: np.ndarray) -> np.ndarray:
    """Which copies of a triangle written more than once (same corners, same group) to leave out.

    What a doubled triangle IS is read from its edges - how many OTHER triangles use each one:
    - two edges used by nothing else: a zero-thickness sliver hanging off one edge (the Fluent
      aorta's defect). Every copy goes; one alone would flap loose.
    - every edge used by at most one other: a face written twice. One copy stays.
    - an edge used by two or more others: a wall two pieces share (the interface of a
      multi-region file written without names). Every copy stays - the wall is real, and its two
      sides are what tells the regions apart.
    """
    m = len(t)
    drop = np.zeros(m, dtype=bool)
    key = np.c_[np.sort(t, axis=1), group]
    _, first, set_of, copies = np.unique(key, axis=0, return_index=True, return_inverse=True,
                                         return_counts=True)
    k = copies[np.asarray(set_of).ravel()]
    if not (k > 1).any():
        return drop
    e = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
    _, einv, ecount = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    edge_of = np.asarray(einv).ravel().reshape(3, -1).T          # (m, 3) edge ids
    others = ecount[edge_of] - k[:, None]                         # uses by any OTHER triangle
    doubled = k > 1
    # copies that all run the SAME way round are one face written twice; copies facing opposite
    # ways are the two sides of a zero-thickness sheet (a baffle), which is kept
    rot = np.argmin(t, axis=1)
    oriented = np.stack([t[np.arange(m), rot], t[np.arange(m), (rot + 1) % 3],
                         t[np.arange(m), (rot + 2) % 3], group], axis=1)
    _, oriented_id = np.unique(oriented, axis=0, return_inverse=True)
    set_ids = np.asarray(set_of).ravel()
    pairs = np.unique(np.c_[set_ids, np.asarray(oriented_id).ravel()], axis=0)
    ways = np.bincount(pairs[:, 0], minlength=len(copies))
    same_way = ways[set_ids] == 1
    sliver = doubled & same_way & ((others == 0).sum(axis=1) >= 2)
    written_twice = doubled & ~sliver & (others.max(axis=1) <= 1)
    is_first = np.zeros(m, dtype=bool)
    is_first[first] = True
    drop[sliver] = True
    drop[written_twice & ~is_first] = True
    return drop


def _consistent_flips(t: np.ndarray) -> np.ndarray:
    """Which triangles to turn so that neighbours agree, across edges exactly two triangles share.
    Per connected piece the smaller set is turned. Edges shared by three or more (a junction of
    regions) are not walked: there is no single right answer across them."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import breadth_first_order

    m = len(t)
    he = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    face = np.tile(np.arange(m), 3)
    key = np.sort(he, axis=1)
    order = np.lexsort((key[:, 1], key[:, 0]))
    ks = key[order]
    new_group = np.r_[True, np.any(ks[1:] != ks[:-1], axis=1)]
    starts = np.flatnonzero(new_group)
    sizes = np.diff(np.r_[starts, len(ks)])
    pair = starts[sizes == 2]
    a, b = order[pair], order[pair + 1]
    fa, fb = face[a], face[b]
    # walked the same way along the shared edge -> the two triangles disagree
    disagree = np.all(he[a] == he[b], axis=1)
    if not disagree.any():
        return np.zeros(m, dtype=bool)
    # weight 2 = the pair disagrees, 1 = it agrees (0 would vanish from a sparse matrix)
    graph = coo_matrix((np.r_[disagree, disagree].astype(np.int8) + 1,
                        (np.r_[fa, fb], np.r_[fb, fa])), shape=(m, m)).tocsr()
    flip = np.zeros(m, dtype=bool)
    seen = np.zeros(m, dtype=bool)
    for root in np.flatnonzero(np.diff(graph.indptr) > 0):
        if seen[root]:
            continue
        order_c, pred = breadth_first_order(graph, int(root), directed=False,
                                            return_predecessors=True)
        seen[order_c] = True
        nodes = order_c[1:]
        if len(nodes) == 0:
            continue
        parents = pred[nodes]
        parity = (np.asarray(graph[nodes, parents]).ravel() == 2).tolist()
        state = {int(root): False}
        for node, parent, odd in zip(nodes.tolist(), parents.tolist(), parity):
            state[node] = state[parent] ^ odd
        piece = np.fromiter(state.keys(), dtype=np.int64, count=len(state))
        turn = np.fromiter(state.values(), dtype=bool, count=len(state))
        # the majority of the piece keeps its facing
        flip[piece] = ~turn if turn.sum() * 2 > len(turn) else turn
    return flip


def safe_solid_name(name: str) -> str:
    """A name an STL solid line, and an OpenFOAM patch after it, can carry unchanged."""
    s = _NAME_UNSAFE.sub("_", str(name or "").strip()).strip("_.")
    return s[:64]


def named_groups(mesh: SurfaceMesh) -> dict[str, np.ndarray]:
    """Triangle masks per meaningful, distinct, file-given name; {} when the file names fewer than
    two separable groups (one name, or none, is one surface)."""
    from meshpipeline.cad.regions import _meaningful

    by_name: dict[str, np.ndarray] = {}
    owner: dict[str, str] = {}              # written name -> the file's own spelling of it
    for gi, raw in enumerate(mesh.names):
        name = safe_solid_name(raw)
        if not _meaningful(name):
            continue
        mask = np.asarray(mesh.group == gi, dtype=bool)
        if not mask.any():
            continue
        # two different names that clean to the same text ("inlet 1" and "inlet_1", or two long
        # names alike in their first 64 characters) stay two boundaries
        n = 2
        base = name
        while name in owner and owner[name] != str(raw):
            name = f"{base[:60]}_{n}"
            n += 1
        owner[name] = str(raw)
        if name in by_name:
            mask = np.logical_or(by_name[name], mask)
        by_name[name] = mask
    if len(by_name) < 2:
        return {}
    covered = np.zeros(mesh.n_triangles, dtype=bool)
    for m in by_name.values():
        covered |= m
    if not covered.all():
        rest = UNNAMED_SOLID
        while rest in by_name:
            rest += "_"
        by_name[rest] = ~covered
    return by_name


def write_binary_stl(path: Path, corners: np.ndarray) -> None:
    """Plain binary STL: an 80-byte header that never starts with "solid", unit normals."""
    corners = np.asarray(corners, dtype=np.float64).reshape(-1, 3, 3)
    n = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    rec = np.zeros(len(corners), dtype=[("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"] = n
    rec["v"] = corners
    header = b"binary STL written by Hexera ingest".ljust(80, b" ")
    with Path(path).open("wb") as fh:
        fh.write(header)
        fh.write(np.uint32(len(corners)).tobytes())
        fh.write(rec.tobytes())


def write_ascii_solids(path: Path, solids: dict[str, np.ndarray]) -> None:
    """Multi-solid ASCII STL - the encoding the rest of the system already reads names from
    (cad/stl_io.read_stl_solids): each named group becomes one solid, which becomes one patch."""
    with Path(path).open("w", encoding="ascii", newline="\n") as fh:
        for name, corners in solids.items():
            c = np.asarray(corners, dtype=np.float64).reshape(-1, 3, 3)
            nrm = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
            ln = np.linalg.norm(nrm, axis=1, keepdims=True)
            nrm = np.divide(nrm, ln, out=np.zeros_like(nrm), where=ln > 0)
            fh.write(f"solid {name}\n")
            rows = np.hstack([nrm, c.reshape(-1, 9)])
            for r in rows:
                fh.write(
                    f"facet normal {r[0]:.6g} {r[1]:.6g} {r[2]:.6g}\n outer loop\n"
                    f"  vertex {r[3]:.9g} {r[4]:.9g} {r[5]:.9g}\n"
                    f"  vertex {r[6]:.9g} {r[7]:.9g} {r[8]:.9g}\n"
                    f"  vertex {r[9]:.9g} {r[10]:.9g} {r[11]:.9g}\n endloop\nendfacet\n")
            fh.write(f"endsolid {name}\n")


def write_canonical_stl(mesh: SurfaceMesh, path: Path) -> tuple[str, ...]:
    """The canonical surface file. Binary when the file names fewer than two groups; multi-solid
    ASCII when it names more, so the names reach the engines as patch names. Returns the names."""
    groups = named_groups(mesh)
    corners = mesh.corners()
    if not groups:
        write_binary_stl(path, corners)
        return ()
    write_ascii_solids(path, {name: corners[mask] for name, mask in groups.items()})
    return tuple(groups)


def read_stl(path: Path) -> SurfaceMesh:
    """Any STL - binary, ASCII, upper or lower case, one solid or many - as a SurfaceMesh."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = int(np.frombuffer(data[80:84], dtype="<u4")[0])
        if n > 0 and 84 + 50 * n <= len(data) and not _looks_ascii(data):
            rec = np.frombuffer(data[84:84 + 50 * n],
                                dtype=[("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])
            corners = rec["v"].astype(np.float64)
            return _from_corners(corners)
    return _read_ascii_stl(data.decode("latin-1"))


def _looks_ascii(data: bytes) -> bool:
    """Text - not a binary STL whose 80-byte header merely says "solid ... facet": a binary file's
    records hold zero bytes (attribute words, small floats) within the first few hundred."""
    head = data[:4096]
    if b"\x00" in head[80:]:
        return False
    head = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    return head[:5].lower() == b"solid" and re.search(rb"(?i)\bfacet\b", head) is not None


_ASCII_TOKEN = re.compile(r"(?im)^\s*(solid|endsolid|vertex)\b[ \t]*(.*)$")


def _read_ascii_stl(text: str) -> SurfaceMesh:
    names: list[str] = []
    verts: list[list[float]] = []
    vgroup: list[int] = []
    current = -1
    for kw, rest in _ASCII_TOKEN.findall(text):
        k = kw.lower()
        if k == "vertex":
            parts = rest.split()
            verts.append([float(parts[0]), float(parts[1]), float(parts[2])])
            vgroup.append(current)
        elif k == "solid":
            name = rest.strip()
            names.append(name)
            current = len(names) - 1
        else:
            current = -1
    if len(verts) < 3:
        raise SurfaceError("the STL file holds no triangles")
    usable = len(verts) - len(verts) % 3
    corners = np.asarray(verts[:usable], dtype=np.float64).reshape(-1, 3, 3)
    groups = np.asarray(vgroup[:usable:3], dtype=np.int64)
    mesh = _from_corners(corners)
    mesh.group = groups
    mesh.names = names
    return mesh


def _from_corners(corners: np.ndarray) -> SurfaceMesh:
    corners = np.asarray(corners, dtype=np.float64).reshape(-1, 3, 3)
    pts = corners.reshape(-1, 3)
    tris = np.arange(len(pts), dtype=np.int64).reshape(-1, 3)
    return SurfaceMesh(pts, tris)


def stats(mesh: SurfaceMesh) -> dict:
    """What the round trip is checked on: size, area, closedness, enclosed volume, triangles."""
    c = mesh.corners()
    lo, hi = mesh.points[np.unique(mesh.triangles)].min(axis=0), \
        mesh.points[np.unique(mesh.triangles)].max(axis=0)
    cross = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
    area = float(0.5 * np.linalg.norm(cross, axis=1).sum())
    # Watertightness is a property of the geometry, not of how the file shared its vertices:
    # coincident corners are one point however many times the file repeats them.
    _, inv = np.unique(c.reshape(-1, 3), axis=0, return_inverse=True)
    t = inv.reshape(-1, 3)
    e = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    open_edges = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    watertight = open_edges == 0 and nonmanifold == 0
    volume = float(np.einsum("ij,ij->i", c[:, 0], np.cross(c[:, 1], c[:, 2])).sum() / 6.0) \
        if watertight else None
    return {"triangles": int(len(c)), "bounds_min": [float(v) for v in lo],
            "bounds_max": [float(v) for v in hi], "area": area, "open_edges": open_edges,
            "nonmanifold_edges": nonmanifold, "watertight": watertight,
            "volume": abs(volume) if volume is not None else None}
