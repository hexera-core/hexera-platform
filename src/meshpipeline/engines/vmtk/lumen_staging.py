# Responsibility: Stage a CAD body for vmtk without improvisation - the fluid wall as an OPEN lumen
#                 with real inlet/outlet holes, rims sized for the remesh, and centerline seeds at
#                 the declared ports.
# Boundaries: worker-side geometry (OCP + pyvista). It writes facts the pype and the builder consume
#             and chooses no meshing strategy.
# Collaborates with: cad.cad_tessellate.tessellate_internal (the product's own wall/port
#             classification), engines.port_binding.declaration_targets, engines.vmtk.rims.
from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path

import numpy as np

from meshpipeline.engines.radius_field import (  # noqa: F401 - re-exported: the radius field lived here
    RADIUS_MIN_REACH,
    _ball_min,
    local_radius,
)

logger = logging.getLogger(__name__)

STAGING_FACT = "vmtk_staging.json"
LUMEN_OPEN = "lumen_open.vtp"
#: point array on lumen_open.vtp: the local radius of the lumen at each wall point (metres)
SIZING_ARRAY = "LocalRadius"

#: The remesh edge length is the smallest declared port divided by this: about twelve edges across
#: the narrowest opening, fine enough for a clean Voronoi diagram (the centerline) and coarse enough
#: that a 1.4 m manifold remeshes in under two minutes.
RIM_DIVISIONS = 12.0
#: OCP angular deflection for the lumen wall: the chord it puts around a circular port of the
#: smallest declared size is about one remesh edge (chord = D * theta / 2 = D / RIM_DIVISIONS), so the
#: rims arrive at the spacing the remesh wants. A finer angle (0.05 was tried) gives 250-vertex rims of
#: 1-3 mm edges next to 8-20 mm cells; vmtksurfaceremeshing then leaves zero-area, non-manifold
#: triangles on the branch-port rims (tee_wye_003, manifold_002) and TetGen dies on them.
ANGULAR_DEFLECTION = 2.0 / RIM_DIVISIONS
#: Ceiling on the radius-adaptive edge length, as a fraction of the largest port: a chamber far
#: wider than its pipes is not filled with cells the size of the chamber.
MAX_EDGE_FRACTION = 0.2
#: The local radius is clipped to [RADIUS_LO * narrowest port, RADIUS_HI * largest port]: a ray that
#: grazes a corner reads near zero, one that crosses a junction into the main run reads the run.
#: The floor is a guard against ZERO, not a size: it is taken on the narrowest port's HYDRAULIC
#: diameter (4 x area / perimeter - the gap of an annulus, the short side of a flat duct), and it
#: sits well under any real passage. It used to be a quarter of the narrowest port's AREA-
#: equivalent diameter, which is above the true half-gap of an annulus (annular_001: a 21 mm floor
#: on a 6.6 mm half-gap), of an orifice (venturi_orifice_003: 73 mm on a 51 mm bore) and of a flat
#: duct's thin side - the field was clipped UP there, the cells came out two to four times too big,
#: and the cells-across gate, reading the same clipped field, still said 13.
RADIUS_LO_FRACTION = 0.1
RADIUS_HI_FRACTION = 0.75
#: Floor on the remesh edge length, as a fraction of the cell at the radius floor: only ever a
#: guard against a zero-size target (where centerlines merge the field can touch zero -
#: manifold_002: 0.7 mm on a 100 mm port - and vmtkmeshgenerator dies on it). It was 5% of the
#: narrowest port, coarser than the edge an orifice or a throat needs (7 cells across a 0.35 bore).
MIN_EDGE_OF_FLOOR = 0.5


def is_cad(path) -> bool:
    # the one CAD-or-surface answer (contracts/intake_formats), not a suffix list of its own
    from meshpipeline.contracts.intake_formats import is_cad as _is_cad

    return _is_cad(path)


def _match_score(m: dict, t: dict) -> float:
    if t.get("near_m") is not None:
        return float(np.linalg.norm(np.asarray(m["centroid"]) - np.asarray(t["near_m"])))
    a = t.get("area_m2")
    return abs(float(m["area_m2"]) - float(a)) / float(a) if a else float("inf")


