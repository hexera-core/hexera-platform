# Responsibility: Turn a staged triangulated surface into gmsh geometry a volume can be meshed in, and an external
#                 body into the fluid around it.
# Owns: the angle classification of a closed surface into faces, the face table the builder binds groups from, the
#       far-field box around a body (discrete or CAD), and which faces of the result are the far field.
# Boundaries: runs on an initialised gmsh model (the driver's, or the inspection's); it authors no spec and judges
#             no mesh. numpy only beyond gmsh, so it runs inside the mesh image's driver process.
"""gmsh meshes a CLOSED volume; this is how a surface or a body becomes one.

* A closed triangulated surface (an STL fluid domain) has no CAD faces to name. It is split into
  faces wherever the surface folds sharper than CLASSIFY_ANGLE_DEG (a port lid meets the wall at a
  right angle; a smooth wall stays one face), reparametrised, and bounded into a volume - gmsh's
  own recipe for remeshing an STL. The split is deterministic, so the face table the inspection
  reports is the face table the driver meshes.
* An EXTERNAL body is meshed as the fluid between it and a far-field box: the box minus the body
  (an OpenCASCADE cut for a CAD solid, two surface loops for a triangulated body). The faces that
  lie on the box planes are the far field; the rest are the body.
"""
from __future__ import annotations

import math

import numpy as np

#: faces of a closed surface are split where the surface folds sharper than this: a port lid meets
#: its wall at ~90 degrees and a straight duct's flat sides at 90, while a smooth wall turns by far
#: less between neighbouring triangles. 40 is gmsh's own tutorial value (t13) for remeshing an STL.
CLASSIFY_ANGLE_DEG = 40.0


def classify_closed_surface(gmsh, stl_path: str, *, angle_deg: float = CLASSIFY_ANGLE_DEG) -> list[int]:
    """Merge a triangulated surface into the current model, split it into faces at sharp folds,
    and build reparametrised geometry for them. Returns the surface tags, in tag order."""
    gmsh.merge(str(stl_path))
    gmsh.model.mesh.removeDuplicateNodes()
    gmsh.model.mesh.classifySurfaces(math.radians(angle_deg), True, True, math.pi)
    gmsh.model.mesh.createGeometry()
    gmsh.model.geo.synchronize()
    return sorted(t for _, t in gmsh.model.getEntities(2))


def _triangles_of(gmsh, tag: int) -> np.ndarray:
    """(n, 3, 3) corner coordinates of the triangles meshed on one surface entity."""
    ntags, coords, _ = gmsh.model.mesh.getNodes(2, tag, includeBoundary=True)
    if not len(ntags):
        return np.zeros((0, 3, 3))
    pts = np.asarray(coords, dtype=float).reshape(-1, 3)
    nt = np.asarray(ntags, dtype=np.int64)
    index = np.full(int(nt.max()) + 1, -1, dtype=np.int64)
    index[nt] = np.arange(len(nt))
    etypes, _etags, enodes = gmsh.model.mesh.getElements(2, tag)
    tris = []
    for et, en in zip(etypes, enodes, strict=True):
        nn = gmsh.model.mesh.getElementProperties(et)[3]
        conn = np.asarray(en, dtype=np.int64).reshape(-1, nn)[:, :3]
        tris.append(index[conn])
    if not tris:
        return np.zeros((0, 3, 3))
    return pts[np.concatenate(tris)]


def surface_table(gmsh, tags) -> list[dict]:
    """{tag, area, centroid} of each discrete surface, measured on its triangles (a discrete face
    has no CAD mass properties) - the same table geometry_report gives for CAD faces."""
    out = []
    for tag in tags:
        t = _triangles_of(gmsh, int(tag))
        if not len(t):
            out.append({"tag": int(tag), "area": 0.0, "centroid": [0.0, 0.0, 0.0]})
            continue
        a = 0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
        c = t.mean(axis=1)
        area = float(a.sum())
        cen = (c * a[:, None]).sum(axis=0) / area if area > 0 else c.mean(axis=0)
        out.append({"tag": int(tag), "area": round(area, 10),
                    "centroid": [round(float(v), 6) for v in cen]})
    return out


def _port_area_m2(port: dict) -> float | None:
    d = port.get("diameter_mm")
    inner = port.get("inner_diameter_mm")
    try:
        if d:
            a = math.pi * (float(d) / 2000.0) ** 2
            if inner and 0 < float(inner) < float(d):
                a -= math.pi * (float(inner) / 2000.0) ** 2
            return a
        if port.get("width_mm") and port.get("height_mm"):
            return float(port["width_mm"]) * float(port["height_mm"]) * 1e-6
        if port.get("area_mm2"):
            return float(port["area_mm2"]) * 1e-6
    except (TypeError, ValueError):
        return None
    return None


