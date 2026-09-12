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
               "default_group", "optimize", "extra_exports", "dimensionality"}
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


def _passage_field(gmsh, ws, h: float, diag: float) -> tuple:
    """For an internal-flow fluid domain: mesh once coarsely at the clamp size h, measure the
    local passage radius on that boundary (engines/vmtk/lumen_staging.local_radius: half the
    inward chord to the opposite wall), and return (callback, surface_points, radius, note).
    Anything missing (no flow_topology, no pyvista, a surface the chord cannot read) returns
    (None, None, None, why) and the clamp sizing stands - the gate still measures the result."""
    if _read_flow_topology(ws) != "internal":
        return None, None, None, "not an internal-flow domain"
    try:
        from meshpipeline.engines.vmtk.lumen_staging import local_radius
    except Exception as exc:  # noqa: BLE001 - standalone use without pyvista
        return None, None, None, f"local radius unavailable ({type(exc).__name__})"
    try:
        gmsh.model.mesh.generate(3)
        pts, faces = _boundary_triangles(gmsh)
        if len(faces) < 4:
            return None, None, None, "no boundary triangles on the coarse mesh"
        r = local_radius(pts, faces, _interior_point(gmsh, pts), diag * 1e-5, diag / 2.0)
        sizes = passage_sizes(r, h_max=h, h_min=h * PASSAGE_MIN_SIZE_FRACTION)
        gmsh.model.mesh.clear()
        return passage_size_callback(pts, sizes), pts, r, "local radius"
    except Exception as exc:  # noqa: BLE001 - a sizing aid must never lose the mesh
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
        gmsh.model.mesh.setOrder(order)
        gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)

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
    geom = ws / "geometry.step"
    if not geom.exists():
        print(f"[GMSH] geometry.step missing in {ws}", file=sys.stderr)
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
        gmsh.model.add("fea")
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
        min_ext, _basis = resolution_extent(ext, diag, planar=_is2d, ports=_ports)
        h = min(h_req, min_ext / MIN_CELLS_ACROSS)
        if h < h_req:
            _what = (f"the {min_ext:.4g} m narrow dimension" if _basis == "bbox"
                     else f"the {min_ext:.4g} m flow width of {_basis[5:]}")
            print(f"[GMSH] resolution clamp: element size {h_req:.4g} m would put only "
                  f"{min_ext / h_req:.1f} cells across {_what}; "
                  f"tightened to {h:.4g} m ({MIN_CELLS_ACROSS} across).", file=sys.stderr)
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        gmsh.option.setNumber("Mesh.MeshSizeMin", h / 20.0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature",
                              int(spec.get("curvature_nodes", 24)))
        _resolution = {"size_h_requested": round(h_req, 8),
                       "min_extent": round(min_ext, 8),
                       "min_extent_basis": _basis,
                       "cells_across_min": round(min_ext / h, 3) if h > 0 else 0.0}

        if _is2d:
            return _mesh_planar(ws, spec, h, resolution=_resolution)

        # Physical groups: every volume is the solid; surfaces per the spec's
        # contracted names; unassigned surfaces land in the default group so
        # the .inp has a complete, named boundary decomposition.
        vols = [t for _, t in gmsh.model.getEntities(3)]
        if not vols:
            print("[GMSH] no solid volumes in geometry.step - cannot mesh a "
                  "solid for FEA (is this a surface-only CAD file?)", file=sys.stderr)
            return 3
        gmsh.model.addPhysicalGroup(3, vols, name="solid")
        all_surfs = {t for _, t in gmsh.model.getEntities(2)}
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
        _cb, _spts, _srad, _why = _passage_field(gmsh, ws, h, diag)
        _passage: dict = {"passage_sizing": _why, "passage_cells_across_target": PASSAGE_CELLS_ACROSS}
        if _cb is not None:
            gmsh.model.mesh.setSizeCallback(_cb)
        else:
            print(f"[GMSH] passage sizing not applied: {_why}", file=sys.stderr)
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
            gmsh.model.mesh.setOrder(order)
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)  # elastic+optim

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
        }, indent=1))
        print(f"[GMSH] elements={n_elem} nodes={n_nodes} order={order} "
              f"min_sicn={min_sicn:.3f} low_frac={low / max(n_elem, 1):.4f}")
        return 0 if not fatal else 4
    finally:
        gmsh.finalize()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
