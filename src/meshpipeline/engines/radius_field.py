# Responsibility: The local-radius field of a closed or port-open wall - half the inward chord at every point,
# floored within its own reach and smoothed once - which VMTK, gmsh and cfMesh all size and measure from.
# Boundaries: engine-neutral geometry on points and triangles; it knows no engine, no mesher and no gate.
from __future__ import annotations

import numpy as np

#: After the ray cast, every point takes the SMALLEST radius found within this many times its
#: own radius. A ray along the normal reads the gap in one direction only: on a wide flat duct
#: (transition_013, 545 x 88 mm) the top and bottom walls read the 88 mm gap while the 88 mm-tall
#: side walls look across the 545 mm width and read a radius four times larger, so the remesh
#: graded 13 mm cells into 55 mm ones over a strip two cells tall. The gap a wall sees must
#: govern the walls beside it: a point claiming radius R has no narrower gap within R of it. A
#: ball in metres, not mesh rings - the staged wall is as coarse as MAX_EDGE_FRACTION allows.
RADIUS_MIN_REACH = 1.0


def local_radius(points: np.ndarray, faces: np.ndarray, interior_point, r_lo: float,
                 r_hi: float) -> np.ndarray:
    """The lumen's local radius at every wall point: half the chord from the point along its
    INWARD normal to the opposite wall, clipped to [r_lo, r_hi], floored to the smallest radius
    within RADIUS_MIN_REACH of its own, and smoothed once over the 1-ring. Rays that leave
    through an open port borrow the nearest measured value.

    WHY NOT vmtk's centerline: vmtkcenterlines traces a steepest descent on the Voronoi diagram
    of the surface, and on a uniformly remeshed straight run the diagram is degenerate - the
    same radius everywhere, co-spherical points - so the descent stalls ("Degenerate descent
    detected. Target not reached", tee_wye_003 and manifold_002 on every clean surface tried,
    2026-09-11) and vmtkmeshgenerator then crashes on the nonsense sizing field. A chord along
    the normal needs no diagram, no seeds and no luck; the generator only needs a size per point."""
    import pyvista as pv
    import vtk
    mesh = pv.PolyData(np.asarray(points, dtype=float),
                       np.hstack([np.full((len(faces), 1), 3, dtype=np.int64),
                                  np.asarray(faces, dtype=np.int64)]).ravel())
    m = mesh.compute_normals(cell_normals=False, point_normals=True, consistent_normals=True,
                             auto_orient_normals=False, flip_normals=False)
    n = np.asarray(m["Normals"], dtype=float)
    pts = np.asarray(m.points, dtype=float)
    # the sheet is consistently oriented, so one majority vote against the interior point
    # decides whether its normals point into the fluid or away from it
    if (np.einsum("ij,ij->i", n, np.asarray(interior_point, dtype=float) - pts) > 0).mean() > 0.5:
        n = -n
    obb = vtk.vtkOBBTree()
    obb.SetDataSet(mesh)
    obb.BuildLocator()
    reach = 4.0 * r_hi
    hits = vtk.vtkPoints()
    ids = vtk.vtkIdList()
    r = np.full(len(pts), np.nan)
    eps = 1e-3 * r_lo
    for i in range(len(pts)):
        hits.Reset()
        ids.Reset()
        if obb.IntersectWithLine(pts[i] - n[i] * eps, pts[i] - n[i] * reach, hits, ids) \
                and hits.GetNumberOfPoints():
            r[i] = 0.5 * float(np.linalg.norm(np.array(hits.GetPoint(0)) - pts[i]))
    miss = np.isnan(r)
    if miss.all():
        return np.full(len(pts), float(r_lo))
    if miss.any():
        loc = vtk.vtkPointLocator()
        loc.SetDataSet(pv.PolyData(pts[~miss]))
        loc.BuildLocator()
        rv = r[~miss]
        for i in np.flatnonzero(miss):
            r[i] = rv[loc.FindClosestPoint(pts[i])]
    r = np.clip(r, r_lo, r_hi)
    r = _ball_min(pts, r, RADIUS_MIN_REACH)
    f = np.asarray(faces, dtype=np.int64)
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    acc = (np.bincount(e[:, 0], weights=r[e[:, 1]], minlength=len(pts))
           + np.bincount(e[:, 1], weights=r[e[:, 0]], minlength=len(pts)))
    cnt = np.bincount(e[:, 0], minlength=len(pts)) + np.bincount(e[:, 1], minlength=len(pts))
    nb = np.where(cnt > 0, acc / np.maximum(cnt, 1), r)
    return 0.5 * r + 0.5 * nb


def _ball_min(pts: np.ndarray, r: np.ndarray, reach: float) -> np.ndarray:
    """Each point takes the smallest radius over every point (itself included) within `reach`
    times its own radius. The wall is first binned into voxels the size of the smallest radius
    (one minimum per voxel), so a 185 mm ball on a 5 mm wall reads a few hundred voxels, not a
    few thousand points; the ball is padded by a voxel diagonal so the read stays inclusive."""
    from scipy.spatial import cKDTree
    r = np.asarray(r, dtype=float)
    cell = float(r.min())
    _, inv = np.unique(np.floor(pts / cell).astype(np.int64), axis=0, return_inverse=True)
    inv = inv.ravel()
    n = int(inv.max()) + 1
    vmin = np.full(n, np.inf)
    np.minimum.at(vmin, inv, r)
    centre = np.zeros((n, 3))
    np.add.at(centre, inv, pts)
    centre /= np.bincount(inv, minlength=n)[:, None]
    tree = cKDTree(centre)
    pad = cell * np.sqrt(3.0)
    out = r.copy()
    step = 4096
    for i in range(0, len(pts), step):
        balls = tree.query_ball_point(pts[i:i + step], r[i:i + step] * float(reach) + pad)
        out[i:i + step] = [min(out[i + j], vmin[b].min()) for j, b in enumerate(balls)]
    return out


__all__ = ["RADIUS_MIN_REACH", "local_radius"]
