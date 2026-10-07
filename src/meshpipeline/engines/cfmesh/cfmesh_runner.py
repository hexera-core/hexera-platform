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

# The internal-flow staging of a triangle surface (any upload that is not a CAD solid), in the
# record tessellate_internal returns.
from meshpipeline.cad.internal_surface import (  # noqa: F401
    InternalSurfaceError,
    stage_internal_surface,
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

    from meshpipeline.contracts.intake_formats import is_cad

    geom_path, out_stl = Path(geom_path), Path(out_stl)
    if is_cad(geom_path):
        _shutil.copy2(geom_path, out_stl.parent / "geometry.step")
    return _cad_tessellate_to_stl(geom_path, out_stl, prepared=prepared)


# #
# surface assembly: body + bounding box -> named-patch .fms (cfMesh input)
# #
def prepare_surface(workspace, *, geometry_file: str,
                    domain_min, domain_max, wall_patch: str = "body",
                    farfield_patch: str = "farfield", feature_angle: float = 30.0,
                    mirror_y_half: bool = False, body_walls: list | None = None,
                    bashrc: str = _DEFAULT_BASHRC) -> dict:
    ws = Path(workspace)
    body_stl = ws / geometry_file
    if body_stl.suffix.lower() != ".stl":
        raise ValueError(f"input must be a surface STL, got '{body_stl.suffix}' "
                         f"(export STL from CAD upstream - OpenFOAM has no CAD import)")
    # REGIONS the surface already names. read_stl_triangles dissolves the solid boundaries, which
    # is the right read for a body meshed as one wall and the wrong one for a surface whose parts
    # are named: renameBoundary below maps FMS solids to patch names, so a collapse here is what
    # decides a multi-patch request can never be honoured - not any limit of cfMesh. The
    # DECLARATION decides what they become (declared_boundary.stage_regions): one declared body
    # wall makes them one solid under its name, several keep each part under the declared wall it
    # matches. Written under the file's own spellings, no renameBoundary entry matched them and
    # every part landed in the default patch - "fixedWalls" - while the approved wall had no faces.
    from meshpipeline.engines.declared_boundary import stage_regions
    solids = {n: drop_degenerate(v) for n, v in read_stl_solids(body_stl).items()}
    regions = {n: v for n, v in solids.items() if v} if len(solids) > 1 else {}
    regions = stage_regions(regions, list(body_walls) if body_walls else [wall_patch])
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
            paths = list(src) if isinstance(src, (list, tuple)) else [src]
            tris = []
            for one in paths:
                tris.extend(drop_degenerate(read_stl_triangles(Path(one))))
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


def domain_from_strategy(body_bbox, L: float, strategy: dict | None = None,
                         request: dict | None = None) -> tuple[list, list]:
    """The far-field box: margins from the strategy, else the approved request's typed extents,
    else the defaults; in the unit the domain-extent gate judges them in (the stated reference
    length, else the body's extent along the flow) and on the declared flow axis
    (engines/far_field.py). `L` - the body's LARGEST extent along a fixed +x - was the ruler
    before, and every body whose largest extent is not streamwise failed that gate: a rotor came
    back with 47.8 lengths downstream where 8 were asked (2026-10-04). It is kept in the signature
    for callers only."""
    from meshpipeline.engines.far_field import far_field_box, margins_from
    (bmin, bmax) = body_bbox
    s = strategy or {}
    req = request or {}
    margins = margins_from(s.get("domain_margin"), req.get("requested_extents"),
                           req.get("request_txt"))
    return far_field_box(bmin, bmax, margins, flow_axis=req.get("flow_axis"),
                         reference_length_m=(s.get("reference_length_m")
                                             or req.get("reference_length_m")))


def _render_object_refinements(features: list, *, cell_floor: float = 0.0) -> str:
    """objectRefinements blocks; no region cuts finer than `cell_floor` (the passage ceiling)."""
    blocks = _object_refinement_blocks(features, cell_floor=cell_floor)
    return "objectRefinements\n{\n" + "\n".join(blocks) + "\n}\n" if blocks else ""


def _object_refinement_blocks(features: list, *, cell_floor: float = 0.0) -> list[str]:
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
            cs = max(float(cs), float(cell_floor or 0.0))
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
    return blocks


#: EXTERNAL FLOW, a builder that sets no wall cell: the wall is sized from the measured wetted area
#: so the wall shell, its grading and its layers spend about this share of the cell budget (the
#: way snappy's planner back-solves its surface level from the area). The old default was L/20:
#: twenty cells along the body whatever the budget - an airliner came out with 452 wall faces
#: and 49k cells in a 4M budget, a car with 1,490 (HOME-TURF lab, 2026-10-05), and every gate
#: passed.
#: The wall cell is then snapped DOWN the octree (snap_to_octree): cfMesh refines by halving until
#: a cell is no larger than the size asked, so a size just under a level costs a whole level - 4x
#: the wall cells. Unsnapped, the SAE notchback, the Windsor body and the ONERA M6 came out at
#: 3.8-4.2M cells on a 2M budget at both a half and a quarter share (lab htf2-/htf5-: identical
#: meshes, the same octree level).
EXTERNAL_WALL_BUDGET_SHARE = 0.5
#: cells per wall face through the refined shell and its 2:1 grading out to the background,
#: before the prism layers (each layer adds one more per face)
EXTERNAL_SHELL_DEPTH = 4.0
#: how many halvings below the background cell the wall may sit, external flow (2^8: a far field
#: tens of body lengths across still reaches a wall cell sized for the body)
EXTERNAL_MAX_HALVINGS = 8


def external_wall_cell(surface_area_m2: float, cell_budget: int, n_layers: int,
                       L: float) -> float:
    """The default wall cell for external flow (metres): the size at which the wall's faces, times
    the shell depth and the layers, fill EXTERNAL_WALL_BUDGET_SHARE of the budget - never coarser
    than the old L/20."""
    area = max(float(surface_area_m2 or 0.0), 0.0)
    if area <= 0.0 or not cell_budget:
        return L / 20.0
    depth = EXTERNAL_SHELL_DEPTH + max(0, int(n_layers))
    cell = (area * depth / (EXTERNAL_WALL_BUDGET_SHARE * float(cell_budget))) ** 0.5
    return min(cell, L / 20.0)


def snap_to_octree(max_cell: float, wall_cell: float) -> float:
    """The octree cell (max_cell / 2^n) no finer than `wall_cell`, nudged up so cfMesh stops at that
    level: it refines a cell while it is larger than the size asked."""
    import math
    if not (max_cell > 0.0 and wall_cell > 0.0) or wall_cell >= max_cell:
        return wall_cell
    n = int(math.floor(math.log2(max_cell / wall_cell) + 1e-9))
    return max_cell / (2 ** n) * 1.001


def render_cfmesh_case(workspace, *, surface_file: str, wall_patch: str,
                       patches: list, body_bbox, L: float,
                       domain_min, domain_max, strategy: dict | None = None,
                       cell_budget: int | None = None,
                       default_boundary: bool = True,
                       passage_radius: dict | None = None,
                       passage_field: tuple | None = None,
                       max_halvings: int = 4, snap_wall: bool = False) -> dict:
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
    # PASSAGE CAPS (internal flow): whatever the strategy asked for, the wall band is no
    # coarser than 13 cells across the narrowest passage and the background no coarser than
    # 13 across the typical one, with the band one radius thick so a narrow passage is filled
    # at the wall size end to end. L/20 put 2.7 cells across a 173 mm bore on a 1.26 m reducer.
    _caps: dict = {}
    if passage_radius and passage_radius.get("p05") and passage_radius.get("median"):
        from meshpipeline.engines.passage import size_caps
        _caps = size_caps(passage_radius)
        max_cell = min(max_cell, _caps["max_cell"])
        wall_cell = min(wall_cell, _caps["wall_cell"])
    wall_cell = max(wall_cell, max_cell / float(2 ** max(1, int(max_halvings))))
    if snap_wall:
        # a wall size the engine chose from the budget: the octree level at or above it
        wall_cell = snap_to_octree(max_cell, wall_cell)
    # PASSAGE CEILING: neither the wall band nor a refinement box may cut finer than 40 cells
    # across the narrowest passage (the top of industry practice). A builder that asked for
    # 79 across turned a tee into 16.4 M hexes, an 886 MB deliverable and a 161-minute run.
    _floor = float(_caps.get("wall_cell_floor") or 0.0)
    wall_cell = max(wall_cell, _floor)
    _thick = (f" refinementThickness {_caps['refinement_thickness']:.6g};" if _caps else "")

    dict_parts = [
        _MESHDICT_HDR,
        f'surfaceFile "{surface_file}";\n',
        f"maxCellSize {max_cell:.6g};\n",
        f"localRefinement\n{{\n    {wall_patch} {{ cellSize {wall_cell:.6g};{_thick} }}\n}}\n",
    ]
    # NARROW PASSAGES, LOCALLY. The band above is sized for the passage most of the wall bounds;
    # where a passage is narrower than that cell carries at the floor, a box refines it to 13
    # across its own narrowest point - and only there (engines/passage.narrow_passage_regions).
    # Sized from the narrowest passage everywhere, the Fluent aorta's 20 mm trunk was cut at its
    # 3 mm branches' cell and cartesianMesh had made no mesh after 47 minutes; sized from the
    # typical one, a tee came in at 11.5 across where the floor is 12 (2026-10-04).
    # passage_field: (points, radius, wall area per point) of the staged wall.
    _narrow: list = []
    _narrow_note = ""
    if passage_field is not None and _caps:
        from meshpipeline.engines.passage import narrow_passage_regions
        _narrow = narrow_passage_regions(
            passage_field[0], passage_field[1], cell_m=wall_cell,
            areas=(passage_field[2] if len(passage_field) > 2 else None),
            budget_cells=(0.5 * cell_budget if cell_budget else None))
        _narrow_note = str(getattr(_narrow, "note", "") or "")
    # the builder's own regions stop at the ceiling; a narrow passage's box is sized for it
    _blocks = (_object_refinement_blocks(strategy.get("features") or [], cell_floor=_floor)
               + _object_refinement_blocks(
                   [{"name": f"narrowPassage{i}", "type": "box", "cellSize": b["cell_needed_m"],
                     "centre": [0.5 * (b["min"][k] + b["max"][k]) for k in range(3)],
                     "lengthX": b["max"][0] - b["min"][0], "lengthY": b["max"][1] - b["min"][1],
                     "lengthZ": b["max"][2] - b["min"][2]} for i, b in enumerate(_narrow)]))
    if _blocks:
        dict_parts.append("objectRefinements\n{\n" + "\n".join(_blocks) + "\n}\n")

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
            "est_background_cells": est_bg,
            "passage_caps": {k: round(v, 6) for k, v in _caps.items()} or None,
            "narrow_regions": [{"cell_size": round(b["cell_needed_m"], 6),
                                "radius": round(b["radius_m"], 6)} for b in _narrow] or None,
            **({"narrow_note": _narrow_note} if _narrow_note else {})}