def bind_ports(measured: list[dict], targets: list[dict], roles: dict[str, str]) -> list[dict]:
    """Assign each measured opening (centroid, area, equivalent diameter) to a declared port: the
    nearest declared location when the declaration carries one, otherwise the closest declared
    area. Greedy, largest opening first, each declared port used once - the rule the OpenFOAM
    engines' bind_intake applies, so the user's names land on the same holes whichever engine runs."""
    out: list[dict] = []
    free = list(targets)
    for m in sorted(measured, key=lambda q: -q["size_m"]):
        if not free:
            break
        best = min(free, key=partial(_match_score, m))
        free.remove(best)
        rec = {"name": str(best["name"]), "role": roles.get(str(best["name"]), "outlet"),
               "engine_key": m["key"], "centroid": [float(v) for v in m["centroid"]],
               "size_m": float(m["size_m"]), "area_m2": float(m["area_m2"])}
        for k in ("hydraulic_m", "loops"):
            if m.get(k):
                rec[k] = m[k]
        out.append(rec)
    return sorted(out, key=lambda p: p["name"])


def seed_points(ports: list[dict]) -> tuple[list[float], list[float]]:
    """ONE source (the largest port) and every other port as a target. vmtk traces one centerline
    per TARGET from the source set, so seeding two inlets as sources yields a single branch and a
    sizing field that is nonsense over the rest of the lumen (tee_wye_003: a 1.3 m 'radius' on a
    0.38 m pipe, then a mesh-generator crash). Flow direction is the intake's business, not the
    centerline's."""
    if len(ports) < 2:
        return [], []
    by_size = sorted(ports, key=lambda p: -p["size_m"])

    def flat(ps: list[dict]) -> list[float]:
        return [float(v) for p in ps for v in p["centroid"]]

    return flat(by_size[:1]), flat(by_size[1:])


def _hydraulic(p: dict) -> float:
    return float(p.get("hydraulic_m") or p["size_m"])


def sizing(ports: list[dict], *, edge_length_factor: float = 0.15) -> dict:
    """The size bounds of the staged lumen. The narrow end is read on the ports' HYDRAULIC
    diameter (an annulus is as narrow as its gap, a flat duct as its thin side), the wide end on
    their area-equivalent diameter."""
    sizes = [float(p["size_m"]) for p in ports]
    narrow = min(_hydraulic(p) for p in ports)
    radius_lo = narrow * RADIUS_LO_FRACTION
    return {"edge_bound": narrow / RIM_DIVISIONS,
            "min_edge_length": float(edge_length_factor) * radius_lo * MIN_EDGE_OF_FLOOR,
            "max_edge_length": max(sizes) * MAX_EDGE_FRACTION,
            "radius_lo": radius_lo,
            "radius_hi": max(sizes) * RADIUS_HI_FRACTION}


def _measure_opening(poly) -> dict:
    ar = np.asarray(poly.compute_cell_sizes(length=False, area=True, volume=False)["Area"], float)
    cc = np.asarray(poly.cell_centers().points, float)
    area = float(ar.sum())
    centroid = (cc * ar[:, None]).sum(axis=0) / area if area > 0 else cc.mean(axis=0)
    rec = {"centroid": [float(v) for v in centroid], "area_m2": area,
           "size_m": 2.0 * float(np.sqrt(area / np.pi))}
    # the lid's rim: its length gives the hydraulic diameter 4A/P, and its loop count says whether
    # the opening is a disk (one loop) or a ring around a centre body (two)
    try:
        rim = poly.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                         manifold_edges=False, non_manifold_edges=False)
        if rim.n_cells:
            perimeter = float(np.sum(rim.compute_cell_sizes(length=True, area=False,
                                                            volume=False)["Length"]))
            if perimeter > 0.0 and area > 0.0:
                rec["hydraulic_m"] = 4.0 * area / perimeter
            rec["loops"] = int(rim.connectivity().split_bodies().n_blocks)
    except Exception:  # noqa: BLE001 - the area-equivalent size stands in
        logger.debug("vmtk staging: opening rim not measured", exc_info=True)
    return rec


