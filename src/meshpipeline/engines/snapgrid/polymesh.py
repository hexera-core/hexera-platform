# Responsibility: Write a Cartesian grid as an OpenFOAM polyMesh: points, faces, owner, neighbour, the named boundary patches and one cellZone per region.
# Owns: the face ordering OpenFOAM requires (internal faces upper-triangular, boundary faces grouped by patch, normals out of the owner) and the binary and ASCII file forms.
# Boundaries: writes what it is given; which cell belongs to which region and which face to which patch is the mesher's.
# Collaborates with: engines/snapgrid/mesher.py (the caller), engines/snapgrid/native.py (splitMeshRegions and checkMesh read what this writes).
"""A tensor grid as an OpenFOAM polyMesh.

Cells are numbered x fastest: c = i + nx (j + ny k); points likewise on the (nx+1, ny+1, nz+1)
lattice. A cell's neighbours on +x, +y, +z are c+1, c+nx, c+nx*ny - always higher - so walking the
cells in order and writing each one's +x, +y, +z face (where it has one) gives exactly the
upper-triangular order OpenFOAM requires of internal faces, with no sort. Every face's points run
counter-clockwise seen from the side its normal points to: from the owner into the neighbour, and
out of the domain on the boundary.

Binary output (the default above a few thousand cells) is OpenFOAM's own: 32-bit labels, 64-bit
scalars, faces as a faceCompactList. ASCII is kept for small grids and tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: Grids up to this many cells are written in ASCII unless binary is asked for.
ASCII_MAX_CELLS = 20_000
_ARCH = "LSB;label=32;scalar=64"
#: The six domain sides, in the order their patch arrays are given: (axis, side 0=min/1=max).
SIDES: tuple[tuple[int, int], ...] = ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1))
SIDE_NAMES = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")


@dataclass(frozen=True)
class Patch:
    name: str
    type: str = "patch"                  # patch | wall


def side_shape(shape_xyz: tuple[int, int, int], axis: int) -> tuple[int, int]:
    """The (rows, columns) of cells on a side normal to `axis`: (z, y) for x, (z, x) for y,
    (y, x) for z - C order, the first index slowest."""
    nx, ny, nz = shape_xyz
    return {0: (nz, ny), 1: (nz, nx), 2: (ny, nx)}[axis]


def _header(cls: str, obj: str, *, binary: bool, location: str = "constant/polyMesh",
            note: str = "") -> str:
    fmt = "binary" if binary else "ascii"
    lines = ["FoamFile", "{", "    version     2.0;", f"    format      {fmt};"]
    if binary:
        lines.append(f'    arch        "{_ARCH}";')
    if note:
        lines.append(f'    note        "{note}";')
    lines += [f"    class       {cls};", f'    location    "{location}";',
              f"    object      {obj};", "}", ""]
    return "\n".join(lines) + "\n"


def _write_labels(fh, values: np.ndarray, binary: bool) -> None:
    values = np.ascontiguousarray(values, dtype="<i4")
    if binary:
        fh.write(f"\n{len(values)}\n(".encode())
        fh.write(values.tobytes())
        fh.write(b")\n")
    else:
        fh.write(f"\n{len(values)}\n(\n".encode())
        if len(values):
            fh.write(("\n".join(map(str, values.tolist())) + "\n").encode())
        fh.write(b")\n")


def _points(lines) -> np.ndarray:
    xs, ys, zs = lines
    pts = np.empty((len(zs), len(ys), len(xs), 3), dtype="<f8")
    pts[..., 0] = xs[None, None, :]
    pts[..., 1] = ys[None, :, None]
    pts[..., 2] = zs[:, None, None]
    return pts.reshape(-1, 3)


def internal_faces(shape_xyz) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(owner, neighbour, quads) of every internal face, in upper-triangular order."""
    nx, ny, nz = shape_xyz
    n = nx * ny * nz
    c = np.arange(n, dtype=np.int64)
    i = c % nx
    j = (c // nx) % ny
    k = c // (nx * ny)
    has = np.stack([i < nx - 1, j < ny - 1, k < nz - 1], axis=1)       # +x, +y, +z
    owner = np.repeat(c, 3)[has.ravel()]
    kind = np.tile(np.arange(3), n)[has.ravel()]
    del has
    step = np.array([1, nx, nx * ny], dtype=np.int64)
    neighbour = owner + step[kind]
    oi, oj, ok = owner % nx, (owner // nx) % ny, owner // (nx * ny)
    px, pxy = nx + 1, (nx + 1) * (ny + 1)

    def P(a, b, d):
        return a + px * b + pxy * d

    quads = np.empty((len(owner), 4), dtype=np.int64)
    sx, sy, sz = kind == 0, kind == 1, kind == 2
    a, b, d = oi[sx] + 1, oj[sx], ok[sx]                 # the +x face, plane x[i+1]
    quads[sx] = np.stack([P(a, b, d), P(a, b + 1, d), P(a, b + 1, d + 1), P(a, b, d + 1)], 1)
    a, b, d = oi[sy], oj[sy] + 1, ok[sy]                 # the +y face, plane y[j+1]
    quads[sy] = np.stack([P(a, b, d), P(a, b, d + 1), P(a + 1, b, d + 1), P(a + 1, b, d)], 1)
    a, b, d = oi[sz], oj[sz], ok[sz] + 1                 # the +z face, plane z[k+1]
    quads[sz] = np.stack([P(a, b, d), P(a + 1, b, d), P(a + 1, b + 1, d), P(a, b + 1, d)], 1)
    return owner, neighbour, quads


def side_faces(shape_xyz, axis: int, side: int) -> tuple[np.ndarray, np.ndarray]:
    """(owner, quads) of the boundary faces on one domain side, in the C order of its side array
    (side_shape), normals pointing out of the domain."""
    nx, ny, nz = shape_xyz
    px, pxy = nx + 1, (nx + 1) * (ny + 1)

    def P(a, b, d):
        return a + px * b + pxy * d

    rows, cols = side_shape(shape_xyz, axis)
    r, q = np.divmod(np.arange(rows * cols, dtype=np.int64), cols)
    if axis == 0:
        k, j = r, q
        i = np.full_like(k, 0 if side == 0 else nx - 1)
        a = np.full_like(k, 0 if side == 0 else nx)
        quad = [P(a, j, k), P(a, j + 1, k), P(a, j + 1, k + 1), P(a, j, k + 1)]
    elif axis == 1:
        k, i = r, q
        j = np.full_like(k, 0 if side == 0 else ny - 1)
        b = np.full_like(k, 0 if side == 0 else ny)
        quad = [P(i, b, k), P(i, b, k + 1), P(i + 1, b, k + 1), P(i + 1, b, k)]
    else:
        j, i = r, q
        k = np.full_like(j, 0 if side == 0 else nz - 1)
        d = np.full_like(j, 0 if side == 0 else nz)
        quad = [P(i, j, d), P(i + 1, j, d), P(i + 1, j + 1, d), P(i, j + 1, d)]
    quads = np.stack(quad, 1)
    if side == 0:                        # the min side points the other way: reverse the loop
        quads = quads[:, [0, 3, 2, 1]]
    owner = i + nx * (j + ny * k)
    return owner, quads


def write_polymesh(case: Path, lines, *, cell_zone: np.ndarray, zone_names: list[str],
                   side_patches: list[np.ndarray], patches: list[Patch],
                   binary: bool | None = None) -> dict:
    """Write constant/polyMesh for the grid `lines` (x, y, z nodes). `cell_zone` holds, per cell
    (x fastest), the index of its zone in `zone_names`; `side_patches[s]` holds, per boundary face
    of side SIDES[s] (side_shape order), the index of its patch in `patches`. A patch with no face
    is not written. Returns the counts."""
    shape = (len(lines[0]) - 1, len(lines[1]) - 1, len(lines[2]) - 1)
    n_cells = shape[0] * shape[1] * shape[2]
    if binary is None:
        binary = n_cells > ASCII_MAX_CELLS
    mesh = Path(case) / "constant" / "polyMesh"
    mesh.mkdir(parents=True, exist_ok=True)
    points = _points(lines)

    owner_i, neigh_i, quads_i = internal_faces(shape)
    b_owner, b_quads, b_patch = [], [], []
    for s, (axis, side) in enumerate(SIDES):
        o, q = side_faces(shape, axis, side)
        b_owner.append(o)
        b_quads.append(q)
        b_patch.append(np.asarray(side_patches[s], dtype=np.int64).ravel())
    bo = np.concatenate(b_owner)
    bq = np.concatenate(b_quads)
    bp = np.concatenate(b_patch)
    order = np.argsort(bp, kind="stable")
    bo, bq, bp = bo[order], bq[order], bp[order]
    n_internal = len(owner_i)
    counts = np.bincount(bp, minlength=len(patches))
    written: list[tuple[Patch, int, int]] = []
    start = n_internal
    for pid, patch in enumerate(patches):
        if counts[pid]:
            written.append((patch, int(counts[pid]), start))
            start += int(counts[pid])
    owner = np.concatenate([owner_i, bo])
    quads = np.concatenate([quads_i, bq])
    del owner_i, bo, bq
    n_faces = len(owner)
    note = (f"nPoints:{len(points)}  nCells:{n_cells}  nFaces:{n_faces}  "
            f"nInternalFaces:{n_internal}")

    with (mesh / "points").open("wb") as fh:
        fh.write(_header("vectorField", "points", binary=binary).encode())
        if binary:
            fh.write(f"\n{len(points)}\n(".encode())
            fh.write(np.ascontiguousarray(points, dtype="<f8").tobytes())
            fh.write(b")\n")
        else:
            fh.write(f"\n{len(points)}\n(\n".encode())
            fh.write("".join(f"({x:.17g} {y:.17g} {z:.17g})\n" for x, y, z in points).encode())
            fh.write(b")\n")
    with (mesh / "faces").open("wb") as fh:
        if binary:
            fh.write(_header("faceCompactList", "faces", binary=True).encode())
            _write_labels(fh, np.arange(0, 4 * n_faces + 1, 4, dtype=np.int64), True)
            _write_labels(fh, quads.ravel(), True)
        else:
            fh.write(_header("faceList", "faces", binary=False).encode())
            fh.write(f"\n{n_faces}\n(\n".encode())
            fh.write("".join(f"4({a} {b} {c} {d})\n" for a, b, c, d in quads.tolist()).encode())
            fh.write(b")\n")
    del quads
    with (mesh / "owner").open("wb") as fh:
        fh.write(_header("labelList", "owner", binary=binary, note=note).encode())
        _write_labels(fh, owner, binary)
    with (mesh / "neighbour").open("wb") as fh:
        fh.write(_header("labelList", "neighbour", binary=binary, note=note).encode())
        _write_labels(fh, neigh_i, binary)
    _write_boundary(mesh / "boundary", written)
    _write_cell_zones(mesh / "cellZones", cell_zone, zone_names, binary)
    return {"cells": n_cells, "faces": n_faces, "internal_faces": n_internal,
            "points": len(points), "binary": binary,
            "patches": {p.name: n for p, n, _s in written},
            "empty_patches": [p.name for pid, p in enumerate(patches) if not counts[pid]]}


def _write_boundary(path: Path, written: list[tuple[Patch, int, int]]) -> None:
    out = [_header("polyBoundaryMesh", "boundary", binary=False), f"{len(written)}", "("]
    for patch, n, start in written:
        out += [f"    {patch.name}", "    {", f"        type            {patch.type};"]
        if patch.type == "wall":
            out.append("        inGroups        List<word> 1(wall);")
        out += [f"        nFaces          {n};", f"        startFace       {start};", "    }"]
    out += [")", ""]
    path.write_text("\n".join(out))


def _write_cell_zones(path: Path, cell_zone: np.ndarray, names: list[str], binary: bool) -> None:
    flat = np.asarray(cell_zone).ravel()
    order = np.argsort(flat, kind="stable")
    bounds = np.searchsorted(flat[order], np.arange(len(names) + 1))
    with path.open("wb") as fh:
        fh.write(_header("regIOobject", "cellZones", binary=binary).encode())
        fh.write(f"\n{len(names)}\n(\n".encode())
        for z, name in enumerate(names):
            cells = order[bounds[z]:bounds[z + 1]]
            fh.write(f"{name}\n{{\n    type            cellZone;\n    cellLabels      "
                     "List<label> ".encode())
            labels = np.ascontiguousarray(cells, dtype="<i4")
            if binary:
                fh.write(f"{len(labels)}(".encode())
                fh.write(labels.tobytes())
                fh.write(b");\n}\n")
            else:
                fh.write(f"{len(labels)}(".encode())
                fh.write(" ".join(map(str, labels.tolist())).encode())
                fh.write(b");\n}\n")
        fh.write(b")\n")


__all__ = ["ASCII_MAX_CELLS", "SIDES", "SIDE_NAMES", "Patch", "internal_faces", "side_faces",
           "side_shape", "write_polymesh"]