def _bind_intake_shared(t: dict, declaration: list, *, bore: bool = True):
    from meshpipeline.engines.port_binding import bind_intake
    return bind_intake(t, declaration, bore=bore)


def _passage_sizing(t: dict, srcs: dict, wall_key: str, declaration: list):
    """(radius statistics for the size caps, (points, radius, wall area per point) for local
    refinement, or None) of the staged cavity. The field is read on the staged wall and held
    near each declared port to that port's half flow-width (engines/passage.staged_passage_field)
    - the flow crosses the port, so the passage there is no wider (a reading off a hollow part's
    outer skin, or along a rectangular duct's long side, sized cells for a passage the flow
    never sees: 20 of 72 corpus walls read 8-173% wide, 2026-10-04) - and its band is never
    wider than the widest declared port. The ports vouch for the reading; one they do not vouch
    for is replaced by the port radii, with no local refinement (the gate still measures the
    delivered mesh)."""
    from meshpipeline.engines.passage import (
        GLOBAL_BAND_MAX_CELLS,
        band_shell_cells,
        choose_passage_radius,
        declared_port_half_width,
        field_radius_stats,
        lid_hydraulic_diameters,
        point_areas,
        port_radius_stats,
        staged_passage_field,
    )
    raw = staged_passage_field(t, srcs, wall_key, declaration, corrected=False)
    # the ports vouch for the RAW reading: held to the port widths first, a reading off a hollow
    # part's outer skin (straight_reducer_015: 210-336 mm on an 82-165 mm bore) passed the check
    # at the ports and sized the whole wall from the skin (5.3 cells across, lab 2026-10-04).
    # Each port is read by its lid's hydraulic diameter: an annulus is its gap, not its bore.
    chord = field_radius_stats(*raw) if raw is not None else {}
    # each port at its narrowest flow width: the lid's hydraulic diameter, and the declared
    # short side of a rectangle or gap of an annulus where stated (a 140 x 384 mm duct's
    # hydraulic diameter is 205 mm; the floor counts cells across its 140 mm side)
    widths = dict(lid_hydraulic_diameters(srcs, wall_key))
    for p in declaration or []:
        hw = declared_port_half_width([p]) if isinstance(p, dict) else None
        if hw and p.get("name"):
            name = str(p["name"])
            widths[name] = min(float(widths.get(name) or float("inf")), 2.0 * hw)
    chosen = choose_passage_radius(chord, port_radius_stats(t.get("openings"), widths),
                                   fluid_boundary=bool(t.get("wall_bounds_fluid")))
    if not chosen or not str(chosen.get("source", "")).startswith("chord") or raw is None:
        return chosen, None
    field = staged_passage_field(t, srcs, wall_key, declaration, field=raw)
    chosen = {**field_radius_stats(*field), "source": chosen["source"]}
    # the band at the NARROWEST passage while that is affordable (a one-width part meshes as it
    # always did); the typical passage, with the narrow ones refined locally, when it is not
    if band_shell_cells(field[0], field[1], field[2], float(chosen["p05"])) <= GLOBAL_BAND_MAX_CELLS:
        chosen = {**chosen, "band": chosen["p05"], "band_from": "narrowest"}
    else:
        chosen = {**chosen, "band_from": "typical"}
    widest = max((declared_port_half_width([p]) or 0.0 for p in declaration or []), default=0.0)
    if widest and chosen.get("band") and float(chosen["band"]) > widest:
        chosen = {**chosen, "band": widest, "band_capped_at_declared_port": True}
    return chosen, (field[0], field[2], point_areas(field[0], field[1]))


