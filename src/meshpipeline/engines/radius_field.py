# Responsibility: The local-radius field of a closed or port-open wall - half the inward chord across the fluid at
# every point, floored within its own reach and smoothed once - which VMTK, gmsh and cfMesh all size and measure from.
# Boundaries: engine-neutral geometry on points and triangles; it knows no engine, no mesher and no gate.
from __future__ import annotations

import math

import numpy as np

#: After the ray cast, every point takes the SMALLEST radius found within this many times its
#: own radius. A ray along the normal reads the gap in one direction only: on a wide flat duct
#: (transition_013, 545 x 88 mm) the top and bottom walls read the 88 mm gap while the 88 mm-tall
#: side walls look across the 545 mm width and read a radius four times larger, so the remesh
#: graded 13 mm cells into 55 mm ones over a strip two cells tall. The gap a wall sees must
#: govern the walls beside it: a point claiming radius R has no narrower gap within R of it. A
#: ball in metres, not mesh rings - the staged wall is as coarse as MAX_EDGE_FRACTION allows.
RADIUS_MIN_REACH = 1.0

#: A chord reads the passage only when it lands on a wall that faces back across it: the hit
#: face's normal within 45 degrees of the ray (a diffuser's walls part by less; so do a
#: pipe's, a duct's, a gap's). A ray that meets a wall at a glance runs along the passage,
#: not across it: into a corner (a blade root leaning over the hub), or into a wall curving
#: across its path (on blade_row_passage_006 blade rays running sideways met the shroud at
#: 57 degrees after 47-52 mm of a 62 mm passage). One that meets a wall from behind went into
#: the wall material. None is a reading. The ball-min hands the smallest reading to every
#: point within its reach, so no bad reading stays local: on blade_row_passage_001 60 of
#: 27,054 wall points read under 5 mm (57 of them folds, see FOLD_REACH) and put its
#: narrowest wall at 0.0 cells across, median 15.7 (2026-09-29). At 60 degrees two of the six
#: blade rows still read 64% and 76% of their hub-to-shroud gap; at 45 all six read it to 0.3%.
CHORD_MIN_FACING = 0.7

#: A hit on the point's own neighbourhood (see _first_chord) is its face folding over only
#: when it is this close: within this fraction of the mean edge length at the point. The
#: folds on blade_row_passage_001 landed within 0.19 of an edge; a far wall one or two cells
#: away - a passage the mesh barely spans, which the resolution floor exists to refuse - also
#: shares vertices with the point's triangles on so thin a passage, and must still be read.
FOLD_REACH = 0.5


