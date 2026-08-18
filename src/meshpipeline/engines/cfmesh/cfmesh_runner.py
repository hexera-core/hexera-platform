# Responsibility: Drive a cfMesh run end to end: author, mesh, create patches and finalize.
# Owns: the run lifecycle and the runtime surface other layers reach through this module.
# Boundaries: 2D and 3D use different native binaries, chosen here; nothing above this module needs to know which.
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from meshpipeline.cad.cad_tessellate import (  # noqa: F401
    tessellate_internal,
)
from meshpipeline.cad.cad_tessellate import (
    tessellate_to_stl as _cad_tessellate_to_stl,
)
from meshpipeline.cad.stl_io import (  # noqa: F401
    _box_triangles,
    _write_solid,
    drop_degenerate,
    inspect_stl,
    mirror_y,
    read_stl_solids,
    read_stl_triangles,
)
from meshpipeline.engines.cfmesh.finalize import finalize  # noqa: F401  (engine-adapter seam)
from meshpipeline.engines.cfmesh.foam_exec import (  # noqa: F401
    _DEFAULT_BASHRC,
    _foam_env,
    check_mesh,
    export_volume_vtk,
    scan_case_dicts,
)
from meshpipeline.engines.cfmesh.native import (  # noqa: F401  (engine-adapter seam)
    run_cartesian_mesh,
)
from meshpipeline.engines.cfmesh.solvability import check_solvability  # noqa: F401  (engine-adapter seam)

# THIS MODULE IS THE ENGINE'S DECLARED NAMESPACE, NOT A COMPATIBILITY LAYER.
# `cfmesh/adapter.py` is nothing but `__getattr__` forwarding to this module, so every name the
# pipeline resolves off the cfMesh engine - `get_engine("cfmesh").finalize`,
# `.check_domain_extents`, `.check_solvability`, and the names `finalize.py` resolves off the
# engine it is handed (`check_mesh`, `write_manifest`, `read_stl_solids`, `inspect_stl`,
# `export_volume_vtk`, `build_review_msh`) - is resolved HERE, at run time, by name.
# None of those reads is a static import, so a static scan reports every one of these imports as
# unused. Deleting them on that evidence breaks cfMesh finalization at run time and nothing else.
# `test_cfmesh_runtime_surface.py` pins the exact set; that test, not this comment, is the guard.
from meshpipeline.engines.domain_extent_gate import (
    extent_gate_for_request as check_domain_extents,  # noqa: F401  (engine-adapter seam; cross-engine-neutral case-level gate)
)
from meshpipeline.engines.manifest import write_manifest  # noqa: F401
from meshpipeline.render.review_artifacts import (  # noqa: F401
    build_review_msh,
)
from meshpipeline.sandbox.safe_exec import (
    run_guarded,
)

logger = logging.getLogger(__name__)

# The review surface (geom.stl) covers only the SURFACE solids (body + far-field
# box/ribbon). Patches the MESHER generates - 2D's merged front/back `empty` patch -
# exist only in the polyMesh boundary, so finalize must declare them from the real
# boundary or the manifest under-reports the mesh and the patch-contract gate
# false-rejects a conformant 2D mesh (live-proven, job 0e123810: boundary was
# airfoil/farfield/empty exactly, manifest listed only airfoil/farfield).
review_surface_is_body_only = True


def tessellate_to_stl(geom_path, out_stl, *, context=None, prepared=None):
    import shutil as _shutil
    geom_path, out_stl = Path(geom_path), Path(out_stl)
    if geom_path.suffix.lower() in (".step", ".stp", ".iges", ".igs"):
        _shutil.copy2(geom_path, out_stl.parent / "geometry.step")
    return _cad_tessellate_to_stl(geom_path, out_stl, prepared=prepared)


