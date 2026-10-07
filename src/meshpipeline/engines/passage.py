# Responsibility: Measure how many cells span the flow passage of an internal-flow mesh, and cap sizes so the industry floor is met.
# Boundaries: engine-neutral geometry - reads a closed boundary surface or a polyMesh, judges nothing; each engine's gate judges.
# Collaborates with: cfmesh (size caps in render_cfmesh_case, the measure beside the mesh in native.py), snappy (the measure
# beside the mesh), gmsh (its driver carries the same measure), and every flow engine's resolution_floor gate.
from __future__ import annotations

import contextlib
import logging
import signal
import threading
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
#: The most cells across the narrowest passage a wall band or a refinement box may ask for:
#: the top of industry practice. Without it the builder refined a tee to 79 across, a 16.4 M
#: hex fill that ran cartesianMesh to the edge of its budget, wrote an 886 MB deliverable and
#: held the mesh service for 161 minutes (2026-09-12); at 40 across the same tee is ~2 M.
PASSAGE_CEILING_CELLS = 40

#: The chord loop casts one ray per boundary point (about 0.25 ms each). A production fill can
#: put several hundred thousand points on its boundary, so above this many the surface is
#: DECIMATED for the chords and the radius is mapped back; the cells-across figure is still
#: taken at every real boundary point, against the real boundary edges.
MAX_MEASURE_POINTS = 60_000

#: The measure is evidence beside a finished mesh, never worth more than a few minutes of the
#: run's budget: past this it is abandoned and the mesh ships without it (the gate then does
#: not judge resolution). Before this cap the measure read the WHOLE volume through a VTK
#: OpenFOAM reader with no limit at all, and a 0.8 M-cell cut-cell fill spent 17 minutes there
#: after a 13-second cartesianMesh (Cloud Run execution f5r99, 2026-09-12); a bigger fill sat
#: for 161 minutes and held the mesh service's capacity while every other run queued behind it.
PASSAGE_MEASURE_BUDGET_S = 240.0


class MeasureOverdue(Exception):
    """The passage measure ran past its budget."""


@contextlib.contextmanager
def measure_deadline(seconds: float):
    """Raise MeasureOverdue inside the block after `seconds` of wall time. SIGALRM, so only in
    the main thread on a POSIX host; elsewhere the block simply runs unbounded."""
    usable = (seconds and seconds > 0 and hasattr(signal, "SIGALRM")
              and threading.current_thread() is threading.main_thread())
    if not usable:
        yield
        return

    def _overdue(signum, frame):
        raise MeasureOverdue(f"passage measure past its {seconds:.0f} s budget")
    previous = signal.signal(signal.SIGALRM, _overdue)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def triangle_edges(faces) -> np.ndarray:
    """The three edges of every triangle, as (k, 2) point ids (an edge two triangles share is
    listed twice, so each point's mean weighs its faces alike)."""
    f = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    return np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])


def polygon_edges(polys) -> np.ndarray:
    """The edges a mesh's boundary polygons really have, as (k, 2) point ids: each polygon's
    sides, never the diagonals a fan triangulation adds inside it."""
    rows = [np.stack([np.asarray(p, dtype=np.int64), np.roll(np.asarray(p, dtype=np.int64), -1)],
                     axis=1) for p in polys if len(p) >= 3]
    return np.concatenate(rows) if rows else np.zeros((0, 2), dtype=np.int64)


def mean_edge(points, edges) -> np.ndarray:
    """The mean length of the edges meeting each point (0 where none does)."""
    pts = np.asarray(points, dtype=float)
    e = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    length = np.linalg.norm(pts[e[:, 1]] - pts[e[:, 0]], axis=1)
    acc = (np.bincount(e[:, 0], weights=length, minlength=len(pts))
           + np.bincount(e[:, 1], weights=length, minlength=len(pts)))
    cnt = np.bincount(e[:, 0], minlength=len(pts)) + np.bincount(e[:, 1], minlength=len(pts))
    return np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0)


def measure_passage(points, faces, radius, *, edges=None) -> dict:
    """Cells across the passage at every boundary point: twice the local radius over the mean
    length of the boundary edges that meet the point (the surface cell is what the volume is
    cut to beside it). Median, 5th percentile (the narrowest wall, less a few outliers), min.

    `edges` are the boundary's REAL edges when the triangles are a fan of polygons (a polyMesh
    boundary, see boundary_triangles_of_polymesh): a fan adds a diagonal inside every quad, and
    counting it as a cell edge read a hex wall 12-14% coarser than it is - three cfMesh elbows
    with 12.7, 10.8 and 20.7 cells across at the narrowest wall read 11.2, 9.5 and 18.3, and the
    first was refused as under 12 (2026-10-04). Without `edges` the triangles' own edges are
    used, which IS the cell edge on a triangulated wall (VMTK, gmsh, a staged surface)."""
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    r = np.asarray(radius, dtype=float)
    if len(f) == 0 or len(pts) == 0:
        return {}
    h = mean_edge(pts, triangle_edges(f) if edges is None else edges)
    ok = (h > 0.0) & (r > 0.0)
    if not ok.any():
        return {}
    across = 2.0 * r[ok] / h[ok]
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


