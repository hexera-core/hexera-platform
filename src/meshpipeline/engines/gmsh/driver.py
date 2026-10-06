# Responsibility: Execute the Gmsh meshing steps against a declarative specification.
# Boundaries: deterministic emission from declared values; the model authors the spec, never the Gmsh calls.
from __future__ import annotations

import json
import sys
from pathlib import Path

SICN_FLOOR = 0.1   # shared with the executor gate + quality criteria
# Minimum elements across the narrowest bbox extent. The clamp aims for 8; the
# resolution_floor gate (gmsh/gates.py) rejects below 6, so a clamped mesh clears
# the gate with margin. Kept a local literal on purpose: this driver runs as a
# standalone script inside the mesh image, decoupled from the settings inventory.
MIN_CELLS_ACROSS = 8

#: INDUSTRY DENSITY for a fluid domain: elements sized so about this many span the LOCAL
#: passage (twice the local radius), the same target VMTK meshes to (2 / edge_length_factor
#: 0.15). The old clamp put 8 across the smallest port or bbox extent and nothing across a
#: passage that narrows inside (a volute scroll: 2-4 cells at the narrowest wall). The
#: resolution_floor gate rejects a fill under PASSAGE_FLOOR_CELLS at the narrowest wall.
PASSAGE_CELLS_ACROSS = 13
#: EXTERNAL bodies: elements across the body's thinnest extent, and the finest body size as a
#: fraction of its diagonal (the floor that keeps a thin plate from asking for millions of tets)
EXTERNAL_THIN_DIVISIONS = 6
EXTERNAL_MAX_DIVISIONS = 400
#: the finest the passage field may ask for, as a fraction of the clamp size h: a chord that
#: grazes a sharp corner reads as a tiny radius, and 2r/13 of that would never finish
PASSAGE_MIN_SIZE_FRACTION = 1.0 / 30.0

#: A bounding-box extent below this fraction of the diagonal is noise, not a dimension of
#: the part: a planar face reports a thickness of ~1e-9 m, and clamping to eight cells
#: across THAT is a mesh that never finishes (the committed 2D fixture ran past its 300 s
#: budget). The corpus's thinnest real features sit near 2.5e-4 of the diagonal.
EXTENT_NOISE_FRACTION = 1e-6


def narrowest_extent(ext, diag: float, *, planar: bool = False) -> float:
    """The narrowest REAL dimension the resolution clamp must put cells across.

    Noise extents are dropped. A planar (2D) case has no thickness at all - its clamp
    spans the in-plane extents, so a tilted face whose box shows three real extents
    still drops the smallest. With nothing real left, the diagonal (no clamp)."""
    real = sorted(e for e in ext if e > EXTENT_NOISE_FRACTION * diag)
    if planar and len(real) == 3:
        real = real[1:]
    return real[0] if real else diag


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def port_flow_width_mm(port: dict) -> float | None:
    """The width the flow crosses at one declared port, in mm: a bore's diameter, a
    rectangle's shorter side, an annulus's radial gap, an area's equivalent diameter."""
    d, inner, outer = _num(port.get("diameter_mm")), _num(port.get("inner_diameter_mm")), \
        _num(port.get("outer_diameter_mm"))
    if inner is not None:
        # an annular port: the flow crosses the radial gap. The bore is outer_diameter_mm, or
        # diameter_mm when that is the larger number; a diameter_mm BELOW the centre body is
        # the gap itself (the intake files it that way - see port_binding._bore_mm)
        bore = outer if outer is not None and outer > inner else (d if d is not None and d > inner else None)
        if bore is not None:
            return (bore - inner) / 2.0
        if d is not None:
            return d
    if d is not None:
        return d
    w, h = _num(port.get("width_mm")), _num(port.get("height_mm"))
    if w is not None and h is not None:
        return min(w, h)
    a = _num(port.get("area_mm2"))
    if a is not None:
        return 2.0 * (a / 3.141592653589793) ** 0.5
    return None


def smallest_port_extent(ports) -> tuple[float, str] | None:
    """(metres, port name) of the narrowest declared flow port, or None without one."""
    best = None
    for p in ports or []:
        if not isinstance(p, dict) or str(p.get("type", "")) not in ("inlet", "outlet"):
            continue
        w = port_flow_width_mm(p)
        if w is not None and (best is None or w < best[0]):
            best = (w, str(p.get("name", "port")))
    return (best[0] / 1000.0, best[1]) if best else None


def resolution_extent(ext, diag: float, *, planar: bool = False, ports=None) -> tuple[float, str]:
    """The extent the resolution clamp puts cells across, and what it is: the narrowest real
    bbox dimension, or the smallest declared port when the flow crosses something narrower
    than the box. A volute's box is wide every way; its outlet bore is what the flow must
    cross, and eight cells across the box left a 21 mm element on a part whose bore is a
    few times that (volute_scroll_005: 9,293 cells, 83.6 degrees non-orthogonality)."""
    box = narrowest_extent(ext, diag, planar=planar)
    port = smallest_port_extent(ports)
    if port is not None and port[0] < box:
        return port[0], f"port:{port[1]}"
    return box, "bbox"

# VALIDATED JSON SCRATCH. The model may author a rich gmsh_spec.json, but the driver
# is the authority on what it supports - it REJECTS unknown/misspelled keys and
# malformed values instead of silently dropping them (which would run to completion
# with the wrong default: a false success). Defaults apply only AFTER validation, to
# omitted-but-recognized keys. To support a new capability, add its key here.
_KNOWN_KEYS = {"element_order", "size", "curvature_nodes", "groups",
               "default_group", "optimize", "extra_exports", "dimensionality",
               "domain_margin"}
_SIZE_FIELDS = {"mode", "value"}
_SIZE_MODES = {"factor", "absolute"}
# 3D groups target CAD surfaces (surface_tags); 2D planar groups target the
# boundary CURVES of the planar face (curve_tags) - one of the two, per mode.
_GROUP_FIELDS = {"name", "role", "surface_tags", "curve_tags"}
_EXPORTS = {"bdf", "unv"}
_DIMS = {"2D", "3D"}



def _final_node_bounds(gmsh) -> list[float]:
    # THE MESHED EXTENT: the coordinates the finished mesh actually occupies. Not
    # gmsh.model.getBoundingBox, which answers about the CAD model and, on curved BSpline faces,
    # returns a control-polygon hull that overstates the real extent - the 90-degree elbow read
    # 0.193 x 0.368 x 0.075 m as a hull while its mesh occupies 0.175 x 0.350 x 0.050 m. The
    # manifest publishes geometry.box_* as the actual meshed extent, so it has to be measured on
    # the mesh.
    _, coords, _ = gmsh.model.mesh.getNodes()
    if not len(coords):
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    xs, ys, zs = coords[0::3], coords[1::3], coords[2::3]
    return [float(xs.min()), float(ys.min()), float(zs.min()),
            float(xs.max()), float(ys.max()), float(zs.max())]


def _read_flow_topology(ws) -> str:
    try:
        return (Path(ws) / "flow_topology").read_text().strip().lower()
    except OSError:
        return ""


