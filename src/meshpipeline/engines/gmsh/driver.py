# Responsibility: Execute the Gmsh meshing steps against a declarative specification.
# Boundaries: deterministic emission from declared values; the model authors the spec, never the Gmsh calls.
from __future__ import annotations

import json
import sys
from pathlib import Path

SICN_FLOOR = 0.1   # shared with the executor gate + quality criteria

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

#: The deterministic optimizer ladder for the 3D FV metrics: gmsh's tet optimizer (""),
#: Netgen, then node relocation, cycled up to the configured pass budget. Applied only
#: while the measured metrics still miss the WARN targets - the sequence is fixed, so a
#: re-run of the same spec walks the same passes.
_FV_LADDER_CYCLE = ("", "Netgen", "Relocate3D")

#: gmsh element types -> element order, per meshed dimension (setOrder is asked for an
#: order; these read back what the mesh actually contains).
_ORDER_BY_TYPE = {3: {4: 1, 11: 2},    # tet4 / tet10
                  2: {2: 1, 9: 2}}     # tri3 / tri6


def _fv():
    """The FV metrics module, under either invocation of this driver.

    Production runs the driver as meshpipeline.engines.gmsh.driver (the installed
    distribution); the standalone tests run it as engines.gmsh.driver with cwd inside the
    package (same fallback _allowed_roles uses).
    """
    try:
        from meshpipeline.engines.gmsh import fv_metrics
    except ImportError:
        from engines.gmsh import fv_metrics
    return fv_metrics


def _fv_thresholds() -> dict:
    """The FV bars: from the declared settings when the package is importable, else the
    catalogue defaults verbatim (settings/inventory.py is the authority on the values)."""
    try:
        from meshpipeline.engines.gmsh import settings as gq
        return {"nonortho_hard": gq.GMSH_FV_NONORTHO_HARD,
                "nonortho_warn": gq.GMSH_FV_NONORTHO_WARN,
                "skew_internal_hard": gq.GMSH_FV_SKEW_INTERNAL_HARD,
                "skew_boundary_hard": gq.GMSH_FV_SKEW_BOUNDARY_HARD,
                "max_passes": gq.GMSH_FV_OPTIMIZE_MAX_PASSES}
    except ImportError:
        return {"nonortho_hard": 70.0, "nonortho_warn": 65.0,
                "skew_internal_hard": 4.0, "skew_boundary_hard": 20.0,
                "max_passes": 5}


def _fv_targets_met(m: dict, th: dict) -> bool:
    # The ladder polishes to the WARN bar (meshQualityControls maxNonOrtho 65), not merely
    # under the hard bar - stopping at 69.9 would ship a mesh the advisory still flags.
    return (m["max_non_ortho"] <= th["nonortho_warn"]
            and m["max_skewness_internal"] <= th["skew_internal_hard"]
            and m["max_skewness_boundary"] <= th["skew_boundary_hard"])


def _fv_measure_and_optimize(gmsh) -> tuple[dict | None, list]:
    """Measure the 3D mesh's FV metrics; while they miss the targets, walk the ladder.

    Returns (final metrics or None on measurement failure, the pass-by-pass history).
    Fail-closed by construction: on None the FV keys never reach quality.json, and the
    fv_quality gate then rejects a fluid-domain mesh as unmeasured.
    """
    fv = _fv()
    th = _fv_thresholds()
    n = int(th["max_passes"])
    ladder = [_FV_LADDER_CYCLE[i % len(_FV_LADDER_CYCLE)] for i in range(n)]
    history: list[dict] = []

    def _snap(label: str, m: dict) -> dict:
        return {"pass": label, "max_non_ortho": m["max_non_ortho"],
                "max_skewness_internal": m["max_skewness_internal"],
                "max_skewness_boundary": m["max_skewness_boundary"]}

    try:
        m = fv.measure_model(gmsh)
    except Exception as exc:  # noqa: BLE001 - a measurement crash must not kill the build
        print(f"[GMSH] FV quality measurement failed: {exc}", file=sys.stderr)
        return None, [{"pass": "initial", "error": str(exc)}]
    history.append(_snap("initial", m))
    for name in ladder:
        if _fv_targets_met(m, th):
            break
        gmsh.model.mesh.optimize(name)
        try:
            m = fv.measure_model(gmsh)
        except Exception as exc:  # noqa: BLE001
            print(f"[GMSH] FV quality measurement failed after optimize({name!r}): {exc}",
                  file=sys.stderr)
            history.append({"pass": name or "tet", "error": str(exc)})
            return None, history
        history.append(_snap(name or "tet", m))
    return m, history


def _delivered_order(gmsh, dim: int) -> int | None:
    """The element order the mesh ACTUALLY contains (None = mixed/unrecognized types)."""
    etypes, _, _ = gmsh.model.mesh.getElements(dim)
    orders = {_ORDER_BY_TYPE[dim].get(int(t)) for t in etypes}
    if len(orders) != 1 or None in orders:
        return None
    return orders.pop()


