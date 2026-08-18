# Responsibility: Drive a multi-region run: mesh the assembly, split it into regions, and reconcile them.
# Owns: the stage sequence, region reconciliation and the completeness check per region.
# Boundaries: a region is complete only with a non-empty polyMesh of its own.
from __future__ import annotations

import logging
import re
from pathlib import Path

from meshpipeline.cad.stl_io import _write_solid, read_stl_triangles

# The snappyHexMesh mechanics this engine shares with `snappy` - the case skeleton the mesher
# needs, and how to read its log. gave them their own authority, so this engine no
# longer depends on another engine's runner for either.
from meshpipeline.engines.snappy_hexmesh import _write_case_skeleton, parse_layer_coverage
from meshpipeline.engines.snappy_multiregion.foam_exec import (  # noqa: F401  re-exported for the adapter
    _CM,
    _DEFAULT_BASHRC,
    _FATAL,
    _foam_env,
    scan_case_dicts,
)
from meshpipeline.engines.snappy_multiregion.foam_exec import check_mesh as _single_region_check_mesh
from meshpipeline.render.review_artifacts import build_review_msh  # noqa: F401  adapter surface

logger = logging.getLogger(__name__)

_HDR = ("FoamFile{{ version 2.0; format ascii; class {cls}; "
        "location \"{loc}\"; object {obj}; }}\n")


# region-name helpers

from dataclasses import dataclass
from dataclasses import field as _dc_field

from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT


@dataclass(frozen=True)
class MultiregionAssemblyPlan:
    fluid_regions: tuple = ()
    solid_regions: tuple = ()
    body_to_region: dict = _dc_field(default_factory=dict)
    expected_regions: tuple = ()
    expected_interfaces: tuple = ()
    allow_inert_background: bool = False


def compile_assembly_plan(regions: list) -> MultiregionAssemblyPlan:
    rmap = region_map(regions)
    return MultiregionAssemblyPlan(
        fluid_regions=tuple(fluid_regions(rmap)),
        solid_regions=tuple(solid_regions(rmap)),
        body_to_region={int(i): n for n, r in rmap.items() for i in r["solids"]},
        expected_regions=tuple(rmap),
    )


def _region_interior_point(stl_path, bbox_min, bbox_max):
    try:
        import pyvista as pv
        surf = pv.read(str(stl_path)).extract_surface().triangulate()
        (x0, y0, z0), (x1, y1, z1) = bbox_min, bbox_max
        dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
        cands = [((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)]
        for fx in (0.05, 0.15, 0.3):
            cands += [(x0 + fx * dx, y0 + fx * dy, z0 + fx * dz),
                      (x1 - fx * dx, y1 - fx * dy, z1 - fx * dz),
                      ((x0 + x1) / 2, y0 + fx * dy, (z0 + z1) / 2),
                      (x0 + fx * dx, (y0 + y1) / 2, (z0 + z1) / 2)]
        pts = pv.PolyData([list(c) for c in cands])
        sel = pts.select_enclosed_points(surf, check_surface=False)
        for i, inside in enumerate(sel["SelectedPoints"]):
            if inside:
                return cands[i]
    except Exception:  # noqa: BLE001 - fall through to the structured failure at the caller
        logger.exception("region interior-point probe failed")
    return None


def _safe_region_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name)).strip("_")
    return s or "region"


def region_map(regions: list) -> dict:
    out: dict[str, dict] = {}
    owned: set[int] = set()
    for r in regions or []:
        name = _safe_region_name(r.get("name", ""))
        rtype = str(r.get("type", "")).strip().lower()
        solids = [int(s) for s in (r.get("solids") or []) if int(s) not in owned]
        owned.update(solids)
        out[name] = {"type": rtype, "solids": solids}
    return out


def fluid_regions(rmap: dict) -> list[str]:
    return [n for n, r in rmap.items() if r["type"] == "fluid"]


def solid_regions(rmap: dict) -> list[str]:
    return [n for n, r in rmap.items() if r["type"] == "solid"]


# pure renderers (unit-tested)

def render_region_properties(regions: list) -> str:
    rmap = region_map(regions)
    fluids = " ".join(fluid_regions(rmap))
    solids = " ".join(solid_regions(rmap))
    return (
        _HDR.format(cls="dictionary", loc="constant", obj="regionProperties")
        + "\nregions\n(\n"
        + f"    fluid       ({fluids})\n"
        + f"    solid       ({solids})\n"
        + ");\n"
    )


