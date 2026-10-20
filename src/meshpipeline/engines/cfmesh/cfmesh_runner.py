# Responsibility: Drive a cfMesh run end to end: author, mesh, create patches and finalize.
# Owns: the run lifecycle and the runtime surface other layers reach through this module.
# Boundaries: 2D and 3D use different native binaries, chosen here; nothing above this module needs to know which.
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

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

#: THE RUN IS PLANNED IN TIME AS WELL AS IN CELLS. The external wall is sized to spend half the
#: 8M-cell budget, which at cfMesh's slower rates is more than its old fixed 20-minute cap could
#: fill: the DrivAer cars came to 4.3M cells in 15-18 minutes, the soccer ball ran out of time in
#: its last step (e2e sweep, 2026-10-17). So the case is timed before it runs and the run is given
#: the time its plan needs, up to a ceiling - and a plan that would not fit even that is made
#: coarser before it runs, never left to time out.
#: cfMesh's fill rate, cautiously: about the tenth percentile of cells per second of the whole
#: native run (cartesianMesh, checkMesh, the export) over the 71 cfMesh meshes above half a million
#: cells in that sweep (median 5,600; a tenth under 2,100).
CELLS_PER_SECOND = 2000.0
#: a plan FITS one run when this many times its estimate is inside the ceiling (a planning rule:
#: a plan that does not fit is made coarser before it runs)
RUN_CAP_HEADROOM = 1.25
#: THE RUN'S GUARD (contracts/work_limits, TIME-2) is set from the planned cells at cfMesh's SLOW
#: rate, and counts the near-edge rescue as planned work: at most this many runs' worth of work in
#: all - the mesher, then an optimiser pass, a protective re-mesh and its optimiser
#: (engines/cfmesh/rescue.py) - so a rescue is never cut short by a clock sized for the mesher
#: alone. Floor: the base cap; ceiling: run_ceiling_s.
RESCUE_RUNS = 3.0
#: the case's own record of its plan, read by the run cap (spec.run_policy.planned_timeout)
RUN_PLAN_FILE = "system/run_plan.json"


def run_ceiling_s() -> int:
    """The longest one cfMesh run may be given: twice the OpenFOAM command budget, at most 50
    minutes - inside one builder attempt (startup holds BUILDER_LOOP_TIMEOUT >= 2 x
    OPENFOAM_COMMAND_TIMEOUT, and run_mesh keeps its feedback reserve on top of the cap)."""
    import meshpipeline.settings.runtime as rtcfg
    return int(min(3000, 2 * rtcfg.OPENFOAM_COMMAND_TIMEOUT))


def base_run_cap_s() -> int:
    """The cap of a run with no plan on record (spec.run_policy.run_timeout)."""
    import meshpipeline.settings.runtime as rtcfg
    return int(min(1200, rtcfg.OPENFOAM_COMMAND_TIMEOUT))


def estimate_cells(*, volume: float, max_cell: float, wall_cell: float,
                   wall_area: float | None, n_layers: int,
                   shell_depth: float = EXTERNAL_SHELL_DEPTH) -> int | None:
    """The cells a rendered external case comes to: the background fill, plus the wall's faces
    times the refined shell and its layers (the model external_wall_cell sizes the wall with). On
    nine cfMesh meshes of the e2e sweep it read 0.8-1.0 of the cells they made. None when the
    wall's area is not known."""
    if not wall_area or wall_area <= 0 or wall_cell <= 0 or max_cell <= 0:
        return None
    background = max(float(volume), 0.0) / max_cell ** 3
    wall = float(wall_area) / wall_cell ** 2 * (shell_depth + max(0, int(n_layers)))
    return int(background + wall)


def run_guard(estimated_cells: float | None):
    """The planned work and GUARD of a cfMesh run planned to make `estimated_cells` cells
    (contracts/work_limits.PlannedWork): GUARD_FACTOR times its planned time at the slow rate,
    rescue included (RESCUE_RUNS), no less than the base cap, no more than the ceiling."""
    from meshpipeline.contracts.work_limits import guard_for
    return guard_for("cfmesh", cells=estimated_cells, steps=RESCUE_RUNS,
                     floor_s=base_run_cap_s(), ceiling_s=run_ceiling_s())