# #
# surface assembly: body + bounding box -> named-patch .fms (cfMesh input)
# #
def prepare_surface(workspace, *, geometry_file: str,
                    domain_min, domain_max, wall_patch: str = "body",
                    farfield_patch: str = "farfield", feature_angle: float = 30.0,
                    mirror_y_half: bool = False,
                    bashrc: str = _DEFAULT_BASHRC) -> dict:
    ws = Path(workspace)
    body_stl = ws / geometry_file
    if body_stl.suffix.lower() != ".stl":
        raise ValueError(f"input must be a surface STL, got '{body_stl.suffix}' "
                         f"(export STL from CAD upstream - OpenFOAM has no CAD import)")
    # REGIONS the surface already names. read_stl_triangles dissolves the solid boundaries, which
    # is the right read for a body meshed as one wall and the wrong one for a surface whose parts
    # are named: renameBoundary below maps FMS solids to patch names, so a collapse here is what
    # decides a multi-patch request can never be honoured - not any limit of cfMesh.
    solids = {n: drop_degenerate(v) for n, v in read_stl_solids(body_stl).items()}
    regions = {n: v for n, v in solids.items() if v} if len(solids) > 1 else {}
    body = drop_degenerate(read_stl_triangles(body_stl))
    if mirror_y_half:
        body = body + mirror_y(body)
        regions = {n: v + mirror_y(v) for n, v in regions.items()}
    bb_min = [min(v[i] for t in body for v in t) for i in range(3)]
    bb_max = [max(v[i] for t in body for v in t) for i in range(3)]

    stl_path = ws / "geom.stl"
    with stl_path.open("w") as fh:
        if regions:
            for name, tris in regions.items():
                _write_solid(fh, name, tris)
        else:
            _write_solid(fh, wall_patch, body)
        _write_solid(fh, farfield_patch, _box_triangles(domain_min, domain_max))

    # Persist the REQUESTED far-field box corners. The domain-extent gate
    # must measure the box the builder prepared - NOT the octree-padded mesh
    # bounds (cfMesh pads the box, which would false-reject a correctly-sized
    # domain). The executor reads this into the manifest's geometry.domain_box.
    (ws / "geom_box.json").write_text(json.dumps({
        "domain_min": [float(v) for v in domain_min],
        "domain_max": [float(v) for v in domain_max],
    }))

    surface = _to_fms(ws, feature_angle, bashrc)
    # Reported like the internal path's patch_names: the caller learns which patches this surface
    # can actually deliver, rather than inferring it from the wall_patch it passed in.
    return {"surface_file": surface, "body_bbox": [bb_min, bb_max],
            "surface_regions": list(regions)}


def _to_fms(ws: Path, feature_angle: float, bashrc: str) -> str:
    reason = scan_case_dicts(ws)
    if reason:
        raise ValueError(f"case dicts rejected: {reason}")
    fms = ws / "geom.fms"
    cmd = (f"source {bashrc} >/dev/null 2>&1 && "
           f"surfaceFeatureEdges -angle {feature_angle} geom.stl geom.fms")
    try:
        rc = run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                         capture_output=True, text=True, timeout=600).returncode
    except subprocess.TimeoutExpired:
        logger.warning("surfaceFeatureEdges exceeded 600s - killed; meshing off raw STL")
        rc = -1
    if rc == 0 and fms.exists():
        return "geom.fms"
    if rc >= 0:
        logger.warning("surfaceFeatureEdges failed (rc=%s); meshing off raw STL", rc)
    return "geom.stl"


def prepare_surface_internal(workspace, *, surfaces_src: dict, feature_angle: float = 30.0,
                             bashrc: str = _DEFAULT_BASHRC) -> dict:
    ws = Path(workspace)
    if not surfaces_src:
        raise ValueError("internal topology needs named boundary surfaces "
                         "(wall/inlet/outlet) from tessellate_internal")

    all_tris: list = []
    stl_path = ws / "geom.stl"
    with stl_path.open("w") as fh:
        for patch, src in surfaces_src.items():
            tris = drop_degenerate(read_stl_triangles(Path(src)))
            if not tris:
                raise ValueError(f"internal surface '{patch}' tessellated to zero triangles")
            _write_solid(fh, patch, tris)
            all_tris.extend(tris)

    bb_min = [min(v[i] for t in all_tris for v in t) for i in range(3)]
    bb_max = [max(v[i] for t in all_tris for v in t) for i in range(3)]
    surface = _to_fms(ws, feature_angle, bashrc)
    return {"surface_file": surface, "body_bbox": [bb_min, bb_max],
            "patch_names": list(surfaces_src)}