def _boundary_triangles(gmsh):
    """Node coordinates and the 3-node boundary triangles of the current mesh, as arrays
    indexed into the node array (gmsh tags are not contiguous)."""
    import numpy as np
    tags, coords, _ = gmsh.model.mesh.getNodes()
    pts = np.asarray(coords, dtype=float).reshape(-1, 3)
    idx = np.zeros(int(max(tags)) + 1 if len(tags) else 1, dtype=np.int64)
    idx[np.asarray(tags, dtype=np.int64)] = np.arange(len(tags))
    tris = []
    etypes, _etags, enodes = gmsh.model.mesh.getElements(2)
    for et, en in zip(etypes, enodes, strict=True):
        nn = gmsh.model.mesh.getElementProperties(et)[3]
        conn = np.asarray(en, dtype=np.int64).reshape(-1, nn)[:, :3]   # corners of tri3/tri6
        tris.append(idx[conn])
    faces = np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)
    return pts, faces


def deepest_point(candidates, boundary_points):
    """The candidate farthest from the boundary: the point the wall normals agree about. The
    chord orientation is a majority vote against this point; one a cell off the wall is inside
    but useless, and a wrong vote flips every chord the radius is read from."""
    import numpy as np
    from scipy.spatial import cKDTree
    c = np.asarray(candidates, dtype=float)
    depth, _ = cKDTree(np.asarray(boundary_points, dtype=float)).query(c)
    return c[int(np.argmax(depth))]


def _interior_point(gmsh, boundary_points):
    """The centroid of a volume element deep inside the fluid (see deepest_point)."""
    import numpy as np
    tags, coords, _ = gmsh.model.mesh.getNodes()
    pts = np.asarray(coords, dtype=float).reshape(-1, 3)
    idx = np.zeros(int(max(tags)) + 1 if len(tags) else 1, dtype=np.int64)
    idx[np.asarray(tags, dtype=np.int64)] = np.arange(len(tags))
    etypes, _etags, enodes = gmsh.model.mesh.getElements(3)
    cents = []
    for et, en in zip(etypes, enodes, strict=True):
        nn = gmsh.model.mesh.getElementProperties(et)[3]
        conn = idx[np.asarray(en, dtype=np.int64).reshape(-1, nn)[:, :4]]
        cents.append(pts[conn].mean(axis=1))
    if not cents:
        return pts.mean(axis=0)
    c = np.concatenate(cents)
    if len(c) > 20000:
        c = c[np.random.default_rng(0).choice(len(c), size=20000, replace=False)]
    return deepest_point(c, boundary_points)


def passage_sizes(radius, *, target: float = PASSAGE_CELLS_ACROSS, h_max: float,
                  h_min: float):
    """Element size at each wall point: 2r / target, held within [h_min, h_max]."""
    import numpy as np
    r = np.asarray(radius, dtype=float)
    return np.clip(2.0 * r / float(target), h_min, h_max)


def passage_size_callback(points, sizes):
    """A gmsh size callback that only TIGHTENS: the size of the nearest wall point, or what
    gmsh already wanted, whichever is smaller. Mid-passage points sit about one radius from
    the nearest wall, whose radius IS the local passage radius, so the field carries inward."""
    import numpy as np
    from scipy.spatial import cKDTree
    tree = cKDTree(np.asarray(points, dtype=float))
    s = np.asarray(sizes, dtype=float)

    def _cb(dim, tag, x, y, z, lc):
        _, i = tree.query((x, y, z))
        return min(float(lc), float(s[i]))
    return _cb


def measure_passage(points, faces, radius) -> dict:
    """Cells across the passage at every boundary point: twice the local radius over the mean
    length of the boundary edges meeting the point (the surface edge is what sizes the tets
    beside it). Median, 5th percentile (the narrowest wall, less a few outliers) and min."""
    import numpy as np
    pts = np.asarray(points, dtype=float)
    f = np.asarray(faces, dtype=np.int64)
    r = np.asarray(radius, dtype=float)
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


def _surface_interior_point(pts, faces):
    """A point deep inside a closed triangulated boundary (engines/passage.inside_point)."""
    import numpy as np
    import pyvista as pv

    from meshpipeline.engines.passage import inside_point
    f = np.asarray(faces, dtype=np.int64)
    surf = pv.PolyData(np.asarray(pts, dtype=float),
                       np.hstack([np.full((len(f), 1), 3, dtype=np.int64), f]).ravel())
    p = inside_point(surf)
    if p is None:
        raise ValueError("no point inside the surface")
    return p


def _passage_field(gmsh, ws, h: float, diag: float, *, discrete: bool = False) -> tuple:
    """For an internal-flow fluid domain: mesh once coarsely at the clamp size h, measure the
    local passage radius on that boundary (engines/radius_field.local_radius: half the
    inward chord to the opposite wall), and return (callback, surface_points, radius, note).
    A CLASSIFIED SURFACE is measured on the triangles it arrived with, before any meshing: they
    ARE its boundary, and a coarse mesh cleared off a discrete surface takes the surface with
    it (the elbow STL's remesh then ran past 13 CPU minutes without finishing, 2026-10-04).
    Anything missing (no flow_topology, no pyvista, a surface the chord cannot read) returns
    (None, None, None, why) and the clamp sizing stands - the gate still measures the result."""
    if _read_flow_topology(ws) != "internal":
        return None, None, None, "not an internal-flow domain"
    try:
        from meshpipeline.engines.radius_field import local_radius
    except Exception as exc:  # noqa: BLE001 - standalone use without pyvista
        return None, None, None, f"local radius unavailable ({type(exc).__name__})"
    try:
        if discrete:
            pts, faces = _boundary_triangles(gmsh)
            if len(faces) < 4:
                return None, None, None, "no boundary triangles on the surface"
            r = local_radius(pts, faces, _surface_interior_point(pts, faces), diag * 1e-5,
                             diag / 2.0)
            sizes = passage_sizes(r, h_max=h, h_min=h * PASSAGE_MIN_SIZE_FRACTION)
            return passage_size_callback(pts, sizes), pts, r, "local radius (input surface)"
        gmsh.model.mesh.generate(3)
        pts, faces = _boundary_triangles(gmsh)
        if len(faces) < 4:
            return None, None, None, "no boundary triangles on the coarse mesh"
        r = local_radius(pts, faces, _interior_point(gmsh, pts), diag * 1e-5, diag / 2.0)
        sizes = passage_sizes(r, h_max=h, h_min=h * PASSAGE_MIN_SIZE_FRACTION)
        gmsh.model.mesh.clear()
        return passage_size_callback(pts, sizes), pts, r, "local radius"
    except Exception as exc:  # noqa: BLE001 - a sizing aid must never lose the mesh
        if not discrete:
            gmsh.model.mesh.clear()
        return None, None, None, f"passage field failed ({type(exc).__name__}: {exc})"


