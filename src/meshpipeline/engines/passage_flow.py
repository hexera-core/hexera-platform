# Responsibility: Tell which under-resolved passages of an internal-flow mesh the flow can go round, and judge the resolution floor on the passages it must go through.
# Boundaries: engine-neutral evidence read from a finished polyMesh's boundary on the worker, plus the one verdict every flow engine's resolution_floor gate asks for. It meshes nothing.
# Collaborates with: engines/passage.py (the measure beside the mesh, whose 5th percentile triggers this),
# engines/radius_field.py (the chord rules, reused), the snappy and cfMesh finalize (which record the
# evidence in the manifest), application/final_result.py (the note a delivered mesh carries).
"""Which narrow passages the flow can go round.

The resolution floor holds 12 cells across the passage at the narrowest wall (the 5th percentile of
the wall points, engines/passage.py). On a part whose narrowest gaps sit BESIDE a wider way through,
that verdict judges the whole part by its gaps. Job 02ed0d14 (shell-and-tube exchanger, shell side,
2026-10-01) built a sound 4.2 M-cell mesh that cleared every quality bar and was refused twice: its
narrowest wall was the 20.4 mm gap between neighbouring tubes (46.3 mm pitch, 25.87 mm tubes). On
the same part's earlier mesh (job bc5ddb08) those gaps line 6% of the wall at about 7 cells
across, while the rest of the wall holds 25 or more at its 5th percentile. The flow does not have to
go through the gaps - it goes round the bundle - so they are a SIDE passage.

A passage is a side passage when it is clearly narrower than the narrowest section of the WIDEST way
that joins every port (the main way through). Every passage narrower than that section can be
bypassed: the widest way never enters one. The main way is found on a voxel copy of the fluid: the
largest radius t such that the ports stay joined through voxels at least t from the wall.

  - Every under-resolved reading on a side passage, and the rest of the passage at the floor or
    better: the mesh passes, and the delivery says plainly which gaps fell short and by how much
    (final_result.narrow_passage_caveat).
  - Anything else - a throat, an annulus, a blade passage, a pipe meshed too coarsely - is judged as
    it always was.

The chords here are the radius_field chords (fold and facing rules included) cast from a sample of
the real wall points, read AS THEY ARE: each point is judged by the passage it bounds. The measure
beside the mesh floors every point to the narrowest radius within its own reach, a sizing rule that
spread the tube gaps' 10.2 mm over 52% of that exchanger's wall (a shell wall 80 mm from the next
wall read 7 cells across), and the same rule among the main way's points alone would let the bundle's
few 15 mm readings floor the shell again. Instead the main way's NECK is judged on its own: its
under-resolved walls whose passages, shut on the voxel copy, part the ports. A short under-resolved
throat still fails; the bundle's shoulders, beside the gaps, are passages the flow goes round and
do not stand in for one.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshpipeline.engines.passage import (
    PASSAGE_CEILING_CELLS,
    PASSAGE_FLOOR_CELLS,
    WALL_PATCH_TYPES,
)

logger = logging.getLogger(__name__)

#: A reading is on a SIDE passage when its radius is under this fraction of the main way's narrowest
#: section (read as a lower bound, main_way_radius). A passage the flow must go through is never
#: narrower than that section, so it can never be set aside; below 1.0 so a parallel passage only a
#: little narrower than the main way is judged with it.
SIDE_FRACTION = 0.9
#: Side passages may excuse the floor only while they line at most this share of the wall (by
#: area). A part whose wall is mostly under-resolved gaps is a coarse mesh, whatever the flow does.
SIDE_SHARE_MAX = 0.25
#: The main way's NECK - its under-resolved walls whose passages the flow cannot go round
#: (FluidCopy.must_cross) - is judged on its own (its 5th percentile must hold the floor too) once
#: this many sampled points read there, so a throat too short to reach the 5th percentile of the
#: whole main way still fails.
NECK_MIN_POINTS = 20
#: The most separate under-resolved passages tested one by one for parting the ports; the rest are
#: taken as passages the flow must go through.
MAX_PASSAGES_TESTED = 24
#: Wall points the chords are cast from. Uniform over the points, like the measure's percentiles.
SAMPLE_POINTS = 12_000
#: A chord is cast this many local edges and no further: a passage wider than that has at least
#: this many cells across it, far above the floor, and a long ray costs the most.
REACH_CELLS = 2.0 * PASSAGE_CEILING_CELLS
#: The voxel copy of the fluid: about this many voxels across the narrow gaps, within this many
#: voxels in all.
VOXELS_ACROSS_NARROW = 10
MAX_VOXELS = 8_000_000
#: How far the main way's radius can read long, in voxels: half a voxel diagonal at a voxel centre,
#: and half a voxel more between two neighbouring centres. Taken off, so the radius is a lower bound.
VOXEL_SLACK = 0.5 * math.sqrt(3.0) + 0.5
#: The main way must be read with at least this many voxels across its radius to be trusted. Few
#: voxels can only make the walls thicker and the main way narrower - so fewer passages count as
#: side passages, never more.
MIN_VOXELS_IN_RADIUS = 2
#: The whole reading is evidence beside a finished mesh: past this it is dropped, and the floor is
#: judged as it always was.
EVIDENCE_BUDGET_S = 180.0
#: OpenFOAM patch types that are openings the flow crosses (`patch`). Walls and every other type
#: (symmetry, empty) bound the flow.
PORT_PATCH_TYPES = frozenset({"patch"})


class EvidenceOverdue(Exception):
    """The reading ran past its budget."""


def _check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise EvidenceOverdue("passage flow reading past its budget")


# #
# the boundary, by patch
# #

def boundary_by_patch(polymesh) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict]]:
    """(points, triangles, patch index of each triangle, patches) of a polyMesh boundary, read from
    its `points`, `faces` and `boundary` files. Polygons are fanned from their first vertex, which
    keeps OpenFOAM's winding: every boundary face normal points OUT of the fluid."""
    pts, tri, owner, patches, _e, _eo = boundary_by_patch_with_edges(polymesh)
    return pts, tri, owner, patches


