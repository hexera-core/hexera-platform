# Responsibility: Measure how many cells span the flow passage of an internal-flow mesh, and cap sizes so the industry floor is met.
# Boundaries: engine-neutral geometry - reads a closed boundary surface or a polyMesh, judges nothing; each engine's gate judges.
# Collaborates with: cfmesh (size caps in render_cfmesh_case, the measure beside the mesh in native.py), snappy (the measure
# beside the mesh), gmsh (its driver carries the same measure), and every flow engine's resolution_floor gate.
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: INDUSTRY DENSITY for a fluid domain: about this many cells across the LOCAL passage (twice
#: the local radius) - the target VMTK fills to (2 / edge_length_factor 0.15) and gmsh sizes to.
#: cfMesh's cut-cell hexes reach it with far fewer cells than tets do: 13 across a pipe is
#: ~130 cells per cross-section, so a 1 m reducer is ~15k hexes, not 900k tets.
PASSAGE_CELLS_ACROSS = 13
#: The floor the resolution_floor gate holds at the narrowest wall (5th percentile of the
#: boundary points). Industry practice for RANS internal flow is 20-40 across.
PASSAGE_FLOOR_CELLS = 12


def measure_passage(points, faces, radius) -> dict:
    """Cells across the passage at every boundary point: twice the local radius over the mean
    length of the boundary edges that meet the point (the surface cell is what the volume is
    cut to beside it). Median, 5th percentile (the narrowest wall, less a few outliers), min."""
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    r = np.asarray(radius, dtype=float)
    if len(f) == 0 or len(pts) == 0:
        return {}
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
    acc = (np.bincount(e[:, 0], weights=length, minlength=len(pts))
           + np.bincount(e[:, 1], weights=length, minlength=len(pts)))
    cnt = np.bincount(e[:, 0], minlength=len(pts)) + np.bincount(e[:, 1], minlength=len(pts))
    ok = (cnt > 0) & (r > 0.0)
    if not ok.any():
        return {}
    across = 2.0 * r[ok] / (acc[ok] / cnt[ok])
    return {"median": round(float(np.median(across)), 1),
            "p05": round(float(np.percentile(across, 5)), 1),
            "min": round(float(across.min()), 1), "points": int(ok.sum())}


def radius_stats(radius) -> dict:
    r = np.asarray(radius, dtype=float)
    r = r[r > 0.0]
    if len(r) == 0:
        return {}
    return {"min": round(float(r.min()), 6), "p05": round(float(np.percentile(r, 5)), 6),
            "median": round(float(np.median(r)), 6), "max": round(float(r.max()), 6),
            "points": int(len(r))}


def size_caps(passage_radius: dict, *, target: float = PASSAGE_CELLS_ACROSS) -> dict:
    """The largest cells that still put `target` across the passage: the wall band at the
    narrowest passage (5th-percentile radius), the background at the typical one (median),
    and a wall band thick enough to carry the wall size across a narrow passage entirely."""
    p05 = float(passage_radius["p05"])
    med = float(passage_radius["median"])
    return {"wall_cell": 2.0 * p05 / target, "max_cell": 2.0 * med / target,
            "refinement_thickness": 1.1 * p05}


def inside_point(surface):
    """A point inside a closed surface: boundary cell centres nudged along the inward normal,
    tested with vtkSelectEnclosedPoints (a bounding-box centre lies OUTSIDE a bent duct or a
    scroll, and a wrong point flips every chord the radius is read from)."""
    import pyvista as pv
    s = surface.compute_normals(cell_normals=True, point_normals=False,
                                auto_orient_normals=True, consistent_normals=True)
    from scipy.spatial import cKDTree
    centres = np.asarray(s.cell_centers().points, dtype=float)
    normals = np.asarray(s["Normals"], dtype=float)
    sizes = np.sqrt(np.asarray(s.compute_cell_sizes(length=False, volume=False)["Area"]))
    rng = np.random.default_rng(0)
    pick = rng.choice(len(centres), size=min(64, len(centres)), replace=False)
    # both ways off each sampled cell, at several depths: the DEEPEST enclosed candidate wins.
    # A point one cell off the wall is inside but useless - the orientation vote that decides
    # which way the chords run needs a point the wall normals agree about, near the axis.
    cands = np.concatenate([centres[pick] + sgn * k * normals[pick] * sizes[pick, None]
                            for sgn in (-1.0, 1.0) for k in (1.0, 3.0, 10.0, 30.0, 100.0)])
    sel = pv.PolyData(cands).select_enclosed_points(surface, check_surface=False)
    inside = np.asarray(sel["SelectedPoints"]).astype(bool)
    if not inside.any():
        return None
    depth, _ = cKDTree(np.asarray(surface.points, dtype=float)).query(cands[inside])
    return cands[inside][int(np.argmax(depth))]


def interior_from_ports(wall_points, cap_points, port_centroids):
    """A point deep inside the passage, from the ports: each port centroid lies on its branch
    axis at the fluid boundary; step a tenth of the way toward the wall's centroid (into the
    fluid for any duct), then walk away from the nearest boundary point until the distance
    stops growing (the caps count as boundary, so the walk cannot leave through a port). The
    deepest of the per-port results wins. None without ports."""
    from scipy.spatial import cKDTree
    wall = np.asarray(wall_points, dtype=float)
    caps = np.asarray(cap_points, dtype=float).reshape(-1, 3)
    if len(wall) == 0 or len(port_centroids) == 0:
        return None
    tree = cKDTree(np.concatenate([wall, caps]) if len(caps) else wall)
    wc = wall.mean(axis=0)
    best, best_d = None, -1.0
    for c in port_centroids:
        p = np.asarray(c, dtype=float) + 0.1 * (wc - np.asarray(c, dtype=float))
        d, i = tree.query(p)
        for _ in range(40):
            q = p + 0.5 * (p - tree.data[i])
            dq, iq = tree.query(q)
            if dq <= d:
                break
            p, d, i = q, dq, iq
        if d > best_d:
            best, best_d = p, d
    return best


def cavity_skin(points, faces, cap_polys, *, feature_angle: float = 60.0):
    """The part of a staged wall that bounds the FLUID: a hollow solid with flanges stages its
    bore skin, its outer skin and the annular flange faces all as 'wall' (straight_reducer_006:
    8.6 mm between the skins, and every chord stopped there). Split the wall at sharp edges,
    keep the smooth pieces that hold a point of a port cap's rim (the bore skin meets the caps;
    the outer skin meets the flange annulus only). Returns (points, faces) of those pieces, or
    the input unchanged when no piece touches a rim."""
    import pyvista as pv
    from scipy.spatial import cKDTree
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    rims = []
    for cap in cap_polys:
        try:
            edge = cap.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                             manifold_edges=False, non_manifold_edges=False)
            if edge.n_points:
                rims.append(np.asarray(edge.points, dtype=float))
        except Exception:  # noqa: BLE001 - a cap that yields no rim is skipped
            logger.debug("cap rim extraction failed; cap skipped", exc_info=True)
    if not rims or len(f) == 0:
        return pts, f
    rim = np.concatenate(rims)
    poly = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    split = poly.compute_normals(cell_normals=False, point_normals=True, split_vertices=True,
                                 feature_angle=feature_angle, consistent_normals=False,
                                 auto_orient_normals=False)
    conn = split.connectivity(extraction_mode="all")
    cpts = np.asarray(conn.points, dtype=float)
    cf = np.asarray(conn.faces).reshape(-1, 4)[:, 1:]
    region = np.asarray(conn.cell_data["RegionId"], dtype=np.int64)
    b = np.asarray(poly.bounds, dtype=float)
    tol = float(np.linalg.norm(b[1::2] - b[0::2])) * 1e-4
    d, _ = cKDTree(rim).query(cpts)
    touching = d <= tol                                   # points that sit on a cap rim
    keep_regions = np.unique(region[touching[cf].any(axis=1)])
    if len(keep_regions) == 0:
        return pts, f
    keep = np.isin(region, keep_regions)
    return cpts, cf[keep]