def _allowed_roles() -> set:
    try:
        from meshpipeline.engines.purposes import PURPOSES
        _kinds = {"solid-volume", "fluid-volume", "surface-mesh"}
        def _req(p):
            r = p.requires_mesh_kind
            return {r} if isinstance(r, str) else set(r)
        return {r for p in PURPOSES.values()
                if _req(p) & _kinds
                for r in p.boundary_roles}
    except Exception:  # standalone/subprocess use without the package importable
        return {"fixed", "load", "contact", "free",
                "wall", "inlet", "outlet", "farfield", "symmetry", "empty"}


def _validate_spec(spec) -> list[str]:
    if not isinstance(spec, dict):
        return ["gmsh_spec.json must be a JSON object {…}"]
    errs: list[str] = []
    unknown = sorted(set(spec) - _KNOWN_KEYS)
    if unknown:
        errs.append(f"unknown key(s) {unknown} - the gmsh driver does not support them "
                    f"(a typo, or a capability that is not implemented). Recognized keys: "
                    f"{sorted(_KNOWN_KEYS)}.")
    if "dimensionality" in spec and str(spec["dimensionality"]).upper() not in _DIMS:
        errs.append(f"dimensionality must be '2D' or '3D', got {spec['dimensionality']!r}")
    if "element_order" in spec and str(spec["element_order"]) not in ("1", "2"):
        errs.append(f"element_order must be 1 or 2, got {spec['element_order']!r}")
    if "curvature_nodes" in spec:
        try:
            int(spec["curvature_nodes"])
        except (TypeError, ValueError):
            errs.append(f"curvature_nodes must be an integer, got {spec['curvature_nodes']!r}")
    if "optimize" in spec and not isinstance(spec["optimize"], bool):
        errs.append(f"optimize must be true or false, got {spec['optimize']!r}")
    if "size" in spec:
        sz = spec["size"]
        if not isinstance(sz, dict):
            errs.append("size must be an object {mode, value}")
        else:
            _u = sorted(set(sz) - _SIZE_FIELDS)
            if _u:
                errs.append(f"size has unknown field(s) {_u} - only mode, value")
            if sz.get("mode", "factor") not in _SIZE_MODES:
                errs.append(f"size.mode must be one of {sorted(_SIZE_MODES)}, got {sz.get('mode')!r}")
            _val = sz.get("value")
            if isinstance(_val, bool) or not isinstance(_val, (int, float)):
                errs.append(f"size.value must be a number, got {_val!r}")
    if "groups" in spec:
        groups = spec["groups"]
        if not isinstance(groups, list):
            errs.append("groups must be a list of {name, role, surface_tags}")
        else:
            for i, g in enumerate(groups):
                if not isinstance(g, dict):
                    errs.append(f"groups[{i}] must be an object {{name, role, surface_tags}}")
                    continue
                _u = sorted(set(g) - _GROUP_FIELDS)
                if _u:
                    errs.append(f"groups[{i}] has unknown field(s) {_u} - only name, role, surface_tags")
                if not str(g.get("name") or "").strip():
                    errs.append(f"groups[{i}].name is required")
                _roles = _allowed_roles()
                if g.get("role") not in _roles:
                    errs.append(f"groups[{i}].role must be one of {sorted(_roles)}, got {g.get('role')!r}")
                _is2d = str(spec.get("dimensionality", "3D")).upper() == "2D"
                _tagkey = "curve_tags" if _is2d else "surface_tags"
                _wrong = "surface_tags" if _is2d else "curve_tags"
                if _wrong in g:
                    errs.append(f"groups[{i}]: use {_tagkey} in a "
                                f"{'2D' if _is2d else '3D'} spec, not {_wrong}")
                if not isinstance(g.get(_tagkey), list):
                    errs.append(f"groups[{i}].{_tagkey} must be a list of integer "
                                f"{'curve' if _is2d else 'surface'} tags")
    if "extra_exports" in spec:
        ex = spec["extra_exports"]
        if not isinstance(ex, list):
            errs.append("extra_exports must be a list of formats")
        else:
            _bad = sorted({str(e) for e in ex if e not in _EXPORTS})
            if _bad:
                errs.append(f"extra_exports has unsupported format(s) {_bad} - only {sorted(_EXPORTS)}")
    return errs


def _mesh_planar(ws: Path, spec: dict, h: float, resolution: dict | None = None) -> int:
    import gmsh
    faces = [t for _, t in gmsh.model.getEntities(2)]
    if not faces:
        print("[GMSH] no faces in geometry.step - a 2D case needs a planar "
              "face/sheet body", file=sys.stderr)
        return 3
    xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
    ext = sorted([xmax - xmin, ymax - ymin, zmax - zmin])
    diag = (ext[0] ** 2 + ext[1] ** 2 + ext[2] ** 2) ** 0.5 or 1.0
    if ext[0] > 1e-6 * diag:
        print(f"[GMSH] geometry is not planar (thinnest extent {ext[0]:.3g} vs "
              f"diagonal {diag:.3g}) - a 2D case needs a FLAT face/sheet body; "
              "for a 3D part, declare the case 3D.", file=sys.stderr)
        return 3

    gmsh.model.addPhysicalGroup(2, faces, name="solid")
    all_curves = {t for _, t in gmsh.model.getEntities(1)}
    # RECONCILIATION (contract → artifact): a declared group with nonexistent
    # curve tags REJECTS - same rule as the 3D surface_tags path.
    _unknown = {str(g.get("name")): sorted(set(g.get("curve_tags") or []) - all_curves)
                for g in spec.get("groups", []) or []}
    _unknown = {n: t for n, t in _unknown.items() if t}
    if _unknown:
        print("[GMSH] gmsh_spec.json REJECTED - group(s) reference curve tags that do "
              f"not exist in the CAD: {_unknown}. Use the tags from geometry_report.",
              file=sys.stderr)
        return 6
    assigned: set[int] = set()
    for g in spec.get("groups", []) or []:
        tags = [t for t in (g.get("curve_tags") or []) if t in all_curves]
        if tags:
            gmsh.model.addPhysicalGroup(1, tags, name=str(g["name"]))
            assigned.update(tags)
    leftover = sorted(all_curves - assigned)
    if leftover:
        gmsh.model.addPhysicalGroup(1, leftover,
                                    name=str(spec.get("default_group", "free")))

    gmsh.model.mesh.generate(2)
    if spec.get("optimize", True):
        gmsh.model.mesh.optimize()
    order = int(spec.get("element_order", 2))
    if order > 1:
        gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)   # read by setOrder: set it first
        gmsh.model.mesh.setOrder(order)

    etypes, etags, _ = gmsh.model.mesh.getElements(2)
    all_tags = [t for arr in etags for t in arr]
    qualities = gmsh.model.mesh.getElementQualities(all_tags, "minSICN")
    n_elem = len(all_tags)
    n_nodes = len(gmsh.model.mesh.getNodes()[0])
    min_sicn = float(min(qualities)) if len(qualities) else 0.0
    low = int(sum(1 for q in qualities if q < SICN_FLOOR))
    fatal = []
    if n_elem == 0:
        fatal.append("no surface elements generated")
    if min_sicn <= 0.0:
        fatal.append("degenerate elements (SICN <= 0)")

    gmsh.option.setNumber("Mesh.SaveGroupsOfNodes", 1)   # *NSET per group (BC targets)
    gmsh.write(str(ws / "mesh.inp"))
    gmsh.write(str(ws / "mesh.msh"))
    for extra in spec.get("extra_exports", []) or []:
        if extra in ("bdf", "unv"):
            gmsh.write(str(ws / f"mesh.{extra}"))

    _role_by_name = {str(g["name"]): str(g.get("role", "free"))
                     for g in spec.get("groups", []) or []}
    # ACTUAL boundary groups read back from the meshed model (dim 1 in 2D)
    _actual_group_names = [gmsh.model.getPhysicalName(dim, tag)
                           for dim, tag in gmsh.model.getPhysicalGroups(1)]
    (ws / "quality.json").write_text(json.dumps({
        "cells": n_elem, "nodes": n_nodes, "element_order": order,
        "dimensionality": "2D",
        "min_sicn": round(min_sicn, 4),
        "sicn_low_fraction": round(low / n_elem, 6) if n_elem else 1.0,
        "fatal": fatal, "size_h": h,
        **(resolution or {}),
        "bounds": _final_node_bounds(gmsh),
        "groups": {name: str(_role_by_name.get(name, "free"))
                   for name in _actual_group_names},
        "default_group": str(spec.get("default_group", "free")),
        "default_group_used": bool(leftover),
    }, indent=1))
    print(f"[GMSH] 2D elements={n_elem} nodes={n_nodes} order={order} "
          f"min_sicn={min_sicn:.3f} low_frac={low / max(n_elem, 1):.4f}")
    return 0 if not fatal else 4