def _configure_internal(workspace, *, strategy: dict, wall_patch: str,
                        contract_patches: list, args: dict, cell_budget: int,
                        surface=None) -> dict:
    from meshpipeline.cad.prepared_surface import require_metre_surface

    ws = Path(workspace)
    staged = require_metre_surface(surface, ws, "geometry.step")
    prepared_state = staged.consumed
    solid = ws / "geometry.step"

    from meshpipeline.engines.port_binding import BindError, declaration_targets
    from meshpipeline.engines.workspace_facts import port_declaration
    _decl = port_declaration(workspace)

    def _surface_way() -> dict:
        # THE UPLOAD IS A SURFACE (STL, OBJ, PLY, ...): the staged metre surface, closed at the
        # openings the user confirmed on it - lids on open ends, the capped faces of a fluid body
        return stage_internal_surface(staged.path, ws / "_internal_stls", intake_patches=_decl)

    if not solid.exists() and not Path(staged.path).exists():
        return {"success": False,
                "error": f"the staged surface ({Path(staged.path).name}) is missing from the "
                         "workspace, so the fluid cannot be closed off - nothing was meshed.",
                "next": "This is a staging fault, not the user's geometry: report it. Do NOT "
                        "retry with a different strategy."}
    try:
        if solid.exists():
            try:
                from meshpipeline.engines.workspace_facts import read_input_kind as _kind
                t = tessellate_internal(solid, ws / "_internal_stls", prepared=prepared_state,
                                        opening_faces=args.get("opening_faces") or None,
                                        declared_ports=declaration_targets(_decl),
                                        # a DECLARED fluid domain: its touching solids are one
                                        # fluid (cad_tessellate._fluid_union)
                                        fluid_solid=(True if _kind(workspace) == "fluid-domain"
                                                     else None))
            except BindError:
                raise
            except Exception as exc:  # noqa: BLE001 - the solid's own surface is tried next
                if not Path(staged.path).exists():
                    raise
                logger.warning("cfMesh internal: the B-rep could not be separated (%s: %s) - "
                               "staging its surface with the confirmed openings instead",
                               type(exc).__name__, exc)
                t = _surface_way()
        else:
            t = _surface_way()
    except InternalSurfaceError as exc:
        return {"success": False,
                "error": f"The fluid could not be closed off from this surface: {exc}",
                "next": "Relay this to the user verbatim - the openings on the picture need "
                        "changing, which only they can do. Do NOT retry with a different strategy."}
    except BindError as exc:
        return {"success": False,
                "error": f"Your declared ports could not be matched to the openings measured "
                         f"on the geometry. {exc}",
                "next": "Relay this to the user verbatim - the declaration needs a size, "
                        "location or interchangeability answer only they can give. Do NOT "
                        "retry with invented values."}
    # the fluid is the solid itself only for a confirmed fluid domain; anything else is a body
    # whose fluid is the bore it closes (which measure of a ring port the flow crosses)
    from meshpipeline.engines.workspace_facts import read_input_kind
    _bore = read_input_kind(ws) != "fluid-domain"
    try:
        t, _wall_key, _bound_note = _bind_intake_shared(t, _decl, bore=_bore)
        from meshpipeline.engines.region_check import record_port_openings, trusted_declaration
        # sized from the measured opening where the typed size disagrees (as the gate judges it)
        _sizing_decl = trusted_declaration(
            _decl, record_port_openings(workspace, t.get("openings"), bore=_bore,
                                        intake_patches=_decl))
    except BindError as exc:
        # a refusal, not a failure: the declaration and the measured geometry disagree, and
        # only the user can settle it
        return {"success": False,
                "error": f"Your declared ports could not be matched to the openings measured "
                         f"on the geometry. {exc}",
                "next": "Relay this to the user verbatim - the declaration needs a size, "
                        "location or interchangeability answer only they can give. Do NOT "
                        "retry with invented values."}
    _srcs = dict(t["stls"])
    if t.get("folded_stls"):
        # blind plugs are wall, physically: their triangles join the wall surface
        _srcs[_wall_key] = [_srcs[_wall_key], *t["folded_stls"].values()]
    from meshpipeline.engines.workspace_facts import read_input_kind
    _fluid_declared = read_input_kind(workspace) == "fluid-domain"
    if not t.get("wall_bounds_fluid") and not _fluid_declared and t.get("source") != "surface":
        # ONE REGION. A hollow wall is staged for a carve: the metal's whole skin and caps over the
        # whole mouths close the metal AND the cavity, and cartesianMesh - no seed point - fills
        # either (the rocket nozzle: its metal). Cut to the bore skin and the bore's part of each
        # cap, the surface closes the fluid alone (cad/bore_staging.py); when that cannot be built
        # closed, the staging stands as it was.
        from meshpipeline.cad.bore_staging import bore_only_surfaces
        try:
            _bore = bore_only_surfaces(_srcs, _wall_key, ws / "_internal_bore")
        except Exception:  # noqa: BLE001 - the whole-mouth staging is the fallback
            logger.warning("cfMesh internal: bore-only staging failed - keeping the whole-mouth "
                           "staging", exc_info=True)
            _bore = None
        if _bore:
            _srcs = {**_srcs, **_bore}
            t = {**t, "wall_bounds_fluid": True}
    prep = prepare_surface_internal(
        workspace, surfaces_src=_srcs,
        feature_angle=float(args.get("feature_angle", 30.0)))
    # the local passage radius of the staged boundary (wall + port caps close it) sizes the
    # wall band and the background; {} when the surfaces do not close, and the strategy stands
    # a solid DECLARED the fluid domain is the fluid's own boundary, whatever its ports look like
    # (this path stages a CAD solid without the declaration, so the record cannot know it)
    if _fluid_declared:
        t = {**t, "wall_bounds_fluid": True}
    passage_radius, _field = _passage_sizing(t, _srcs, _wall_key, _sizing_decl)

    _patches = list(contract_patches or [])
    if not _patches:
        _patches = [{"name": n, "type": ("wall" if n == "wall" else n)} for n in t["stls"]]

    bb_min, bb_max = prep["body_bbox"]
    L = max(bb_max[i] - bb_min[i] for i in range(3))
    summary = render_cfmesh_case(
        workspace, surface_file=prep["surface_file"], wall_patch=wall_patch,
        patches=_patches, body_bbox=prep["body_bbox"], L=L,
        domain_min=bb_min, domain_max=bb_max, strategy=strategy, cell_budget=cell_budget,
        passage_radius=passage_radius, passage_field=_field)
    _sizes = list((t.get("binding") or {}).get("size_notes") or [])
    return {"success": True, "wrote": ["system/meshDict"], "topology": "internal",
            "openings": t.get("openings"), "passage_radius": passage_radius, **summary,
            **({"port_sizes": _sizes} if _sizes else {}),
            "next": ("meshDict written for the enclosed cavity (valid + budget-clamped). "
                     + ("Tell the user, in these words, that a declared port size disagrees with "
                        "the geometry and the measured opening is used: " + " ".join(_sizes) + " "
                        if _sizes else "")
                     + "Call run_mesh NOW. Reconfigure ONLY on a concrete run_mesh failure.")}