# #
# DECLARATIVE meshDict rendering - the LLM chooses STRATEGY VALUES; this code
# renders a VALID, budget-clamped meshDict. The model never hand-writes cfMesh
# syntax, so it cannot emit a malformed dict (a dropped keyword silently breaks
# a block), blow the cell budget, or mis-name a patch. Mirrors the snappy path
# (render_snappy_case): the whole "LLM writes raw dict text" failure class is
# gone once every engine is declarative.
# #
_MESHDICT_HDR = ("/*--- cfMesh meshDict - RENDERED from strategy (not hand-written) ---*/\n"
                 "FoamFile { version 2.0; format ascii; class dictionary; object meshDict; }\n\n")

# boundary ROLE (intake vocabulary) -> OpenFOAM patch type in the mesh.
_ROLE_TO_OF_TYPE = {"wall": "wall", "farfield": "patch", "inlet": "patch",
                    "outlet": "patch", "symmetry": "symmetry",
                    "symmetryplane": "symmetryPlane", "empty": "empty",
                    "patch": "patch"}


def domain_from_strategy(body_bbox, L: float, strategy: dict | None = None) -> tuple[list, list]:
    (bmin, bmax) = body_bbox
    m = (strategy or {}).get("domain_margin") or {}
    up, dn = float(m.get("up", 10.0)), float(m.get("down", 20.0))
    side, vert = float(m.get("side", 10.0)), float(m.get("vert", 10.0))
    dmin = [bmin[0] - up * L, bmin[1] - side * L, bmin[2] - vert * L]
    dmax = [bmax[0] + dn * L, bmax[1] + side * L, bmax[2] + vert * L]
    return dmin, dmax


def _render_object_refinements(features: list) -> str:
    def _v(p) -> str:
        return f"({float(p[0]):.6g} {float(p[1]):.6g} {float(p[2]):.6g})"
    blocks = []
    for i, f in enumerate(features or []):
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or f"region{i}").strip() or f"region{i}"
        typ = str(f.get("type", "box")).strip().lower()
        cs = f.get("cellSize")
        if cs is None:
            continue
        try:
            cs = float(cs)
            if typ == "box":
                body = (f"type box; cellSize {cs:.6g}; centre {_v(f['centre'])}; "
                        f"lengthX {float(f['lengthX']):.6g}; lengthY {float(f['lengthY']):.6g}; "
                        f"lengthZ {float(f['lengthZ']):.6g};")
            elif typ == "sphere":
                body = f"type sphere; cellSize {cs:.6g}; centre {_v(f['centre'])}; radius {float(f['radius']):.6g};"
            elif typ == "cone":
                body = (f"type cone; cellSize {cs:.6g}; p0 {_v(f['p0'])}; p1 {_v(f['p1'])}; "
                        f"radius0 {float(f['radius0']):.6g}; radius1 {float(f['radius1']):.6g};")
            elif typ == "line":
                body = f"type line; cellSize {cs:.6g}; p0 {_v(f['p0'])}; p1 {_v(f['p1'])};"
            else:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        blocks.append(f"    {name} {{ {body} }}")
    return "objectRefinements\n{\n" + "\n".join(blocks) + "\n}\n" if blocks else ""


