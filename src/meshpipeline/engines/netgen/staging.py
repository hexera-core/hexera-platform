# Responsibility: Stage an internal-flow upload for Netgen: the closed fluid boundary with every patch under its declared name, the openings recorded for the port gate, and the measured passage field the plan sizes from.
# Boundaries: worker-side geometry; the staging itself is the shared one every internal engine reads (cad/internal_surface.stage_internal) - nothing here re-derives a port or a wall.
# Collaborates with: agents/builder/attempt._stage_declared (calls stage_declared), engines/netgen/plan.py, engines/region_check.record_port_openings.
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: the staged boundary Netgen meshes: one named ASCII solid per patch, metres
FLUID_BOUNDARY = "fluid_boundary.stl"
#: the staging record (openings, interior point, the patch files) beside it
STAGING_FACT = "netgen_staging.json"
STAGED_DIR = "_internal_stls"


def stage_declared(workspace, *, geometry_path, prepared, intake_patches: list,
                   input_kind: str = "") -> dict | None:
    """Stage the fluid boundary for any source - a CAD solid on its B-rep (a fluid domain, or a
    part whose cavity is the fluid), any triangle upload as the staged metre surface - through the
    one internal-flow staging, and record it. None when nothing is declared to open."""
    from meshpipeline.cad.internal_surface import stage_internal
    from meshpipeline.engines.port_binding import declaration_targets
    from meshpipeline.engines.region_check import record_port_openings
    ws = Path(workspace)
    ports = [p for p in (intake_patches or []) if isinstance(p, dict)
             and str(p.get("type") or "").strip() in ("inlet", "outlet")]
    if not ports:
        return None
    from meshpipeline.cad.internal_surface import is_cad, stage_internal_surface
    from meshpipeline.engines.port_binding import bind_intake
    fluid_solid = str(input_kind or "").strip() == "fluid-domain"
    t = None
    if fluid_solid or not is_cad(geometry_path):
        # a CAD solid that IS the fluid, on its B-rep; any triangle upload, on the staged surface
        t = stage_internal(geometry_path, ws, prepared=prepared, intake_patches=intake_patches or [],
                           input_kind=input_kind, out_name=STAGED_DIR,
                           declared_ports=declaration_targets(intake_patches or []))
    if t is None or not t.get("wall_bounds_fluid", True):
        # A CAD PART AROUND THE FLUID (a pipe, a housing): its B-rep tessellation is the METAL skin -
        # bore, outer skin and ring end faces - and Netgen fills whatever a closed surface bounds, so
        # it would mesh the metal. The surface staging finds the fluid side of the wall and closes it
        # at the confirmed openings (as engines/gmsh/gmsh_runner.stage_declared does).
        t = stage_internal_surface(ws / "input.stl", ws / STAGED_DIR, intake_patches=intake_patches,
                                   input_kind=input_kind)
    # THE USER'S NAMES, as every carving engine binds them (engines/port_binding.bind_intake): the
    # B-rep path stages engine-canonical keys (inlet, outlet_1, ...), the surface path the declared
    # ones - one binding re-keys both, and folds a blind opening into the wall
    bore = str(input_kind or "").strip() != "fluid-domain"
    t, wall_name, note = bind_intake(t, intake_patches or [], bore=bore)
    write_fluid_boundary(ws / FLUID_BOUNDARY, t, wall_name)
    record_port_openings(ws, t["openings"], bore=bore, intake_patches=intake_patches)
    keep = {k: t.get(k) for k in ("openings", "interior_point", "bbox_min", "bbox_max",
                                  "wall_bounds_fluid", "sealed", "source")}
    keep["wall_name"] = wall_name
    keep["binding_note"] = note
    keep["stls"] = {("wall" if k == wall_name else k): _relative(Path(v), ws)
                    for k, v in (t.get("stls") or {}).items()}
    keep["patches"] = (t.get("facts") or {}).get("patches")
    (ws / STAGING_FACT).write_text(json.dumps(keep, indent=1, default=str))
    return {"ports": list(t.get("openings") or {}), **keep}


def _relative(p: Path, ws: Path) -> str:
    """A staged file inside the workspace by its workspace-relative path (an attempt's workspace is
    carried forward to the next one), anything else as it is."""
    try:
        return p.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        return str(p)


def write_fluid_boundary(path: Path, t: dict, wall_name: str) -> dict[str, int]:
    """ONE ASCII STL, every patch a named solid: the wall (with any opening folded into it) under
    the declared wall name, then each bound opening under its declared name. Returns the triangle
    count per patch."""
    from meshpipeline.cad.stl_io import _write_solid, read_stl_triangles
    stls = dict(t.get("stls") or {})
    wall = list(read_stl_triangles(Path(stls.pop(wall_name))))
    for p in (t.get("folded_stls") or {}).values():
        wall += list(read_stl_triangles(Path(p)))
    counts = {wall_name: len(wall)}
    with open(path, "w", encoding="utf-8") as fh:
        _write_solid(fh, wall_name, wall)
        for name in sorted(stls):
            tris = list(read_stl_triangles(Path(stls[name])))
            if not tris:
                continue
            _write_solid(fh, name, tris)
            counts[name] = len(tris)
    return counts


def read_staging(workspace) -> dict:
    p = Path(workspace) / STAGING_FACT
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def staged_patch_files(workspace, record: dict) -> dict:
    """patch name -> its staged STL (absolute), the wall under "wall"."""
    ws = Path(workspace)
    out = {}
    for k, v in (record.get("stls") or {}).items():
        p = Path(v)
        out[k] = p if p.is_absolute() else ws / p
    return out


def staged_triangles(workspace) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """The staged boundary by patch, and all its triangles."""
    from meshpipeline.cad.stl_io import read_stl_solids
    solids = read_stl_solids(Path(workspace) / FLUID_BOUNDARY)
    arrs = {k: np.asarray(v, float).reshape(-1, 3, 3) for k, v in solids.items() if len(v)}
    allt = np.concatenate(list(arrs.values())) if arrs else np.zeros((0, 3, 3))
    return arrs, allt