#: The closed fluid boundary a triangle-surface upload is staged as for internal flow (one named
#: solid per patch; engines/gmsh/gmsh_runner.stage_declared). Meshed when there is no geometry.step.
FLUID_BOUNDARY = "fluid_boundary.stl"


def _load_fluid_boundary(gmsh, path: Path) -> None:
    """The staged fluid boundary as gmsh geometry: each named solid of the STL becomes one discrete
    surface (tags 1..N in file order, the names geometry_report showed), the curves where patches
    meet are built, every surface gets its own parametrisation so it is REMESHED to the size
    asked for rather than inheriting the upload's triangles, and the volume they close is the one
    solid the rest of this driver meshes - exactly as it meshes a CAD solid."""
    gmsh.merge(str(path))
    gmsh.model.mesh.removeDuplicateNodes()
    gmsh.model.mesh.createTopology()
    gmsh.model.mesh.createGeometry()
    surfaces = [t for _, t in gmsh.model.getEntities(2)]
    loop = gmsh.model.geo.addSurfaceLoop(surfaces)
    gmsh.model.geo.addVolume([loop])
    gmsh.model.geo.synchronize()


def _refill_classified(gmsh, path: Path, spec: dict) -> list:
    """The staged fluid boundary loaded again, split by angle into surfaces gmsh can map (its own
    STL-remesh route), each new surface named by the patch its triangles came from, and the
    spec's groups rebuilt on those names. Returns the surfaces no group claimed."""
    import numpy as np
    from scipy.spatial import cKDTree

    # the tags the spec names are the per-patch load's: tag k is the k-th solid of the file
    gmsh.model.remove()
    gmsh.model.add("fea")
    gmsh.merge(str(path))
    named = {t: gmsh.model.getEntityName(2, t) for _, t in gmsh.model.getEntities(2)}
    ntags, coords, _ = gmsh.model.mesh.getNodes()
    pts = np.asarray(coords, dtype=float).reshape(-1, 3)
    index = np.zeros(int(max(ntags)) + 1, dtype=np.int64)
    index[np.asarray(ntags, dtype=np.int64)] = np.arange(len(ntags))

    def centroids(tag):
        _ty, _tg, nodes = gmsh.model.mesh.getElements(2, tag)
        if not len(nodes):
            return np.zeros((0, 3))
        return pts[index[np.asarray(nodes[0], dtype=np.int64).reshape(-1, 3)]].mean(axis=1)

    ref = [(centroids(t), n) for t, n in named.items()]
    tree = cKDTree(np.vstack([c for c, _ in ref]))
    label = np.concatenate([np.full(len(c), i) for i, (c, _) in enumerate(ref)])
    names = [n for _, n in ref]
    gmsh.model.mesh.removeDuplicateNodes()
    gmsh.model.mesh.classifySurfaces(40.0 * np.pi / 180.0, True, True, np.pi)
    ntags, coords, _ = gmsh.model.mesh.getNodes()
    pts = np.asarray(coords, dtype=float).reshape(-1, 3)
    index = np.zeros(int(max(ntags)) + 1, dtype=np.int64)
    index[np.asarray(ntags, dtype=np.int64)] = np.arange(len(ntags))
    by_name: dict[str, list[int]] = {}
    for _, t in gmsh.model.getEntities(2):
        c = centroids(t)
        if not len(c):
            continue
        _, k = tree.query(c)
        by_name.setdefault(names[int(np.bincount(label[k]).argmax())], []).append(t)
    gmsh.model.mesh.createGeometry()
    surfaces = [t for _, t in gmsh.model.getEntities(2)]
    loop = gmsh.model.geo.addSurfaceLoop(surfaces)
    gmsh.model.geo.addVolume([loop])
    gmsh.model.geo.synchronize()
    gmsh.model.addPhysicalGroup(3, [t for _, t in gmsh.model.getEntities(3)], name="solid")
    assigned: set[int] = set()
    for g in spec.get("groups", []) or []:
        wanted = {named.get(t) for t in (g.get("surface_tags") or [])}
        tags = sorted(t for n in wanted if n for t in by_name.get(n, []))
        if tags:
            gmsh.model.addPhysicalGroup(2, tags, name=str(g["name"]))
            assigned.update(tags)
    leftover = sorted(set(surfaces) - assigned)
    if leftover:
        gmsh.model.addPhysicalGroup(2, leftover, name=str(spec.get("default_group", "free")))
    return leftover


def _read_input_kind(ws) -> str:
    try:
        return (Path(ws) / "input_kind").read_text().strip()
    except OSError:
        return ""