def orient_wall_faces(points, faces, port_centroids):
    """The staged wall STL is a set of separately wound CAD faces: after cleaning, each is its own
    connected component, and their windings disagree (production reducers: half outward, half
    inward, mean radial normal 0.0). Every component is flipped or kept by its own majority vote,
    each point judged against the port centroid nearest to it (a point on that point's branch
    axis), so that every face normal points OUT of the fluid. Returns (points, faces) in the
    connectivity filter's order - the faces index THOSE points."""
    import pyvista as pv
    from scipy.spatial import cKDTree
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64).copy()
    if len(f) == 0 or len(port_centroids) == 0:
        return pts, f
    poly = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    conn = poly.connectivity(extraction_mode="all")
    # the connectivity filter RE-ORDERS cells: read regions, normals and faces off its output
    f = np.asarray(conn.faces).reshape(-1, 4)[:, 1:].copy()
    pts = np.asarray(conn.points, dtype=float)
    region = np.asarray(conn.cell_data["RegionId"], dtype=np.int64)
    # cell normals from the winding as it stands (vtk computes them per polygon, not by vote)
    cn = np.asarray(conn.compute_normals(cell_normals=True, point_normals=False,
                                         consistent_normals=False,
                                         auto_orient_normals=False)["Normals"], dtype=float)
    centres = pts[f].mean(axis=1)
    _, near = cKDTree(np.asarray(port_centroids, dtype=float)).query(centres)
    toward = np.asarray(port_centroids, dtype=float)[near] - centres
    inward = np.einsum("ij,ij->i", cn, toward) > 0.0        # normal points toward the fluid
    for rid in np.unique(region):
        cells = region == rid
        if inward[cells].mean() > 0.5:                        # majority inward: flip the piece
            f[cells, 1], f[cells, 2] = f[cells, 2].copy(), f[cells, 1].copy()
    return pts, f