def port_radius_stats(openings, hydraulic: dict | None = None) -> dict:
    """Radii of the bound openings: the passage radius AT the ports, always readable, the
    yardstick for the chord reading. Half the lid's hydraulic diameter where `hydraulic` has it
    (lid_hydraulic_diameters - an annulus is its gap, not its bore), else the equivalent radius
    sqrt(area / pi). {} without areas."""
    radii = []
    for name, rec in (openings or {}).items():
        if not isinstance(rec, dict):
            continue
        area = (rec.get("opening") or {}).get("area") if isinstance(rec.get("opening"), dict) else None
        if area is None:
            area = rec.get("area")
        if area is None:
            continue
        try:
            a = float(area)
        except (TypeError, ValueError):
            continue
        if a > 0.0:
            r_eq = float(np.sqrt(a / np.pi))
            # the lid only ever NARROWS the reading: a wall solid's lid can be its whole end
            # (flange and all, wider than the bore the opening area measures)
            dh = (hydraulic or {}).get(str(name))
            radii.append(min(r_eq, 0.5 * float(dh)) if dh else r_eq)
    if not radii:
        return {}
    r = np.asarray(radii, dtype=float)
    return {"min": round(float(r.min()), 6), "p05": round(float(r.min()), 6),
            "median": round(float(np.median(r)), 6), "max": round(float(r.max()), 6),
            "points": int(len(r)), "source": "ports"}


def plausible_radius(chord: dict, ports: dict, *, low: float = 0.3, high: float = 1.5) -> bool:
    """A chord reading is trusted only when its 5th percentile sits within low..high times the
    smallest port radius: a reading off the outer skin (straight_reducer_015: 210 mm on a
    ~100 mm bore) or the wall thickness (006: 4 mm) is not the passage."""
    try:
        c, p = float(chord["p05"]), float(ports["p05"])
    except (KeyError, TypeError, ValueError):
        return False
    return p > 0.0 and low * p <= c <= high * p


def vouched_by_ports(chord: dict, ports: dict, *, low: float = 0.3, high: float = 1.5) -> bool:
    """plausible_radius for a part whose ports differ in size: the chord reading's 5th percentile
    sits between low x the SMALLEST port radius and high x the LARGEST. A reading off a hollow
    part's outer skin is still wider than every port it serves, and one across the wall metal
    still thinner than every port; but a vessel with a 20 mm inlet and a 1.6 mm side branch
    reads its 5th percentile in its many small branches - a true reading that the smallest
    port's own band (0.24 - 1.2 mm) refused, and the aorta lost its local refinement with it."""
    try:
        c, lo, hi = float(chord["p05"]), float(ports["min"]), float(ports["max"])
    except (KeyError, TypeError, ValueError):
        return False
    return lo > 0.0 and low * lo <= c <= high * max(hi, lo)


def choose_passage_radius(chord: dict | None, ports: dict | None) -> dict | None:
    """The radius statistics the size caps use: the chord reading when the ports vouch for it,
    the port figures otherwise, None without either."""
    chord = chord or {}
    ports = ports or {}
    if chord and ports and plausible_radius(chord, ports):
        return {**chord, "source": "chord"}
    if chord and not ports:
        return {**chord, "source": "chord-unchecked"}
    if ports:
        return ports
    return None


def declared_hydraulic_diameters(intake_patches) -> dict:
    """{port name: hydraulic diameter in metres} of every sized declared inlet/outlet: the bore
    of a round port, D - d of an annulus, 2wh / (w + h) of a rectangle. The intake states these
    in millimetres. A port declared by area alone has no perimeter, so it is left out."""
    from meshpipeline.engines.port_binding import DeclaredPatch
    out: dict = {}
    for p in intake_patches or []:
        if not isinstance(p, dict) or str(p.get("type") or "") not in ("inlet", "outlet"):
            continue
        try:
            dp = DeclaredPatch.from_intake(p)
        except (KeyError, TypeError, ValueError):
            continue
        d, inner = dp.diameter_mm, dp.inner_diameter_mm
        dh = None
        if d is not None and d > 0:
            dh = d - inner if inner is not None and 0.0 < inner < d else d
        elif dp.width_mm and dp.height_mm and dp.width_mm > 0 and dp.height_mm > 0:
            dh = 2.0 * dp.width_mm * dp.height_mm / (dp.width_mm + dp.height_mm)
        if dh and dh > 0 and p.get("name"):
            out[str(p["name"])] = float(dh) / 1000.0
    return out


def declared_port_half_width(intake_patches) -> float | None:
    """Half the narrowest width the flow crosses at a DECLARED port, in metres: half a bore,
    half a rectangle's SHORT side, half an annulus's radial gap, the equivalent radius of a port
    declared by area only. None without a sized port. The intake states these in millimetres."""
    from meshpipeline.engines.port_binding import DeclaredPatch
    best: float | None = None
    for p in intake_patches or []:
        if not isinstance(p, dict) or str(p.get("type") or "") not in ("inlet", "outlet"):
            continue
        try:
            dp = DeclaredPatch.from_intake(p)
        except (KeyError, TypeError, ValueError):
            continue
        width = None
        d, inner = dp.diameter_mm, dp.inner_diameter_mm
        if d is not None and d > 0:
            width = (d - inner) / 2.0 if inner is not None and 0.0 < inner < d else d
        elif dp.width_mm and dp.height_mm and dp.width_mm > 0 and dp.height_mm > 0:
            width = min(dp.width_mm, dp.height_mm)
        elif dp.area_mm2 and dp.area_mm2 > 0:
            width = 2.0 * float(np.sqrt(dp.area_mm2 / np.pi))
        if width is not None and width > 0 and (best is None or width < best):
            best = float(width)
    return best / 2000.0 if best is not None else None