def local_radius(points: np.ndarray, faces: np.ndarray, interior_point, r_lo: float,
                 r_hi: float, *, oriented: bool = False,
                 read_sharp_edges: bool = False) -> np.ndarray:
    """The lumen's local radius at every wall point: half the chord from the point along its
    INWARD normal to the opposite wall, median-filtered over the 1-ring, clipped to
    [r_lo, r_hi], floored to the smallest radius within RADIUS_MIN_REACH of its own, and
    smoothed once over the 1-ring. Rays that leave through an open port borrow the nearest
    measured value, and so do rays that read no passage at all (see _first_chord).
    With `read_sharp_edges` the points on sharp edges are also read along each adjoining
    face's own normal (see below) - VMTK's staging, which sizes cells from this field, asks
    for it; the passage measures beside the other engines' meshes read as before.

    WHY NOT vmtk's centerline: vmtkcenterlines traces a steepest descent on the Voronoi diagram
    of the surface, and on a uniformly remeshed straight run the diagram is degenerate - the
    same radius everywhere, co-spherical points - so the descent stalls ("Degenerate descent
    detected. Target not reached", tee_wye_003 and manifold_002 on every clean surface tried,
    2026-09-11) and vmtkmeshgenerator then crashes on the nonsense sizing field. A chord along
    the normal needs no diagram, no seeds and no luck; the generator only needs a size per point.

    `oriented`: the faces are already wound with their normals OUT of the fluid, piece by piece
    (engines/passage.orient_fluid_boundary), so no vote against the interior point is taken - with
    an obstacle in the flow (a tube bank's tubes) the point-count vote can turn every piece the
    wrong way."""
    import pyvista as pv
    import vtk
    mesh = pv.PolyData(np.asarray(points, dtype=float),
                       np.hstack([np.full((len(faces), 1), 3, dtype=np.int64),
                                  np.asarray(faces, dtype=np.int64)]).ravel())
    # cell normals from the same filter: its consistency pass may rewind cells, and the facing
    # test below must judge each hit face in the orientation the point normals were built in
    # (splitting stays off, so the cells keep their order and the OBB tree's ids index them)
    m = mesh.compute_normals(cell_normals=True, point_normals=True, consistent_normals=True,
                             auto_orient_normals=False, flip_normals=False)
    n = np.asarray(m.point_data["Normals"], dtype=float)
    fn = np.asarray(m.cell_data["Normals"], dtype=float)
    pts = np.asarray(m.points, dtype=float)
    # the sheet is consistently oriented, so one majority vote against the interior point
    # decides whether its normals point into the fluid or away from it
    if not oriented and (np.einsum("ij,ij->i", n, np.asarray(interior_point, dtype=float) - pts)
                         > 0).mean() > 0.5:
        n = -n
        fn = -fn
    obb = vtk.vtkOBBTree()
    obb.SetDataSet(mesh)
    obb.BuildLocator()
    reach = 4.0 * r_hi
    hits = vtk.vtkPoints()
    ids = vtk.vtkIdList()
    f = np.asarray(faces, dtype=np.int64)
    ring = _vertex_rings(f, len(pts))
    fold = FOLD_REACH * _mean_edge(pts, f)
    r = np.full(len(pts), np.nan)
    first = np.full(len(pts), np.nan)
    eps = 1e-3 * r_lo
    for i in range(len(pts)):
        hits.Reset()
        ids.Reset()
        if obb.IntersectWithLine(pts[i] - n[i] * eps, pts[i] - n[i] * reach, hits, ids) \
                and hits.GetNumberOfPoints():
            first[i], r[i] = _first_chord(pts[i], -n[i], hits, ids, f, fn, ring[i], fold[i])
    # SHARP EDGES ARE READ ALONG EACH SIDE'S OWN NORMAL. A point on a sharp edge has an averaged
    # normal that points along neither face, and a SHORT narrow passage has no other points:
    # an orifice bore 9 mm long, a sharp throat, a step's lip carry vertices only on their two
    # rims, every one of them on a 90-degree edge with the plate, its ray running diagonally
    # into the pipe. venturi_orifice_003 (a 0.35 orifice in a 290 mm pipe) read 145 mm - the
    # pipe - at every point, and the orifice was meshed 4.7 cells across while the gate,
    # reading the same field, said 13. Each face meeting such an edge is read from its centroid
    # along its own normal, and the edge point keeps the narrowest valid reading.
    sharp = _sharp_points(f, fn, len(pts)) if read_sharp_edges else np.zeros(len(pts), bool)
    if sharp.any():
        cent = pts[f].mean(axis=1)
        fold_f = fold[f].mean(axis=1)
        face_r = np.full(len(f), np.nan)
        # ...BUT ONLY A CHORD ACROSS OPEN FLUID IS A PASSAGE. Two faces of a shallow groove or a
        # step recess face each other too: the rocket nozzle's 0.5 mm deep, 3.2 mm wide groove
        # read a 1.6 mm 'radius' that the ball-min below spread over the whole 38 mm bore - the
        # field sat on its floor everywhere, the fill grew from 180 k to 2.5 M cells and TetGen
        # failed on the STEP. A chord reads the passage when the fluid is open around its middle:
        # the wall stands at least CHORD_OPEN_FRACTION of the half-chord away from the midpoint
        # (an orifice bore's midpoint is on its axis, a full half-chord from the wall; a shallow
        # groove's is within its depth of the groove floor).
        loc = vtk.vtkStaticCellLocator()
        loc.SetDataSet(mesh)
        loc.BuildLocator()
        cp = [0.0, 0.0, 0.0]
        cid, sid, d2 = vtk.reference(0), vtk.reference(0), vtk.reference(0.0)
        for t in np.flatnonzero(sharp[f].any(axis=1)):
            hits.Reset()
            ids.Reset()
            if obb.IntersectWithLine(cent[t] - fn[t] * eps, cent[t] - fn[t] * reach, hits, ids) \
                    and hits.GetNumberOfPoints():
                half = _first_chord(cent[t], -fn[t], hits, ids, f, fn,
                                    frozenset(f[t].tolist()), fold_f[t])[1]
                if not np.isfinite(half):
                    continue
                mid = cent[t] - fn[t] * half
                loc.FindClosestPoint(mid.tolist(), cp, cid, sid, d2)
                if math.sqrt(float(d2)) >= CHORD_OPEN_FRACTION * half:
                    face_r[t] = half
        for k in range(3):
            on = sharp[f[:, k]] & ~np.isnan(face_r)
            np.fmin.at(r, f[on, k], face_r[on])
    if np.isnan(r).all() and not np.isnan(first).all():
        # nothing read as a passage (a surface wound against its own normals?): the raw first
        # hits, as this field read before the chords were judged, rather than r_lo everywhere
        r = first
    # A gap is read by a patch of wall, never by one point: each reading becomes the median of
    # the readings around it (_ring_median). A lone ray that clips a leaning leading edge
    # (46.6 mm under blade_row_passage_001's shroud, every neighbour 92 mm) goes; a narrow
    # passage, read by every point along both of its walls, stays. Before the borrow below, so
    # a lone reading cannot first hand itself to its unread neighbours.
    r = _ring_median(r, ring)
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
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    acc = (np.bincount(e[:, 0], weights=r[e[:, 1]], minlength=len(pts))
           + np.bincount(e[:, 1], weights=r[e[:, 0]], minlength=len(pts)))
    cnt = np.bincount(e[:, 0], minlength=len(pts)) + np.bincount(e[:, 1], minlength=len(pts))
    nb = np.where(cnt > 0, acc / np.maximum(cnt, 1), r)
    return 0.5 * r + 0.5 * nb