def render_refinement_surfaces(rmap: dict, surface_level: tuple, interface_refinement: int,
                               region_refinement: dict | None = None) -> str:
    region_refinement = region_refinement or {}
    lines = ["    refinementSurfaces", "    {"]
    for name, r in rmap.items():
        _ovr = region_refinement.get(name)
        lo, hi = (int(_ovr[0]), int(_ovr[1])) if _ovr else (int(surface_level[0]), int(surface_level[1]))
        lvl_hi = hi + (int(interface_refinement) if r["type"] == "solid" else 0)
        lines += [
            f"        {name}",
            "        {",
            f"            level ({lo} {lvl_hi});",
            f"            cellZone {name};",
            f"            faceZone {name};",
            "            cellZoneInside inside;",
            "        }",
        ]
    lines += ["    }"]
    return "\n".join(lines)


def render_geometry_block(rmap: dict) -> str:
    lines = ["geometry", "{"]
    for name in rmap:
        lines += [f"    {name}.stl {{ type triSurfaceMesh; name {name}; }}"]
    lines += ["}"]
    return "\n".join(lines)


def render_block_mesh(bbox_min, bbox_max, base_cell: float, pad: float = 0.15) -> str:
    (x0, y0, z0), (x1, y1, z1) = bbox_min, bbox_max
    dx, dy, dz = (x1 - x0), (y1 - y0), (z1 - z0)
    mx, my, mz = dx * pad, dy * pad, dz * pad
    x0, y0, z0 = x0 - mx, y0 - my, z0 - mz
    x1, y1, z1 = x1 + mx, y1 + my, z1 + mz
    bc = max(base_cell, 1e-9)
    nx = max(1, round((x1 - x0) / bc))
    ny = max(1, round((y1 - y0) / bc))
    nz = max(1, round((z1 - z0) / bc))
    verts = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    vtxt = "\n".join(f"    ({v[0]:.6g} {v[1]:.6g} {v[2]:.6g})" for v in verts)
    return (
        _HDR.format(cls="dictionary", loc="system", obj="blockMeshDict")
        + "\nscale 1;\n\nvertices\n(\n" + vtxt + "\n);\n\n"
        + f"blocks\n(\n    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)\n);\n\n"
        + "edges ();\nboundary ();\nmergePatchPairs ();\n"
    )


def interface_name(region_a: str, region_b: str) -> str:
    return f"{region_a}_to_{region_b}"


# boundary parsing / interface conformality

def _parse_boundary_nfaces(boundary_file: Path) -> dict:
    if not boundary_file.exists():
        return {}
    txt = boundary_file.read_text(errors="replace")
    out: dict[str, int] = {}
    # walk each `name { ... nFaces N; ... }` block
    for m in re.finditer(r"([A-Za-z0-9_]+)\s*\{(.*?)\}", txt, re.S):
        block = m.group(2)
        nf = re.search(r"nFaces\s+(\d+)\s*;", block)
        if nf:
            out[m.group(1)] = int(nf.group(1))
    return out


def _parse_boundary_of_types(boundary_file: Path) -> dict:
    if not boundary_file.exists():
        return {}
    txt = boundary_file.read_text(errors="replace")
    out: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z0-9_]+)\s*\{(.*?)\}", txt, re.S):
        t = re.search(r"\btype\s+([A-Za-z]+)\s*;", m.group(2))
        if t:
            out[m.group(1)] = t.group(1)
    return out


# OpenFOAM geometric type -> the user-contract ROLE it evidences. Only these load-bearing kinds are
# recorded in the mesh boundary itself; a generic `patch` carries no role by itself.
_OF_TYPE_TO_ROLE = {"wall": "wall", "symmetryplane": "symmetry", "symmetry": "symmetry",
                    "empty": "empty"}
# Roles the MESH must physically evidence (they are real OF boundary types). If the user declared
# one of these but the mesh delivered a generic patch, that is a re-role - never echo the
# declaration back to mask it.
_MESH_EVIDENCED_ROLES = frozenset({"wall", "symmetry", "empty"})


def _delivered_user_role(of_type: str, declared_role: str | None) -> str:
    mapped = _OF_TYPE_TO_ROLE.get((of_type or "").strip().lower())
    if mapped:
        return mapped
    # generic OF patch: echo the declared role ONLY for flow roles the mesh genuinely cannot witness
    if declared_role and declared_role.strip().lower() not in _MESH_EVIDENCED_ROLES:
        return declared_role
    return "patch"


