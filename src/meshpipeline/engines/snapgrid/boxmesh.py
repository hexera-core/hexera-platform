# Responsibility: Write a mesh of axis-aligned box cells (the snap grid's blocks, each a tensor grid of its own) as one OpenFOAM polyMesh: faces cut where blocks meet, every point lying on a face's edge put into that face (hanging nodes), OpenFOAM's face order, one cellZone per region.
# Owns: the block-to-block face overlay, the hanging-node insertion, the integer point lattice, the face order, and the files.
# Boundaries: topology and files only; which cell is which region and which boundary face is which patch is the mesher's.
# Collaborates with: engines/snapgrid/blocks.py (the layout), engines/snapgrid/mesher.py (the caller), engines/snapgrid/polymesh.py (the per-block faces and the file forms).
"""Box cells with hanging nodes, as a polyMesh.

Every block's lines are a subset of one global line set per axis, so every point of the mesh has
INTEGER coordinates: its line index on x, y and z. That makes the whole topology exact integer
work:

* inside a block, faces are the tensor grid's own (engines/snapgrid/polymesh.internal_faces);
* where two blocks meet, the shared rectangle is cut along both blocks' lines; each piece is one
  face between the one cell above it and the one cell below (a coarse cell gets several);
* any mesh point lying on a face's edge between its corners - a finer neighbour's corner, or a
  point where one block's line crosses the other's on the shared plane - is inserted into that
  face's point loop. Points on one grid line are found by one sorted search per axis, so an edge
  is checked in O(log n), all edges at once.

Faces stay planar rectangles (inserted points are collinear with their edge), every cell stays an
axis-aligned box, and a cell's faces close it - what snappyHexMesh's split-hex cells look like too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.engines.snapgrid.polymesh import (
    SIDES,
    Patch,
    _header,
    _write_cell_zones,
    _write_labels,
    internal_faces,
    side_faces,
)

#: +axis face loops in (u, v) cross coordinates: the loop runs counter-clockwise seen from +axis.
#: u, v are the two other axes in increasing order (x: y,z; y: x,z; z: x,y).
_PLUS_LOOP = {0: ((0, 0), (1, 0), (1, 1), (0, 1)),
              1: ((0, 0), (0, 1), (1, 1), (1, 0)),
              2: ((0, 0), (1, 0), (1, 1), (0, 1))}


@dataclass
class Chunk:
    """The boundary faces of one block on one domain side, in that side's array order."""
    block: int
    side: int                                   # index into SIDES
    shape: tuple[int, int]                      # (rows, cols) of the side array
    start: int                                  # first boundary face of the chunk


@dataclass
class BoxTopology:
    dims: tuple[int, int, int]                  # global line counts per axis
    offsets: np.ndarray                         # first cell of each block
    n_cells: int
    owner: np.ndarray                           # internal faces, sorted upper-triangular
    neighbour: np.ndarray
    face_off: np.ndarray                        # polygon offsets (len n_internal + 1)
    face_pts: np.ndarray                        # polygon point KEYS
    b_owner: np.ndarray                         # boundary faces, chunk by chunk
    b_off: np.ndarray
    b_pts: np.ndarray
    chunks: list[Chunk] = field(default_factory=list)
    hanging_faces: int = 0                      # faces that carry a hanging node
    block_faces: int = 0                        # faces cut where two blocks meet

    @property
    def n_internal(self) -> int:
        return len(self.owner)


def _key(ix, iy, iz, dims):
    nx, ny, _nz = dims
    return (np.asarray(iz, dtype=np.int64) * ny + np.asarray(iy, dtype=np.int64)) * nx + \
        np.asarray(ix, dtype=np.int64)