#: Two faces meeting at a point turn by more than this (cosine 0.7, about 45 degrees) make it a
#: sharp-edge point, read along each face's own normal as well (see local_radius).
SHARP_EDGE_COS = 0.7
#: A sharp-edge face chord counts only when the wall stands at least this share of the
#: half-chord away from the chord midpoint - open fluid, not a groove floor (see local_radius).
CHORD_OPEN_FRACTION = 0.6


def _sharp_points(faces: np.ndarray, face_normals: np.ndarray, n_points: int) -> np.ndarray:
    """Points where two of the faces meeting there turn by more than SHARP_EDGE_COS."""
    out = np.zeros(n_points, dtype=bool)
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    owner = np.tile(np.arange(len(faces)), 3)
    key = np.sort(e, axis=1)
    order = np.lexsort((key[:, 1], key[:, 0]))
    k, o = key[order], owner[order]
    same = np.all(k[1:] == k[:-1], axis=1)
    a, b = o[:-1][same], o[1:][same]
    turn = np.einsum("ij,ij->i", face_normals[a], face_normals[b]) < SHARP_EDGE_COS
    out[k[:-1][same][turn].ravel()] = True
    return out


def _mean_edge(pts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """The mean length of the triangle edges meeting each point (0 where none does)."""
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
    acc = (np.bincount(e[:, 0], weights=length, minlength=len(pts))
           + np.bincount(e[:, 1], weights=length, minlength=len(pts)))
    cnt = np.bincount(e[:, 0], minlength=len(pts)) + np.bincount(e[:, 1], minlength=len(pts))
    return acc / np.maximum(cnt, 1)


def _vertex_rings(faces: np.ndarray, n_points: int) -> list[frozenset]:
    """Each point's own vertex and every vertex of the triangles it belongs to."""
    rings: list[set] = [{i} for i in range(n_points)]
    for a, b, c in faces.tolist():
        rings[a].update((b, c))
        rings[b].update((a, c))
        rings[c].update((a, b))
    return [frozenset(s) for s in rings]


def _ring_median(r: np.ndarray, ring: list[frozenset]) -> np.ndarray:
    """Each read (non-NaN) value becomes the median over its ring, the upper one of an even
    count, with an unread point voting as no gap at all: a narrow reading needs more than half
    of its ring to read it too. One that its ring outvotes is dropped (NaN) and borrows later,
    like the points that read nothing; so is a pair of corner readings whose other neighbours
    read nothing (four such pairs under blade_row_passage_006's shroud read a 15 mm chord in a
    62 mm passage). When no reading survives, the readings stand as they were."""
    out = np.full(len(r), np.nan)
    for i in np.flatnonzero(~np.isnan(r)):
        v = r[list(ring[i])]
        v = np.sort(np.where(np.isnan(v), np.inf, v))
        m = float(v[len(v) // 2])
        if math.isfinite(m):
            out[i] = m
    return r.copy() if np.isnan(out).all() else out


def _first_chord(origin, direction, hits, ids, faces, face_normals, ring,
                 fold: float) -> tuple[float, float]:
    """(first hit, reading) along one inward ray, each as half the chord; NaN where none.
    The OBB tree returns the hits in order along the ray.

    The reading is the first hit that is not the point's own face folding over and faces back
    along the ray (CHORD_MIN_FACING); a grazing or from-behind hit ends the ray unread.

    A fold is a hit closer than `fold` on a triangle that shares a vertex with a triangle of
    the point. A polyMesh boundary is polygons fanned from their first vertex, and a snapped
    polygon carries hanging nodes a hair off its edges: the fan folds, and a ray from such a
    node lands on its own polygon's other half microns away (57 of the 60 near-zero chords on
    blade_row_passage_001's snappy wall, along the knife-edge trailing edges and the blade
    roots, 2026-09-29). Every fan triangle of a polygon holds its first vertex, so the siblings
    of any triangle of the point share a vertex with it; a tessellated CAD surface folds the
    same way at a sliver."""
    o = tuple(float(x) for x in origin)
    first = 0.5 * math.dist(hits.GetPoint(0), o)
    for j in range(hits.GetNumberOfPoints()):
        c = ids.GetId(j)
        d = math.dist(hits.GetPoint(j), o)
        if d < fold and not ring.isdisjoint(faces[c].tolist()):
            continue
        if float(np.dot(direction, face_normals[c])) < CHORD_MIN_FACING:
            return first, float("nan")
        return first, 0.5 * d
    return first, float("nan")


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


__all__ = ["CHORD_MIN_FACING", "FOLD_REACH", "RADIUS_MIN_REACH", "local_radius"]
