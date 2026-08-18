# Responsibility: Read and write STL, and answer structural questions about one.
# Boundaries: file-level operations; it makes no meshing decision.
from __future__ import annotations

import struct
from pathlib import Path


def read_stl_triangles(path: Path) -> list[tuple]:
    head = path.open("rb").read(5)
    tris: list[tuple] = []
    if head == b"solid":
        verts: list[list[float]] = []
        with path.open() as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("vertex"):
                    verts.append([float(x) for x in s.split()[1:4]])
                    if len(verts) == 3:
                        tris.append((verts[0], verts[1], verts[2])); verts = []
        if tris:
            return tris
    data = path.open("rb").read()
    n = struct.unpack("<I", data[80:84])[0]; off = 84
    for _ in range(n):
        f = struct.unpack("<12fH", data[off:off + 50]); off += 50
        tris.append((list(f[3:6]), list(f[6:9]), list(f[9:12])))
    return tris


def inspect_stl(workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
    p = Path(workspace) / geometry_file
    if not p.exists():
        return {"error": f"{geometry_file} not found in workspace"}
    tris = read_stl_triangles(p)
    mn = [min(v[i] for t in tris for v in t) for i in range(3)]
    mx = [max(v[i] for t in tris for v in t) for i in range(3)]
    ext = [round(mx[i] - mn[i], 4) for i in range(3)]
    return {
        "bbox_min": [round(v, 4) for v in mn],
        "bbox_max": [round(v, 4) for v in mx],
        "extents_xyz": ext,
        "thinnest_axis": "xyz"[ext.index(min(ext))],
        "n_triangles": len(tris),
        "units": "metres (STL is unitless; assumes the CAD export was in metres)",
    }


def read_stl_solids(path: Path) -> dict:
    if Path(path).open("rb").read(5) != b"solid":
        # Binary STL stores no solid names, so there is nothing product-specific to preserve and
        # VTK owns the mechanics. Its triangles come back under one entry named for the file.
        import pyvista as pv

        mesh = pv.read(str(path))
        pts, faces = mesh.points, mesh.faces.reshape(-1, 4)[:, 1:]
        tris = [tuple(tuple(float(c) for c in pts[i]) for i in f) for f in faces]
        return {Path(path).stem: tris}
    return _read_ascii_stl_solids(Path(path))


def _read_ascii_stl_solids(path: Path) -> dict:
    solids: dict[str, list] = {}
    name = None; verts: list = []
    with Path(path).open() as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("solid "):
                name = s[6:].strip() or "solid"; solids.setdefault(name, []); verts = []
            elif s.startswith("endsolid"):
                name = None
            elif s.startswith("vertex") and name is not None:
                verts.append([float(x) for x in s.split()[1:4]])
                if len(verts) == 3:
                    solids[name].append((verts[0], verts[1], verts[2])); verts = []
    return solids


def drop_degenerate(tris: list[tuple]) -> list[tuple]:
    def coincident(p, q): return p[0] == q[0] and p[1] == q[1] and p[2] == q[2]
    return [t for t in tris if not (coincident(t[0], t[1])
            or coincident(t[1], t[2]) or coincident(t[0], t[2]))]


def mirror_y(tris: list[tuple]) -> list[tuple]:
    out = []
    for a, b, c in tris:
        out.append(([a[0], -a[1], a[2]], [c[0], -c[1], c[2]], [b[0], -b[1], b[2]]))
    return out


def _box_triangles(lo, hi) -> list[tuple]:
    x0, y0, z0 = lo; x1, y1, z1 = hi; out: list[tuple] = []
    def quad(a, b, c, d): out.append((a, b, c)); out.append((a, c, d))
    quad([x0, y0, z0], [x0, y1, z0], [x0, y1, z1], [x0, y0, z1])
    quad([x1, y0, z0], [x1, y0, z1], [x1, y1, z1], [x1, y1, z0])
    quad([x0, y0, z0], [x0, y0, z1], [x1, y0, z1], [x1, y0, z0])
    quad([x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1])
    quad([x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0])
    quad([x0, y0, z1], [x0, y1, z1], [x1, y1, z1], [x1, y0, z1])
    return out


def _write_solid(fh, name: str, tris: list[tuple]) -> None:
    fh.write(f"solid {name}\n")
    for a, b, c in tris:
        fh.write("facet normal 0 0 0\n outer loop\n")
        for v in (a, b, c):
            fh.write(f"  vertex {v[0]} {v[1]} {v[2]}\n")
        fh.write(" endloop\nendfacet\n")
    fh.write(f"endsolid {name}\n")


def write_stl_solids(path: Path, solids: dict) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        for name, tris in solids.items():
            _write_solid(fh, name, tris)