def delivered_user_boundary_types(ws: Path, intake_patches: list) -> dict[str, str]:
    declared_role = {(p.get("name") or "").strip(): (p.get("type") or "").strip()
                     for p in (intake_patches or []) if isinstance(p, dict) and p.get("name")}
    out: dict[str, str] = {}
    for region in _region_dirs(ws):
        bnd = ws / "constant" / region / "polyMesh" / "boundary"
        of_types = _parse_boundary_of_types(bnd)
        for p in _parse_boundary_nfaces(bnd):
            if "_to_" in p:
                continue   # region interface - engine-owned, never part of the user contract
            out.setdefault(p, _delivered_user_role(of_types.get(p, ""), declared_role.get(p)))
    return out


def check_interfaces(workspace, rmap: dict) -> dict:
    ws = Path(workspace)
    interfaces: list[dict] = []
    mismatch: list[str] = []
    fluids, solids = fluid_regions(rmap), solid_regions(rmap)
    for f in fluids:
        fb = _parse_boundary_nfaces(ws / "constant" / f / "polyMesh" / "boundary")
        for s in solids:
            sb = _parse_boundary_nfaces(ws / "constant" / s / "polyMesh" / "boundary")
            a, b = interface_name(f, s), interface_name(s, f)
            fa, sb_ = fb.get(a), sb.get(b)
            if fa is None and sb_ is None:
                continue                       # these two regions do not touch - fine
            interfaces.append({"name": a, "fluid": f, "solid": s,
                               "faces_fluid": fa, "faces_solid": sb_})
            if fa is None or sb_ is None or fa != sb_:
                mismatch.append(a)
    return {
        "interface_ok": (bool(interfaces) and not mismatch),
        "interfaces": interfaces,
        "interface_mismatch": mismatch,
    }


# geometry: split the assembly into per-solid surfaces

def _prepared_from(context):
    geometry = getattr(context, "geometry", None)
    if geometry is None:
        raise ValueError(
            "the multiregion bundle was asked to read an assembly without execution geometry; "
            "the physical size of every region would be a guess")
    return geometry.prepared