def run_cap_for(estimated_seconds: float | None) -> int:
    """The guard of a run planned to take `estimated_seconds` at the planning rate
    (CELLS_PER_SECOND): see run_guard. The base cap when nothing was planned."""
    if not estimated_seconds or estimated_seconds <= 0:
        return base_run_cap_s()
    return run_guard(float(estimated_seconds) * CELLS_PER_SECOND).guard_seconds


def fits_one_run(estimated_seconds: float) -> bool:
    return RUN_CAP_HEADROOM * float(estimated_seconds) <= run_ceiling_s()


def planned_run_cap(workspace) -> int | None:
    """The cap for the case written in `workspace`, from its run plan (RUN_PLAN_FILE) - None when
    the case carries no plan, and the base cap stands."""
    try:
        plan = json.loads((Path(workspace) / RUN_PLAN_FILE).read_text())
    except (OSError, ValueError, TypeError):
        return None
    cap = plan.get("run_cap_s") if isinstance(plan, dict) else None
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        return None
    return int(cap)


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


def _plan_run(*, volume: float, max_cell: float, wall_cell: float, wall_area: float | None,
              n_layers: int, coarsest: float, fit: bool) -> dict | None:
    """The run plan of a case: its estimated cells and time, the cap that gives it, whether it fits
    one run - and, with `fit`, the wall cell made one octree level coarser at a time (each level
    a quarter of the wall's cells) while it would not, never past `coarsest`. None when the cells
    cannot be estimated."""
    est = estimate_cells(volume=volume, max_cell=max_cell, wall_cell=wall_cell,
                         wall_area=wall_area, n_layers=n_layers)
    if est is None:
        return None
    asked, levels = est, 0
    while (fit and not fits_one_run(est / CELLS_PER_SECOND)
           and wall_cell * 2.0 <= coarsest * (1.0 + 1e-6)):
        wall_cell *= 2.0
        levels += 1
        est = estimate_cells(volume=volume, max_cell=max_cell, wall_cell=wall_cell,
                             wall_area=wall_area, n_layers=n_layers) or est
    seconds = est / CELLS_PER_SECOND
    plan: dict[str, Any] = {"estimated_cells": int(est), "cells_per_second": CELLS_PER_SECOND,
            "estimated_seconds": round(seconds, 1), "run_cap_s": run_cap_for(seconds),
            "fits": fits_one_run(seconds), "wall_cell": wall_cell,
            "guard": run_guard(est).as_dict()}
    if levels:
        plan.update(coarsened_levels=levels, cells_before=int(asked), note=(
            f"The wall cells were made {levels} level{'s' if levels != 1 else ''} coarser "
            f"({int(asked):,} -> {int(est):,} cells) so the run fits the "
            f"{run_ceiling_s() // 60}-minute limit of one cfMesh run."))
    if not plan["fits"]:
        plan["note"] = (f"About {int(est):,} cells, about {seconds / 60:.0f} minutes of cfMesh: more "
                        f"than one run may take ({run_ceiling_s() // 60} minutes), even with the "
                        "coarsest wall this body allows.")
    if levels or not plan["fits"]:
        logger.warning("cfMesh run plan: %s", plan.get("note"))
    return plan