def stage_lumen(workspace, geom_path, *, prepared, intake_patches: list,
                input_kind: str = "") -> dict | None:
    """From the uploaded geometry and the intake's declared ports, write into the workspace:
      lumen_open.vtp     the fluid WALL only, its port faces removed (real holes), rims refined
      lumen.vtp          the same surface (what geometry_report inspects; the pype overwrites it
                         with the remeshed lumen)
      vmtk_staging.json  ports (declared name, role, centroid, size), seeds, sizing
    A CAD body is opened on its B-rep. Any other upload is the staged metre surface (input.stl):
    the shared internal-flow staging (cad/internal_surface) closes it at the confirmed openings
    and names them, and its wall - the lumen with real holes where the openings are - is what
    vmtk reads, whether the file came with open ends, capped ends or a thick wall.
    A STEP whose B-rep cannot be separated (a faceted shell, an opening that is not a flat face)
    is staged from its own surface the same way.
    Returns the record, or None when this does not apply (nothing declared - then a surface upload
    is still written as lumen.vtp as it stands, for geometry_report to list its open profiles).
    Geometry failures propagate: a body that cannot be opened is reported, not guessed around."""
    ws = Path(workspace)
    geom = Path(geom_path)
    from meshpipeline.engines.port_binding import declaration_targets
    targets = declaration_targets(intake_patches or [])
    surface = ws / "input.stl"
    if not targets:
        if not is_cad(geom) and surface.exists() and not (ws / "lumen.vtp").exists():
            import pyvista as pv
            # points welded: an STL repeats each corner per triangle, and unwelded every edge
            # would read as an open profile
            pv.read(str(surface)).extract_surface(algorithm="dataset_surface").clean().save(str(ws / "lumen.vtp"))
        return None
    roles = {str(p.get("name")): str(p.get("type"))
             for p in (intake_patches or []) if isinstance(p, dict) and p.get("name")}
    if not is_cad(geom) and not surface.exists():
        return None
    from meshpipeline.cad.internal_surface import stage_internal
    # one call for every source: a B-rep is opened on its faces (and, when it cannot be, staged
    # from its own surface); any other upload is input.stl closed at the confirmed openings, its
    # ports already under their declared names, so the binding below finds each at distance zero
    res = stage_internal(geom, ws, prepared=prepared, intake_patches=intake_patches or [],
                         input_kind=input_kind, out_name="_lumen_stls", declared_ports=targets,
                         cad_kwargs={"angular_deflection": ANGULAR_DEFLECTION})
    import pyvista as pv
    stls = dict(res["stls"])
    measured = []
    for key, path in stls.items():
        if key == "wall":
            continue
        rec = _measure_opening(pv.read(str(path)).clean())
        rec["key"] = key
        measured.append(rec)
    ports = bind_ports(measured, targets, roles)
    if not ports:
        return None
    from typing import Any

    from meshpipeline.engines.vmtk.rims import bound_edges
    wall: Any = pv.read(str(stls["wall"])).clean()
    wall = fluid_wall(wall, [pv.read(str(p)) for k, p in stls.items() if k != "wall"],
                      res.get("interior_point"))
    # THE OPENINGS AS VMTK WILL SEE THEM: the rims the wall leaves at each port. A lid can
    # cover more than the opening (a CAD fluid annulus's lid also caps the rod's end), so the
    # opening's area, rim length and ring-or-disk are read off the wall itself
    capping = measure_openings(
        wall, ports, [pv.read(str(stls[p["engine_key"]])).extract_surface(algorithm="dataset_surface")
                      if p.get("engine_key") in stls else None for p in ports])
    size = sizing(ports)
    # OCP tessellates a straight cylinder as slivers spanning its whole length (min angle 0.001
    # degrees on the tee), a flat 545 mm wall as a handful of giant triangles (transition_013)
    # and the foot of a manifold stub as 50 x 0.8 mm needles (manifold_002): vmtksurfaceremeshing
    # pinches the rims of such input into zero-area non-manifold triangles and the generator
    # segfaults. The rims are cut to the remesh length, the interior only to the coarsest cell
    # the remesh may produce, and nothing needle-shaped is left anywhere (rims.py says why each).
    pts, tri = bound_edges(np.asarray(wall.points), wall.faces.reshape(-1, 4)[:, 1:],
                           h_wall=size["max_edge_length"], h_rim=size["edge_bound"])
    lumen: Any = pv.PolyData(pts, np.hstack([np.full((len(tri), 1), 3, dtype=np.int64),
                                             tri]).ravel()).clean()
    # sharp edges read along each side's own normal: a short orifice or a sharp throat has
    # vertices only on its rims, and their averaged normals look past it (radius_field)
    radius = local_radius(np.asarray(lumen.points), lumen.faces.reshape(-1, 4)[:, 1:],
                          res["interior_point"], size["radius_lo"], size["radius_hi"],
                          read_sharp_edges=True)
    lumen.point_data[SIZING_ARRAY] = radius
    edges = lumen.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                        manifold_edges=False, non_manifold_edges=False)
    n_loops = int(edges.connectivity().split_bodies().n_blocks) if edges.n_cells else 0
    # A WALL IN SEVERAL PIECES - the bore and the centre rod of an annulus, a closed body standing
    # in the flow - is meshed whole: the remesh keeps every piece (it kept only the largest, and
    # an annulus was delivered as the full pipe with its rod left out, every gate green)
    pieces = int(lumen.connectivity().point_data["RegionId"].max()) + 1 if lumen.n_points else 0
    oriented = False
    if pieces > 1:
        # every piece wound with its normals OUT of the fluid, which is what vmtk's layer
        # generator grows against - read from the fluid's side, not guessed per piece
        lumen, oriented = orient_out_of_fluid(
            lumen, [pv.read(str(p)) for k, p in stls.items() if k != "wall"],
            res.get("interior_point"))
    lumen.save(str(ws / LUMEN_OPEN))
    lumen.save(str(ws / "lumen.vtp"))
    # input.stl is NOT replaced. It is the CAD surface the engine's admission judges
    # (require_no_self_intersection): the open wall with its fan-split rims read as
    # self-intersecting to that check on a rectangular elbow (bend_elbow_003, job c0a6dac1,
    # 2026-09-11) and run_mesh was refused before the pype ever ran, while the same wall
    # fills cleanly. The builder's geometry_report reads lumen.vtp, so nothing else needs it.
    src, tgt = seed_points(ports)
    record = {"ports": ports, "source_points": src, "target_points": tgt, **size,
              "sizing_array": SIZING_ARRAY,
              "radius_m": {"min": float(radius.min()), "median": float(np.median(radius)),
                           "max": float(radius.max())},
              "n_open_loops": n_loops, "wall_triangles": int(lumen.n_cells),
              "input_kind": str(input_kind or ""), "angular_deflection": ANGULAR_DEFLECTION,
              "interior_point": res.get("interior_point"),
              "capping_method": capping, "wall_pieces": pieces, "wall_oriented": oriented}
    (ws / STAGING_FACT).write_text(json.dumps(record, indent=2))
    logger.info("vmtk staging: %d port(s) opened, %d wall triangles, %d open loop(s), local "
                "radius %.4g..%.4g m", len(ports), lumen.n_cells, n_loops,
                float(radius.min()), float(radius.max()))
    return record