def read_assembly_solids(geom_path, out_dir, *, prepared=None) -> list[dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.gp import gp_Trsf
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    reader = STEPControl_Reader()
    if reader.ReadFile(str(geom_path)) != IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read CAD file: {Path(geom_path).name}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if prepared is None:
        raise ValueError(
            "the multiregion bundle needs the typed coordinate state to place its assembly in "
            "metres; the fixed 1e-3 it used to apply was right only for millimetre CAD and "
            "silently wrong by 25.4x for an inch assembly")
    trsf = gp_Trsf(); trsf.SetScaleFactor(prepared.to_metres)
    shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
    solids: list[dict] = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    idx = 0
    while exp.More():
        solid = exp.Current()
        props = GProp_GProps(); BRepGProp.VolumeProperties_s(solid, props)
        vol = props.Mass()
        c = props.CentreOfMass()
        stl = out_dir / f"solid_{idx}.stl"
        BRepMesh_IncrementalMesh(solid, 5.0e-4, False, 0.2, True)
        StlAPI_Writer().Write(solid, str(stl))
        tris = read_stl_triangles(stl)
        if tris:
            bb_min = [min(v[i] for t in tris for v in t) for i in range(3)]
            bb_max = [max(v[i] for t in tris for v in t) for i in range(3)]
        else:
            bb_min = bb_max = [0.0, 0.0, 0.0]
        solids.append({"index": idx, "stl": str(stl), "volume": round(vol, 12),
                       "bbox_min": bb_min, "bbox_max": bb_max,
                       "centroid": [round(c.X(), 6), round(c.Y(), 6), round(c.Z(), 6)]})
        idx += 1
        exp.Next()
    if not solids:
        raise RuntimeError("the CAD file contains no solids - a multi-region case needs a multi-solid assembly")
    return solids


def inspect_stl(workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
    import json
    ws = Path(workspace)
    meta = ws / "_assembly" / "solids.json"
    if meta.exists():
        solids = json.loads(meta.read_text())
    else:
        # Recomputing needs the coordinate state; it comes from the run's own geometry through
        # the builder tool context, never from a lookup this bundle performs for itself.
        solids = read_assembly_solids(ws / "geometry.step", ws / "_assembly",
                                      prepared=_prepared_from(context))
        (ws / "_assembly").mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(solids))
    # SCALE-AWARE PLANNING (engine-owned): a multi-scale assembly (mm fasteners in a
    # 0.3 m air box) cannot be resolved by one global surface level - the 19-solid stress
    # runs burned every attempt on exactly that. Report each solid's characteristic size,
    # the level the DEFAULT base cell needs to resolve it (~4 cells across), and flag the
    # tiny ones so the builder plans per-region refinement instead of guessing globally.
    import math
    allmins = [min(s["bbox_min"][i] for s in solids) for i in range(3)] if solids else [0, 0, 0]
    allmaxs = [max(s["bbox_max"][i] for s in solids) for i in range(3)] if solids else [1, 1, 1]
    assembly_diag = sum((allmaxs[i] - allmins[i]) ** 2 for i in range(3)) ** 0.5 or 1.0
    base_cell = assembly_diag / 40.0   # mirrors configure_mesh's background sizing
    per_solid = []
    small = []
    for s2 in solids:
        ext = [max(s2["bbox_max"][i] - s2["bbox_min"][i], 1e-12) for i in range(3)]
        char = min(ext)   # the thinnest axis is what castellation must resolve
        need = max(0, math.ceil(math.log2(max(base_cell / max(char / 4.0, 1e-12), 1.0))))
        per_solid.append({"index": s2["index"], "char_size": round(char, 6),
                          "needed_level": need})
        if need >= 5:
            small.append(s2["index"])
    scale_ratio = (min(p["char_size"] for p in per_solid) / assembly_diag) if per_solid else 1.0
    warnings = []
    if small:
        warnings.append(
            f"solids {small} are TINY relative to the assembly (scale ratio "
            f"{scale_ratio:.2e}): resolving them needs surface level >= 5 at the default "
            "background. Give THEIR region a higher region_refinement level instead of "
            "raising the global surface_level (which multiplies cells everywhere); if the "
            "budget cannot afford their needed_level, say so and stop - do not silently "
            "under-resolve them (their cellZone will leak and the region split will fail).")
    return {"solids": [{k: s[k] for k in ("index", "volume", "bbox_min", "bbox_max", "centroid")}
                       for s in solids],
            "n_solids": len(solids),
            "assembly_diag": round(assembly_diag, 6),
            "base_cell_estimate": round(base_cell, 6),
            "per_solid_scale": per_solid,
            "small_solids": small,
            "scale_ratio": scale_ratio,
            "scale_ratio_warnings": warnings,
            "note": "assign every solid index to a fluid or solid region in configure_mesh; "
                    "use region_refinement {region: [min,max]} for regions holding small "
                    "solids (see per_solid_scale.needed_level)"}


def tessellate_to_stl(geom_path, out_stl, *, context=None, prepared=None):
    import json
    import shutil

    from meshpipeline.cad.cad_tessellate import tessellate_to_stl as _whole
    out_stl = Path(out_stl); ws = out_stl.parent
    shutil.copy2(geom_path, ws / "geometry.step")
    _whole(geom_path, out_stl, prepared=prepared)                # preview of the whole assembly
    solids = read_assembly_solids(geom_path, ws / "_assembly", prepared=prepared)
    (ws / "_assembly" / "solids.json").write_text(json.dumps(solids))
    return out_stl


# configure: render the multi-region case

def _merge_region_stl(ws: Path, name: str, solid_stls: list[str]) -> None:
    tris: list = []
    for p in solid_stls:
        tris += read_stl_triangles(Path(p))
    dst = ws / "constant" / "triSurface" / f"{name}.stl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w") as fh:
        _write_solid(fh, name, tris)