#: INTERNAL FLOW: the cells cfMesh is asked to put across the passage - the wall band at its
#: radius, the background at the typical one, each narrow passage at its own. The resolution
#: floor counts 12 at the narrowest wall, on the delivered mesh's own boundary edges and chords,
#: and those scatter round the size planned on the staged wall. Sized for 13 - the shared target,
#: half a cell over the floor - every dev-side cfMesh mesh refused at the floor in the verify-5fix
#: sweep (bar one that had meshed the wall's metal) read just under it at its 5th percentile:
#: 11.0, 11.2, 11.6, 11.6, 11.6 (the mitral phantom's atrium cover and four venturi orifice
#: parts). 15 gives the reading two cells of room (about 1.5x the background cells of 13; the
#: ceiling of 40 is untouched, and internal_band_cells below takes much of the cost back).
CFMESH_CELLS_ACROSS = 15.0
#: ...and the fewest a wall point may get before it is refined: the wall band once it is put on the
#: octree, and the narrow-passage detection. cfMesh refines a cell by halving while it is larger
#: than the size asked, so a band asked for 0.1% under the background cell costs a whole level -
#: twice the cells across, eight times the cells in the band. annular_006 asked 1.55269 mm under a
#: 1.55492 mm background and came out at 6.3 M cells in 1,237 s of a 1,200 s cap; annular_010 at
#: 6.0 M in 1,335 s (verify-5fix). So the band is put on the octree by what it must hold
#: (internal_band_cells), not by a size a hair off. Half a cell under the 15 asked: at 15 itself,
#: the reading's own scatter along a plain pipe (14.98 here, 15.02 there) boxed whole walls one
#: level finer - 16 to 31 times the cells on five dev bends, reducers and transitions, offline.
CFMESH_MIN_CELLS_ACROSS = CFMESH_CELLS_ACROSS - 0.5
#: When the background cell is at most this much coarser than the band needs, the whole background
#: is made that little finer (at most twice its cells) rather than the band a whole level finer
#: (eight times the cells over a band that, in a tube, is most of the fluid).
BAND_BACKGROUND_SHRINK = 2.0 ** (1.0 / 3.0)
#: Narrow passages are detected this far under CFMESH_MIN_CELLS_ACROSS (see the call).
NARROW_DETECT_FACTOR = 0.99


def internal_band_cells(max_cell: float, wall_cell: float, band_radius: float, *,
                        min_across: float = CFMESH_MIN_CELLS_ACROSS,
                        shrink: float = BAND_BACKGROUND_SHRINK) -> tuple[float, float]:
    """(background cell, wall band cell) for an internal band of `band_radius`, on the octree:
      * the background already puts `min_across` across the band - the band IS the background
        (no refinement at all, the wall asks for exactly maxCellSize);
      * it is short by no more than `shrink` - the background is made just fine enough instead;
      * else the coarsest level (max_cell / 2^n) holding `min_across`, nudged 0.1% up so cfMesh
        stops there - or `wall_cell` when that is coarser than the level (between two levels;
        cfMesh's halving lands on the finer one either way)."""
    if not (max_cell > 0.0 and wall_cell > 0.0 and band_radius > 0.0):
        return max_cell, wall_cell
    need = 2.0 * band_radius / float(min_across)
    if max_cell <= need * (1.0 + 1e-9):
        return max_cell, max_cell
    if max_cell <= shrink * need:
        return need, need
    n = 0
    while n < 12 and max_cell / 2 ** n > need * (1.0 + 1e-9):
        n += 1
    level = max_cell / 2 ** n
    if level < wall_cell:
        return max_cell, wall_cell
    return max_cell, level * 1.001