def boundary_by_patch_with_edges(polymesh) -> tuple[np.ndarray, np.ndarray, np.ndarray,
                                                     list[dict], np.ndarray, np.ndarray]:
    """boundary_by_patch, plus the polygons' own edges and the patch index of each edge: the
    edges a cell size is read from (engines/passage.measure_passage says why not the fan's)."""
    from meshpipeline.engines.cfmesh.polymesh_surface import (
        read_boundary_faces,
        read_boundary_patches,
        read_points,
    )
    from meshpipeline.engines.passage import polygon_edges
    pm = Path(polymesh)
    patches = [p for p in read_boundary_patches(pm) if p["n_faces"] > 0]
    empty: tuple[np.ndarray, np.ndarray, np.ndarray, list[dict], np.ndarray, np.ndarray] = (
        np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), np.zeros(0, dtype=np.int64), [],
        np.zeros((0, 2), dtype=np.int64), np.zeros(0, dtype=np.int64))
    if not patches:
        return empty
    points = np.asarray(read_points(pm), dtype=float)
    first = min(p["start_face"] for p in patches)
    last = max(p["start_face"] + p["n_faces"] for p in patches)
    tail = read_boundary_faces(pm, first, last - first)
    tris: list[np.ndarray] = []
    owner: list[np.ndarray] = []
    edges: list[np.ndarray] = []
    edge_owner: list[np.ndarray] = []
    for k, p in enumerate(patches):
        polys = [f for f in tail[p["start_face"] - first:p["start_face"] - first + p["n_faces"]]
                 if len(f) >= 3]
        if not polys:
            continue
        sizes = np.fromiter((len(f) for f in polys), dtype=np.int64, count=len(polys))
        for n in np.unique(sizes):
            ids = np.asarray([polys[i] for i in np.flatnonzero(sizes == n)], dtype=np.int64)
            for j in range(1, int(n) - 1):
                tris.append(np.stack([ids[:, 0], ids[:, j], ids[:, j + 1]], axis=1))
                owner.append(np.full(len(ids), k, dtype=np.int64))
        pe = polygon_edges(polys)
        edges.append(pe)
        edge_owner.append(np.full(len(pe), k, dtype=np.int64))
    if not tris:
        return empty
    tri = np.concatenate(tris)
    used, inv = np.unique(tri, return_inverse=True)
    # every polygon corner is a corner of its fan, so the same numbering holds the edges
    return (points[used], inv.reshape(tri.shape), np.concatenate(owner), patches,
            np.searchsorted(used, np.concatenate(edges)), np.concatenate(edge_owner))


# #
# the main way through, on a voxel copy of the fluid
# #