def size_caps(passage_radius: dict, *, target: float = PASSAGE_CELLS_ACROSS,
              ceiling: float = PASSAGE_CEILING_CELLS) -> dict:
    """The largest cells that still put `target` across the passage: the wall band at the
    band radius (field_radius_stats; the narrowest passage, the 5th-percentile radius, when the
    statistics carry none - and then nothing narrower is refined locally), the background at the
    typical one (median), and a wall band thick enough to carry the wall size across a band-wide
    passage entirely. Also the SMALLEST wall cell worth cutting, `wall_cell_floor`: `ceiling`
    across the narrowest passage, the top of industry practice."""
    p05 = float(passage_radius["p05"])
    med = float(passage_radius["median"])
    band = float(passage_radius.get("band") or p05)
    return {"wall_cell": 2.0 * band / target, "max_cell": 2.0 * max(med, band) / target,
            "refinement_thickness": 1.1 * band, "wall_cell_floor": 2.0 * p05 / ceiling}


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
    # cell normals straight from the winding (numpy cross products: vtkPolyDataNormals was
    # not deterministic here - a third of a piece came back flipped on some runs)
    tri = pts[f]
    cn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    cn /= np.linalg.norm(cn, axis=1, keepdims=True).clip(1e-30)
    centres = tri.mean(axis=1)
    _, near = cKDTree(np.asarray(port_centroids, dtype=float)).query(centres)
    toward = np.asarray(port_centroids, dtype=float)[near] - centres
    inward = np.einsum("ij,ij->i", cn, toward) > 0.0        # normal points toward the fluid
    for rid in np.unique(region):
        cells = region == rid
        if inward[cells].mean() > 0.5:                        # majority inward: flip the piece
            f[cells, 1], f[cells, 2] = f[cells, 2].copy(), f[cells, 1].copy()
    return pts, f


def passage_of_surface(points, faces, *, max_points: int = MAX_MEASURE_POINTS,
                       wall=None, edges=None) -> dict:
    """Local radius (half the inward chord to the opposite wall, engines/vmtk/lumen_staging)
    at every point of a closed triangulated boundary, and the cells-across it implies.

    `wall`, when given as (points, faces), is the WALL part of that boundary: the chords are
    cast at its points and against its faces only, and the cells-across figure is taken
    there. The full closed boundary is still what locates the interior point. Port caps are
    not walls: on a cut-cell fill a cap can be a stepped sheet of tiny faces, and a chord
    cast from one step hits the next step a couple of millimetres away - the elbow
    bend_elbow_014 read 0.5 cells across at its narrowest 'wall' that way (99% of its cap
    points at a 2 mm chord) while every wall point read 28 (2026-09-13). A wall chord that
    leaves through an open port misses and borrows its neighbour's radius, as it does for
    VMTK's staged lumen.

    Above `max_points` points the chords are cast on a decimated copy of the surface
    (vtkQuadricDecimation keeps the shape; the radius varies over metres, not over one cell)
    and each real point takes the radius of its nearest decimated point. The cells-across
    figure is always measured on the real boundary, against the real boundary edges: `edges`
    (in the numbering of the points measured - the wall's when `wall` is given) when the
    triangles are a fan of polygons, which measure_passage explains."""
    import pyvista as pv

    from meshpipeline.engines.vmtk.lumen_staging import local_radius
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    surf = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    interior = inside_point(surf)
    if interior is None:
        return {}
    diag = float(np.linalg.norm(np.asarray(surf.bounds[1::2]) - np.asarray(surf.bounds[0::2])))
    if wall is not None:
        pts = np.asarray(wall[0], dtype=float)
        f = np.asarray(wall[1], dtype=np.int64)
        if len(f) < 4:
            return {}
        surf = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    if max_points and len(pts) > max_points:
        from scipy.spatial import cKDTree
        coarse = surf.decimate(1.0 - max_points / float(len(pts))).clean()
        cpts = np.asarray(coarse.points, dtype=float)
        cf = np.asarray(coarse.faces).reshape(-1, 4)[:, 1:]
        if len(cf) < 4:
            return {}
        r = local_radius(cpts, cf, interior, diag * 1e-5, diag / 2.0)
        r = r[cKDTree(cpts).query(pts)[1]]
        logger.info("passage measure: %d boundary points, chords cast on %d", len(pts), len(cpts))
    else:
        r = local_radius(pts, f, interior, diag * 1e-5, diag / 2.0)
    out = {"passage_radius": radius_stats(r),
           "passage_cells_across_local": measure_passage(pts, f, r, edges=edges)}
    local = out["passage_cells_across_local"]
    if local and float(local.get("p05") or 0.0) < PASSAGE_FLOOR_CELLS:
        try:
            h = mean_edge(pts, triangle_edges(f) if edges is None else edges)
            regions = under_floor_regions(pts, r, h)
            if regions:
                out["passage_under_floor"] = regions
        except Exception:  # noqa: BLE001 - where it fell short is a retry's aid, never a verdict
            logger.warning("under-floor regions of the passage measure failed", exc_info=True)
    return out


#: The wall points under the floor are boxed in at most this many groups (under_floor_regions).
UNDER_FLOOR_MAX_REGIONS = 128