def render_cfmesh_case(workspace, *, surface_file: str, wall_patch: str,
                       patches: list, body_bbox, L: float,
                       domain_min, domain_max, strategy: dict | None = None,
                       cell_budget: int | None = None,
                       default_boundary: bool = True,
                       passage_radius: dict | None = None,
                       passage_field: tuple | None = None,
                       max_halvings: int = 4, snap_wall: bool = False,
                       wall_area: float | None = None, fit_run: bool = False,
                       wall_ceiling: float | None = None) -> dict:
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
    # coarser than CFMESH_CELLS_ACROSS (15) across its passage and the background no coarser
    # than 15 across the typical one, with the band one radius thick so a narrow passage is
    # filled at the wall size end to end - then put on the coarsest octree level that still holds
    # CFMESH_MIN_CELLS_ACROSS (internal_band_cells). L/20 put 2.7 cells across a 173 mm bore on
    # a 1.26 m reducer.
    _caps: dict = {}
    _band_r = 0.0               # the band radius the caps sized the wall for, when they did
    if passage_radius and passage_radius.get("p05") and passage_radius.get("median"):
        from meshpipeline.engines.passage import size_caps
        _caps = size_caps(passage_radius, target=CFMESH_CELLS_ACROSS)
        max_cell = min(max_cell, _caps["max_cell"])
        _asked = strategy.get("wall_cell")
        # the wall size is the caps' own unless the builder asked for a finer one (a retry
        # answering the resolution floor), which stands as asked
        if _asked is None or float(_asked) >= _caps["wall_cell"]:
            _band_r = float(passage_radius.get("band") or passage_radius["p05"])
        wall_cell = min(wall_cell, _caps["wall_cell"])
    wall_cell = max(wall_cell, max_cell / float(2 ** max(1, int(max_halvings))))
    if snap_wall:
        # a wall size the engine chose from the budget: the octree level at or above it
        wall_cell = snap_to_octree(max_cell, wall_cell)
    elif _band_r > 0.0:
        # ...and the internal band the same way, by the cells it must put across the band
        max_cell, wall_cell = internal_band_cells(max_cell, wall_cell, _band_r)
    # PASSAGE CEILING: neither the wall band nor a refinement box may cut finer than 40 cells
    # across the narrowest passage (the top of industry practice). A builder that asked for
    # 79 across turned a tee into 16.4 M hexes, an 886 MB deliverable and a 161-minute run.
    _floor = float(_caps.get("wall_cell_floor") or 0.0)
    wall_cell = max(wall_cell, _floor)
    _thick = (f" refinementThickness {_caps['refinement_thickness']:.6g};" if _caps else "")

    # THE RUN, TIMED BEFORE IT RUNS (CELLS_PER_SECOND): the cap it gets, and - for a wall the
    # engine sized itself (fit_run) - one octree level coarser at a time, while the plan would not
    # fit the longest run allowed, never coarser than `wall_ceiling` (the old L/20 by default)
    plan = _plan_run(volume=vol, max_cell=max_cell, wall_cell=wall_cell, wall_area=wall_area,
                     n_layers=max(0, int(strategy.get("n_layers", 0))),
                     coarsest=(wall_ceiling if wall_ceiling else L / 20.0), fit=fit_run)
    if plan is not None:
        wall_cell = plan["wall_cell"]

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
            # 1% under the band's own minimum: a band put on an octree level is nudged 0.1% up
            # (internal_band_cells), and its own walls must not read as narrow and be boxed one
            # level finer - the very level the band just saved
            target=CFMESH_CELLS_ACROSS, detect=NARROW_DETECT_FACTOR * CFMESH_MIN_CELLS_ACROSS,
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
        # cfMesh's keyword is allowedDiscontinuity: the old "allowDiscontinuity" was silently
        # ignored. 0 keeps the wall's count on the layers it shares with the inlet/outlet caps,
        # so a stack does not end in a step at a port. untangleLayers lets cfMesh shrink, not
        # drop, a stack that would fold in a tight bend or a narrow branch.
        # EVERY declared wall gets the layers, not only the first: a multi-wall body (a car and
        # its wheels, a wing and its flap) is staged as one surface region per wall, and a region
        # the dict did not name kept cfMesh's single default boundary cell
        from meshpipeline.engines.ground_plane import is_ground
        _walls = [wall_patch] + [str(p.get("name")) for p in (patches or [])
                                 if str(p.get("type") or "").lower() == "wall" and p.get("name")
                                 and str(p.get("name")) != wall_patch
                                 and not is_ground(str(p.get("name")), "wall")]
        _entries = "".join(
            f"        {w}\n        {{ nLayers {n_layers}; thicknessRatio {ratio:g};"
            f"{first_line} allowedDiscontinuity 0; }}\n" for w in dict.fromkeys(_walls))
        dict_parts.append(
            "boundaryLayers\n{\n    patchBoundaryLayers\n    {\n" + _entries
            + "    }\n    optimiseLayer 1;\n    untangleLayers 1;\n}\n")

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
    _plan_path = ws / RUN_PLAN_FILE
    if plan is not None:
        _plan_path.write_text(json.dumps({k: v for k, v in plan.items() if k != "wall_cell"}))
    else:
        # a case with no plan runs on the base cap: never on a plan written for another case
        _plan_path.unlink(missing_ok=True)

    est_bg = int(vol / max(max_cell, 1e-30) ** 3)
    return {"max_cell_size": round(max_cell, 6), "wall_cell_size": round(wall_cell, 6),
            "n_layers": n_layers, "features": len(strategy.get("features") or []),
            "est_background_cells": est_bg,
            **({"run_plan": {k: v for k, v in plan.items() if k != "wall_cell"}} if plan else {}),
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
    # read along each side of a sharp edge too: a short orifice or throat has no other points
    raw = staged_passage_field(t, srcs, wall_key, declaration, corrected=False,
                               read_sharp_edges=True)
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
    if band_shell_cells(field[0], field[1], field[2], float(chosen["p05"]),
                        target=CFMESH_CELLS_ACROSS) <= GLOBAL_BAND_MAX_CELLS:
        chosen = {**chosen, "band": chosen["p05"], "band_from": "narrowest"}
    else:
        chosen = {**chosen, "band_from": "typical"}
    widest = max((declared_port_half_width([p]) or 0.0 for p in declaration or []), default=0.0)
    if widest and chosen.get("band") and float(chosen["band"]) > widest:
        chosen = {**chosen, "band": widest, "band_capped_at_declared_port": True}
    return chosen, (field[0], field[2], point_areas(field[0], field[1]))


def _stage_passages(ws: Path, srcs: dict, wall_key: str, declaration: list | None, *,
                    feature_angle: float = 30.0) -> dict:
    """The staged surface split into the passages the mesh fills (cad/fluid_passages.plan_passages):
    {"srcs": the first passage's patch surfaces (cartesianMesh meshes it in the case itself),
    "sizing": every meshed passage's surfaces (what the passage measure sizes the cells from),
    "bbox": their joint box, or None when nothing changed} - or {"error", "next"}: a declared
    opening that opens into nothing a mesher can fill, said before any run. Each further passage
    is its own surface in `_passages/p<k>/geom.stl`, listed in PASSAGES_FILE for the native run
    (engines/cfmesh/native.mesh_passages) to mesh beside the first and merge. A surface whose
    passages cannot be read is meshed as it was staged."""
    from meshpipeline.cad.fluid_passages import PASSAGES_FILE, plan_staged, write_passage

    (ws / PASSAGES_FILE).unlink(missing_ok=True)
    unchanged = {"srcs": srcs, "sizing": srcs, "bbox": None}
    plan = plan_staged(ws, srcs, wall_key, declaration)
    if plan is None or not plan.reading.read or not plan.meshed:
        return unchanged
    why = plan.refusal()
    if why:
        return {"error": why[0].upper() + why[1:],
                "next": "Relay this to the user verbatim - only they can say which openings are "
                        "real. Do NOT retry with a different strategy."}
    if not plan.left_out and not plan.several:
        return unchanged
    from meshpipeline.cad.fluid_passages import passage_surfaces
    from meshpipeline.cad.stl_io import _write_solid
    groups = passage_surfaces(plan)
    root = ws / "_passages"
    shutil.rmtree(root, ignore_errors=True)
    first = write_passage(groups[0], root / "p0")
    sizing: dict = {}
    for k, g in enumerate(groups):
        for name, path in write_passage(g, root / "all", prefix=f"p{k}_").items():
            sizing.setdefault(name, []).append(path)
    runs = []
    for k, g in enumerate(groups[1:], start=1):
        d = root / f"p{k}"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "geom.stl").open("w") as fh:
            for name, tris in g.items():
                _write_solid(fh, name, tris)
        runs.append(f"_passages/p{k}")
    if runs:
        (ws / PASSAGES_FILE).write_text(json.dumps({
            "runs": runs, "feature_angle": feature_angle,
            "ports": [p.ports for p in plan.meshed], "notes": plan.notes()}, indent=1))
    lo = [min(p.bbox[0][i] for p in plan.meshed) for i in range(3)]
    hi = [max(p.bbox[1][i] for p in plan.meshed) for i in range(3)]
    logger.info("cfMesh internal: %d passage(s) meshed, %d piece(s) left out - %s",
                len(plan.meshed), len(plan.left_out), "; ".join(plan.notes()))
    return {"srcs": first, "sizing": sizing, "bbox": [lo, hi], "notes": plan.notes()}


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
        _bore_surfaces: dict | None
        try:
            _bore_surfaces = bore_only_surfaces(_srcs, _wall_key, ws / "_internal_bore")
        except Exception:  # noqa: BLE001 - the whole-mouth staging is the fallback
            logger.warning("cfMesh internal: bore-only staging failed - keeping the whole-mouth "
                           "staging", exc_info=True)
            _bore_surfaces = None
        if _bore_surfaces:
            _srcs = {**_srcs, **_bore_surfaces}
            t = {**t, "wall_bounds_fluid": True}
    # THE PASSAGES THE MESH FILLS, read before it runs (cad/fluid_passages): cartesianMesh has no
    # seed point and keeps the LARGEST closed region, so a declared port on any other region came
    # back with no faces at all - one carotid of two, one tube of a sixteen-tube bundle, every port
    # of a nozzle whose port-less solid was the larger. Every passage that carries a declared
    # opening is meshed (side by side, one cartesianMesh run each, merged); a closed piece no
    # opening touches is left out; a declared opening into nothing meshable is said now, not
    # found as a zero-face patch after the run.
    _staged = _stage_passages(ws, _srcs, _wall_key, _decl,
                              feature_angle=float(args.get("feature_angle", 30.0)))
    if "error" in _staged:
        return {"success": False, "error": _staged["error"], "next": _staged["next"]}
    _srcs = _staged["srcs"]
    prep = prepare_surface_internal(
        workspace, surfaces_src=_srcs,
        feature_angle=float(args.get("feature_angle", 30.0)))
    if _staged.get("bbox"):
        prep["body_bbox"] = _staged["bbox"]          # the domain is every passage's
    # the local passage radius of the staged boundary (wall + port caps close it) sizes the
    # wall band and the background; {} when the surfaces do not close, and the strategy stands
    # a solid DECLARED the fluid domain is the fluid's own boundary, whatever its ports look like
    # (this path stages a CAD solid without the declaration, so the record cannot know it)
    if _fluid_declared:
        t = {**t, "wall_bounds_fluid": True}
    passage_radius, _field = _passage_sizing(t, _staged["sizing"], _wall_key, _sizing_decl)
    for _done in ("all", "p0"):
        # read into geom.stl and the passage field: only the further passages' surfaces travel
        shutil.rmtree(ws / "_passages" / _done, ignore_errors=True)

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
            **({"passages": _staged["notes"]} if _staged.get("notes") else {}),
            "next": ("meshDict written for the enclosed cavity (valid + budget-clamped). "
                     + ("Tell the user, in these words: " + " ".join(n[0].upper() + n[1:] + "." for n in _staged["notes"]) + " "
                        if _staged.get("notes") else "")
                     + ("Tell the user, in these words, that a declared port size disagrees with "
                        "the geometry and the measured opening is used: " + " ".join(_sizes) + " "
                        if _sizes else "")
                     + "Call run_mesh NOW. Reconfigure ONLY on a concrete run_mesh failure.")}


