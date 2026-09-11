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

logger = logging.getLogger(__name__)

STAGING_FACT = "vmtk_staging.json"
LUMEN_OPEN = "lumen_open.vtp"
#: point array on lumen_open.vtp: the local radius of the lumen at each wall point (metres)
SIZING_ARRAY = "LocalRadius"
_CAD_SUFFIXES = (".step", ".stp", ".iges", ".igs")

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
#: Floor and ceiling on the radius-adaptive edge length, as fractions of the smallest / largest
#: port. Where several centerlines merge the distance field can touch zero (manifold_002: 0.7 mm on
#: a 100 mm port) and vmtkmeshgenerator then dies on a zero-size target.
MIN_EDGE_FRACTION = 0.05
MAX_EDGE_FRACTION = 0.2
#: The local radius is clipped to [RADIUS_LO * smallest port, RADIUS_HI * largest port]: a ray that
#: grazes a corner reads near zero, one that crosses a junction into the main run reads the run.
RADIUS_LO_FRACTION = 0.25
RADIUS_HI_FRACTION = 0.75


def is_cad(path) -> bool:
    return Path(path).suffix.lower() in _CAD_SUFFIXES


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
        out.append({"name": str(best["name"]), "role": roles.get(str(best["name"]), "outlet"),
                    "engine_key": m["key"], "centroid": [float(v) for v in m["centroid"]],
                    "size_m": float(m["size_m"]), "area_m2": float(m["area_m2"])})
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


def sizing(ports: list[dict]) -> dict:
    sizes = [float(p["size_m"]) for p in ports]
    return {"edge_bound": min(sizes) / RIM_DIVISIONS,
            "min_edge_length": min(sizes) * MIN_EDGE_FRACTION,
            "max_edge_length": max(sizes) * MAX_EDGE_FRACTION,
            "radius_lo": min(sizes) * RADIUS_LO_FRACTION,
            "radius_hi": max(sizes) * RADIUS_HI_FRACTION}


def local_radius(points: np.ndarray, faces: np.ndarray, interior_point, r_lo: float,
                 r_hi: float) -> np.ndarray:
    """The lumen's local radius at every wall point: half the chord from the point along its
    INWARD normal to the opposite wall, clipped to [r_lo, r_hi] and smoothed once over the
    1-ring. Rays that leave through an open port borrow the nearest measured value.

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
    f = np.asarray(faces, dtype=np.int64)
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    acc = (np.bincount(e[:, 0], weights=r[e[:, 1]], minlength=len(pts))
           + np.bincount(e[:, 1], weights=r[e[:, 0]], minlength=len(pts)))
    cnt = np.bincount(e[:, 0], minlength=len(pts)) + np.bincount(e[:, 1], minlength=len(pts))
    nb = np.where(cnt > 0, acc / np.maximum(cnt, 1), r)
    return 0.5 * r + 0.5 * nb


def _measure_opening(poly) -> dict:
    ar = np.asarray(poly.compute_cell_sizes(length=False, area=True, volume=False)["Area"], float)
    cc = np.asarray(poly.cell_centers().points, float)
    area = float(ar.sum())
    centroid = (cc * ar[:, None]).sum(axis=0) / area if area > 0 else cc.mean(axis=0)
    return {"centroid": [float(v) for v in centroid], "area_m2": area,
            "size_m": 2.0 * float(np.sqrt(area / np.pi))}


def stage_lumen(workspace, geom_path, *, prepared, intake_patches: list,
                input_kind: str = "") -> dict | None:
    """From a CAD body and the intake's declared ports, write into the workspace:
      lumen_open.vtp     the fluid WALL only, its port faces removed (real holes), rims refined
      lumen.vtp          the same surface (what geometry_report inspects; the pype overwrites it
                         with the remeshed lumen)
      vmtk_staging.json  ports (declared name, role, centroid, size), seeds, sizing
    Returns the record, or None when this does not apply (not CAD, or nothing declared).
    Geometry failures propagate: a body that cannot be opened is reported, not guessed around."""
    ws = Path(workspace)
    geom = Path(geom_path)
    if not is_cad(geom):
        return None
    from meshpipeline.engines.port_binding import declaration_targets
    targets = declaration_targets(intake_patches or [])
    if not targets:
        return None
    roles = {str(p.get("name")): str(p.get("type"))
             for p in (intake_patches or []) if isinstance(p, dict) and p.get("name")}
    from meshpipeline.cad.cad_tessellate import tessellate_internal
    # the same reading of input_kind the snappy driver applies to the same intake fact
    fluid_solid = str(input_kind or "").strip() == "fluid-domain"
    res = tessellate_internal(geom, ws / "_lumen_stls", prepared=prepared, declared_ports=targets,
                              fluid_solid=fluid_solid, angular_deflection=ANGULAR_DEFLECTION)
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
    size = sizing(ports)
    from typing import Any

    from meshpipeline.engines.vmtk.rims import split_long_edges
    wall: Any = pv.read(str(stls["wall"])).clean()
    # OCP tessellates a straight cylinder as slivers spanning its whole length (min angle 0.001
    # degrees on the tee): vmtksurfaceremeshing corrupts the rims of such input. Bounding every
    # edge - rims and interior alike - to the remesh length first turns each sliver into short
    # pieces of sane aspect ratio (see rims.py for why not a subdivision filter).
    pts, tri = split_long_edges(np.asarray(wall.points), wall.faces.reshape(-1, 4)[:, 1:],
                                size["edge_bound"])
    lumen: Any = pv.PolyData(pts, np.hstack([np.full((len(tri), 1), 3, dtype=np.int64),
                                             tri]).ravel()).clean()
    radius = local_radius(np.asarray(lumen.points), lumen.faces.reshape(-1, 4)[:, 1:],
                          res["interior_point"], size["radius_lo"], size["radius_hi"])
    lumen.point_data[SIZING_ARRAY] = radius
    edges = lumen.extract_feature_edges(boundary_edges=True, feature_edges=False,
                                        manifold_edges=False, non_manifold_edges=False)
    n_loops = int(edges.connectivity().split_bodies().n_blocks) if edges.n_cells else 0
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
              "interior_point": res.get("interior_point")}
    (ws / STAGING_FACT).write_text(json.dumps(record, indent=2))
    logger.info("vmtk staging: %d port(s) opened, %d wall triangles, %d open loop(s), local "
                "radius %.4g..%.4g m", len(ports), lumen.n_cells, n_loops,
                float(radius.min()), float(radius.max()))
    return record


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
    return s