def configure_mesh(workspace, *, strategy: dict, wall_patch: str = "",
                   geometry_file: str = "input.stl", **_ignored) -> dict:
    import json
    ws = Path(workspace)
    regions = strategy.get("regions") or []
    rmap = region_map(regions)
    surface_level = tuple(strategy.get("surface_level") or (2, 2))
    region_refinement = strategy.get("region_refinement") or {}
    interface_refinement = int(strategy.get("interface_refinement", 1))
    n_layers = int(strategy.get("n_layers", 3))
    first_rel = float(strategy.get("first_layer_rel", 0.35))
    quality = strategy.get("quality", "balanced")
    max_cells = int(strategy.get("max_cells", 6_000_000))

    # resolve region -> solid STLs from the staged assembly. COVERAGE IS A SCHEMA RULE:
    # every assembly solid must be assigned exactly once, no unknown indices, nothing
    # silently dropped. (The prompt always said "assign EVERY solid"; a live 19-solid run
    # showed prompt-only rules do not hold - and the old `if i in by_index` silently
    # dropped unknown indices instead of rejecting them.)
    solids = json.loads((ws / "_assembly" / "solids.json").read_text())
    by_index = {int(s["index"]): s for s in solids}
    _assigned: list[int] = [int(i) for r in rmap.values() for i in r["solids"]]
    _seen: set[int] = set()
    _dups: set[int] = set()
    for _i in _assigned:
        (_dups if _i in _seen else _seen).add(_i)
    duplicate = sorted(_dups)
    unknown = sorted({i for i in _assigned if i not in by_index})
    missing = sorted(set(by_index) - set(_assigned))
    if duplicate or unknown or missing:
        return {
            "success": False,
            "code": "multiregion_region_coverage_invalid",
            "missing_indices": missing,
            "unknown_indices": unknown,
            "duplicate_indices": duplicate,
            "expected_inventory_count": len(by_index),
            "assigned_count": len(set(_assigned) & set(by_index)),
            "error": ("Every assembly solid must be assigned exactly once to either the fluid "
                      "region or a solid region. Fix the regions map using the indices from "
                      "geometry_report and call configure_mesh again."),
        }
    for name, r in rmap.items():
        _merge_region_stl(ws, name, [by_index[int(i)]["stl"] for i in r["solids"]])

    # per-region interior seed points: locationsInMesh both ASSIGNS the region and makes
    # snappy REMOVE cells reachable from no seed (the unzoned-background fix). A region we
    # cannot seed is a structured failure, not a guess.
    region_points: dict = {}
    for name in fluid_regions(rmap):
        r = rmap[name]
        _mins = [min(by_index[int(i)]["bbox_min"][k] for i in r["solids"]) for k in range(3)]
        _maxs = [max(by_index[int(i)]["bbox_max"][k] for i in r["solids"]) for k in range(3)]
        _pt = _region_interior_point(ws / "constant" / "triSurface" / f"{name}.stl", _mins, _maxs)
        if _pt is None:
            return {"success": False, "code": "multiregion_region_unseedable",
                    "region": name,
                    "error": (f"could not find a point provably inside fluid region {name!r} - "
                              "its merged surface may be open or degenerate. Check the region's "
                              "solid assignment.")}
        region_points[name] = _pt

    plan = compile_assembly_plan(regions)
    (ws / ".assembly_plan.json").write_text(json.dumps({
        "fluid_regions": list(plan.fluid_regions), "solid_regions": list(plan.solid_regions),
        "body_to_region": {str(k): v for k, v in plan.body_to_region.items()},
        "expected_regions": list(plan.expected_regions),
        "allow_inert_background": plan.allow_inert_background}))

    # background box over the whole assembly bbox
    allmins = [min(s["bbox_min"][i] for s in solids) for i in range(3)]
    allmaxs = [max(s["bbox_max"][i] for s in solids) for i in range(3)]
    diag = sum((allmaxs[i] - allmins[i]) ** 2 for i in range(3)) ** 0.5
    base_cell = max(diag / 40.0, 1e-6)

    _write_case_skeleton(ws)
    # BACKGROUND CONTAINMENT: when one fluid region's bbox spans the whole assembly (an
    # enclosing fluid, e.g. an air box around the parts), padding the background beyond it
    # guarantees a shell of unzoned cells - the delivered undeclared domain0. Zero padding
    # makes the background coincide with the enclosing fluid's outer faces, so every cell
    # belongs to a declared region. Non-enclosing assemblies keep the normal margin; any
    # residual unzoned cells are caught by the reconciliation gate, never shipped.
    _pad = 0.15
    for _fname in fluid_regions(rmap):
        _fr = rmap[_fname]
        _fmins = [min(by_index[int(i)]["bbox_min"][k] for i in _fr["solids"]) for k in range(3)]
        _fmaxs = [max(by_index[int(i)]["bbox_max"][k] for i in _fr["solids"]) for k in range(3)]
        _diag = sum((allmaxs[k] - allmins[k]) ** 2 for k in range(3)) ** 0.5 or 1.0
        if all(abs(_fmins[k] - allmins[k]) < 0.001 * _diag
               and abs(_fmaxs[k] - allmaxs[k]) < 0.001 * _diag for k in range(3)):
            _pad = 0.0
            break
    (ws / "system" / "blockMeshDict").write_text(
        render_block_mesh(allmins, allmaxs, base_cell, pad=_pad))
    (ws / "system" / "snappyHexMeshDict").write_text(
        render_snappy_multiregion_dict(rmap, allmins, allmaxs, surface_level=surface_level,
                               interface_refinement=interface_refinement, n_layers=n_layers,
                               first_layer_rel=first_rel, quality=quality, max_cells=max_cells,
                               region_points=region_points, region_refinement=region_refinement))
    # feature extraction dict for every region surface. MUST carry the FoamFile header
    # like every other rendered dict - the first live assembly run failed at
    # surfaceFeatureExtract "line 1" because this render alone omitted it.
    (ws / "system" / "surfaceFeatureExtractDict").write_text(
        _HDR.format(cls="dictionary", loc="system", obj="surfaceFeatureExtractDict")
        + "\n" + "\n".join(f'{n}.stl {{ extractionMethod extractFromSurface; '
                             f'extractFromSurfaceCoeffs {{ includedAngle 150; }} writeObj no; }}'
                             for n in rmap) + "\n")
    (ws / ".regions.json").write_text(json.dumps(regions))
    reason = scan_case_dicts(ws)
    if reason:
        raise ValueError(f"case dicts rejected: {reason}")
    return {"regions": list(rmap), "fluids": fluid_regions(rmap), "solids": solid_regions(rmap),
            "surface_level": list(surface_level), "n_layers": n_layers, "base_cell": base_cell}