def _clear_run_too_long(workspace) -> None:
    """A case re-planned to fit drops the refusal an earlier, too-long plan of it recorded."""
    from meshpipeline.engines.preflight import PREFLIGHT_RECORD, read_refusal
    rec = read_refusal(workspace)
    if rec is not None and rec.builder_text.startswith("[PREFLIGHT_RUN_TOO_LONG]"):
        (Path(workspace) / PREFLIGHT_RECORD).unlink(missing_ok=True)


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
    strategy, _layer_note = with_requested_layers(workspace, strategy)
    if topology == "internal":
        out = _configure_internal(workspace, strategy=strategy, wall_patch=wall_patch,
                                  contract_patches=contract_patches, args=args,
                                  cell_budget=cell_budget, surface=surface)
        return {**out, **_layer_note} if out.get("success") else out
    out = _configure_external_3d(workspace, geometry_file=geometry_file, strategy=strategy,
                                 wall_patch=wall_patch, contract_patches=contract_patches,
                                 args=args, cell_budget=cell_budget, surface=surface)
    return {**out, **_layer_note} if out.get("success") else out


def with_requested_layers(workspace, strategy: dict | None) -> tuple[dict, dict]:
    """The strategy with the layer count the REQUEST states ("5 prism layers") when the builder
    gave none or 0 - a strategy without layers rendered no boundaryLayers at all - and a note for the
    builder when it asked for fewer than the request: the delivered mesh is measured against the
    request's count (engines/layer_census), so 2 where 5 were asked reads as 2 of 5."""
    from meshpipeline.engines.layer_census import stated_layers
    from meshpipeline.engines.workspace_facts import read_request_txt

    strategy = dict(strategy or {})
    stated = stated_layers(read_request_txt(workspace))
    if not stated:
        return strategy, {}
    if "n_layers" not in strategy:
        strategy["n_layers"] = stated
        return strategy, {"layers_requested": stated,
                          "layers_note": f"n_layers {stated}: the count the request states"}
    asked = int(strategy.get("n_layers") or 0)
    if asked <= 0:
        # NO LAYERS where the request states some is never the mesh asked for: the review
        # rejects it and the attempt is spent (a valid run is submitted as it stands). Fewer
        # than stated stays the builder's call - a concrete run_mesh failure can make a
        # thinner stack necessary.
        strategy["n_layers"] = stated
        return strategy, {"layers_requested": stated, "layers_note": (
            f"n_layers {stated}: the count the request states (the strategy asked for none)")}
    if asked < stated:
        return strategy, {"layers_requested": stated, "layers_note": (
            f"the request asks for {stated} prism layers and this strategy for {asked}: the "
            f"delivered mesh is measured against {stated}, so it will read short unless a "
            "concrete run_mesh failure made fewer layers necessary")}
    return strategy, {"layers_requested": stated}