def render_cfmesh_case(workspace, *, surface_file: str, wall_patch: str,
                       patches: list, body_bbox, L: float,
                       domain_min, domain_max, strategy: dict | None = None,
                       cell_budget: int | None = None,
                       default_boundary: bool = True) -> dict:
    ws = Path(workspace)
    strategy = strategy or {}
    ext = [float(domain_max[i] - domain_min[i]) for i in range(3)]
    vol = max(ext[0] * ext[1] * ext[2], 1e-30)

    # maxCellSize = COARSE background cell. factor is (largest extent / cell);
    # higher factor = finer background. cfMesh refines DOWN from this, so a
    # coarse background keeps even a large far-field cheap.
    factor = max(4.0, float(strategy.get("max_cell_factor", 40.0)))
    max_cell = max(ext) / factor
    # BUDGET CLAMP: keep the background grid under 40% of the cell budget so the
    # wall shell + any objectRefinements have headroom. Coarsen (raise) max_cell
    # if the strategy asked for a background finer than the budget allows.
    if cell_budget:
        floor_cell = (vol / (0.4 * cell_budget)) ** (1.0 / 3.0)
        if max_cell < floor_cell:
            max_cell = floor_cell
    # wall refinement cell (absolute m): default L/20, never finer than
    # max_cell/16 (cfMesh halves - an unreachable size just wastes the run).
    wall_cell = float(strategy.get("wall_cell", L / 20.0))
    wall_cell = max(wall_cell, max_cell / 16.0)

    dict_parts = [
        _MESHDICT_HDR,
        f'surfaceFile "{surface_file}";\n',
        f"maxCellSize {max_cell:.6g};\n",
        f"localRefinement\n{{\n    {wall_patch} {{ cellSize {wall_cell:.6g}; }}\n}}\n",
    ]
    _obj = _render_object_refinements(strategy.get("features") or [])
    if _obj:
        dict_parts.append(_obj)

    n_layers = max(0, int(strategy.get("n_layers", 0)))
    if n_layers > 0:
        ratio = float(strategy.get("thickness_ratio", 1.2))
        first = strategy.get("first_layer_thickness")
        first_line = (f" maxFirstLayerThickness {float(first):.6g};" if first else "")
        dict_parts.append(
            "boundaryLayers\n{\n    patchBoundaryLayers\n    {\n"
            f"        {wall_patch}\n        {{ nLayers {n_layers}; thicknessRatio {ratio:g};"
            f"{first_line} allowDiscontinuity 0; }}\n"
            "    }\n    optimiseLayer 1;\n}\n")

    # renameBoundary: every contract patch → its OpenFOAM type. The solids in
    # geom.fms are named for the contract (prepare_surface), so the mesh's final
    # patch NAMES equal the contract - what the executor's patch-contract gate
    # checks. Unknown roles fall back to 'patch'.
    entries = []
    for p in (patches or []):
        nm = str(p.get("name") or "").strip()
        role = str(p.get("type") or "").strip().lower()
        if not nm:
            continue
        of_type = _ROLE_TO_OF_TYPE.get(role, "patch")
        entries.append(f"        {nm} {{ newName {nm}; type {of_type}; }}")
    # 2D (default_boundary=False): NO defaultName/defaultType. cartesian2DMesh's
    # generated front/back planes do NOT match newPatchNames entries (live-proven:
    # even identity entries missed them and defaultName absorbed all 34k faces into
    # fixedWalls type patch, breaking the empty merge) - they only survive with
    # their native names when renameBoundary has no default to swallow them.
    _default = ("    defaultName fixedWalls;\n    defaultType patch;\n"
                if default_boundary else "")
    dict_parts.append(
        "renameBoundary\n{\n" + _default
        + "    newPatchNames\n    {\n" + "\n".join(entries) + "\n    }\n}\n")

    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "meshDict").write_text("".join(dict_parts))

    est_bg = int(vol / max(max_cell, 1e-30) ** 3)
    return {"max_cell_size": round(max_cell, 6), "wall_cell_size": round(wall_cell, 6),
            "n_layers": n_layers, "features": len(strategy.get("features") or []),
            "est_background_cells": est_bg}


def _configure_internal(workspace, *, strategy: dict, wall_patch: str,
                        contract_patches: list, args: dict, cell_budget: int,
                        surface=None) -> dict:
    from meshpipeline.cad.prepared_surface import require_metre_surface

    ws = Path(workspace)
    prepared_state = require_metre_surface(surface, ws, "geometry.step").consumed
    solid = ws / "geometry.step"
    if not solid.exists():
        return {"success": False,
                "error": "internal topology needs the CAD solid (geometry.step). The upload "
                         "was a surface (STL/VTP), from which the inlet/outlet openings and "
                         "an interior point cannot be recovered.",
                "next": "Re-submit the fluid domain as a CAD solid (STEP/IGES), or use the "
                        "external topology if you meant flow around this body."}

    t = tessellate_internal(solid, ws / "_internal_stls", prepared=prepared_state,
                            opening_faces=args.get("opening_faces") or None)
    prep = prepare_surface_internal(
        workspace, surfaces_src=t["stls"],
        feature_angle=float(args.get("feature_angle", 30.0)))

    _patches = list(contract_patches or [])
    if not _patches:
        _patches = [{"name": n, "type": ("wall" if n == "wall" else n)} for n in t["stls"]]

    bb_min, bb_max = prep["body_bbox"]
    L = max(bb_max[i] - bb_min[i] for i in range(3))
    summary = render_cfmesh_case(
        workspace, surface_file=prep["surface_file"], wall_patch=wall_patch,
        patches=_patches, body_bbox=prep["body_bbox"], L=L,
        domain_min=bb_min, domain_max=bb_max, strategy=strategy, cell_budget=cell_budget)
    return {"success": True, "wrote": ["system/meshDict"], "topology": "internal",
            "openings": t.get("openings"), **summary,
            "next": "meshDict written for the enclosed cavity (valid + budget-clamped). "
                    "Call run_mesh NOW. Reconfigure ONLY on a concrete run_mesh failure."}