def _decode(key, dims):
    nx, ny, _nz = dims
    key = np.asarray(key, dtype=np.int64)
    return key % nx, (key // nx) % ny, key // (nx * ny)


def _lattice_keys(lines, dims) -> np.ndarray:
    """Keys of a block's lattice points in local point order (x fastest)."""
    gx, gy, gz = (np.asarray(v, dtype=np.int64) for v in lines)
    return _key(gx[None, None, :], gy[None, :, None], gz[:, None, None], dims).ravel()


def build(blocks, dims: tuple[int, int, int], last: tuple[int, int, int]) -> BoxTopology:
    """The topology of `blocks` (each with `.lines`: global line indices per axis, `.shape`):
    `dims` are the global line counts, `last` the global index of each axis's upper domain plane
    (0 is the lower). Internal faces come out sorted (owner, neighbour) with owner < neighbour."""
    shapes = [b.shape for b in blocks]
    counts = np.array([s[0] * s[1] * s[2] for s in shapes], dtype=np.int64)
    offsets = np.concatenate(([0], np.cumsum(counts)[:-1])).astype(np.int64)
    n_cells = int(counts.sum())

    own_parts, nei_parts, quad_parts, check_parts = [], [], [], []
    b_own, b_quads, chunks = [], [], []
    b_start = 0
    for bi, b in enumerate(blocks):
        shape = shapes[bi]
        keys = _lattice_keys(b.lines, dims)
        o, n, q = internal_faces(shape)
        if len(o):
            nx, ny, nz = shape
            lp = q                                        # local lattice point ids
            pi = lp % (nx + 1)
            pj = (lp // (nx + 1)) % (ny + 1)
            pk = lp // ((nx + 1) * (ny + 1))
            skin = (pi == 0) | (pi == nx) | (pj == 0) | (pj == ny) | (pk == 0) | (pk == nz)
            own_parts.append(o + offsets[bi])
            nei_parts.append(n + offsets[bi])
            quad_parts.append(keys[q])
            check_parts.append(skin.sum(axis=1) >= 2)
        for s, (axis, side) in enumerate(SIDES):
            g = b.lines[axis][0] if side == 0 else b.lines[axis][-1]
            if g != (0 if side == 0 else last[axis]):
                continue
            o, q = side_faces(shape, axis, side)
            rows_cols = {0: (shape[2], shape[1]), 1: (shape[2], shape[0]), 2: (shape[1], shape[0])}
            chunks.append(Chunk(block=bi, side=s, shape=rows_cols[axis], start=b_start))
            b_own.append(o + offsets[bi])
            b_quads.append(keys[q])
            b_start += len(o)

    # faces where two blocks meet
    n_block_faces = 0
    for o, n, q in _block_faces(blocks, offsets, dims):
        own_parts.append(o)
        nei_parts.append(n)
        quad_parts.append(q)
        check_parts.append(np.ones(len(o), dtype=bool))
        n_block_faces += len(o)

    owner = np.concatenate(own_parts) if own_parts else np.zeros(0, dtype=np.int64)
    neighbour = np.concatenate(nei_parts) if nei_parts else np.zeros(0, dtype=np.int64)
    quads = np.concatenate(quad_parts) if quad_parts else np.zeros((0, 4), dtype=np.int64)
    check = np.concatenate(check_parts) if check_parts else np.zeros(0, dtype=bool)
    bo = np.concatenate(b_own) if b_own else np.zeros(0, dtype=np.int64)
    bq = np.concatenate(b_quads) if b_quads else np.zeros((0, 4), dtype=np.int64)

    points = np.unique(np.concatenate([quads.ravel(), bq.ravel()]))
    finder = _EdgeFinder(points, dims)
    face_off, face_pts, hang_i = finder.loops(quads, check)
    b_off, b_pts, hang_b = finder.loops(bq, np.ones(len(bq), dtype=bool))

    order = np.lexsort((neighbour, owner))
    owner, neighbour = owner[order], neighbour[order]
    face_off, face_pts = _reorder(face_off, face_pts, order)
    return BoxTopology(dims=dims, offsets=offsets, n_cells=n_cells, owner=owner,
                       neighbour=neighbour, face_off=face_off, face_pts=face_pts,
                       b_owner=bo, b_off=b_off, b_pts=b_pts, chunks=chunks,
                       hanging_faces=int(hang_i + hang_b), block_faces=n_block_faces)


def _reorder(off: np.ndarray, pts: np.ndarray, order: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sizes = np.diff(off)[order]
    new_off = np.concatenate(([0], np.cumsum(sizes))).astype(np.int64)
    starts = off[:-1][order]
    idx = np.repeat(starts - new_off[:-1], sizes) + np.arange(int(new_off[-1]))
    return new_off, pts[idx]


def _block_faces(blocks, offsets, dims):
    """For every pair of blocks that share a rectangle: the rectangle cut along both blocks'
    lines, each piece a face (owner, neighbour, +axis loop of point keys)."""
    by_plane: dict[tuple[int, int], tuple[list[int], list[int]]] = {}
    for bi, b in enumerate(blocks):
        for a in range(3):
            by_plane.setdefault((a, int(b.lines[a][-1])), ([], []))[0].append(bi)
            by_plane.setdefault((a, int(b.lines[a][0])), ([], []))[1].append(bi)
    for (a, plane), (below, above) in by_plane.items():
        if not below or not above:
            continue
        u, v = [c for c in range(3) if c != a]
        for ia in below:
            A = blocks[ia]
            for ic in above:
                C = blocks[ic]
                u0 = max(A.lines[u][0], C.lines[u][0])
                u1 = min(A.lines[u][-1], C.lines[u][-1])
                v0 = max(A.lines[v][0], C.lines[v][0])
                v1 = min(A.lines[v][-1], C.lines[v][-1])
                if u1 <= u0 or v1 <= v0:
                    continue
                yield _overlay(A, C, ia, ic, a, (u, v), plane, (u0, u1), (v0, v1), offsets,
                               dims)


def _overlay(A, C, ia, ic, a, uv, plane, ur, vr, offsets, dims):
    u, v = uv
    U = np.union1d(A.lines[u][(A.lines[u] >= ur[0]) & (A.lines[u] <= ur[1])],
                   C.lines[u][(C.lines[u] >= ur[0]) & (C.lines[u] <= ur[1])])
    V = np.union1d(A.lines[v][(A.lines[v] >= vr[0]) & (A.lines[v] <= vr[1])],
                   C.lines[v][(C.lines[v] >= vr[0]) & (C.lines[v] <= vr[1])])
    nu, nv = len(U) - 1, len(V) - 1
    qu, qv = np.meshgrid(np.arange(nu), np.arange(nv), indexing="ij")
    qu, qv = qu.ravel(), qv.ravel()

    def cells(B, off, layer):
        iu = np.searchsorted(B.lines[u], U[qu], side="right") - 1
        iv = np.searchsorted(B.lines[v], V[qv], side="right") - 1
        idx = [None, None, None]
        idx[a] = np.full(len(qu), layer, dtype=np.int64)
        idx[u], idx[v] = iu, iv
        nx, ny, _nz = B.shape
        return off + idx[0] + nx * (idx[1] + ny * idx[2])

    ca = cells(A, offsets[ia], A.shape[a] - 1)
    cc = cells(C, offsets[ic], 0)
    loop = _PLUS_LOOP[a]
    corners = []
    for du, dv in loop:
        coord = [None, None, None]
        coord[a] = np.full(len(qu), plane, dtype=np.int64)
        coord[u] = U[qu + du]
        coord[v] = V[qv + dv]
        corners.append(coord)
    quads = np.stack([_key(c[0], c[1], c[2], dims) for c in corners], axis=1)
    swap = cc < ca                               # the face must point from owner to neighbour
    owner = np.where(swap, cc, ca)
    neighbour = np.where(swap, ca, cc)
    quads[swap] = quads[swap][:, [0, 3, 2, 1]]
    return owner, neighbour, quads


def build_topology(layout) -> BoxTopology:
    dims = (len(layout.global_lines[0]), len(layout.global_lines[1]), len(layout.global_lines[2]))
    last = (dims[0] - 1, dims[1] - 1, dims[2] - 1)
    return build(layout.blocks, dims, last)


class _EdgeFinder:
    """Every mesh point, searchable along each axis's lines: the points strictly between two
    corners of an axis-aligned edge are one sorted range."""

    def __init__(self, points: np.ndarray, dims) -> None:
        self.dims = dims
        self.points = points                                   # sorted keys (x fastest)
        ix, iy, iz = _decode(points, dims)
        nx, ny, nz = dims
        self.alt = []
        for axis in range(3):
            if axis == 0:
                ek = points
            elif axis == 1:
                ek = (iz * nx + ix) * ny + iy
            else:
                ek = (iy * nx + ix) * nz + iz
            order = np.argsort(ek, kind="stable")
            self.alt.append((ek[order], points[order]))

    def edge_key(self, axis: int, ix, iy, iz):
        nx, ny, nz = self.dims
        if axis == 0:
            return (iz * ny + iy) * nx + ix
        if axis == 1:
            return (iz * nx + ix) * ny + iy
        return (iy * nx + ix) * nz + iz

    def loops(self, quads: np.ndarray, check: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
        """Point loops (offsets, flat keys) of the faces `quads`: each corner, then the mesh points
        on the edge to the next corner, in order. Only `check` faces are searched."""
        n = len(quads)
        counts = np.zeros((n, 4), dtype=np.int64)
        ranges = np.zeros((n, 4, 2), dtype=np.int64)
        axes = np.zeros((n, 4), dtype=np.int64)
        rev = np.zeros((n, 4), dtype=bool)
        idx = np.flatnonzero(check)
        if len(idx):
            q = quads[idx]
            cx, cy, cz = _decode(q, self.dims)
            for e in range(4):
                f = (e + 1) % 4
                dx, dy = cx[:, f] != cx[:, e], cy[:, f] != cy[:, e]
                ax = np.where(dx, 0, np.where(dy, 1, 2))
                for a in range(3):
                    sel = ax == a
                    if not sel.any():
                        continue
                    k0 = self.edge_key(a, cx[sel, e], cy[sel, e], cz[sel, e])
                    k1 = self.edge_key(a, cx[sel, f], cy[sel, f], cz[sel, f])
                    lo, hi = np.minimum(k0, k1), np.maximum(k0, k1)
                    keys = self.alt[a][0]
                    s0 = np.searchsorted(keys, lo, side="right")
                    s1 = np.searchsorted(keys, hi, side="left")
                    rows = idx[sel]
                    counts[rows, e] = np.maximum(s1 - s0, 0)
                    ranges[rows, e, 0] = s0
                    ranges[rows, e, 1] = s1
                    axes[rows, e] = a
                    rev[rows, e] = k1 < k0
        sizes = 4 + counts.sum(axis=1)
        off = np.concatenate(([0], np.cumsum(sizes))).astype(np.int64)
        pts = np.empty(int(off[-1]), dtype=np.int64)
        plain = sizes == 4
        if plain.any():
            pos = off[:-1][plain]
            for c in range(4):
                pts[pos + c] = quads[plain, c]
        hanging = np.flatnonzero(~plain)
        for r in hanging.tolist():
            loop = []
            for e in range(4):
                loop.append(quads[r, e])
                if counts[r, e]:
                    seg = self.alt[axes[r, e]][1][ranges[r, e, 0]:ranges[r, e, 1]]
                    loop.extend((seg[::-1] if rev[r, e] else seg).tolist())
            pts[off[r]:off[r + 1]] = loop
        return off, pts, int(len(hanging))


def write(case: Path, layout, topo: BoxTopology, *, cell_zone: np.ndarray, zone_names: list[str],
          chunk_patches: list[np.ndarray], patches: list[Patch], binary: bool = True) -> dict:
    """Write constant/polyMesh. `cell_zone` holds each cell's zone (global cell order);
    `chunk_patches[c]` the patch index of each face of topo.chunks[c] (side-array order)."""
    mesh = Path(case) / "constant" / "polyMesh"
    mesh.mkdir(parents=True, exist_ok=True)
    keys = np.unique(np.concatenate([topo.face_pts, topo.b_pts]))
    ix, iy, iz = _decode(keys, topo.dims)
    G = layout.global_lines
    points = np.stack([G[0][ix], G[1][iy], G[2][iz]], axis=1)

    bp = np.concatenate([np.asarray(p, dtype=np.int64).ravel() for p in chunk_patches]) \
        if chunk_patches else np.zeros(0, dtype=np.int64)
    order = np.argsort(bp, kind="stable")
    b_owner = topo.b_owner[order]
    b_off, b_pts = _reorder(topo.b_off, topo.b_pts, order)
    bp = bp[order]
    counts = np.bincount(bp, minlength=len(patches))
    n_int = topo.n_internal
    written = []
    start = n_int
    for pid, patch in enumerate(patches):
        if counts[pid]:
            written.append((patch, int(counts[pid]), start))
            start += int(counts[pid])
    owner = np.concatenate([topo.owner, b_owner])
    flat = np.concatenate([topo.face_pts, b_pts])
    off = np.concatenate([topo.face_off, topo.face_off[-1] + b_off[1:]])
    ids = np.searchsorted(keys, flat)
    n_faces = len(owner)
    note = (f"nPoints:{len(points)}  nCells:{topo.n_cells}  nFaces:{n_faces}  "
            f"nInternalFaces:{n_int}")
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
            _write_labels(fh, off, True)
            _write_labels(fh, ids, True)
        else:
            fh.write(_header("faceList", "faces", binary=False).encode())
            fh.write(f"\n{n_faces}\n(\n".encode())
            rows = []
            for f in range(n_faces):
                loop = ids[off[f]:off[f + 1]]
                rows.append(f"{len(loop)}({' '.join(map(str, loop.tolist()))})\n")
            fh.write("".join(rows).encode())
            fh.write(b")\n")
    with (mesh / "owner").open("wb") as fh:
        fh.write(_header("labelList", "owner", binary=binary, note=note).encode())
        _write_labels(fh, owner, binary)
    with (mesh / "neighbour").open("wb") as fh:
        fh.write(_header("labelList", "neighbour", binary=binary, note=note).encode())
        _write_labels(fh, topo.neighbour, binary)
    from meshpipeline.engines.snapgrid.polymesh import _write_boundary
    _write_boundary(mesh / "boundary", written)
    _write_cell_zones(mesh / "cellZones", cell_zone, zone_names, binary)
    return {"cells": topo.n_cells, "faces": n_faces, "internal_faces": n_int,
            "points": len(points), "binary": binary,
            "hanging_node_faces": topo.hanging_faces, "block_faces": topo.block_faces,
            "patches": {p.name: n for p, n, _s in written},
            "empty_patches": [p.name for pid, p in enumerate(patches) if not counts[pid]]}


__all__ = ["BoxTopology", "Chunk", "build", "build_topology", "write"]