def _resolve_element_order(ws: Path, spec: dict) -> tuple[str, int]:
    """Enforce the INTAKE-DECLARED element_order (engine_params.json) end-to-end.

    The user's declared param is authoritative, exactly like the `dimensionality` file: a
    spec that contradicts it REJECTS, a spec that omits it inherits it. Only without an
    intake declaration does the spec (or the order-2 default) decide. Returns
    (source, rc): rc 0 = resolved into spec["element_order"], else the driver exit code.
    The audit that forced this: intake stored element_order=1, the builder-authored spec
    said 2 (the prompt's example value), and the delivered .msh files were tet10.
    """
    ep = ws / "engine_params.json"
    declared: str | None = None
    if ep.exists():
        try:
            raw = json.loads(ep.read_text()).get("element_order")
            declared = None if raw is None else str(raw)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[GMSH] engine_params.json could not be read: {exc} - the user's "
                  "declared params are unreadable, refusing to mesh with guessed defaults",
                  file=sys.stderr)
            return "intake", 2
    if declared is None:
        return ("spec" if "element_order" in spec else "default"), 0
    if declared not in ("1", "2"):
        print(f"[GMSH] engine_params.json declares element_order {declared!r} - not a "
              "supported order (1 or 2); refusing to mesh with a guessed default",
              file=sys.stderr)
        return "intake", 2
    spec_order = spec.get("element_order")
    if spec_order is not None and str(spec_order) != declared:
        print(f"[GMSH] gmsh_spec.json REJECTED - element_order {spec_order!r} contradicts "
              f"the user's declared element_order {declared!r} (the intake declaration is "
              "enforced; omit the key or match it).", file=sys.stderr)
        return "intake", 6
    spec["element_order"] = int(declared)
    return "intake", 0


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


def _mesh_planar(ws: Path, spec: dict, h: float, order_source: str = "default") -> int:
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
    # the DELIVERED order, read back from the mesh - never an echo of the request
    delivered = _delivered_order(gmsh, 2) if n_elem else None
    if n_elem and delivered != order:
        fatal.append(f"delivered element order {delivered} does not match the requested "
                     f"order {order} ({order_source})")

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
        "cells": n_elem, "nodes": n_nodes,
        "element_order": delivered if delivered is not None else order,
        "element_order_requested": order,
        "element_order_source": order_source,
        "dimensionality": "2D",
        "min_sicn": round(min_sicn, 4),
        "sicn_low_fraction": round(low / n_elem, 6) if n_elem else 1.0,
        "fatal": fatal, "size_h": h,
        "bounds": _final_node_bounds(gmsh),
        "groups": {name: str(_role_by_name.get(name, "free"))
                   for name in _actual_group_names},
        "default_group": str(spec.get("default_group", "free")),
        "default_group_used": bool(leftover),
    }, indent=1))
    print(f"[GMSH] 2D elements={n_elem} nodes={n_nodes} order={order} "
          f"({order_source}) min_sicn={min_sicn:.3f} low_frac={low / max(n_elem, 1):.4f}")
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
    # The INTAKE-DECLARED element_order (engine_params.json) is likewise authoritative:
    # the spec may not contradict it, and omitting it inherits it (never the default).
    _order_source, _order_rc = _resolve_element_order(ws, spec)
    if _order_rc:
        return _order_rc

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
        h = (float(size_spec["value"]) * diag
             if size_spec.get("mode", "factor") == "factor"
             else float(size_spec["value"]))
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        gmsh.option.setNumber("Mesh.MeshSizeMin", h / 20.0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature",
                              int(spec.get("curvature_nodes", 24)))

        _is2d = str(spec.get("dimensionality", "3D")).upper() == "2D"
        if _is2d:
            return _mesh_planar(ws, spec, h, order_source=_order_source)

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

        gmsh.model.mesh.generate(3)
        if spec.get("optimize", True):
            gmsh.model.mesh.optimize("Netgen")
        # FV quality pass: measure the finite-volume metrics (checkMesh formulas, vendored
        # from the calibrated scorer) and, while they miss the targets, walk the
        # deterministic optimizer ladder. Runs on the LINEAR mesh - the optimizers move
        # corner nodes, which is the geometry a finite-volume solver sees.
        _fv_m, _fv_history = _fv_measure_and_optimize(gmsh)
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
        # the DELIVERED order, read back from the mesh - never an echo of the request.
        # (The audit's contract deviation: order 1 was declared, tet10 was delivered.)
        delivered = _delivered_order(gmsh, 3) if n_elem else None
        if n_elem and delivered != order:
            fatal.append(f"delivered element order {delivered} does not match the "
                         f"requested order {order} ({_order_source})")

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
            "cells": n_elem, "nodes": n_nodes,
            "element_order": delivered if delivered is not None else order,
            "element_order_requested": order,
            "element_order_source": _order_source,
            "min_sicn": round(min_sicn, 4),
            "sicn_low_fraction": round(low / n_elem, 6) if n_elem else 1.0,
            # the FV metrics (checkMesh conventions) + the optimizer-ladder history.
            # ABSENT keys (measurement failed) are deliberate: the fv_quality gate
            # fail-closes on a fluid domain that was never measured.
            **(_fv_m or {}),
            "fv_optimization": _fv_history,
            "fatal": fatal, "size_h": h,
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
        _fv_note = (f" max_non_ortho={_fv_m['max_non_ortho']:.1f} "
                    f"max_skew_int={_fv_m['max_skewness_internal']:.2f} "
                    f"fv_passes={max(len(_fv_history) - 1, 0)}"
                    if _fv_m else " fv_metrics=UNMEASURED")
        print(f"[GMSH] elements={n_elem} nodes={n_nodes} order={order} "
              f"({_order_source}) min_sicn={min_sicn:.3f} "
              f"low_frac={low / max(n_elem, 1):.4f}{_fv_note}")
        return 0 if not fatal else 4
    finally:
        gmsh.finalize()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