def _locations_in_mesh(region_points: dict | None, fallback) -> str:
    if region_points:
        p = next(iter(region_points.values()))
        return f"locationInMesh ({p[0]:.6g} {p[1]:.6g} {p[2]:.6g});"
    return f"locationInMesh ({fallback[0]:.6g} {fallback[1]:.6g} {fallback[2]:.6g});"


def render_snappy_multiregion_dict(rmap: dict, bbox_min, bbox_max, *, surface_level, interface_refinement,
                           n_layers, first_layer_rel, quality, max_cells,
                           region_points: dict | None = None,
                           region_refinement: dict | None = None) -> str:
    (x0, y0, z0), (x1, y1, z1) = bbox_min, bbox_max
    loc = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    fluids = fluid_regions(rmap)
    features = "\n".join(f'        {{ file "{n}.eMesh"; level {int(surface_level[1])}; }}' for n in rmap)
    strict = quality == "strict"
    # layer spec: prism layers on every fluid-side interface patch and fluid external walls
    layer_patches = []
    for f in fluids:
        for s in solid_regions(rmap):
            layer_patches.append(f'        "{interface_name(f, s)}" {{ nSurfaceLayers {n_layers}; }}')
    layer_block = "\n".join(layer_patches) or "        // (no fluid-solid interfaces to layer)"
    return f"""{_HDR.format(cls="dictionary", loc="system", obj="snappyHexMeshDict")}
castellatedMesh true;
snap            true;
addLayers       {str(n_layers > 0).lower()};

{render_geometry_block(rmap)}

castellatedMeshControls
{{
    maxLocalCells   {max(100000, max_cells)};
    maxGlobalCells  {max_cells};
    minRefinementCells 10;
    maxLoadUnbalance 0.10;
    nCellsBetweenLevels 3;
    resolveFeatureAngle 30;
    allowFreeStandingZoneFaces true;

    features
    (
{features}
    );

{render_refinement_surfaces(rmap, surface_level, interface_refinement, region_refinement)}

    refinementRegions {{}}

    {_locations_in_mesh(region_points, loc)}
}}

snapControls
{{
    nSmoothPatch 3; tolerance 2.0; nSolveIter 30; nRelaxIter 5;
    nFeatureSnapIter 10; implicitFeatureSnap false; explicitFeatureSnap true;
    multiRegionFeatureSnap true;
}}

addLayersControls
{{
    relativeSizes true;
    layers
    {{
{layer_block}
    }}
    expansionRatio 1.2;
    finalLayerThickness {first_layer_rel};
    minThickness {first_layer_rel * 0.25:.4g};
    nGrow 0; featureAngle 130; nRelaxIter 5;
    nSmoothSurfaceNormals 1; nSmoothNormals 3; nSmoothThickness 10;
    maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle 90; nBufferCellsNoExtrude 0;
    nLayerIter 50; nRelaxedIter 20;
}}

meshQualityControls
{{
    maxNonOrtho {65 if strict else 70};
    maxBoundarySkewness 20;
    maxInternalSkewness {3.5 if strict else 4};
    maxConcave 80; minVol 1e-13; minTetQuality 1e-15;
    minArea -1; minTwist 0.02; minDeterminant 0.001;
    minFaceWeight 0.05; minVolRatio 0.01; minTriangleTwist -1;
    nSmoothScale 4; errorReduction 0.75;
}}

mergeTolerance 1e-6;
"""


