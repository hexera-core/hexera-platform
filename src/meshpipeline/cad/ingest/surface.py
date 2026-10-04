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
    """Refuse what cannot be meshed from; drop triangles that have collapsed to a line or a point.

    Nothing is moved, merged or re-oriented: the surface stays the one the file describes."""
    if mesh.n_triangles == 0 or len(mesh.points) == 0:
        raise SurfaceError("the file holds no surface triangles")
    if not np.isfinite(mesh.points).all():
        raise SurfaceError("the file has coordinates that are not numbers (NaN or infinity)")
    t = mesh.triangles
    if t.min() < 0 or t.max() >= len(mesh.points):
        raise SurfaceError("the file's faces point at vertices that do not exist")
    c = mesh.points[t]
    area2 = np.linalg.norm(np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]), axis=1)
    keep = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2]) & (area2 > 0.0)
    dropped = int((~keep).sum())
    if not keep.any():
        raise SurfaceError("every triangle in the file has zero area")
    notes = list(mesh.notes)
    if dropped:
        notes.append(f"{dropped} zero-area triangle(s) were left out")
    return SurfaceMesh(mesh.points, t[keep], mesh.group[keep], list(mesh.names), notes)


def safe_solid_name(name: str) -> str:
    """A name an STL solid line, and an OpenFOAM patch after it, can carry unchanged."""
    s = _NAME_UNSAFE.sub("_", str(name or "").strip()).strip("_.")
    return s[:64]


def named_groups(mesh: SurfaceMesh) -> dict[str, np.ndarray]:
    """Triangle masks per meaningful, distinct, file-given name; {} when the file names fewer than
    two separable groups (one name, or none, is one surface)."""
    from meshpipeline.cad.regions import _meaningful

    by_name: dict[str, np.ndarray] = {}
    for gi, raw in enumerate(mesh.names):
        name = safe_solid_name(raw)
        if not _meaningful(name):
            continue
        mask = np.asarray(mesh.group == gi, dtype=bool)
        if not mask.any():
            continue
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
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
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