def configure_mesh(workspace, *, geometry_file: str, strategy: dict, wall_patch: str,
                   contract_patches: list, args: dict, cell_budget: int,
                   surface=None) -> dict:
    from meshpipeline.engines.workspace_facts import read_dimensionality, read_flow_topology
    topology = read_flow_topology(workspace) or "external"
    # the INTAKE-DECLARED dimensionality decides (neutral workspace file, same
    # treatment as flow_topology); an explicit arg only fills in when none was declared
    _dim = read_dimensionality(workspace) or str(args.get("dimensionality") or "3D").upper()
    if _dim == "2D":
        # REAL 2D: cfMesh's native cartesian2DMesh - not an extrusion trick.
        # Recipe locked by an in-container run on the real NACA ribbon.
        if topology == "internal":
            # the supported 2D envelope is EXTERNAL only (profile ribbon in a far-field);
            # refuse cleanly instead of wrapping an internal profile in a bogus far-field.
            return {"success": False, "error": (
                "2D internal flow is not supported - cfmesh supports the declared 2D "
                "(external profile-in-far-field) and 3D Cartesian envelopes only. "
                "Declare the case 3D, or external.")}
        return _configure_external_2d(workspace, geometry_file=geometry_file,
                                      strategy=strategy, wall_patch=wall_patch,
                                      contract_patches=contract_patches, args=args,
                                      cell_budget=cell_budget, surface=surface)
    if topology == "internal":
        return _configure_internal(workspace, strategy=strategy, wall_patch=wall_patch,
                                   contract_patches=contract_patches, args=args,
                                   cell_budget=cell_budget, surface=surface)

    from meshpipeline.cad.analysis import analyze_surface
    from meshpipeline.cad.prepared_surface import require_metre_surface
    analysis = analyze_surface(require_metre_surface(surface, workspace, geometry_file))
    body_bbox = (analysis["bbox_min"], analysis["bbox_max"])
    if args.get("domain_min") and args.get("domain_max"):
        dmin, dmax = args["domain_min"], args["domain_max"]
    else:
        dmin, dmax = domain_from_strategy(body_bbox, analysis["L"], strategy)
    _patches = list(contract_patches or [])
    _farfield = next((p["name"] for p in _patches if p["type"] != "wall"), "farfield")
    if not _patches:
        _patches = [{"name": wall_patch, "type": "wall"},
                    {"name": _farfield, "type": "farfield"}]
    prep = prepare_surface(
        workspace, geometry_file=geometry_file, domain_min=dmin, domain_max=dmax,
        wall_patch=wall_patch, farfield_patch=_farfield,
        feature_angle=float(args.get("feature_angle", 30.0)),
        mirror_y_half=bool(args.get("mirror_y_half", False)))
    summary = render_cfmesh_case(
        workspace, surface_file=prep["surface_file"], wall_patch=wall_patch,
        patches=_patches, body_bbox=prep["body_bbox"], L=analysis["L"],
        domain_min=dmin, domain_max=dmax, strategy=strategy, cell_budget=cell_budget)
    return {"success": True, "wrote": ["system/meshDict"], "topology": "external", **summary,
            "next": "meshDict written (valid + budget-clamped). Call run_mesh NOW. "
                    "Reconfigure ONLY in response to a concrete run_mesh failure."}