def configure_mesh(workspace, *, geometry_file: str, strategy: dict, wall_patch: str,
                   contract_patches: list, args: dict, cell_budget: int,
                   surface=None) -> dict:
    from meshpipeline.engines.workspace_facts import read_dimensionality, read_flow_topology
    topology = read_flow_topology(workspace) or "external"
    # A GROUND PLANE is refused at intake for this engine (supports_ground_plane is False): its
    # far field is one closed box surface around the body, and standing the body on that box's
    # floor would need the two surfaces merged into one - which this path does not do. A case that
    # got here anyway is stopped before a build whose ground patch would come back with no faces.
    from meshpipeline.engines.ground_plane import ground_patch_name
    _ground = ground_patch_name(contract_patches) if topology != "internal" else None
    if _ground:
        return {"success": False, "error": (
            f"the patch contract declares a ground plane ('{_ground}'), and cfMesh's far field "
            "here is a closed box around the body - it cannot lay the body on the box's floor."),
            "next": (f"Relay this to the user: mesh the body free in the flow (drop '{_ground}'), "
                     "or use an engine that builds a ground plane. Do NOT retry with a different "
                     "strategy - no strategy value changes this.")}
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
        from meshpipeline.engines.workspace_facts import read_far_field_request, read_request_txt
        dmin, dmax = domain_from_strategy(body_bbox, analysis["L"], strategy,
                                          request={**read_far_field_request(workspace),
                                                   "request_txt": read_request_txt(workspace)})
    from meshpipeline.engines.declared_boundary import body_walls, farfield_name
    _patches = list(contract_patches or [])
    _farfield = farfield_name(_patches)
    if not _patches:
        _patches = [{"name": wall_patch, "type": "wall"},
                    {"name": _farfield, "type": "farfield"}]
    prep = prepare_surface(
        workspace, geometry_file=geometry_file, domain_min=dmin, domain_max=dmax,
        wall_patch=wall_patch, farfield_patch=_farfield,
        feature_angle=float(args.get("feature_angle", 30.0)),
        mirror_y_half=bool(args.get("mirror_y_half", False)),
        body_walls=body_walls(_patches) or None)
    strategy = dict(strategy or {})
    _sized_here = strategy.get("wall_cell") is None
    if _sized_here:
        # no size from the builder: spend the budget on the wall, measured from its area
        strategy = {**strategy, "wall_cell": external_wall_cell(
            analysis.get("surface_area") or 0.0, cell_budget,
            int(strategy.get("n_layers", 0) or 0), float(analysis["L"]))}
    summary = render_cfmesh_case(
        workspace, surface_file=prep["surface_file"], wall_patch=wall_patch,
        patches=_patches, body_bbox=prep["body_bbox"], L=analysis["L"],
        domain_min=dmin, domain_max=dmax, strategy=strategy, cell_budget=cell_budget,
        max_halvings=EXTERNAL_MAX_HALVINGS, snap_wall=_sized_here)
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
    # the profile's NAMED PARTS, staged under the declared walls exactly as the 3D path does
    # (declared_boundary.stage_regions): written as one solid under the first wall, every other
    # approved wall of a multi-element section came back with zero faces after the run
    from meshpipeline.engines.declared_boundary import body_walls, stage_regions
    _solids = {n: drop_degenerate(v) for n, v in read_stl_solids(ws / geometry_file).items()}
    _regions = stage_regions({n: v for n, v in _solids.items() if v} if len(_solids) > 1 else {},
                             body_walls(_inplane) or [wall_patch])
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
        from meshpipeline.engines.workspace_facts import read_far_field_request, read_request_txt
        dmin, dmax = domain_from_strategy(body_bbox, analysis["L"], strategy,
                                          request={**read_far_field_request(workspace),
                                                   "request_txt": read_request_txt(workspace)})
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
        if _regions:
            for _name, _tris in _regions.items():
                _write_solid(fh, _name, _tris)
        else:
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