def under_floor_regions(points, radius, edge, *, floor: float = PASSAGE_FLOOR_CELLS,
                        max_regions: int = UNDER_FLOOR_MAX_REGIONS) -> dict:
    """WHERE a finished mesh's wall falls under the floor, as boxes a retry can refine there:
    the wall points reading under `floor` cells across, grouped in bins about one passage wide
    (coarser while there are more than `max_regions` groups), each boxed tight round its points
    (two of their cells out). A narrow branch's box spans the branch, wall to wall; a wider
    passage's wall that reads a narrow neighbour's radius (the measure floors every point to the
    narrowest within its reach - the trunk round a branch's mouth) gets a box hugging that wall,
    not its whole passage. {"points": wall points measured, "regions": [{min, max (metres),
    radius_m (the passage there, median), edge_m (the cell at the wall there, median), cells
    (5th percentile there), points}]}; {} when no point is under. Measured on the mesh, so it
    names the passages the gate measured - not the ones a reading of the staged wall expected."""
    pts = np.asarray(points, dtype=float)
    r = np.asarray(radius, dtype=float)
    h = np.asarray(edge, dtype=float)
    ok = (h > 0.0) & (r > 0.0) & np.isfinite(r)
    cells = np.where(ok, 2.0 * r / np.maximum(h, 1e-300), np.inf)
    under = ok & (cells < float(floor))
    if not under.any():
        return {}
    P, R, H, C = pts[under], r[under], h[under], cells[under]
    size = max(3.0 * float(np.median(H)), 2.0 * float(np.median(R)))
    groups: list[np.ndarray] = []
    for _ in range(40):
        keys = np.floor(P / size).astype(np.int64)
        _, lab = np.unique(keys, axis=0, return_inverse=True)
        lab = np.asarray(lab).ravel()
        groups = [np.flatnonzero(lab == g) for g in range(int(lab.max()) + 1)]
        if len(groups) <= max_regions:
            break
        size *= 1.5
    regions = []
    for idx in sorted(groups, key=len, reverse=True):
        pad = 2.0 * float(H[idx].max())
        regions.append({"min": [round(float(v), 6) for v in P[idx].min(axis=0) - pad],
                        "max": [round(float(v), 6) for v in P[idx].max(axis=0) + pad],
                        "radius_m": round(float(np.median(R[idx])), 7),
                        "edge_m": round(float(np.median(H[idx])), 7),
                        "cells": round(float(np.percentile(C[idx], 5)), 2),
                        "points": int(len(idx))})
    return {"points": int(ok.sum()), "regions": regions}


def _triangles(poly):
    # the staged STLs share their rim points only approximately: merge within 1e-5 of the diagonal
    # so the caps close the wall (an open rim leaks every chord that runs through it)
    b = np.asarray(poly.bounds, dtype=float)
    diag = float(np.linalg.norm(b[1::2] - b[0::2])) or 1.0
    poly = poly.triangulate().clean(tolerance=diag * 1e-5, absolute=True)
    faces = np.asarray(poly.faces).reshape(-1, 4)[:, 1:]
    return np.asarray(poly.points, dtype=float), faces


#: OpenFOAM patch types that are a wall of the flow passage (everything else - `patch` ports,
#: `empty`, `symmetry` - is an opening or a plane the flow crosses, not a wall).
WALL_PATCH_TYPES = frozenset({"wall"})