def passage_of_surface(points, faces) -> dict:
    """Local radius (half the inward chord to the opposite wall, engines/vmtk/lumen_staging)
    at every point of a closed triangulated boundary, and the cells-across it implies."""
    import pyvista as pv

    from meshpipeline.engines.vmtk.lumen_staging import local_radius
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    surf = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    interior = inside_point(surf)
    if interior is None:
        return {}
    diag = float(np.linalg.norm(np.asarray(surf.bounds[1::2]) - np.asarray(surf.bounds[0::2])))
    r = local_radius(pts, f, interior, diag * 1e-5, diag / 2.0)
    return {"passage_radius": radius_stats(r),
            "passage_cells_across_local": measure_passage(pts, f, r)}


def _triangles(poly):
    # the staged STLs share their rim points only approximately: merge within 1e-5 of the diagonal
    # so the caps close the wall (an open rim leaks every chord that runs through it)
    b = np.asarray(poly.bounds, dtype=float)
    diag = float(np.linalg.norm(b[1::2] - b[0::2])) or 1.0
    poly = poly.triangulate().clean(tolerance=diag * 1e-5, absolute=True)
    faces = np.asarray(poly.faces).reshape(-1, 4)[:, 1:]
    return np.asarray(poly.points, dtype=float), faces


def passage_of_polymesh(workspace) -> dict:
    """The passage measure of the polyMesh under <workspace>/constant, read through a
    scratch case directory of symlinks (the reader wants a .foam stub beside `constant`).
    {} when anything is missing - a measurement never loses a finished mesh."""
    import pyvista as pv
    ws = Path(workspace)
    if not (ws / "constant" / "polyMesh" / "owner").exists():
        return {}
    try:
        with tempfile.TemporaryDirectory(prefix="passage-") as case:
            os.symlink(str(ws / "constant"), os.path.join(case, "constant"))
            foam = os.path.join(case, "case.foam")
            open(foam, "a").close()
            rd = pv.OpenFOAMReader(foam)
            mb = rd.read()
            grid = mb["internalMesh"] if "internalMesh" in mb.keys() else mb[0]
            if not isinstance(grid, pv.DataSet):
                return {}
            pts, faces = _triangles(grid.extract_surface())
            return passage_of_surface(pts, faces)
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("passage measure of the polyMesh failed", exc_info=True)
        return {}


def passage_of_stls(paths, *, interior_point=None, cap_paths=(), port_centroids=()) -> dict:
    """The radius statistics of the staged WALL (open at the ports) BEFORE meshing, for sizing,
    read the way VMTK's staging reads them (a ray leaving through a port borrows its neighbour).
    The chords are oriented from a point deep in the cavity found from the port centroids
    (interior_from_ports); the tessellation's own interior point is NOT trusted - for a hollow
    wall solid it sits inside the wall material, a wall thickness off the cavity surface, and
    the orientation vote from there is a coin toss (production read a 6 mm radius on a 173 mm
    bore and cartesianMesh was killed at 1 mm cells). The staged caps do not stitch to the wall
    rim exactly, so a merged 'closed' surface is only the last resort. {} when nothing reads."""
    import pyvista as pv

    from meshpipeline.engines.vmtk.lumen_staging import local_radius
    try:
        parts = [pv.read(str(p)) for p in paths if Path(p).exists()]
        if not parts:
            return {}
        merged = parts[0].merge(parts[1:]) if len(parts) > 1 else parts[0]
        pts, faces = _triangles(merged.extract_surface())
        caps = [pv.read(str(p)) for p in cap_paths if Path(p).exists()]
        cap_pts = (np.concatenate([np.asarray(c.points, dtype=float) for c in caps])
                   if caps else np.zeros((0, 3)))
        if caps:
            pts, faces = cavity_skin(pts, faces, caps)
        deep = interior_from_ports(pts, cap_pts, list(port_centroids)) if len(port_centroids) else None
        if deep is None and interior_point is not None:
            deep = np.asarray(interior_point, dtype=float)
        if deep is None:
            return passage_of_surface(pts, faces).get("passage_radius") or {}
        if len(port_centroids):
            pts, faces = orient_wall_faces(pts, faces, list(port_centroids))
        b = np.asarray(merged.bounds, dtype=float)
        diag = float(np.linalg.norm(b[1::2] - b[0::2]))
        r = local_radius(pts, faces, deep, diag * 1e-5, diag / 2.0)
        return radius_stats(r)
    except Exception:  # noqa: BLE001 - sizing aid, not a verdict
        logger.warning("passage radius of the staged surface failed", exc_info=True)
        return {}


__all__ = ["PASSAGE_CELLS_ACROSS", "PASSAGE_FLOOR_CELLS", "cavity_skin", "inside_point",
           "interior_from_ports", "measure_passage", "orient_wall_faces", "passage_of_polymesh",
           "passage_of_stls", "passage_of_surface", "radius_stats", "size_caps"]