def assembly_preflight(ws) -> dict | None:
    import json
    ws = Path(ws)
    inventory = ws / "_assembly" / "solids.json"
    if not inventory.exists():
        return {"code": "multiregion_not_staged",
                "error": "this workspace has no staged assembly inventory - the geometry was "
                         "never tessellated for the multi-region engine"}
    try:
        solids = json.loads(inventory.read_text())
    except (ValueError, OSError) as exc:
        return {"code": "multiregion_not_staged",
                "error": f"the staged assembly inventory could not be read: {exc}"}

    if len(solids) < 2:
        return {"code": "multiregion_requires_multiple_solids",
                "solids_found": len(solids),
                "error": (f"this geometry contains {len(solids)} solid; a multi-region "
                          "conjugate case needs at least two so that a shared interface "
                          "exists between them. Mesh it with a single-region engine, or "
                          "supply an assembly whose parts are separate solids.")}

    plan_file = ws / ".assembly_plan.json"
    if not plan_file.exists():
        return {"code": "multiregion_region_coverage_invalid",
                "expected_inventory_count": len(solids),
                "error": ("no validated region plan is staged for this workspace - "
                          "configure_mesh must accept a region map before meshing can start.")}
    try:
        plan = json.loads(plan_file.read_text())
    except (ValueError, OSError) as exc:
        return {"code": "multiregion_region_coverage_invalid",
                "error": f"the staged region plan could not be read: {exc}"}

    expected = list(plan.get("expected_regions") or [])
    if len(expected) < 2:
        return {"code": "multiregion_requires_multiple_solids",
                "regions_declared": len(expected),
                "error": (f"{len(expected)} region is declared; a conjugate case needs at "
                          "least two regions with a shared interface.")}
    if len(set(expected)) != len(expected):
        dup = sorted({n for n in expected if expected.count(n) > 1})
        return {"code": "multiregion_region_names_ambiguous", "duplicate_regions": dup,
                "error": (f"region name(s) {dup} are declared more than once - each region "
                          "must have one unique name so its mesh and interfaces can be "
                          "identified.")}
    missing = [n for n in expected if not (ws / "constant" / "triSurface" / f"{n}.stl").exists()]
    if missing:
        return {"code": "multiregion_region_surface_missing", "missing_regions": missing,
                "error": (f"no staged surface for region(s) {missing} - the region map names "
                          "regions this workspace has no geometry for.")}
    return None


# run: mesh -> split -> regionProperties -> per-region check

def _run_snappy_multiregion_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                                  timeout: int = 2400) -> dict:
    from meshpipeline.engines.snappy_multiregion.native import run_native_build

    return run_native_build(workspace, preflight=assembly_preflight,
                            render_region_properties=render_region_properties,
                            parse_layer_coverage=parse_layer_coverage,
                            bashrc=bashrc, timeout=timeout)


def _region_dirs(ws: Path) -> list[str]:
    from meshpipeline.engines.snappy_multiregion.regions import present_regions

    return list(present_regions(ws))


def check_mesh(workspace) -> dict:
    import json
    ws = Path(workspace)
    regions_declared = []
    if (ws / ".regions.json").exists():
        regions_declared = json.loads((ws / ".regions.json").read_text())
    rmap = region_map(regions_declared)
    present = _region_dirs(ws)
    missing = [n for n in rmap if n not in present]

    per_region: list[dict] = []
    fatal: list = []
    total_cells = 0
    worst_skew_frac = 0.0
    worst_non_ortho = None
    for name in present:
        # region-aware: checkMesh -region <name> from the CASE ROOT (constant/<name> is
        # not a case - running there parses nothing and every region reads as 0 cells)
        q = _single_region_check_mesh(ws, region=name) if (ws / "constant" / name).is_dir() else {}
        cells = int(q.get("cells", 0) or 0)
        total_cells += cells
        if q.get("fatal"):
            fatal += [f"{name}:{f}" for f in q["fatal"]]
        worst_skew_frac = max(worst_skew_frac, q.get("skew_fraction", 0.0) or 0.0)
        no = q.get("max_non_ortho")
        if no is not None:
            worst_non_ortho = no if worst_non_ortho is None else max(worst_non_ortho, no)
        per_region.append({"name": name, "type": rmap.get(name, {}).get("type", "?"),
                           "cells": cells, "fatal": q.get("fatal", []),
                           "skew_fraction": q.get("skew_fraction", 0.0)})
    iface = check_interfaces(ws, rmap)
    # RECONCILIATION: actual split regions vs the declared plan. An undeclared region is a
    # semantic defect, not cosmetic - the delivered domain0 carried a coupled domain0_to_air
    # patch that regionProperties never listed, i.e. a region the solver would trip over.
    undeclared = [n for n in present if rmap and n not in rmap]
    return {
        "cells": total_cells,
        "fatal": fatal,
        "skew_fraction": worst_skew_frac,
        "max_non_ortho": worst_non_ortho,
        "regions": per_region,
        "regions_missing": missing,
        "regions_undeclared": undeclared,
        **iface,
        "mesh_ok": (not fatal) and (not missing) and (not undeclared) and iface["interface_ok"],
    }


