# Responsibility: Drive a Gmsh run: mesh the solid and write the element deck.
# Boundaries: the driver runs as a module of the installed distribution, so image and wheel cannot drift.
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
from meshpipeline.sandbox.safe_exec import (
    NativeOutcome,
    describe_native_result,
    run_guarded,
)

logger = logging.getLogger(__name__)

SICN_FLOOR = 0.1   # single source for the gate + criteria threshold


# geometry staging + inspection

def tessellate_to_stl(geom_path, out_stl, *, context=None, prepared=None) -> Path:
    out_stl = Path(out_stl)
    ws = out_stl.parent
    ws.mkdir(parents=True, exist_ok=True)
    staged_brep = ws / "geometry.step"

    factor = _metre_factor(prepared)

    import gmsh
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("tess")
        tags = gmsh.model.occ.importShapes(str(geom_path))
        if factor != 1.0:
            # ONE dilation about the origin, before synchronize: entity tags, volumes and
            # surfaces are carried through it, so physical groups assigned later still land on
            # the same topology.
            gmsh.model.occ.dilate(tags, 0, 0, 0, factor, factor, factor)
        gmsh.model.occ.synchronize()
        # the metre-normalised B-rep IS the staged geometry - the driver reads this, unscaled
        gmsh.write(str(staged_brep))
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 24)
        gmsh.model.mesh.generate(2)
        gmsh.write(str(out_stl))
        gmsh.model.remove()
    finally:
        if _mine:
            gmsh.finalize()
    return out_stl


def _metre_factor(prepared) -> float:
    if prepared is None:
        raise ValueError(
            "the gmsh bundle needs the typed coordinate state to stage its B-rep; without it the "
            "physical size of the imported model would be a guess")
    return prepared.to_metres


def inspect_stl(workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
    ws = Path(workspace)
    geom = ws / "geometry.step"
    if not geom.exists():
        return {"error": "geometry.step missing - the workspace was not staged "
                         "for gmsh (upload a CAD solid, not a bare STL)"}
    import gmsh
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("inspect")
        gmsh.model.occ.importShapes(str(geom))
        gmsh.model.occ.synchronize()
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        n_volumes = len(gmsh.model.getEntities(3))
        surfaces = []
        for _, tag in gmsh.model.getEntities(2):
            area = gmsh.model.occ.getMass(2, tag)
            cx, cy, cz = gmsh.model.occ.getCenterOfMass(2, tag)
            surfaces.append({"tag": tag, "area": round(area, 8),
                             "centroid": [round(cx, 6), round(cy, 6), round(cz, 6)]})
        # curves too - but ONLY for a model with no solids: a 2D planar case maps its
        # contracted boundary groups onto CURVE tags (curve_tags), the way 3D maps onto
        # surface_tags. For a SOLID model the curve table binds nothing and is pure bulk:
        # on a 57-face pump volute it was 73% of the report and pushed it past the builder's
        # tool-output cap, which replaced the whole report with an error the model could not
        # act on - the builder never learned a single surface tag (job d0fc1033, 2026-08-28).
        curves = []
        if n_volumes == 0:
            for _, tag in gmsh.model.getEntities(1):
                ln = gmsh.model.occ.getMass(1, tag)
                mx, my, mz = gmsh.model.occ.getCenterOfMass(1, tag)
                curves.append({"tag": tag, "length": round(ln, 8),
                               "midpoint": [round(mx, 6), round(my, 6), round(mz, 6)]})
        out = {
            "volumes": n_volumes,
            "surfaces": surfaces,
            "curves": curves,
            "bbox": [xmin, ymin, zmin, xmax, ymax, zmax],
            "diag": ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5,
        }
        gmsh.model.remove()
        return out
    finally:
        if _mine:
            gmsh.finalize()


# meshing (subprocess) + quality

def run_cartesian_mesh(workspace, *, timeout: int, context=None) -> dict:
    from meshpipeline.contracts.mesh_execution import run_mesh
    return run_mesh(workspace, engine="gmsh", timeout=timeout)


def _run_gmsh_local(workspace, *, timeout: int, **_ignored) -> dict:
    ws = Path(workspace)
    # Invoked by FULL module path, as a module of the installed distribution. A shorter path
    # would only resolve with cwd inside the package, which puts its subpackages on sys.path as
    # top-level modules - the bare-namespace bypass the src layout exists to prevent.
    try:
        proc = run_guarded(
            [sys.executable, "-m", "meshpipeline.engines.gmsh.driver", str(ws)],
            capture_output=True, text=True, timeout=timeout,
        )
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-2000:]
        return describe_native_result(returncode=proc.returncode, args=proc.args,
                                      stage="gmsh driver", output=tail)
    except subprocess.TimeoutExpired as exc:
        tail = (((exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
                + "\n" + ((exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")))[-2000:]
        return describe_native_result(returncode=-1, args=exc.cmd, stage="gmsh driver",
                                      output=tail, outcome=NativeOutcome.timed_out)


def check_mesh(workspace) -> dict:
    ws = Path(workspace)
    qp = ws / "quality.json"
    if not qp.exists():
        return {"cells": 0, "fatal": ["quality.json missing - run_mesh has not "
                                      "produced a mesh yet"], "mesh_ok": False}
    q = json.loads(qp.read_text())
    q["mesh_ok"] = (not q.get("fatal")) and q.get("min_sicn", 0.0) >= SICN_FLOOR
    return q


# finalize: manifest + deliverable check (executor seam)

def finalize(workspace_dir: str, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    ws = Path(workspace_dir)
    if not (ws / "mesh.inp").exists():
        return {"success": False, "stdout": "", "stderr": "",
                "output": "[GMSH] no mesh.inp - the Builder did not produce a valid mesh deck"}
    q = check_mesh(ws)
    groups: dict = dict(q.get("groups", {}))
    # Include the default (unassigned-surfaces) group ONLY when the driver actually
    # created it - fabricating a group the deck lacks fails the patch contract as an
    # undeclared extra. Older quality.json (no flag) predates this and always created
    # it, so absent reads as True.
    if q.get("default_group_used", True):
        groups.setdefault(str(q.get("default_group", "free")), "free")
    bounds = q.get("bounds") or [0, 0, 0, 1, 1, 1]
    xmin, ymin, zmin, xmax, ymax, zmax = bounds
    from meshpipeline.engines.manifest import write_manifest
    write_manifest(
        ws,
        patch_types=groups,                       # group name → role
        patch_entities={n: [] for n in groups},
        bbox=tuple(bounds),
        quality=q,
        domain=domain or "structural FEA",
        body_bbox=((xmin, ymin, zmin), (xmax, ymax, zmax)),
        mesh_bounds=tuple(bounds),
        volume_path=str((ws / "mesh.inp").resolve()),   # the deliverable deck
        mesh_units=COMPLETED_MESH_UNIT.value,
        mesh_mode="gmsh",
        flow_topology=flow_topology,
        engine_params=engine_params or {},
    )
    out = (f"[GMSH] elements={q.get('cells')} nodes={q.get('nodes')} "
           f"order={q.get('element_order')} min_sicn={q.get('min_sicn')} "
           f"fatal={q.get('fatal', [])}")
    return {"success": not q.get("fatal"), "stdout": out, "stderr": "", "output": out}