def _configure_external_2d(workspace, *, geometry_file: str, strategy: dict, surface=None,
                           wall_patch: str, contract_patches: list, args: dict,
                           cell_budget: int) -> dict:
    from meshpipeline.cad.analysis import analyze_surface
    ws = Path(workspace)
    strategy = strategy or {}
    _patches = list(contract_patches or [])
    _empties = [p["name"] for p in _patches
                if str(p.get("type") or "").lower() == "empty"]
    if len(_empties) != 1:
        return {"success": False, "error": (
            "a 2D case needs EXACTLY ONE contracted patch of type 'empty' (the merged "
            f"front/back plane faces); the contract declares {_empties or 'none'}. Fix "
            "the patch contract at intake before configuring.")}
    _inplane = [p for p in _patches if str(p.get("type") or "").lower() != "empty"]
    _farfield = next((p["name"] for p in _inplane
                      if str(p.get("type") or "").lower() != "wall"), "farfield")
    if not _inplane:
        _inplane = [{"name": wall_patch, "type": "wall"},
                    {"name": _farfield, "type": "farfield"}]

    body = drop_degenerate(read_stl_triangles(ws / geometry_file))
    zs = [v[2] for t in body for v in t]
    z0, z1 = min(zs), max(zs)
    if not (z1 - z0) > 0:
        return {"success": False, "error": (
            "the uploaded geometry is perfectly flat in z - cartesian2DMesh needs a "
            "profile RIBBON (the 2D section extruded through any nonzero span).")}
    from meshpipeline.cad.prepared_surface import require_metre_surface
    analysis = analyze_surface(require_metre_surface(surface, ws, geometry_file))
    body_bbox = (analysis["bbox_min"], analysis["bbox_max"])
    if args.get("domain_min") and args.get("domain_max"):
        dmin, dmax = list(args["domain_min"]), list(args["domain_max"])
    else:
        dmin, dmax = domain_from_strategy(body_bbox, analysis["L"], strategy)
    # 2D: the far-field is a SIDE RIBBON over the body's exact z span (cartesian2DMesh
    # meshes one cell through the thickness; a z-padded or capped box breaks it).
    dmin[2], dmax[2] = z0, z1
    (x0, y0), (x1, y1) = (dmin[0], dmin[1]), (dmax[0], dmax[1])

    def _side(p1, p2):
        (xa, ya), (xb, yb) = p1, p2
        return [((xa, ya, z0), (xb, yb, z0), (xb, yb, z1)),
                ((xa, ya, z0), (xb, yb, z1), (xa, ya, z1))]
    ff = (_side((x0, y0), (x1, y0)) + _side((x1, y0), (x1, y1))
          + _side((x1, y1), (x0, y1)) + _side((x0, y1), (x0, y0)))
    with (ws / "geom.stl").open("w") as fh:
        _write_solid(fh, wall_patch, body)
        _write_solid(fh, _farfield, ff)
    (ws / "geom_box.json").write_text(json.dumps({
        "domain_min": [float(v) for v in dmin],
        "domain_max": [float(v) for v in dmax],
    }))
    surface = _to_fms(ws, float(args.get("feature_angle", 30.0)), _DEFAULT_BASHRC)

    # renameBoundary carries ONLY the in-plane contract patches and NO default
    # block: the tool's generated bottomEmptyFaces/topEmptyFaces don't match
    # newPatchNames entries (live-proven - identity entries missed; a defaultName
    # absorbed them into fixedWalls/patch) and must pass through untouched for the
    # createPatch merge to find them.
    summary = render_cfmesh_case(
        workspace, surface_file=surface, wall_patch=wall_patch,
        patches=_inplane, body_bbox=body_bbox, L=analysis["L"],
        domain_min=dmin, domain_max=dmax, strategy=strategy,
        cell_budget=cell_budget, default_boundary=False)
    # post-mesh merge of the tool's native top/bottom empties into the contract name
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "createPatchDict").write_text(
        "FoamFile { version 2.0; format ascii; class dictionary; "
        "object createPatchDict; }\n"
        "pointSync false;\n"
        f"patches ( {{ name {_empties[0]}; patchInfo {{ type empty; }} "
        "constructFrom patches; patches (bottomEmptyFaces topEmptyFaces); } );\n")
    (ws / ".cartesian2d").write_text("cartesian2DMesh\n")
    return {"success": True, "wrote": ["system/meshDict", "system/createPatchDict"],
            "topology": "external", "dimensionality": "2D", **summary,
            "next": "2D case written (cartesian2DMesh + front/back merge). Call "
                    "run_mesh NOW. Reconfigure ONLY on a concrete run_mesh failure."}