# finalize: manifest (executor seam)

def finalize(workspace_dir: str, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    import json
    ws = Path(workspace_dir)
    # The region declaration must be present AND readable. An existence check passed a truncated
    # file straight through to the manifest, where nothing else looks at it.
    from meshpipeline.engines.snappy_multiregion import regions as _regions
    try:
        _regions.read_region_properties(ws)
    except _regions.RegionPropertiesError as exc:
        return {"success": False, "stdout": "", "stderr": "",
                "output": f"[SNAPPY_MULTIREGION] {exc}"}
    q = check_mesh(ws)
    # DELIVERED USER BOUNDARIES (the user-contract surface): external, non-interface patches across
    # every region, each carrying the role the mesh actually evidences. See
    # delivered_user_boundary_types - extracted so the interface exclusion and role derivation are
    # directly testable rather than inlined here.
    patch_types = delivered_user_boundary_types(ws, intake_patches)
    # REVIEW SURFACE: this engine declares visual review, so the reviewer NEEDS mesh.msh -
    # a live 2-solid run passed every executor gate and then FAILED at the reviewer because
    # this finalize never wrote one. Build it from the staged per-region surfaces (one named
    # physical group per region), same shared writer the flow engines use. Render-only.
    patch_entities: dict = {}
    try:
        from meshpipeline.cad.stl_io import read_stl_solids
        _tris: dict = {}
        for _name in _region_dirs(ws):
            _stl = ws / "constant" / "triSurface" / f"{_name}.stl"
            if _stl.exists():
                _sol = read_stl_solids(_stl)
                _tris[_name] = [t for v in _sol.values() for t in v]
        if _tris:
            patch_entities, _ = build_review_msh(ws, _tris)
    except Exception:
        logger.exception("multiregion finalize: review-mesh build failed (non-fatal)")
    allmins = [0.0, 0.0, 0.0]; allmaxs = [1.0, 1.0, 1.0]
    if (ws / "_assembly" / "solids.json").exists():
        solids = json.loads((ws / "_assembly" / "solids.json").read_text())
        allmins = [min(s["bbox_min"][i] for s in solids) for i in range(3)]
        allmaxs = [max(s["bbox_max"][i] for s in solids) for i in range(3)]
    # carry the declared regions so the region-contract gate can compare
    ep = dict(engine_params or {})
    if (ws / ".regions.json").exists():
        ep["_regions"] = json.loads((ws / ".regions.json").read_text())
    from meshpipeline.engines.manifest import write_manifest
    write_manifest(
        ws,
        patch_types=patch_types,
        patch_entities={**{n: [] for n in patch_types}, **patch_entities},
        bbox=(allmins[0], allmins[1], allmins[2], allmaxs[0], allmaxs[1], allmaxs[2]),
        quality=q,
        domain=domain or "multi-region coupled",
        body_bbox=(tuple(allmins), tuple(allmaxs)),
        mesh_bounds=(allmins[0], allmins[1], allmins[2], allmaxs[0], allmaxs[1], allmaxs[2]),
        volume_path=str((ws / "constant").resolve()),
        mesh_units=COMPLETED_MESH_UNIT.value,
        mesh_mode="snappy_multiregion",
        flow_topology=flow_topology,
        engine_params=ep,
    )
    out = (f"[SNAPPY_MULTIREGION] regions={[r['name'] for r in q['regions']]} "
           f"cells={q['cells']} missing={q['regions_missing']} "
           f"interfaces_ok={q['interface_ok']} fatal={q['fatal']}")
    return {"success": q["mesh_ok"], "stdout": out, "stderr": "", "output": out}