def boundary_triangles_of_polymesh(polymesh, *, wall_only: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """The boundary of an OpenFOAM polyMesh as triangles, read straight from its `points`,
    `faces` and `boundary` files (the boundary faces are the tail of the faces list, one run
    per patch). Polygons are fanned from their first vertex; only the points the boundary
    uses are kept. `wall_only` keeps the patches typed `wall` (empty when there are none).
    Never the volume: a VTK OpenFOAM reader decomposes every polyhedron of the fill to hand
    back a surface that is a few percent of it."""
    pts, tri, _edges = boundary_of_polymesh(polymesh, wall_only=wall_only)
    return pts, tri


def boundary_of_polymesh(polymesh, *, wall_only: bool = False
                         ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(points, fan triangles, the polygons' own edges) of a polyMesh boundary, read as
    boundary_triangles_of_polymesh reads it. The edges are what a cell-size figure is taken
    from (measure_passage): the triangles carry the fan's diagonals as well."""
    from meshpipeline.engines.cfmesh.polymesh_surface import (
        read_boundary_faces,
        read_boundary_patches,
        read_points,
    )
    pm = Path(polymesh)
    patches = read_boundary_patches(pm)
    empty = (np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), np.zeros((0, 2), dtype=np.int64))
    if wall_only:
        patches = [p for p in patches if p["type"] in WALL_PATCH_TYPES]
    if not patches:
        return empty
    points = np.asarray(read_points(pm), dtype=float)
    first = min(p["start_face"] for p in patches)
    last = max(p["start_face"] + p["n_faces"] for p in patches)
    tail = read_boundary_faces(pm, first, last - first)
    polys = [f for p in patches
             for f in tail[p["start_face"] - first:p["start_face"] - first + p["n_faces"]]
             if len(f) >= 3]
    if not polys:
        return empty
    sizes = np.fromiter((len(f) for f in polys), dtype=np.int64, count=len(polys))
    fans = []
    for n in np.unique(sizes):
        ids = np.asarray([polys[i] for i in np.flatnonzero(sizes == n)], dtype=np.int64)
        for k in range(1, int(n) - 1):
            fans.append(np.stack([ids[:, 0], ids[:, k], ids[:, k + 1]], axis=1))
    tri = np.concatenate(fans)
    used, inv = np.unique(tri, return_inverse=True)
    # every polygon corner is a corner of its fan, so the same numbering holds the edges
    edges = np.searchsorted(used, polygon_edges(polys))
    return points[used], inv.reshape(tri.shape), edges


def passage_of_polymesh(workspace, *, budget_s: float = PASSAGE_MEASURE_BUDGET_S) -> dict:
    """The passage measure of the polyMesh under <workspace>/constant, from its boundary
    files alone, abandoned past `budget_s`. {} when anything is missing or the budget runs
    out - a measurement never loses a finished mesh."""
    ws = Path(workspace)
    pm = ws / "constant" / "polyMesh"
    if not (pm / "owner").exists():
        return {}
    try:
        with measure_deadline(budget_s):
            pts, faces, edges = boundary_of_polymesh(pm)
            if len(faces) < 4:
                return {}
            wpts, wfaces, wedges = boundary_of_polymesh(pm, wall_only=True)
            if len(wfaces) >= 4:
                return passage_of_surface(pts, faces, wall=(wpts, wfaces), edges=wedges)
            return passage_of_surface(pts, faces, edges=edges)
    except MeasureOverdue:
        logger.warning("passage measure abandoned after %.0f s; the mesh ships without it",
                       budget_s)
        return {}
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("passage measure of the polyMesh failed", exc_info=True)
        return {}


def passage_of_stls(paths, *, interior_point=None, cap_paths=(), port_centroids=()) -> dict:
    """The radius statistics of the staged WALL (open at the ports) BEFORE meshing, for sizing
    (passage_field_of_stls says how the field is read). {} when nothing reads."""
    field = passage_field_of_stls(paths, interior_point=interior_point, cap_paths=cap_paths,
                                  port_centroids=port_centroids)
    if field is None:
        return {}
    return radius_stats(field[2])


#: The staged wall is read at points no farther apart than this fraction of the part's diagonal.
#: A CAD tessellation puts no point inside a flat face and none along a straight pipe - only at
#: the face's edges, where the normal leans 45 degrees into a corner and the chord reads a
#: glance - so a rectangular duct read its LONG side everywhere (bend_elbow_003: 200 points, all
#: 192 mm on a 140 x 384 mm duct) and a narrow branch had readings at its two ends only.
FIELD_EDGE_FRACTION = 1.0 / 150.0

#: The sizing field is read at no more points than this: a chord per point, about a millisecond
#: each - 20,000 read the Fluent aorta's 31,000-point wall in under 20 s (the gate's measure of a
#: finished mesh keeps MAX_MEASURE_POINTS).
FIELD_MAX_POINTS = 20_000


def point_areas(points, faces) -> np.ndarray:
    """The wall area each point stands for: a third of every triangle it belongs to."""
    p = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    a = 0.5 * np.linalg.norm(np.cross(p[f[:, 1]] - p[f[:, 0]], p[f[:, 2]] - p[f[:, 0]]), axis=1)
    return np.bincount(f.ravel(), weights=np.repeat(a / 3.0, 3), minlength=len(p))


def weighted_percentile(values, weights, q: float) -> float:
    """The q-th percentile of values, each counted by its weight."""
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    order = np.argsort(v)
    c = np.cumsum(w[order])
    if c[-1] <= 0.0:
        return float(np.percentile(v, q))
    return float(v[order][min(len(v) - 1, int(np.searchsorted(c, q / 100.0 * c[-1])))])


def passage_field_of_stls(paths, *, interior_point=None, cap_paths=(), port_centroids=(),
                          max_points: int = FIELD_MAX_POINTS):
    """(points, faces, radius at each point) of the staged WALL (open at the ports) BEFORE
    meshing, read the way VMTK's staging reads them (a ray leaving through a port borrows its
    neighbour). The chords are oriented from a point deep in the cavity found from the port
    centroids (interior_from_ports); the tessellation's own interior point is NOT trusted - for a
    hollow wall solid it sits inside the wall material, a wall thickness off the cavity surface,
    and the orientation vote from there is a coin toss (production read a 6 mm radius on a 173 mm
    bore and cartesianMesh was killed at 1 mm cells). The staged caps do not stitch to the wall
    rim exactly, so a merged 'closed' surface is only the last resort.

    The wall is read at points FIELD_EDGE_FRACTION of the diagonal apart at most (long edges are
    halved, which keeps the shape), and on a decimated copy when it has more than `max_points`
    points (a scanned vessel), so the field covers every passage at a bounded cost. The points
    returned are the ones read. None when nothing reads."""
    import pyvista as pv

    from meshpipeline.engines.vmtk.lumen_staging import local_radius
    from meshpipeline.engines.vmtk.rims import _halve_long_edges
    try:
        parts = [pv.read(str(p)) for p in paths if Path(p).exists()]
        if not parts:
            return None
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
        b = np.asarray(merged.bounds, dtype=float)
        diag = float(np.linalg.norm(b[1::2] - b[0::2]))
        if deep is None:
            surf = pv.PolyData(pts, np.hstack([np.full((len(faces), 1), 3, dtype=np.int64),
                                               faces]).ravel())
            deep = inside_point(surf)
            if deep is None:
                return None
        elif len(port_centroids):
            pts, faces = orient_wall_faces(pts, faces, list(port_centroids))
        if len(pts) > max_points:
            surf = pv.PolyData(pts, np.hstack([np.full((len(faces), 1), 3, dtype=np.int64),
                                               faces]).ravel())
            coarse = surf.decimate(1.0 - max_points / float(len(pts))).clean()
            cf = np.asarray(coarse.faces).reshape(-1, 4)[:, 1:]
            if len(cf) >= 4:
                pts, faces = np.asarray(coarse.points, dtype=float), cf
        else:
            # no edge longer than the field spacing - nor so many points that the read runs
            # long: halving a CAD pipe's end-to-end slivers multiplies points far past the
            # area's share (a 48-point cylinder came back with 210,000 at 1/150 of its length,
            # and the chords took 8 minutes), so the spacing widens until the count fits
            area = float(point_areas(pts, faces).sum())
            h = max(diag * FIELD_EDGE_FRACTION, float(np.sqrt(area / (0.6 * max_points))))
            p0, f0 = np.asarray(pts, dtype=float), np.asarray(faces, dtype=np.int64)
            for _ in range(24):
                pts, faces = _halve_long_edges(p0, f0, h)
                if len(pts) <= max_points:
                    break
                h *= 1.5
        r = local_radius(pts, faces, deep, diag * 1e-5, diag / 2.0)
        return pts, faces, np.asarray(r, dtype=float)
    except Exception:  # noqa: BLE001 - sizing aid, not a verdict
        logger.warning("passage radius of the staged surface failed", exc_info=True)
        return None


#: The wall band is sized for the passage half the wall AREA is at least as narrow as (the
#: median by area); what is narrower than the band can carry is refined locally, level by level
#: (narrow_passage_regions). By area, not by point: a scanned vessel's small branches carry many
#: more points per square millimetre than its trunk. Lower, the band itself cost the cells: on
#: the Fluent aorta, where a fifth of the wall bounds vessels under 1.7 mm in radius, a band at
#: that 20th percentile was a 0.26 mm shell over the whole 19,000 mm^2 wall.
BAND_AREA_PERCENTILE = 50.0


def field_radius_stats(points, faces, radius) -> dict:
    """radius_stats of a passage field, plus `band` (the BAND_AREA_PERCENTILE radius by area)
    and the median by area (the background's yardstick); the narrow end (min, p05) stays by
    point - it is what the resolution floor measures."""
    r = np.asarray(radius, dtype=float)
    out = radius_stats(r)
    if not out:
        return out
    w = point_areas(points, faces)
    ok = r > 0.0
    if w[ok].sum() > 0.0:
        out["band"] = round(weighted_percentile(r[ok], w[ok], BAND_AREA_PERCENTILE), 6)
        out["median"] = round(max(weighted_percentile(r[ok], w[ok], 50.0), out["band"]), 6)
    return out


#: A declared port vouches for the passage within this many port diameters of it: the passage
#: there is never wider than the port the flow crosses (a reading off a hollow part's outer skin,
#: or along a rectangular duct's long side, is corrected by it).
PORT_VOUCH_DIAMETERS = 1.5


def port_corrected_radius(points, radius, ports) -> np.ndarray:
    """The radius field with every point within PORT_VOUCH_DIAMETERS of a declared port held to
    that port's half flow-width. ports: [(centroid in metres, half width in metres)]."""
    pts = np.asarray(points, dtype=float)
    out = np.asarray(radius, dtype=float).copy()
    for c, hw in ports or ():
        if not hw or hw <= 0.0 or c is None:
            continue
        d = np.linalg.norm(pts - np.asarray(c, dtype=float), axis=1)
        near = d < PORT_VOUCH_DIAMETERS * 2.0 * float(hw)
        out[near] = np.minimum(out[near], float(hw))
    return out


def declared_port_widths(intake_patches, openings: dict | None = None) -> list[tuple]:
    """[(centroid in metres, half flow-width in metres)] of every sized declared port: the bound
    opening's measured centroid when there is one, else the declared location."""
    out = []
    for p in intake_patches or []:
        if not isinstance(p, dict) or str(p.get("type") or "") not in ("inlet", "outlet"):
            continue
        hw = declared_port_half_width([p])
        if not hw:
            continue
        rec = (openings or {}).get(str(p.get("name"))) or {}
        c = rec.get("centroid") if isinstance(rec, dict) else None
        if c is None and p.get("near_mm") is not None:
            c = [float(v) / 1000.0 for v in p["near_mm"]]
        if c is not None:
            out.append((tuple(float(v) for v in c), hw))
    return out


#: A wall band at the NARROWEST passage is kept while it costs fewer cells than this (estimated
#: by band_shell_cells); above it, the band is the typical passage and the narrow ones are refined
#: locally. Sized from the narrowest passage everywhere, a part with one passage width meshes
#: as it always did (most of the corpus: 50-430k cells); a multi-scale one - the Fluent aorta
#: (an estimated 3.8 M in the band alone, no mesh in 47 min), manifold_003 (4.6 M cells),
#: manifold_004's wall solid (6.6 M) - is not cut at its smallest passage's cell end to end.
GLOBAL_BAND_MAX_CELLS = 300_000


def band_shell_cells(points, faces, radius, band_radius: float,
                     target: float = PASSAGE_CELLS_ACROSS) -> float:
    """Cells a wall band sized for `band_radius` costs: the wall's area, a band one radius thick
    (thinner where the passage is), at `target` cells across that radius."""
    if not band_radius or band_radius <= 0.0:
        return float("inf")
    a = point_areas(points, faces)
    cell = 2.0 * float(band_radius) / float(target)
    depth = np.minimum(1.1 * float(band_radius), np.asarray(radius, dtype=float))
    return float((a * depth).sum()) / cell ** 3


def lid_hydraulic_diameter(path) -> float | None:
    """The hydraulic diameter of a staged port lid, 4 x area / perimeter, in its file's unit:
    the bore of a round lid, twice the gap of an annulus (an annular port with a centre body),
    the right width of a slot or a rectangle - where the area-equivalent diameter read an
    annulus as its 151 mm bore while the flow crosses a 13.2 mm gap (annular_001: 3 cells across
    the gap, 2026-10-04). The perimeter is the lid's free boundary once coincident points are
    merged. None when the lid cannot be read or its boundary is not a few closed loops."""
    import pyvista as pv
    try:
        lid = pv.read(str(path)).extract_surface().triangulate()
        if lid.n_cells == 0:
            return None
        area = float(lid.area)
        span = float(np.linalg.norm(np.asarray(lid.bounds[1::2]) - np.asarray(lid.bounds[0::2])))
        lid = lid.clean(tolerance=1e-6 * max(span, 1e-12), absolute=True)
        edges = lid.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                          manifold_edges=False, non_manifold_edges=False)
        if edges.n_cells == 0:
            return None
        e = np.asarray(edges.lines).reshape(-1, 3)[:, 1:]
        p = np.asarray(edges.points, dtype=float)
        perimeter = float(np.linalg.norm(p[e[:, 1]] - p[e[:, 0]], axis=1).sum())
    except Exception:  # noqa: BLE001 - a sizing aid; the caller keeps its own yardstick
        logger.warning("hydraulic diameter of %s could not be read", path, exc_info=True)
        return None
    if area <= 0.0 or perimeter <= 0.0:
        return None
    dh = 4.0 * area / perimeter
    d_eq = 2.0 * float(np.sqrt(area / np.pi))
    # never wider than the circle of the same area (a circle is the widest); far narrower than
    # any real slot means the boundary is a soup of unjoined triangles, not the lid's outline
    return dh if 0.02 * d_eq <= dh <= 1.0001 * d_eq else None


def lid_hydraulic_diameters(srcs: dict, wall_key: str) -> dict:
    """{port name: hydraulic diameter} of every staged lid that reads (lid_hydraulic_diameter)."""
    out = {}
    for name, v in (srcs or {}).items():
        if name == wall_key:
            continue
        paths = v if isinstance(v, list) else [v]
        if len(paths) != 1:
            continue
        dh = lid_hydraulic_diameter(paths[0])
        if dh:
            out[str(name)] = dh
    return out


def staged_passage_field(t: dict, srcs: dict, wall_key: str, declaration, *,
                         corrected: bool = True, field=None):
    """(points, faces, radius) of an internal-flow staging's cavity wall - the record
    tessellate_internal returns (`openings` with their centroids) and its surfaces by patch
    (`srcs`: the wall under `wall_key`, the port caps the rest) - with the radius held near each
    declared port to that port's half flow-width (port_corrected_radius) unless `corrected` is
    False. A `field` already read (uncorrected) is corrected rather than read again. None when
    the wall cannot be read."""
    openings = t.get("openings") or {}
    if field is None:
        wall_src = srcs.get(wall_key) or []
        wall_paths = wall_src if isinstance(wall_src, list) else [wall_src]
        cap_paths = [p for k, v in srcs.items() if k != wall_key
                     for p in (v if isinstance(v, list) else [v])]
        centroids = [o.get("centroid") for o in openings.values()
                     if isinstance(o, dict) and o.get("centroid")]
        field = passage_field_of_stls(wall_paths, cap_paths=cap_paths, port_centroids=centroids)
    if field is None:
        return None
    pts, faces, r = field
    if not corrected:
        return pts, faces, r
    return pts, faces, port_corrected_radius(pts, r, declared_port_widths(declaration, openings))


class NarrowRegions(list):
    """Refinement boxes for narrow passages; `note` says what a budget left out."""

    note: str = ""


#: A wall point is in a narrow passage when the planned wall cell puts fewer than this many
#: cells across it: the floor, with half a cell to spare for a reading's noise.
NARROW_DETECT_CELLS = PASSAGE_FLOOR_CELLS + 0.5


def narrow_passage_regions(points, radius, *, cell_m: float, areas=None,
                           target: float = PASSAGE_CELLS_ACROSS,
                           detect: float = NARROW_DETECT_CELLS, max_level_bump: int = 4,
                           max_regions: int = 128,
                           budget_cells: float | None = None) -> NarrowRegions:
    """Boxes around the walls of passages too NARROW for the planned wall cell (fewer than
    `detect` cells across), each with the cell that puts `target` across its narrowest point
    (`cell_needed_m`) and the octree level bump over cell_m that reaches it (`level_bump`,
    `cell_m`).

    The wall cell of a mesh is one number; the passage is not. Sized from the inlet (snappy) or
    the typical passage (cfMesh), the branches of an aorta or a manifold came in under the
    12-across floor; sized from the narrowest passage everywhere, the Fluent aorta's 20 mm trunk
    was cut at its 3 mm branches' cell and cfMesh made no mesh in 47 minutes. So the cell is held
    where the passage is narrow, and only there - the same local treatment cad/thin_features
    gives thin plates: the narrow wall points are sorted by the level they need (one octree
    level per halving of the cell), each level's points binned into compact groups (a few cells,
    or a few radii, across), and each group boxed out to its passage (padded by its widest
    radius). Where boxes overlap, the finer level wins in both meshers. More than `max_regions`
    groups coarsen the bins.

    With a `budget_cells`, the extra levels are lowered while the boxes' cost exceeds it, and if
    even +1 does not fit, the cheapest boxes that fit are kept (and `note` says so). The cost is
    the fluid a box refines at its cell: with the wall `areas` of the points, the passage their
    wall bounds (area x radius, a tube's volume twice over); without, the box's whole volume."""
    pts = np.asarray(points, dtype=float)
    r = np.asarray(radius, dtype=float)
    out = NarrowRegions()
    if not (cell_m and cell_m > 0.0) or len(pts) == 0 or len(r) != len(pts):
        return out
    narrow = np.isfinite(r) & (r > 0.0) & (2.0 * r / float(cell_m) < float(detect))
    if not narrow.any():
        return out
    P, R = pts[narrow], r[narrow]
    A = (np.asarray(areas, dtype=float)[narrow]
         if areas is not None and len(areas) == len(pts) else None)
    need = 2.0 * R / float(target)
    level = np.clip(np.ceil(np.log2(np.maximum(float(cell_m) / need, 1.0)) - 1e-9), 1,
                    int(max_level_bump)).astype(np.int64)
    classes = sorted(set(level.tolist()))
    base_bin = {b: max(3.0 * float(cell_m) / (2 ** b), 8.0 * float(R[level == b].min()))
                for b in classes}
    scale = 1.0
    groups: list[tuple[int, np.ndarray]] = []
    for _ in range(40):
        groups = []
        for b in classes:
            sel = np.flatnonzero(level == b)
            keys = np.floor(P[sel] / (base_bin[b] * scale)).astype(np.int64)
            _, lab = np.unique(keys, axis=0, return_inverse=True)
            lab = np.asarray(lab).ravel()
            groups += [(b, sel[lab == g]) for g in range(int(lab.max()) + 1)]
        if len(groups) <= max_regions:
            break
        scale *= 1.5
    boxes = []
    for b, idx in groups:
        if not len(idx):
            continue
        rg = R[idx]
        lo = P[idx].min(axis=0) - float(rg.max())
        hi = P[idx].max(axis=0) + float(rg.max())
        rmin = float(rg.min())
        box_volume = float(np.prod(np.maximum(hi - lo, 0.0)))
        fluid = (min(box_volume, float((A[idx] * rg).sum())) if A is not None else box_volume)
        boxes.append({"min": lo.tolist(), "max": hi.tolist(), "radius_m": rmin,
                      "cell_needed_m": 2.0 * rmin / float(target), "level_bump": int(b),
                      "n_points": int(len(idx)), "volume_m3": fluid})

    def _cost(bs) -> float:
        return sum(b["volume_m3"] / (float(cell_m) / (2 ** b["level_bump"])) ** 3 for b in bs)

    note = ""
    if budget_cells is not None and budget_cells > 0 and boxes:
        while _cost(boxes) > float(budget_cells) and max(b["level_bump"] for b in boxes) > 1:
            top = max(b["level_bump"] for b in boxes)
            for b in boxes:
                if b["level_bump"] == top:
                    b["level_bump"] -= 1
            note = "refinement held back by the cell budget"
        if _cost(boxes) > float(budget_cells):
            kept, spent = [], 0.0
            for b in sorted(boxes, key=lambda b_: b_["volume_m3"]):
                c = _cost([b])
                if spent + c <= float(budget_cells):
                    kept.append(b)
                    spent += c
            note = (f"{len(boxes) - len(kept)} of {len(boxes)} narrow region(s) not refined: "
                    "the cell budget does not cover them")
            boxes = kept
    for b in boxes:
        b["cell_m"] = float(cell_m) / (2 ** b["level_bump"])
        # never finer than its level reaches (a level held back by the budget, or the deepest
        # level allowed): what cfMesh is asked for is what the octree can afford
        b["cell_needed_m"] = max(b["cell_needed_m"], b["cell_m"])
    out.extend(boxes)
    out.note = note
    return out


__all__ = ["MAX_MEASURE_POINTS", "PASSAGE_CEILING_CELLS", "PASSAGE_CELLS_ACROSS",
           "PASSAGE_FLOOR_CELLS", "PASSAGE_MEASURE_BUDGET_S", "MeasureOverdue", "NarrowRegions",
           "boundary_of_polymesh", "boundary_triangles_of_polymesh",
           "declared_port_half_width", "declared_port_widths", "field_radius_stats",
           "narrow_passage_regions", "passage_field_of_stls", "point_areas",
           "port_corrected_radius", "staged_passage_field", "weighted_percentile",
           "FIELD_MAX_POINTS",
           "cavity_skin", "choose_passage_radius", "inside_point", "interior_from_ports",
           "mean_edge", "measure_deadline", "measure_passage", "orient_wall_faces",
           "passage_of_polymesh", "polygon_edges", "triangle_edges",
           "passage_of_stls", "passage_of_surface", "plausible_radius", "port_radius_stats",
           "radius_stats", "size_caps", "under_floor_regions", "vouched_by_ports",
           "UNDER_FLOOR_MAX_REGIONS"]