def _read_far_field(ws) -> dict:
    try:
        d = json.loads((Path(ws) / "far_field_request.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _declared_names(ports) -> tuple[str, str]:
    """(body wall name, far-field name) the intake approved; 'body'/'farfield' without one."""
    walls = [str(p.get("name")) for p in ports or [] if isinstance(p, dict)
             and str(p.get("type") or "") == "wall" and p.get("name")]
    far = [str(p.get("name")) for p in ports or [] if isinstance(p, dict)
           and str(p.get("type") or "") == "farfield" and p.get("name")]
    return (walls[0] if walls else "body"), (far[0] if far else "farfield")


def _external_fluid(gmsh, ws, spec, discrete, surfaces, bmin, bmax, h, ports):
    """(fluid volumes, the body + far-field groups, facts for quality.json) of an external case."""
    from meshpipeline.engines.far_field import far_field_box, margins_from, ruler_of
    from meshpipeline.engines.gmsh.surface_volume import (
        external_fluid_cad,
        external_fluid_discrete,
        grade_from_body,
    )
    ffr = _read_far_field(ws)
    try:
        request_txt = (Path(ws) / "request.txt").read_text(errors="replace")
    except OSError:
        request_txt = ""
    margins = margins_from(spec.get("domain_margin"), ffr.get("requested_extents"), request_txt)
    ruler = ruler_of(bmin, bmax, ffr.get("flow_axis"), ffr.get("reference_length_m"))
    dmin, dmax = far_field_box(bmin, bmax, margins, flow_axis=ffr.get("flow_axis"),
                               reference_length_m=ffr.get("reference_length_m"))
    if discrete:
        vols, far = external_fluid_discrete(gmsh, surfaces, dmin, dmax)
        body = list(surfaces)
        origin = {t: t for t in body}
    else:
        if not gmsh.model.getEntities(3):
            return [], [], {}
        before = {t: [float(v) for v in gmsh.model.occ.getCenterOfMass(2, t)]
                  for _, t in gmsh.model.getEntities(2)}
        vols, far, body = external_fluid_cad(gmsh, dmin, dmax)
        origin = _face_origin(gmsh, body, before)
    span = max(float(dmax[k]) - float(dmin[k]) for k in range(3))
    h_far = max(float(h), span / 15.0)
    grade_from_body(gmsh, body, h_body=float(h), h_far=h_far, ruler=ruler)
    gmsh.option.setNumber("Mesh.MeshSizeMax", h_far)
    groups = _external_groups(spec.get("groups"), body, far, origin, ports)
    print(f"[GMSH] external: far-field box {[round(v, 4) for v in dmin]} .. "
          f"{[round(v, 4) for v in dmax]} ({margins}, ruler {ruler:.4g} m); body size {h:.4g} m "
          f"grading to {h_far:.4g} m", file=sys.stderr)
    facts = {"external": True, "domain_box": [list(dmin), list(dmax)],
             "body_bounds": [list(bmin), list(bmax)], "far_field_ruler": ruler,
             "far_field_margins": margins}
    if ffr.get("reference_length_m"):
        facts["reference_length"] = float(ffr["reference_length_m"])
    return vols, groups, facts


def _face_origin(gmsh, faces, before: dict) -> dict:
    """{face of the cut fluid: the body face it came from}, by nearest centre of mass - the box
    cut renumbers the body's faces but does not move them."""
    if not before:
        return {}
    import numpy as np
    from scipy.spatial import cKDTree
    old = list(before)
    tree = cKDTree(np.asarray([before[t] for t in old], dtype=float))
    out = {}
    for t in faces:
        _, i = tree.query(np.asarray(gmsh.model.occ.getCenterOfMass(2, t), dtype=float))
        out[int(t)] = int(old[int(i)])
    return out


def _external_groups(spec_groups, body, far, origin: dict, ports) -> list[dict]:
    """The groups of an external case. The builder's groups name the BODY's faces by the tags it
    was shown (geometry_report), before the far-field box was cut around it: each is carried to
    the faces those became; body faces it left out join its first wall group; every box face is
    its first far-field group. Without builder groups: the declared wall and far field."""
    wall, farfield = _declared_names(ports)
    groups = [g for g in (spec_groups or []) if isinstance(g, dict) and g.get("name")]
    if not groups:
        return [{"name": wall, "role": "wall", "surface_tags": list(body)},
                {"name": farfield, "role": "farfield", "surface_tags": list(far)}]
    out, used = [], set()
    far_groups = [g for g in groups if str(g.get("role")) == "farfield"]
    for g in groups:
        if g in far_groups:
            continue
        want = {int(t) for t in g.get("surface_tags") or []}
        tags = [t for t in body if origin.get(t) in want and t not in used]
        used.update(tags)
        out.append({**g, "surface_tags": tags})
    rest = [t for t in body if t not in used]
    if rest:
        first_wall = next((g for g in out if str(g.get("role")) == "wall"), None)
        if first_wall is not None:
            first_wall["surface_tags"] = [*first_wall["surface_tags"], *rest]
        else:
            out.append({"name": wall, "role": "wall", "surface_tags": rest})
    if far_groups:
        out.append({**far_groups[0], "surface_tags": list(far)})
        out.extend({**g, "surface_tags": []} for g in far_groups[1:])
    else:
        out.append({"name": farfield, "role": "farfield", "surface_tags": list(far)})
    return out


#: A builder's port group whose faces add up to more than this far from the declared opening's
#: area (either way) is not that opening (engines/region_check.PORT_AREA_BAND, the gate's band).
PORT_GROUP_BAND = (0.6, 1.6)


def _face_table(gmsh, surfaces, *, cad: bool) -> list[dict]:
    from meshpipeline.engines.gmsh.surface_volume import surface_table
    if cad:
        return [{"tag": int(t), "area": float(gmsh.model.occ.getMass(2, t)),
                 "centroid": [float(v) for v in gmsh.model.occ.getCenterOfMass(2, t)]}
                for t in surfaces]
    return surface_table(gmsh, surfaces)


def _checked_port_groups(gmsh, groups, surfaces, ports, *, cad: bool = False) -> list[dict]:
    """The builder's groups, with every PORT group held to the opening it names: a group whose
    faces add up to an area far from the declared opening (outside PORT_GROUP_BAND) is replaced
    by the face the engine's own binder finds for that port, and the faces it gave up join the
    first wall group. A builder that picked port faces by distance alone swept the wall next to
    each opening into the port (lab, 2026-10-06: inlets 2.4-5.3x the declared opening on three
    fluid domains, every gate green). Groups that hold their opening, and ports declared by
    location alone, are left exactly as written."""
    from meshpipeline.engines.gmsh.surface_volume import _port_area_m2, bind_ports
    declared = {str(p.get("name")): p for p in ports or []
                if isinstance(p, dict) and str(p.get("type") or "") in ("inlet", "outlet")}
    if not declared or not groups:
        return groups
    table = _face_table(gmsh, surfaces, cad=cad)
    area_of = {int(r["tag"]): float(r["area"]) for r in table}
    wrong = {}
    for g in groups:
        p = declared.get(str(g.get("name")))
        want = _port_area_m2(p) if p else None
        if not want:
            continue
        got = sum(area_of.get(int(t), 0.0) for t in g.get("surface_tags") or [])
        if not PORT_GROUP_BAND[0] <= got / want <= PORT_GROUP_BAND[1]:
            wrong[str(g["name"])] = got / want
    if not wrong:
        return groups
    bound = bind_ports(table, [declared[n] for n in wrong])
    out = [dict(g) for g in groups]
    freed: list[int] = []
    for g in out:
        if str(g.get("name")) in bound:
            freed += [int(t) for t in g.get("surface_tags") or [] if int(t) not in bound[str(g["name"])]]
            g["surface_tags"] = list(bound[str(g["name"])])
    taken = {t for tags in bound.values() for t in tags}
    for g in out:
        if str(g.get("name")) not in bound:
            g["surface_tags"] = [int(t) for t in g.get("surface_tags") or [] if int(t) not in taken]
    wall = next((g for g in out if str(g.get("role")) == "wall"), None)
    if wall is not None:
        wall["surface_tags"] = list(dict.fromkeys([*wall["surface_tags"], *freed]))
    print(f"[GMSH] port group(s) {', '.join(f'{n} ({r:.2f}x its declared opening)' for n, r in wrong.items())} "
          f"rebound to the opening face(s) {bound}; {len(freed)} face(s) returned to the wall",
          file=sys.stderr)
    return out


def _group_areas(gmsh) -> dict[str, float]:
    """{physical surface group: area in m2} of the generated surface mesh."""
    import numpy as np
    tags, xyz, _ = gmsh.model.mesh.getNodes()
    tags = np.asarray(tags, dtype=np.int64)
    idx = np.zeros(int(tags.max()) + 1, dtype=np.int64) if len(tags) else np.zeros(1, np.int64)
    idx[tags] = np.arange(len(tags))
    X = np.asarray(xyz, dtype=float).reshape(-1, 3)
    out: dict[str, float] = {}
    for dim, ptag in gmsh.model.getPhysicalGroups(2):
        name = gmsh.model.getPhysicalName(dim, ptag)
        a = 0.0
        for ent in gmsh.model.getEntitiesForPhysicalGroup(dim, ptag):
            etypes, _, enodes = gmsh.model.mesh.getElements(2, int(ent))
            for et, nodes in zip(etypes, enodes):
                per = {2: 3, 9: 6}.get(int(et))
                if not per:
                    continue
                c = idx[np.asarray(nodes, dtype=np.int64).reshape(-1, per)[:, :3]]
                cr = np.cross(X[c[:, 1]] - X[c[:, 0]], X[c[:, 2]] - X[c[:, 0]])
                a += 0.5 * float(np.linalg.norm(cr, axis=1).sum())
        out[name] = out.get(name, 0.0) + a
    return out


def _bound_groups(gmsh, surfaces, ports, *, cad: bool = False) -> list[dict]:
    """The groups when the builder named none: each declared port bound to its face
    (engines/gmsh/surface_volume.bind_ports), every other face the declared wall - the same
    reading for a CAD face table (mass properties) and a classified surface (its triangles)."""
    from meshpipeline.engines.gmsh.surface_volume import bind_ports, surface_table
    if cad:
        table = [{"tag": int(t), "area": float(gmsh.model.occ.getMass(2, t)),
                  "centroid": [float(v) for v in gmsh.model.occ.getCenterOfMass(2, t)]}
                 for t in surfaces]
    else:
        table = surface_table(gmsh, surfaces)
    bound = bind_ports(table, ports)
    roles = {str(p.get("name")): str(p.get("type")) for p in ports or [] if isinstance(p, dict)}
    used = {t for tags in bound.values() for t in tags}
    wall, _ = _declared_names(ports)
    groups = [{"name": n, "role": roles.get(n, "inlet"), "surface_tags": tags}
              for n, tags in sorted(bound.items())]
    rest = [t for t in surfaces if t not in used]
    if rest:
        groups.append({"name": wall, "role": "wall", "surface_tags": rest})
    print(f"[GMSH] no groups in the spec: ports bound to faces {bound}; {len(rest)} face(s) are "
          "the wall", file=sys.stderr)
    return groups


def main(workspace: str) -> int:
    ws = Path(workspace)
    try:
        spec = json.loads((ws / "gmsh_spec.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[GMSH] gmsh_spec.json could not be read as JSON: {exc}", file=sys.stderr)
        return 5
    _problems = _validate_spec(spec)
    if _problems:
        print("[GMSH] gmsh_spec.json REJECTED (fix these and re-run - nothing was meshed):\n  - "
              + "\n  - ".join(_problems), file=sys.stderr)
        return 6
    # THE SOURCE, in this order: the fluid boundary staged from the upload for internal flow
    # (gmsh_runner.stage_declared, one named solid per patch), else the CAD solid as it is, else
    # the uploaded surface itself (input.stl), classified into faces here.
    boundary = ws / FLUID_BOUNDARY
    geom = ws / "geometry.step"
    surface = ws / "input.stl"
    if not boundary.exists() and not geom.exists() and not surface.exists():
        print(f"[GMSH] geometry.step missing in {ws} (and no staged fluid_boundary.stl or "
              "input.stl)", file=sys.stderr)
        return 2
    # The INTAKE-DECLARED dimensionality (neutral workspace file) is authoritative;
    # the spec key is optional but may not contradict it.
    _decl = ""
    try:
        _decl = (ws / "dimensionality").read_text().strip().upper()
    except OSError:
        pass
    if _decl in _DIMS:
        _spec_dim = str(spec.get("dimensionality", _decl)).upper()
        if _spec_dim != _decl:
            print(f"[GMSH] gmsh_spec.json REJECTED - dimensionality {_spec_dim!r} "
                  f"contradicts the case's declared {_decl!r}.", file=sys.stderr)
            return 6
        spec["dimensionality"] = _decl

    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 1)
        # PINNED: one thread and a fixed seed, so a staged surface gives the same mesh wherever
        # it runs. Threaded meshing inserts nodes in the order threads finish, and the worst
        # tet with it; a staged tee read worst SICN 0.18, 0.12 and 0.002 on three lab runs
        # (2026-10-04), where three local runs of each of three cases matched to the digit.
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Mesh.RandomSeed", 1)
        # the model's bounding box from its surface, not OpenCascade's loose envelope of B-spline
        # control points: the element size is a factor of this diagonal and the resolution clamp
        # reads its narrowest side (a 132 mm Supra read 570 x 492 x 401 mm without it)
        gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
        gmsh.model.add("fea")
        # an EXTERNAL BODY is cut out of a far-field box here; an external fluid domain the user
        # prepared (the air box itself) is meshed as it is, like any other fluid domain
        external = (_read_flow_topology(ws) == "external"
                    and _read_input_kind(ws) in ("solid-body", "body-surface"))
        # the staged fluid boundary first: it exists only when the fluid had to be derived from
        # the upload for internal flow (a surface, or a wall), and then geometry.step - if any -
        # is the metal
        staged = boundary.exists() and not external
        # a surface upload with no B-rep and nothing staged: its triangles are split into faces
        # wherever the surface folds sharply and reparametrised (engines/gmsh/surface_volume.py) -
        # gmsh's own recipe for remeshing an STL - instead of the run stopping on a missing
        # geometry.step
        classified = not staged and not geom.exists()
        # both are DISCRETE: the passage radius is read off their own triangles
        discrete = staged or classified
        surfaces: list[int] = []
        if staged:
            _load_fluid_boundary(gmsh, boundary)
        elif classified:
            from meshpipeline.engines.gmsh.surface_volume import classify_closed_surface
            surfaces = classify_closed_surface(gmsh, str(surface))
            print(f"[GMSH] surface input: {len(surfaces)} face(s) classified from input.stl",
                  file=sys.stderr)
        else:
            gmsh.model.occ.importShapes(str(geom))
            gmsh.model.occ.synchronize()

        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        diag = ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5
        size_spec = spec.get("size") or {"mode": "factor", "value": 0.04}
        h_req = (float(size_spec["value"]) * diag
                 if size_spec.get("mode", "factor") == "factor"
                 else float(size_spec["value"]))
        # RESOLUTION CLAMP. gmsh sizes from a factor of the DIAGONAL, but a duct's
        # diagonal is its length - 4% of it can exceed the bore, leaving a handful of
        # cells across the flow (the corpus's 6,455-cell "passes"). Force at least
        # MIN_CELLS_ACROSS elements across the narrowest bbox extent, whatever the
        # factor gives. Only ever tightens h; never coarsens an already-fine request.
        ext = [xmax - xmin, ymax - ymin, zmax - zmin]
        _is2d = str(spec.get("dimensionality", "3D")).upper() == "2D"
        # the ports the intake declared (neutral workspace file); absent on an FEA part
        _ports = []
        try:
            _ports = json.loads((ws / "port_declaration.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if _is2d and discrete:
            print("[GMSH] a 2D case needs a planar CAD face (STEP/IGES); a triangulated surface "
                  "has none", file=sys.stderr)
            return 3
        min_ext, _basis = resolution_extent(ext, diag, planar=_is2d, ports=_ports)
        # EXTERNAL flow is not confined by the body: the clamp's "cells across the narrowest
        # dimension" is a passage rule, and across a wing's thickness it would refine the whole
        # far field to the thickness. The body size stands; the box grades away from it.
        h = h_req if external else min(h_req, min_ext / MIN_CELLS_ACROSS)
        if external:
            # THE BODY'S THINNEST DIMENSION gets a few elements: a wing 79 mm thick under a 68 mm
            # body size left its trailing edge to one element and boundary-locked slivers no
            # optimiser can move (ONERA M6: min SICN 0.04; at a sixth of the thickness 0.08 with
            # one sliver left, 2026-10-04). Bounded below so a thin plate cannot ask for millions.
            h = min(h, max(narrowest_extent(ext, diag) / EXTERNAL_THIN_DIVISIONS,
                           diag / EXTERNAL_MAX_DIVISIONS))
        if h < h_req:
            _what = (f"the {min_ext:.4g} m narrow dimension" if _basis == "bbox"
                     else f"the {min_ext:.4g} m flow width of {_basis[5:]}")
            print(f"[GMSH] resolution clamp: element size {h_req:.4g} m would put only "
                  f"{min_ext / h_req:.1f} cells across {_what}; "
                  f"tightened to {h:.4g} m ({MIN_CELLS_ACROSS} across).", file=sys.stderr)
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        gmsh.option.setNumber("Mesh.MeshSizeMin", h / 20.0)
        # CURVATURE SIZING. Off for a CLASSIFIED upload: it reads the facets' kinks as curvature
        # and refined a 69 mm elbow past 300 s of surface meshing without finishing
        # (bend_elbow_021 STL, 2026-10-04) - the passage field sizes it. On for the STAGED fluid
        # boundary: it reads each patch's own parametrisation, not the upload's facets, and
        # turning it off dropped the worst tet of a capped housing from SICN 0.38 to 0.02
        # (2026-10-04). On for a CAD face, which has a real curvature.
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature",
                              0 if classified else int(spec.get("curvature_nodes", 24)))
        _resolution = ({"size_h_requested": round(h_req, 8), "min_extent_basis": "external"}
                       if external else
                       {"size_h_requested": round(h_req, 8),
                        "min_extent": round(min_ext, 8),
                        "min_extent_basis": _basis,
                        "cells_across_min": round(min_ext / h, 3) if h > 0 else 0.0})

        if _is2d:
            return _mesh_planar(ws, spec, h, resolution=_resolution)

        # THE VOLUME. Internal CAD: the solids as they are. The staged fluid boundary: the one
        # volume its patches close. A classified surface: the volume it closes. External: the
        # fluid between the body and a far-field box (box minus body), sized in the unit the
        # domain-extent gate judges it in (engines/far_field.py).
        _external: dict = {}
        if external:
            vols, _groups_auto, _external = _external_fluid(gmsh, ws, spec, classified, surfaces,
                                                            (xmin, ymin, zmin), (xmax, ymax, zmax),
                                                            h, _ports)
            if not vols:
                print("[GMSH] external flow needs a closed body: no solid could be cut out of "
                      "the far-field box", file=sys.stderr)
                return 3
            spec["groups"] = _groups_auto
        elif staged:
            vols = [t for _, t in gmsh.model.getEntities(3)]
        elif classified:
            from meshpipeline.engines.gmsh.surface_volume import internal_volume_discrete
            vols = internal_volume_discrete(gmsh, surfaces)
            if not spec.get("groups"):
                spec["groups"] = _bound_groups(gmsh, surfaces, _ports)
            else:
                spec["groups"] = _checked_port_groups(gmsh, spec["groups"], surfaces, _ports)
        else:
            vols = [t for _, t in gmsh.model.getEntities(3)]
            _faces2 = [t for _, t in gmsh.model.getEntities(2)]
            if not spec.get("groups") and any(isinstance(p, dict) and p.get("type") in
                                               ("inlet", "outlet") for p in _ports):
                spec["groups"] = _bound_groups(gmsh, _faces2, _ports, cad=True)
            elif spec.get("groups"):
                spec["groups"] = _checked_port_groups(gmsh, spec["groups"], _faces2, _ports,
                                                      cad=True)
        # Physical groups: every volume is the solid; surfaces per the spec's
        # contracted names; unassigned surfaces land in the default group so
        # the .inp has a complete, named boundary decomposition.
        if not vols:
            print("[GMSH] no solid volumes in geometry.step - cannot mesh a "
                  "solid for FEA (is this a surface-only CAD file?)", file=sys.stderr)
            return 3
        gmsh.model.addPhysicalGroup(3, vols, name="solid")
        all_surfs = {t for _, t in gmsh.model.getBoundary([(3, v) for v in vols], oriented=False,
                                                          combined=True)} \
            if (external or classified) else {t for _, t in gmsh.model.getEntities(2)}
        # RECONCILIATION (contract -> artifact): a declared group whose tags do not exist
        # must REJECT, not silently vanish - the old `if tags:` dropped it while the
        # manifest still listed it (the group existed on paper, not in the deck).
        _unknown = {str(g.get("name")): sorted(set(g.get("surface_tags") or []) - all_surfs)
                    for g in spec.get("groups", []) or []}
        _unknown = {n: t for n, t in _unknown.items() if t}
        if _unknown:
            print("[GMSH] gmsh_spec.json REJECTED - group(s) reference surface tags that do "
                  f"not exist in the CAD: {_unknown}. Use the tags from geometry_report.",
                  file=sys.stderr)
            return 6
        assigned: set[int] = set()
        for g in spec.get("groups", []) or []:
            tags = [t for t in (g.get("surface_tags") or []) if t in all_surfs]
            if tags:
                gmsh.model.addPhysicalGroup(2, tags, name=str(g["name"]))
                assigned.update(tags)
        leftover = sorted(all_surfs - assigned)
        if leftover:
            gmsh.model.addPhysicalGroup(2, leftover,
                                        name=str(spec.get("default_group", "free")))

        # PASSAGE SIZING (fluid domains): elements from the local radius, about
        # PASSAGE_CELLS_ACROSS across every passage, instead of one size for the whole part.
        _cb, _spts, _srad, _why = _passage_field(gmsh, ws, h, diag, discrete=discrete)
        _passage: dict = {"passage_sizing": _why, "passage_cells_across_target": PASSAGE_CELLS_ACROSS}
        if _cb is not None:
            gmsh.model.mesh.setSizeCallback(_cb)
        else:
            print(f"[GMSH] passage sizing not applied: {_why}", file=sys.stderr)
        try:
            gmsh.model.mesh.generate(3)
            if staged and not any(len(t) for t in gmsh.model.mesh.getElements(3)[1]):
                # gmsh can fail to recover the boundary, say so in its log and carry on with
                # an empty volume: that is a failed fill too
                raise RuntimeError("no volume elements were generated")
        except Exception as exc:  # noqa: BLE001 - a staged surface gets one other remesh, below
            if not staged:
                raise
            # THE PATCHES, REMESHED EACH ON ITS OWN, DID NOT CLOSE INTO A FILLABLE BOUNDARY (a long
            # bent wall can come back with facets crossing). Once more the way gmsh remeshes an
            # STL itself - split by angle into patches it can map - with every group carried over
            # by the patch name its surfaces came from.
            print(f"[GMSH] the staged boundary did not fill ({exc}); remeshing it split by "
                  "angle instead", file=sys.stderr)
            if _cb is not None:
                gmsh.model.mesh.removeSizeCallback()
            # a failed fill leaves gmsh's meshing state behind it: start it afresh, same options
            gmsh.finalize()
            gmsh.initialize(interruptible=False)
            gmsh.option.setNumber("General.Terminal", 1)
            gmsh.option.setNumber("General.NumThreads", 1)
            gmsh.option.setNumber("Mesh.RandomSeed", 1)
            gmsh.option.setNumber("Mesh.MeshSizeMax", h)
            gmsh.option.setNumber("Mesh.MeshSizeMin", h / 20.0)
            gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", int(spec.get("curvature_nodes", 24)))
            leftover = _refill_classified(gmsh, boundary, spec)
            _cb, _spts, _srad, _why = _passage_field(gmsh, ws, h, diag, discrete=True)
            _passage = {"passage_sizing": _why, "passage_cells_across_target": PASSAGE_CELLS_ACROSS,
                        "surface_route": "classified by angle"}
            if _cb is not None:
                gmsh.model.mesh.setSizeCallback(_cb)
            gmsh.model.mesh.generate(3)
        if _cb is not None:
            gmsh.model.mesh.removeSizeCallback()
        if spec.get("optimize", True):
            gmsh.model.mesh.optimize("Netgen")
        if _srad is not None:
            # measured on the FINAL boundary: the radius field carried over from the coarse
            # surface (nearest point; the radius does not change with refinement)
            try:
                import numpy as np
                from scipy.spatial import cKDTree
                _fpts, _ffaces = _boundary_triangles(gmsh)
                _, _near = cKDTree(np.asarray(_spts)).query(_fpts)
                _passage["passage_cells_across_local"] = measure_passage(
                    _fpts, _ffaces, np.asarray(_srad)[_near])
                print(f"[GMSH] passage resolution: {_passage['passage_cells_across_local']}",
                      file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - evidence, not a verdict
                print(f"[GMSH] passage measurement failed: {exc}", file=sys.stderr)
        order = int(spec.get("element_order", 2))
        if order > 1:
            # BEFORE setOrder: the option is read while the high-order nodes are placed, so set
            # after it (as it was) it optimised nothing and curved elements shipped as snapped
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)  # elastic+optim
            gmsh.model.mesh.setOrder(order)

        # Quality: SICN over the volume elements (signed inverse condition
        # number - Gmsh's standard quality measure; 0 = degenerate, 1 = ideal).
        etypes, etags, _ = gmsh.model.mesh.getElements(3)
        all_tags = [t for arr in etags for t in arr]
        qualities = gmsh.model.mesh.getElementQualities(all_tags, "minSICN")
        n_elem = len(all_tags)
        n_nodes = len(gmsh.model.mesh.getNodes()[0])
        min_sicn = float(min(qualities)) if len(qualities) else 0.0
        low = int(sum(1 for q in qualities if q < SICN_FLOOR))
        fatal = []
        if n_elem == 0:
            fatal.append("no volume elements generated")
        if min_sicn <= 0.0:
            fatal.append("degenerate elements (SICN <= 0)")

        _areas: dict = {}
        try:
            _areas = {k: round(v, 10) for k, v in _group_areas(gmsh).items()}
        except Exception as exc:  # noqa: BLE001 - evidence; the port-area gate then judges nothing
            print(f"[GMSH] group areas not measured: {exc}", file=sys.stderr)

        gmsh.option.setNumber("Mesh.SaveGroupsOfNodes", 1)   # *NSET per group (BC targets)
        gmsh.write(str(ws / "mesh.inp"))
        gmsh.write(str(ws / "mesh.msh"))
        for extra in spec.get("extra_exports", []) or []:
            if extra in ("bdf", "unv"):
                gmsh.write(str(ws / f"mesh.{extra}"))

        _role_by_name = {str(g["name"]): str(g.get("role", "free"))
                         for g in spec.get("groups", []) or []}
        _actual_group_names = [gmsh.model.getPhysicalName(dim, tag)
                               for dim, tag in gmsh.model.getPhysicalGroups(2)]
        (ws / "quality.json").write_text(json.dumps({
            "cells": n_elem, "nodes": n_nodes, "element_order": order,
            "min_sicn": round(min_sicn, 4),
            "sicn_low_fraction": round(low / n_elem, 6) if n_elem else 1.0,
            "fatal": fatal, "size_h": h,
            # each named group's area: the shared port-area gate (engines/region_check.py)
            **({"patch_areas_m2": _areas} if _areas else {}),
            **_resolution,
            **_passage,
            "bounds": _final_node_bounds(gmsh),
            # groups = the ACTUAL physical groups present in the meshed model (read back),
            # role-annotated from the spec - the manifest must reflect the artifact, not
            # echo the request (a dropped group used to survive here on paper).
            "groups": {name: str(_role_by_name.get(name, "free"))
                       for name in _actual_group_names},
            "default_group": str(spec.get("default_group", "free")),
            # whether the default group EXISTS in the deck (surfaces were left
            # unassigned) - finalize must not fabricate a group the mesh lacks:
            # a phantom entry fails the patch contract as an undeclared extra.
            "default_group_used": bool(leftover),
            **({"surface_source": "input.stl, classified"} if classified else {}),
            **({"surface_source": "fluid_boundary.stl, staged"} if staged else {}),
            **_external,
        }, indent=1))
        print(f"[GMSH] elements={n_elem} nodes={n_nodes} order={order} "
              f"min_sicn={min_sicn:.3f} low_frac={low / max(n_elem, 1):.4f}")
        return 0 if not fatal else 4
    finally:
        gmsh.finalize()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