def bind_ports(table: list[dict], ports: list[dict]) -> dict[str, list[int]]:
    """Each declared inlet/outlet bound to ONE face of the table: the face nearest its declared
    location among those whose area is within the binder's band of the declared size (any face
    when the location is all it states, or when no face fits the band), each face used once,
    largest declared port first. Returns {port name: [tag]}; ports that bind nothing are left out
    (the patch contract then names them)."""
    free = {int(r["tag"]): r for r in table}
    out: dict[str, list[int]] = {}
    order = sorted((p for p in ports if isinstance(p, dict)
                    and str(p.get("type") or "") in ("inlet", "outlet") and p.get("name")),
                   key=lambda p: -(_port_area_m2(p) or 0.0))
    for p in order:
        if not free:
            break
        near = p.get("near_mm")
        area = _port_area_m2(p)
        cands = list(free.values())
        if area:
            band = [r for r in cands if 0.75 <= float(r["area"]) / area <= 1.25]
            cands = band or cands
        if near is not None:
            nm = [float(v) / 1000.0 for v in near]
            best = min(cands, key=lambda r: math.dist(r["centroid"], nm))
        elif area:
            best = min(cands, key=lambda r: abs(float(r["area"]) - area))
        else:
            continue
        out[str(p["name"])] = [int(best["tag"])]
        free.pop(int(best["tag"]))
    return out


def box_surfaces_geo(gmsh, dmin, dmax) -> list[int]:
    """The six faces of an axis-aligned box in the built-in (geo) kernel, outward-wound."""
    g = gmsh.model.geo
    x0, y0, z0 = (float(v) for v in dmin)
    x1, y1, z1 = (float(v) for v in dmax)
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    p = [g.addPoint(*c) for c in corners]
    lines: dict[tuple[int, int], int] = {}

    def ln(a: int, b: int) -> int:
        k = (min(a, b), max(a, b))
        if k not in lines:
            lines[k] = g.addLine(p[k[0]], p[k[1]])
        return lines[k] if (a, b) == k else -lines[k]

    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
    out = []
    for f in faces:
        cl = g.addCurveLoop([ln(f[i], f[(i + 1) % 4]) for i in range(4)])
        out.append(g.addPlaneSurface([cl]))
    return out


def on_box(gmsh, tag: int, dmin, dmax, *, rel_tol: float = 1e-6) -> bool:
    """True when the surface lies flat on one of the box's six planes."""
    x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, int(tag))
    lo, hi = (x0, y0, z0), (x1, y1, z1)
    span = max(float(dmax[k]) - float(dmin[k]) for k in range(3))
    tol = rel_tol * span + 1e-12
    for k in range(3):
        for plane in (float(dmin[k]), float(dmax[k])):
            if abs(lo[k] - plane) <= tol and abs(hi[k] - plane) <= tol:
                return True
    return False


def external_fluid_cad(gmsh, dmin, dmax) -> tuple[list[int], list[int], list[int]]:
    """The OpenCASCADE model's solids become the body inside a far-field box: box minus body.
    Returns (fluid volume tags, far-field surface tags, body surface tags)."""
    occ = gmsh.model.occ
    body = [(3, t) for _, t in gmsh.model.getEntities(3)]
    box = occ.addBox(float(dmin[0]), float(dmin[1]), float(dmin[2]),
                     float(dmax[0] - dmin[0]), float(dmax[1] - dmin[1]), float(dmax[2] - dmin[2]))
    out, _ = occ.cut([(3, box)], body, removeObject=True, removeTool=True)
    occ.synchronize()
    vols = [t for d, t in out if d == 3]
    faces = sorted({t for _, t in gmsh.model.getBoundary([(3, v) for v in vols],
                                                          oriented=False, combined=True)})
    far = [t for t in faces if on_box(gmsh, t, dmin, dmax)]
    return vols, far, [t for t in faces if t not in far]


def external_fluid_discrete(gmsh, body_surfaces: list[int], dmin, dmax) -> tuple[list[int], list[int]]:
    """A triangulated body (already classified) inside a geo far-field box: one volume between the
    box loop and the body loop. Returns (fluid volume tags, far-field surface tags)."""
    far = box_surfaces_geo(gmsh, dmin, dmax)
    g = gmsh.model.geo
    outer = g.addSurfaceLoop(far)
    inner = g.addSurfaceLoop(list(body_surfaces))
    vol = g.addVolume([outer, inner])
    g.synchronize()
    return [vol], far


def internal_volume_discrete(gmsh, surfaces: list[int]) -> list[int]:
    """One volume bounded by a closed set of classified surfaces."""
    g = gmsh.model.geo
    loop = g.addSurfaceLoop(list(surfaces))
    vol = g.addVolume([loop])
    g.synchronize()
    return [vol]


def grade_from_body(gmsh, body_surfaces: list[int], *, h_body: float, h_far: float,
                    ruler: float) -> int:
    """A background size field: h_body on the body, growing to h_far several rulers away."""
    f = gmsh.model.mesh.field
    dist = f.add("Distance")
    f.setNumbers(dist, "SurfacesList", list(body_surfaces))
    f.setNumber(dist, "Sampling", 20)
    thr = f.add("Threshold")
    f.setNumber(thr, "InField", dist)
    f.setNumber(thr, "SizeMin", float(h_body))
    f.setNumber(thr, "SizeMax", float(h_far))
    f.setNumber(thr, "DistMin", 0.05 * float(ruler))
    f.setNumber(thr, "DistMax", 4.0 * float(ruler))
    f.setAsBackgroundMesh(thr)
    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    return thr


__all__ = ["CLASSIFY_ANGLE_DEG", "bind_ports", "box_surfaces_geo", "classify_closed_surface",
           "external_fluid_cad", "external_fluid_discrete", "grade_from_body",
           "internal_volume_discrete", "on_box", "surface_table"]