def _rasterize(origin: np.ndarray, h: float, shape: tuple, tri_pts: np.ndarray,
               deadline: float | None = None) -> np.ndarray:
    """The voxels the triangles pass through: each triangle sampled at most h/3 apart, along its
    longest edge and across its height (so a 1.5 m sliver costs a row of samples, not a square)."""
    hit = np.zeros(int(np.prod(shape)), dtype=bool)
    if len(tri_pts) == 0:
        return hit.reshape(shape)
    a, b, c = tri_pts[:, 0], tri_pts[:, 1], tri_pts[:, 2]
    lengths = np.stack([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1),
                        np.linalg.norm(a - c, axis=1)], axis=1)
    k = np.argmax(lengths, axis=1)
    p = np.where((k == 0)[:, None], a, np.where((k == 1)[:, None], b, c))
    q = np.where((k == 0)[:, None], b, np.where((k == 1)[:, None], c, a))
    r = np.where((k == 0)[:, None], c, np.where((k == 1)[:, None], a, b))
    base = lengths[np.arange(len(lengths)), k]
    area2 = np.linalg.norm(np.cross(q - p, r - p), axis=1)
    height = np.where(base > 0.0, area2 / np.maximum(base, 1e-300), 0.0)
    step = h / 3.0
    na = np.clip(np.ceil(base / step), 1, 4096).astype(np.int64)
    nb = np.clip(np.ceil(height / step), 1, 4096).astype(np.int64)
    key = na * 8192 + nb
    dims = np.asarray(shape) - 1
    for kk in np.unique(key):
        _check(deadline)
        sel = np.flatnonzero(key == kk)
        n_a, n_b = int(kk // 8192), int(kk % 8192)
        tt, ss = np.meshgrid(np.linspace(0.0, 1.0, n_b + 1), np.linspace(0.0, 1.0, n_a + 1),
                             indexing="ij")
        tt, ss = tt.ravel(), ss.ravel()
        chunk = max(1, 2_000_000 // len(tt))
        for i0 in range(0, len(sel), chunk):
            s = sel[i0:i0 + chunk]
            # P + t (R - P) + s (1 - t) (Q - P): t walks from the longest edge to the apex
            pts = (p[s][:, None, :] + tt[None, :, None] * (r[s] - p[s])[:, None, :]
                   + (ss * (1.0 - tt))[None, :, None] * (q[s] - p[s])[:, None, :]).reshape(-1, 3)
            ijk = np.clip(np.floor((pts - origin) / h).astype(np.int64), 0, dims)
            hit[np.ravel_multi_index(ijk.T, shape)] = True
    return hit.reshape(shape)


def _fluid_components(label: np.ndarray, border: set, tri_pts: np.ndarray, origin: np.ndarray,
                      h: float, shape: tuple, probes: int = 20_000) -> list[int]:
    """The enclosed spaces that ARE the fluid, by the boundary's own winding: a polyMesh boundary
    face points out of the fluid, so a step back along its normal lands inside it. Each space that
    a twentieth of the probes land in counts. Asking only which spaces the ports open into also
    takes any space a port cap happens to close off - a blade row's hub, behind an inlet cap drawn
    as a full disc - and read a 98 mm "main way" through a 50 mm annulus."""
    if len(tri_pts) == 0:
        return []
    step = max(1, len(tri_pts) // probes)
    t = tri_pts[::step]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    norm = np.linalg.norm(n, axis=1)
    ok = norm > 0.0
    probe = t[ok].mean(axis=1) - (n[ok] / norm[ok, None]) * (1.5 * h)
    ijk = np.floor((probe - origin) / h).astype(np.int64)
    inside_grid = np.all((ijk >= 0) & (ijk < np.asarray(shape)), axis=1)
    hit = label[tuple(ijk[inside_grid].T)]
    hit = hit[(hit > 0) & ~np.isin(hit, list(border))]
    if len(hit) == 0:
        return []
    comps, votes = np.unique(hit, return_counts=True)
    return [int(c) for c, v in zip(comps, votes) if v >= 0.05 * len(hit)]


@dataclass(frozen=True)
class MainWay:
    """The narrowest section of the widest way that joins every port."""

    #: its radius, a lower bound (FluidCopy.main_way)
    radius: float
    #: the voxel it was read at
    voxel: float
    #: the radius as the voxel copy read it, before the bound
    read: float


class FluidCopy:
    """A voxel copy of the fluid inside a closed boundary wound out of it (a polyMesh boundary
    is): the fluid voxels, each one's distance to the nearest voxel the wall passes through, and
    the fluid voxels beside each port. Built by fluid_copy."""

    def __init__(self, fluid: np.ndarray, dist: np.ndarray, at_port: list, origin: np.ndarray,
                 h: float, deadline: float | None = None) -> None:
        self.fluid, self.dist, self.at_port = fluid, dist, at_port
        self.origin, self.h, self.deadline = origin, h, deadline
        self.shape = fluid.shape

    def joined(self, t: float = 0.0, blocked: np.ndarray | None = None) -> bool:
        """Whether every port is joined to every other through fluid voxels at least `t` from the
        wall and outside `blocked`."""
        from scipy import ndimage
        _check(self.deadline)
        m = self.fluid & (self.dist >= t)
        if blocked is not None:
            m &= ~blocked
        lab, _ = ndimage.label(m)
        common: set | None = None
        for pa in self.at_port:
            s = set(np.unique(lab[pa & m]).tolist()) - {0}
            common = s if common is None else (common & s)
            if not common:
                return False
        return bool(common)

    def main_way(self) -> MainWay | None:
        """The narrowest section of the widest way that joins every port: the largest t at which
        the ports stay joined through voxels at least t from the wall. None when it is too few
        voxels across to be trusted.

        The radius is a LOWER bound: VOXEL_SLACK voxels are taken off what the copy reads (the
        exchanger's 33 mm nozzle mouth reads 25 mm across at 2.2 mm voxels). Read as it came, a
        long annulus copied in 3.3 mm voxels came out 2.6 mm wider than its own 18.2 mm gap, and
        the very passage every bit of the flow goes through looked like one it could go round.
        Bounded below, a passage the flow must go through is never narrower than the main way's
        narrowest section, so it is never set aside as a side passage."""
        vals = np.unique(self.dist[self.fluid])
        if len(vals) == 0 or not self.joined(float(vals[0])):
            return None
        lo_i, hi_i = 0, len(vals) - 1
        while lo_i < hi_i:
            mid = (lo_i + hi_i + 1) // 2
            if self.joined(float(vals[mid])):
                lo_i = mid
            else:
                hi_i = mid - 1
        read = float(vals[lo_i])
        # a voxel centre's distance reads up to half a voxel diagonal long, and the way between
        # two neighbouring centres dips up to half a voxel nearer the wall: the bound holds
        radius = read - VOXEL_SLACK * self.h
        if radius < MIN_VOXELS_IN_RADIUS * self.h:
            return None
        return MainWay(radius=radius, voxel=self.h, read=read)

    def must_cross(self, centres: np.ndarray, radii: np.ndarray) -> np.ndarray:
        """Which of these passages the flow cannot go round, each given by the ball that spans it
        (a centre and a radius). The balls that touch are taken as one passage; one whose voxels,
        shut, part the ports is a passage the flow must go through - a throat, an annulus, a coarse
        stretch of the main way. Gaps the flow can go round - a tube bank's, however wide their
        shoulders - leave the ports joined."""
        from scipy import ndimage
        out = np.zeros(len(centres), dtype=bool)
        if len(centres) == 0:
            return out
        h, shape = self.h, np.asarray(self.shape)
        shut = np.zeros(self.shape, dtype=bool)
        lo = np.floor((centres - radii[:, None] - h - self.origin) / h).astype(np.int64)
        hi = np.ceil((centres + radii[:, None] + h - self.origin) / h).astype(np.int64)
        lo, hi = np.clip(lo, 0, shape - 1), np.clip(hi, 0, shape - 1)
        for i in range(len(centres)):
            if i % 256 == 0:
                _check(self.deadline)
            sl = tuple(slice(int(a), int(b) + 1) for a, b in zip(lo[i], hi[i]))
            grid = np.meshgrid(*[self.origin[k] + (np.arange(sl[k].start, sl[k].stop) + 0.5) * h
                                 for k in range(3)], indexing="ij")
            d2 = sum((g - centres[i][k]) ** 2 for k, g in enumerate(grid))
            shut[sl] |= d2 <= (radii[i] + h) ** 2
        shut &= self.fluid
        if not shut.any() or self.joined(0.0, blocked=shut):
            return out                   # all of them shut at once, and the ports still meet
        lab, n = ndimage.label(shut, structure=np.ones((3, 3, 3), dtype=bool))
        ijk = np.clip(np.floor((centres - self.origin) / h).astype(np.int64), 0, shape - 1)
        of = lab[tuple(ijk.T)]
        ks, counts = np.unique(of[of > 0], return_counts=True)
        for rank, k in enumerate(ks[np.argsort(-counts, kind="stable")].tolist()):
            # past the biggest few, a passage is taken as one the flow must go through: the
            # floor is then judged on it, as it always was
            if rank >= MAX_PASSAGES_TESTED or not self.joined(0.0, blocked=lab == k):
                out |= of == k
        return out


def fluid_copy(points, tris, port_of_tri, *, narrow_width: float,
               max_voxels: int = MAX_VOXELS,
               deadline: float | None = None) -> FluidCopy | None:
    """The voxel copy of the fluid inside a closed boundary (`port_of_tri` is -1 for a wall
    triangle and the port's index otherwise; the boundary must be wound out of the fluid, as a
    polyMesh boundary is). The voxels are about a tenth of `narrow_width`, within `max_voxels`.
    None when it cannot be made (fewer than two ports, a boundary that leaks at that voxel)."""
    from scipy import ndimage

    pts = np.asarray(points, dtype=float)
    tri = np.asarray(tris, dtype=np.int64)
    port = np.asarray(port_of_tri, dtype=np.int64)
    port_ids = sorted({int(x) for x in port[port >= 0].tolist()})
    if len(port_ids) < 2 or len(tri) == 0:
        return None
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    ext = np.maximum(hi - lo, 1e-12)
    pad = 3
    h = max(float(narrow_width) / VOXELS_ACROSS_NARROW, 1e-12)
    while np.prod(np.ceil(ext / h) + 2 * pad + 1) > max_voxels:
        h *= 1.1
    origin = lo - pad * h
    shape = tuple(int(x) for x in (np.ceil(ext / h).astype(np.int64) + 2 * pad + 1))
    tp = pts[tri]
    wall = _rasterize(origin, h, shape, tp[port < 0], deadline)
    ports = [_rasterize(origin, h, shape, tp[port == k], deadline) & ~wall for k in port_ids]
    _check(deadline)
    any_port = np.zeros(shape, dtype=bool)
    for m in ports:
        any_port |= m
    free = ~(wall | any_port)
    label, _ = ndimage.label(free)
    border = set(np.unique(np.concatenate([
        label[0].ravel(), label[-1].ravel(), label[:, 0].ravel(), label[:, -1].ravel(),
        label[:, :, 0].ravel(), label[:, :, -1].ravel()])).tolist())
    inside = _fluid_components(label, border, tp, origin, h, shape)
    if not inside:
        # no vote (a boundary wound the other way, or every probe inside a wall): the enclosed
        # spaces the ports open into
        beside_port = ndimage.binary_dilation(any_port) & free
        inside = [int(c) for c in np.unique(label[beside_port]) if c and int(c) not in border]
    if not inside:
        return None
    fluid = np.isin(label, inside)
    del label, free
    _check(deadline)
    dist = ndimage.distance_transform_edt(~wall, sampling=h)
    at_port = [ndimage.binary_dilation(m) & fluid for m in ports]
    return FluidCopy(fluid, dist, at_port, origin, h, deadline)


def main_way_radius(points, tris, port_of_tri, *, narrow_width: float,
                    max_voxels: int = MAX_VOXELS,
                    deadline: float | None = None) -> MainWay | None:
    """The main way of a closed boundary's fluid (FluidCopy.main_way), or None."""
    copy = fluid_copy(points, tris, port_of_tri, narrow_width=narrow_width,
                      max_voxels=max_voxels, deadline=deadline)
    return None if copy is None else copy.main_way()


# #
# the chords, as each wall point reads them
# #

def own_readings(points, wall_tris, sample, *, reach_cells: float = REACH_CELLS,
                 deadline: float | None = None, edges=None
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(radius, mean edge, vertex area, inward normal) at each sampled wall point: the radius is
    half the chord from the point along its inward normal to the first wall that faces back across
    it, by the radius_field rules (a fold of the point's own face is skipped; a grazing or
    from-behind hit is no reading, NaN). The faces must be wound outward (a polyMesh boundary is).
    No hit within `reach_cells` local edges reads +inf: a passage at least that many cells across.
    `edges`, the wall polygons' own edges when the triangles are their fans, give the mean edge
    (engines/passage.measure_passage: a fan's diagonals are not cell edges).

    Read AS IS, with no floor from the neighbours: each point is judged by the passage it bounds.
    """
    import pyvista as pv
    import vtk

    from meshpipeline.engines import radius_field as rf

    pts = np.asarray(points, dtype=float)
    f = np.asarray(wall_tris, dtype=np.int64)
    sample = np.asarray(sample, dtype=np.int64)
    tri = pts[f]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area2 = np.linalg.norm(fn, axis=1)
    fn_unit = fn / np.maximum(area2, 1e-300)[:, None]
    pn = np.zeros_like(pts)
    va = np.zeros(len(pts))
    for k in range(3):
        np.add.at(pn, f[:, k], fn)
        np.add.at(va, f[:, k], area2 / 6.0)
    pn /= np.maximum(np.linalg.norm(pn, axis=1), 1e-300)[:, None]
    from meshpipeline.engines.passage import mean_edge, triangle_edges
    edge = mean_edge(pts, triangle_edges(f) if edges is None else edges)
    mesh = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    # the static cell locator, not the OBB tree local_radius uses: the same hits ten times faster
    # on a 400k-face wall; the hit points are recomputed exactly below
    loc = vtk.vtkStaticCellLocator()
    loc.SetDataSet(mesh)
    loc.BuildLocator()
    flat = f.ravel()
    order = np.argsort(flat, kind="stable")
    sorted_flat = flat[order]
    starts = np.searchsorted(sorted_flat, sample, side="left")
    ends = np.searchsorted(sorted_flat, sample, side="right")
    tri_of = order // 3
    v0, e1, e2 = tri[:, 0], tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
    hits, ids = vtk.vtkPoints(), vtk.vtkIdList()
    out = np.full(len(sample), np.nan)
    for j, i in enumerate(sample.tolist()):
        if j % 512 == 0:
            _check(deadline)
        d = -pn[i]
        if not np.isfinite(d).all() or not d.any():
            continue
        hits.Reset()
        ids.Reset()
        loc.IntersectWithLine(pts[i], pts[i] + d * (reach_cells * edge[i]), 0.0, hits, ids, None)
        n = ids.GetNumberOfIds()
        if n == 0:
            out[j] = np.inf
            continue
        c = np.unique(np.fromiter((ids.GetId(m) for m in range(n)), dtype=np.int64, count=n))
        # exact ray-triangle intersection (Moller-Trumbore) on the candidate faces
        pv_ = np.cross(d, e2[c])
        det = np.einsum("ij,ij->i", e1[c], pv_)
        ok = np.abs(det) > 1e-30
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        s = pts[i] - v0[c]
        u = np.einsum("ij,ij->i", s, pv_) * inv
        qv = np.cross(s, e1[c])
        v = (qv @ d) * inv
        t = np.einsum("ij,ij->i", e2[c], qv) * inv
        good = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1.0 + 1e-9) & (t > 0.0)
        if not good.any():
            out[j] = np.inf
            continue
        c, t = c[good], t[good]
        k_ = np.argsort(t, kind="stable")
        ring = set(f[tri_of[starts[j]:ends[j]]].ravel().tolist())
        fold = rf.FOLD_REACH * edge[i]
        for cc, tt in zip(c[k_].tolist(), t[k_].tolist()):
            if tt < fold and not ring.isdisjoint(f[cc].tolist()):
                continue                 # the point's own face folding over
            if float(np.dot(d, fn_unit[cc])) >= rf.CHORD_MIN_FACING:
                out[j] = 0.5 * tt
            break                        # a grazing or from-behind hit is no reading
    return out, edge[sample], va[sample], -pn[sample]


# #
# the evidence
# #

def _stats(values: np.ndarray) -> dict:
    v = np.asarray(values, dtype=float)
    if len(v) == 0:
        return {}
    return {"p05": round(float(np.percentile(v, 5)), 1), "median": round(float(np.median(v)), 1),
            "min": round(float(v.min()), 1), "points": int(len(v))}


def flow_evidence(points, tris, port_of_tri, *, wall_of_tri=None,
                  floor: float = PASSAGE_FLOOR_CELLS, sample_points: int = SAMPLE_POINTS,
                  seed: int = 0, deadline: float | None = None, wall_edges=None) -> dict:
    """The passage-flow record of a closed fluid boundary (port triangles carry their port's index
    in `port_of_tri`, every other one -1): the cells across at sampled wall points, split into side
    passages and the rest by the main way's narrowest section. `wall_of_tri` marks the WALL
    triangles the chords are cast from and against, as the measure beside the mesh casts them
    (a symmetry plane closes the fluid but is no wall); every non-port triangle by default.
    `wall_edges`, the wall polygons' own edges when the triangles are their fans, give the cell
    size, as they do for the measure beside the mesh. {} when nothing can be said (no wall, fewer
    than two ports)."""
    pts = np.asarray(points, dtype=float)
    tri = np.asarray(tris, dtype=np.int64)
    port = np.asarray(port_of_tri, dtype=np.int64)
    is_wall = (port < 0) if wall_of_tri is None else np.asarray(wall_of_tri, dtype=bool)
    wall_tris = tri[is_wall]
    if len(wall_tris) < 4 or len({int(x) for x in port[port >= 0].tolist()}) < 2:
        return {}
    wall_ids = np.unique(wall_tris)
    rng = np.random.default_rng(seed)
    sample = np.sort(rng.choice(wall_ids, size=min(int(sample_points), len(wall_ids)),
                                replace=False))
    r, edge, area, inward = own_readings(pts, wall_tris, sample, deadline=deadline,
                                         edges=wall_edges)
    read = ~np.isnan(r)
    if read.sum() < 20:
        return {}
    wide = np.isinf(r)
    # a point that saw no wall within reach bounds a passage at least REACH_CELLS across
    r = np.where(wide, 0.5 * REACH_CELLS * edge, r)
    cells = 2.0 * r / np.maximum(edge, 1e-300)
    under = read & (cells < floor)
    record: dict = {"version": 1, "floor": float(floor), "sampled": int(len(sample)),
                    "read": int(read.sum()), "own": _stats(cells[read])}
    if not under.any():
        record["side"] = {"share": 0.0}
        return record
    # voxels fine enough for the part's narrow passages (its 5th-percentile width), so the main way
    # - never narrower than the passages the flow must go through - is read with many across it
    narrow = 2.0 * float(np.percentile(r[read], 5))
    copy = fluid_copy(pts, tri, port, narrow_width=narrow, max_voxels=MAX_VOXELS,
                      deadline=deadline)
    way = None if copy is None else copy.main_way()
    if copy is None or way is None:
        record["main_way"] = None
        return record
    radius, h = way.radius, way.voxel
    side = read & (r < SIDE_FRACTION * radius)
    main = read & ~side
    # THE NECK: the main way's under-resolved walls whose passages the flow cannot go round - each
    # passage shut on the voxel copy by the ball that spans it, and found to part the ports
    neck = np.zeros_like(main)
    weak = np.flatnonzero(main & under)
    if len(weak):
        centres = pts[sample[weak]] + inward[weak] * r[weak, None]
        neck[weak[copy.must_cross(centres, r[weak])]] = True
    total_area = float(area[read].sum()) or 1.0
    record["main_way"] = {"width_m": round(2.0 * radius, 6), "voxel_m": round(h, 6)}
    record["main"] = _stats(cells[main]) if main.any() else {}
    record["neck"] = {"share": round(float(area[neck].sum()) / total_area, 4),
                      **(_stats(cells[neck]) if neck.any() else {})}
    side_under = side & under
    record["side"] = {
        "share": round(float(area[side].sum()) / total_area, 4),
        "under_floor_share": round(float(area[side_under].sum()) / total_area, 4),
        "points": int(side.sum()), "under_floor_points": int(side_under.sum()),
        "main_under_floor_points": int((main & under).sum()),
    }
    if side_under.any():
        record["side"].update({
            "cells_across": _stats(cells[side_under]),
            "width_m": round(2.0 * float(np.median(r[side_under])), 6),
            "narrowest_width_m": round(2.0 * float(np.percentile(r[side_under], 5)), 6),
        })
    return record


def passage_flow_of_polymesh(workspace, quality: dict | None, *,
                             floor: float = PASSAGE_FLOOR_CELLS,
                             budget_s: float = EVIDENCE_BUDGET_S) -> dict:
    """{"passage_flow": record} for the polyMesh under <workspace>/constant when the measure beside
    it put the narrowest wall under the floor; {} otherwise, or when anything is missing or the
    budget runs out - evidence never loses a finished mesh, and without it the floor is judged as
    it always was."""
    local = (quality or {}).get("passage_cells_across_local") or {}
    p05 = _num(local.get("p05"))
    if p05 is None or p05 >= floor:
        return {}
    pm = Path(workspace) / "constant" / "polyMesh"
    if not (pm / "owner").exists():
        return {}
    t0 = time.monotonic()
    deadline = t0 + budget_s if budget_s and budget_s > 0 else None
    try:
        pts, tris, patch_of_tri, patches, edges, patch_of_edge = boundary_by_patch_with_edges(pm)
        if not patches:
            return {}
        is_port = np.asarray([p["type"] in PORT_PATCH_TYPES for p in patches])
        port_index = {k: n for n, k in enumerate(np.flatnonzero(is_port).tolist())}
        port_of_tri = np.asarray([port_index.get(int(k), -1) for k in range(len(patches))],
                                 dtype=np.int64)[patch_of_tri]
        # the chords run between the patches typed `wall`, as the measure beside the mesh reads
        # them (engines/passage.WALL_PATCH_TYPES); a mesh without one is read on every non-port
        is_wall = np.asarray([p["type"] in WALL_PATCH_TYPES for p in patches])
        wall_of_tri = is_wall[patch_of_tri] if is_wall.any() else None
        # the cell size at a wall point from the wall polygons' own edges, not their fans'
        wall_of_edge = (is_wall[patch_of_edge] if is_wall.any()
                        else ~is_port[patch_of_edge])
        record = flow_evidence(pts, tris, port_of_tri, wall_of_tri=wall_of_tri, floor=floor,
                               deadline=deadline, wall_edges=edges[wall_of_edge])
    except EvidenceOverdue:
        logger.warning("passage flow reading abandoned after %.0f s; the floor is judged as "
                       "measured", budget_s)
        return {}
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("passage flow reading of the polyMesh failed", exc_info=True)
        return {}
    if not record:
        return {}
    record["seconds"] = round(time.monotonic() - t0, 1)
    logger.info("passage flow: %s", record)
    return {"passage_flow": record}


# #
# the verdict
# #

@dataclass(frozen=True)
class ResolutionVerdict:
    """What the resolution floor decided, and the figures it rests on."""

    ok: bool
    #: "unmeasured" (no measure: not judged), "narrowest" (the measure as read beside the mesh),
    #: "main" (side passages set aside; the figures are the main way's)
    scope: str
    cells_across: float | None = None
    median: float | None = None
    floor: float = PASSAGE_FLOOR_CELLS
    #: the side passages set aside, as passage_flow recorded them (scope "main" only)
    side: dict = field(default_factory=dict)
    main_way_width_m: float | None = None


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def judge_resolution(quality: dict | None, floor: float = PASSAGE_FLOOR_CELLS) -> ResolutionVerdict:
    """THE resolution floor, for every flow engine. The 5th percentile beside the mesh decides,
    unless the passage-flow evidence shows that the under-resolved readings lie on side passages
    (ones the flow can go round through a way at least 1/SIDE_FRACTION wider) lining no more than
    SIDE_SHARE_MAX of the wall, and that the main way holds the floor: its walls' 5th percentile,
    and its neck's once NECK_MIN_POINTS read there. Then the mesh passes, and the side passages are
    the delivered note."""
    q = quality or {}
    local = q.get("passage_cells_across_local") or {}
    p05 = _num(local.get("p05"))
    if p05 is None:
        return ResolutionVerdict(True, "unmeasured", floor=floor)
    narrowest = ResolutionVerdict(p05 >= floor, "narrowest", p05, _num(local.get("median")),
                                  floor=floor)
    if narrowest.ok:
        return narrowest
    ev = q.get("passage_flow")
    if not isinstance(ev, dict) or not isinstance(ev.get("main_way"), dict):
        return narrowest

    def part(key: str) -> dict:
        v = ev.get(key)
        return v if isinstance(v, dict) else {}

    side, main, neck = part("side"), part("main"), part("neck")
    side_share = _num(side.get("under_floor_share")) or 0.0
    main_p05 = _num(main.get("p05"))
    width = _num(part("main_way").get("width_m"))
    if side_share <= 0.0 or main_p05 is None or side_share > SIDE_SHARE_MAX:
        return narrowest
    figure, median = main_p05, _num(main.get("median"))
    neck_p05 = _num(neck.get("p05"))
    if ((_num(neck.get("points")) or 0) >= NECK_MIN_POINTS and neck_p05 is not None
            and neck_p05 < figure):
        figure, median = neck_p05, _num(neck.get("median"))
    return ResolutionVerdict(figure >= floor, "main", figure, median, floor=floor,
                             side=dict(side), main_way_width_m=width)


def refuse_under_resolved(verdict: ResolutionVerdict, advice: str, **facts):
    """The resolution floor's rejection, in one wording for every flow engine; `advice` is the
    engine's own repair instruction for the builder. The facts carry the figure the verdict rests
    on (`cells_across`, `needed`) and which passages it judged (`scope`)."""
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.gates import refuse

    x = float(verdict.cells_across or 0.0)
    if verdict.scope == "main":
        side = verdict.side or {}
        w = _num(side.get("width_m"))
        share = _num(side.get("under_floor_share"))
        where = ("cells across the passages the flow must go through (5th percentile of their "
                 f"walls; median {verdict.median}) - the narrower side passages it can go round"
                 + (f" (about {w * 1000:.3g} mm across" if w else "")
                 + (f", {share * 100:.2g}% of the wall)" if w and share is not None else
                    ")" if w else "")
                 + " are set aside")
    else:
        where = (f"cells across the passage at the narrowest wall (5th percentile; median "
                 f"{verdict.median})")
    return refuse(
        f"[RESOLUTION] undermeshed: {x:g} {where} - a CFD mesh needs at least {verdict.floor:g} "
        f"everywhere (industry practice is 20-40). {advice}",
        FailureCause.UNDER_RESOLVED, cells_across=x, needed=verdict.floor, scope=verdict.scope,
        **facts)


__all__ = ["EVIDENCE_BUDGET_S", "MAX_VOXELS", "NECK_MIN_POINTS", "REACH_CELLS", "VOXEL_SLACK",
           "SAMPLE_POINTS", "SIDE_FRACTION", "SIDE_SHARE_MAX", "FluidCopy", "MainWay",
           "ResolutionVerdict", "fluid_copy",
           "boundary_by_patch", "boundary_by_patch_with_edges", "flow_evidence",
           "judge_resolution", "main_way_radius",
           "own_readings", "passage_flow_of_polymesh", "refuse_under_resolved"]