def _configure_external_3d(workspace, *, geometry_file: str, strategy: dict, wall_patch: str,
                           contract_patches: list, args: dict, cell_budget: int,
                           surface=None) -> dict:
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
        max_halvings=EXTERNAL_MAX_HALVINGS, snap_wall=_sized_here,
        wall_area=float(analysis.get("surface_area") or 0.0) or None, fit_run=_sized_here)
    plan = summary.get("run_plan") or {}
    if plan and not plan.get("fits"):
        # A RUN THAT WOULD CLEARLY NOT FINISH is not started: it is refused here, in seconds, with
        # the numbers - and recorded for the executor, so the run ends on what was decided (and the
        # fallback ladder offers the next engine) instead of on a time-out 50 minutes later.
        from meshpipeline.contracts.failure_cause import FailureCause
        from meshpipeline.engines.preflight import PreflightRefusal
        _why = str(plan.get("note") or "the planned mesh is too big for one cfMesh run")
        PreflightRefusal(
            gate="finalize", cause=FailureCause.NOT_BUILT.value,
            builder_text=f"[PREFLIGHT_RUN_TOO_LONG] {_why}",
            facts={"engine": "cfMesh", "reason": _why, "before_meshing": True,
                   "deterministic": not _sized_here, "meshers_started": 0,
                   "estimated_cells": plan.get("estimated_cells"),
                   "estimated_seconds": plan.get("estimated_seconds")}).write(workspace)
        return {"success": False, "too_long_for_one_run": True, "error": _why, **summary,
                "next": ("Do NOT run_mesh: this plan cannot finish in one run. "
                         + ("Raise wall_cell (coarser wall cells) or drop objectRefinements, then "
                            "configure_mesh again." if not _sized_here else
                            "No setting of this engine makes it fit - STOP and end your turn."))}
    _clear_run_too_long(workspace)
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