#: Rays cast from the proven fluid point, and from points along those rays, to find what the
#: fluid touches (fluid_wall): first-generation directions, second-generation origins, and the
#: hits a piece needs before it counts as seen.
FLUID_RAYS = 600
FLUID_ECHO_ORIGINS = 60
FLUID_MIN_HITS = 3


def _sphere_dirs(n: int) -> np.ndarray:
    k = np.arange(n) + 0.5
    z = 1.0 - 2.0 * k / n
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    t = np.pi * (3.0 - np.sqrt(5.0)) * k
    return np.stack([r * np.cos(t), r * np.sin(t), z], axis=1)


def fluid_wall(wall, lids: list, interior_point):
    """The pieces of the staged wall that bound the FLUID. A CAD part declared a body (a pipe's
    metal) stages every face of its solid as wall: the bore, the centre rod, and the part's
    OUTER skin and flanges, which bound the metal, not the fluid. The OpenFOAM meshers never
    see the skin (their seed picks the fluid's side); vmtk fills the inside of whatever piece
    it keeps, and its remesh kept the largest - the skin: bend_elbow_021's metal STEP was
    delivered as the volume inside its outer skin, flanges and metal included, every gate green.
    A piece bounds the fluid when the fluid SEES it: rays from the staging's proven fluid point
    (and from points along those rays, all in the fluid) stop first on the bore, on a centre
    rod, on a body standing in the flow - never on the skin behind the bore. With nothing to
    tell by, the wall stands as it is."""
    import vtk
    if wall.n_cells == 0 or interior_point is None:
        return wall
    con = wall.connectivity()
    region = np.asarray(con.cell_data["RegionId"])
    n = int(region.max()) + 1
    if n < 2:
        return wall
    parts = [con.extract_surface(algorithm="dataset_surface")]
    tags = [region]
    for lid in lids:
        s = lid.extract_surface(algorithm="dataset_surface")
        parts.append(s)
        tags.append(np.full(s.n_cells, -1))
    scene = parts[0].merge(parts[1:], merge_points=False) if len(parts) > 1 else parts[0]
    tag = np.concatenate(tags)
    scene = scene.triangulate()
    if scene.n_cells != len(tag):            # triangulate split a polygon: carry the tags over
        return wall
    b = np.asarray(scene.bounds, dtype=float).reshape(3, 2)
    reach = 2.0 * float(np.linalg.norm(b[:, 1] - b[:, 0]))
    obb = vtk.vtkOBBTree()
    obb.SetDataSet(scene)
    obb.BuildLocator()
    hits_n = np.zeros(n, dtype=int)
    pts, ids = vtk.vtkPoints(), vtk.vtkIdList()
    eps = 1e-6 * reach

    def cast(origin, dirs) -> list:
        ends = []
        for d in dirs:
            pts.Reset()
            ids.Reset()
            if obb.IntersectWithLine(origin + eps * d, origin + reach * d, pts, ids) \
                    and pts.GetNumberOfPoints():
                t = int(tag[ids.GetId(0)])
                hit = np.asarray(pts.GetPoint(0))
                if t >= 0:
                    hits_n[t] += 1
                ends.append((origin, hit))
        return ends

    seed = np.asarray(interior_point, dtype=float)
    ends = cast(seed, _sphere_dirs(FLUID_RAYS))
    rng = np.random.default_rng(3)
    if ends:
        pick = rng.choice(len(ends), size=min(FLUID_ECHO_ORIGINS, len(ends)), replace=False)
        for i in pick:
            o, h = ends[i]
            # a point two thirds of the way to the first hit: still in the fluid
            cast(o + (2.0 / 3.0) * (h - o), _sphere_dirs(FLUID_RAYS // 6))
    keep = [k for k in range(n) if hits_n[k] >= FLUID_MIN_HITS]
    if not keep or len(keep) == n:
        return wall
    logger.info("vmtk staging: %d of %d wall piece(s) bound the fluid (rays from the fluid hit "
                "%s); the rest - an outer skin, a body outside the flow - are left out",
                len(keep), n, hits_n.tolist())
    return con.extract_cells(np.flatnonzero(np.isin(region, keep))).extract_surface(
        algorithm="dataset_surface").clean()


def _point_regions(faces: np.ndarray, n_points: int) -> np.ndarray:
    """The connected piece of every point of a triangle sheet (shared vertices connect)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    g = coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(n_points, n_points))
    return connected_components(g, directed=False)[1]


def orient_out_of_fluid(lumen, lids: list, interior_point):
    """(lumen, ok): every piece of the wall wound so its normals point OUT of the fluid - what
    vmtk's boundary layer grows against. vmtk orients each piece on its own (vtkPolyDataNormals'
    auto-orientation reads 'outward' from the piece's own extreme point), and on an annulus that
    pointed the centre rod's normals into the fluid: its layer grew INTO the rod and the tets
    overlapped by twice the layer's share of the volume (annular_006: 7.8% at a 10% stack, 3.9%
    at 5%). Here each piece is read from the fluid's side - rays from the staging's proven fluid
    point (and from points along them) stop first on the wall from inside the fluid, where a
    normal out of the fluid points along the ray - and a piece that faces the other way is
    turned. ok is False (and nothing is turned) when some piece is never seen."""
    import pyvista as pv
    import vtk
    if interior_point is None or lumen.n_cells == 0:
        return lumen, False
    pts = np.asarray(lumen.points, dtype=float)
    faces = np.asarray(lumen.faces).reshape(-1, 4)[:, 1:].astype(np.int64)
    region = _point_regions(faces, len(pts))[faces[:, 0]]
    n_regions = int(region.max()) + 1
    lid_tris = []
    for lid in lids:
        s = lid.extract_surface(algorithm="dataset_surface").triangulate()
        if s.n_cells:
            lid_tris.append(np.asarray(s.points, dtype=float)[
                np.asarray(s.faces).reshape(-1, 4)[:, 1:]])
    tri = np.concatenate([pts[faces], *lid_tris]) if lid_tris else pts[faces]
    tag = np.concatenate([region, np.full(len(tri) - len(faces), -1)])
    flat = tri.reshape(-1, 3)
    scene = pv.PolyData(flat, np.hstack([np.full((len(tri), 1), 3, dtype=np.int64),
                                         np.arange(len(flat), dtype=np.int64).reshape(-1, 3)]).ravel())
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    reach = 2.0 * float(np.linalg.norm(flat.max(axis=0) - flat.min(axis=0)))
    obb = vtk.vtkOBBTree()
    obb.SetDataSet(scene)
    obb.BuildLocator()
    vote = np.zeros(n_regions)
    seen = np.zeros(n_regions, dtype=int)
    hp, ids = vtk.vtkPoints(), vtk.vtkIdList()
    eps = 1e-6 * reach

    def cast(origin, dirs) -> list:
        ends = []
        for d in dirs:
            hp.Reset()
            ids.Reset()
            if obb.IntersectWithLine(origin + eps * d, origin + reach * d, hp, ids) \
                    and hp.GetNumberOfPoints():
                c = int(ids.GetId(0))
                if tag[c] >= 0:
                    seen[tag[c]] += 1
                    vote[tag[c]] += np.sign(float(np.dot(normal[c], d)))
                ends.append((origin, np.asarray(hp.GetPoint(0))))
        return ends

    seed = np.asarray(interior_point, dtype=float)
    ends = cast(seed, _sphere_dirs(FLUID_RAYS))
    rng = np.random.default_rng(5)
    for i in (rng.choice(len(ends), size=min(FLUID_ECHO_ORIGINS, len(ends)), replace=False)
              if ends else []):
        o, h = ends[i]
        cast(o + (2.0 / 3.0) * (h - o), _sphere_dirs(FLUID_RAYS // 6))
    if (seen < FLUID_MIN_HITS).any():
        logger.warning("vmtk staging: a wall piece was never seen from the fluid (hits %s); its "
                       "winding is left as staged", seen.tolist())
        return lumen, False
    flip = vote < 0
    if not flip.any():
        return lumen, True
    f = faces.copy()
    turn = flip[region]
    f[turn, 1], f[turn, 2] = faces[turn, 2], faces[turn, 1]
    out = pv.PolyData(pts, np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    for name in lumen.point_data:
        out.point_data[name] = np.asarray(lumen.point_data[name])
    logger.info("vmtk staging: %d of %d wall piece(s) turned to face out of the fluid",
                int(flip.sum()), n_regions)
    return out, True


class OpeningShapeError(ValueError):
    """The openings are of kinds vmtk cannot cap in one run (see measure_openings)."""


def _loops(surface) -> list[dict]:
    """Every open rim of a triangle sheet: its points, length and VECTOR area - the polygon's,
    walked edge to edge from one point, so the edges' own directions do not matter; its
    magnitude is the area the rim encloses."""
    rim = surface.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                        manifold_edges=False, non_manifold_edges=False)
    out: list[dict] = []
    if not rim.n_cells:
        return out
    for loop in rim.connectivity().split_bodies():
        seg = np.asarray(loop.cells_dict.get(3, np.zeros((0, 2))), dtype=np.int64)  # VTK_LINE
        if not len(seg):
            continue
        p = np.asarray(loop.points, dtype=float)
        c = p.mean(axis=0)
        nbr: dict[int, list[int]] = {}
        for a, b in seg.tolist():
            nbr.setdefault(a, []).append(b)
            nbr.setdefault(b, []).append(a)
        order, prev, cur = [int(seg[0, 0])], -1, int(seg[0, 0])
        for _ in range(len(seg)):
            nxt = [q for q in nbr.get(cur, []) if q != prev]
            if not nxt or nxt[0] == order[0]:
                break
            prev, cur = cur, nxt[0]
            order.append(cur)
        q = p[order] - c
        out.append({"centroid": c, "points": p,
                    "length": float(np.linalg.norm(p[seg[:, 1]] - p[seg[:, 0]], axis=1).sum()),
                    "vector_area": 0.5 * np.cross(q, np.roll(q, -1, axis=0)).sum(axis=0)})
    return out


def measure_openings(wall, ports: list[dict], lids: list | None = None) -> str:
    """Each staged port measured on the rims the WALL leaves there: how many (one for a disk,
    two for a ring round a centre body), the open area between them, and the hydraulic diameter
    4 x area / rim length. Updates `ports` in place and returns the capping vmtk needs:
    'simple' (every opening one rim - a flat fan per rim) or 'annular' (every opening two rims -
    a ring stitched between them). vmtk caps every opening the same way, so a part that mixes
    rings and disks (or has an opening of three rims) cannot be capped in one run: that raises,
    with the reason, rather than meshing the rod's end over.
    A rim belongs to the port whose LID it lies on (`lids`, one surface per port, in order): a
    small side branch's rim sits a few millimetres from a big neighbour's centre, closer than
    its own port's size would suggest, but only on its own lid. Without lids, the nearest port
    centre in units of the port's size decides."""
    loops = _loops(wall)
    if not loops or not ports:
        return "simple"
    centres = np.asarray([p["centroid"] for p in ports], dtype=float)
    scale = np.asarray([max(float(p["size_m"]), 1e-12) for p in ports])
    use_lids = lids is not None and len(lids) == len(ports) and all(
        lid is not None and lid.n_cells for lid in lids)
    owned: dict[int, list[dict]] = {i: [] for i in range(len(ports))}
    for lp in loops:
        if use_lids:
            d = np.asarray([float(np.mean(np.linalg.norm(
                np.asarray(lid.find_closest_cell(lp["points"], return_closest_point=True)[1])
                - lp["points"], axis=1))) for lid in lids])   # type: ignore[union-attr]
            owned[int(np.argmin(d))].append(lp)
            continue
        # the port whose lid centre is nearest to the rim, in units of the port's size
        d = np.linalg.norm(lp["points"][None, :, :] - centres[:, None, :], axis=2).min(axis=1)
        owned[int(np.argmin(d / scale))].append(lp)
    counts = []
    for i, p in enumerate(ports):
        mine = owned[i]
        if not mine:
            counts.append(1)
            continue
        # the outer rim's area less what the inner rims enclose, whichever way each is wound
        sizes = sorted((float(np.linalg.norm(lp["vector_area"])) for lp in mine), reverse=True)
        area = sizes[0] - sum(sizes[1:])
        if area <= 0.0:
            area = float(np.linalg.norm(np.sum([lp["vector_area"] for lp in mine], axis=0)))
        rim = float(sum(lp["length"] for lp in mine))
        p["loops"] = len(mine)
        if area > 0.0 and rim > 0.0:
            p["hydraulic_m"] = 4.0 * area / rim
            p["open_area_m2"] = area
            # the port's size is the OPENING's, not its lid's: a CAD lid can span a rod's end or
            # a metal part's end face, and every size bound below is read off size_m
            p["lid_size_m"] = float(p["size_m"])
            p["size_m"] = 2.0 * float(np.sqrt(area / np.pi))
        counts.append(len(mine))
    if all(c == 1 for c in counts):
        return "simple"
    if all(c == 2 for c in counts):
        return "annular"
    desc = ", ".join(f"{p['name']}: {c} rim{'s' if c != 1 else ''}" for p, c in zip(ports, counts))
    raise OpeningShapeError(
        "VMTK closes every opening of a run the same way - each as a flat disk, or each as a ring "
        f"round a centre body - and this part mixes them ({desc}). Mesh it with snappyHexMesh or "
        "cfMesh, which take any mix of openings, or declare the openings so they are all rings or "
        "all disks.")


def read_staging(workspace) -> dict | None:
    p = Path(workspace) / STAGING_FACT
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def merge_staged(strategy: dict | None, staged: dict | None) -> dict:
    """The builder's strategy with the staged facts filled in wherever it stated nothing: the
    sizing array, the size clamps, and seeds (unless it gave either seeding form)."""
    s = dict(strategy or {})
    if not staged:
        return s
    has_seeds = bool((s.get("source_points") and s.get("target_points"))
                     or (s.get("source_ids") and s.get("target_ids")))
    if not has_seeds and staged.get("source_points") and staged.get("target_points"):
        s["source_points"] = list(staged["source_points"])
        s["target_points"] = list(staged["target_points"])
        s.pop("source_ids", None)
        s.pop("target_ids", None)
    for k in ("min_edge_length", "max_edge_length"):
        if s.get(k) is None and staged.get(k) is not None:
            s[k] = float(staged[k])
    if not s.get("sizing_array") and staged.get("sizing_array"):
        s["sizing_array"] = str(staged["sizing_array"])
    # how the openings are capped and whether every wall piece is kept: facts of the geometry,
    # never the builder's to choose
    if staged.get("capping_method"):
        s["capping_method"] = str(staged["capping_method"])
    if staged.get("wall_pieces"):
        s["wall_pieces"] = int(staged["wall_pieces"])
    if staged.get("wall_oriented"):
        s["wall_oriented"] = True
    return s
