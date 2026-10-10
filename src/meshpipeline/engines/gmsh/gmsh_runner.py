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
    drawn = False
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("tess")
        # The metre scale is applied AS the shapes are read (a uniform similarity, OCC's
        # BRepBuilderAPI_Transform), not by occ.dilate afterwards: dilate is a general affine map
        # that rebuilds every curve, and on the Toyota Supra STEP it stopped staging with
        # "Geom_TrimmedCurve::parameters out of range" before any mesher ran (2026-10-04).
        gmsh.option.setNumber("Geometry.OCCScaling", float(factor))
        try:
            gmsh.model.occ.importShapes(str(geom_path))
        finally:
            gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        # the metre-normalised B-rep IS the staged geometry - the driver reads this, unscaled
        gmsh.write(str(staged_brep))
        # The staged SURFACE (input.stl) is a picture of that B-rep for the report and review,
        # never what the driver meshes. A size floor keeps a degenerate CAD edge from asking for
        # a zero size (CRM high-lift: "Wrong mesh element size lc = 0" stopped staging, lab
        # 2026-10-04); a surface gmsh still cannot draw is drawn by the shared OCC tessellator.
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
        gmsh.option.setNumber("Mesh.MeshSizeMin", max(diag * 1e-5, 1e-9))
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 24)
        try:
            gmsh.model.mesh.generate(2)
            gmsh.write(str(out_stl))
            drawn = True
        except Exception as exc:  # noqa: BLE001 - the B-rep is staged; only the picture failed
            logger.warning("gmsh staging: the surface picture could not be meshed (%s) - drawing "
                           "it with the shared OCC tessellator", exc)
            drawn = False
        gmsh.model.remove()
    finally:
        if _mine:
            gmsh.finalize()
    if not drawn:
        from meshpipeline.cad.cad_tessellate import tessellate_to_stl as _occ_tessellate
        _occ_tessellate(geom_path, out_stl, prepared=prepared)
    return out_stl


def _metre_factor(prepared) -> float:
    if prepared is None:
        raise ValueError(
            "the gmsh bundle needs the typed coordinate state to stage its B-rep; without it the "
            "physical size of the imported model would be a guess")
    return prepared.to_metres


#: A surface upload staged for internal flow (cad/internal_surface): the closed fluid boundary, one
#: named solid per patch - the wall and each confirmed opening. The driver fills it when there is
#: no CAD solid (geometry.step) to mesh.
FLUID_BOUNDARY = "fluid_boundary.stl"


def stage_declared(workspace, *, geometry_path, prepared, intake_patches: list,
                   input_kind: str = "") -> dict | None:
    """A fluid with declared openings whose volume is not the uploaded solid itself - a triangle
    surface (STL, OBJ, PLY, ...), or a CAD part declared to be the WALL around the fluid: the
    shared internal-flow staging closes the staged surface at the confirmed openings and names
    every patch, and the result is staged as FLUID_BOUNDARY for the driver to fill (it takes
    precedence over geometry.step, which would be the metal). None when it does not apply - a CAD
    solid that is the fluid or the part itself (meshed from geometry.step as before), or nothing
    declared to open."""
    import shutil

    from meshpipeline.cad.internal_surface import is_cad, stage_internal_surface
    ws = Path(workspace)
    ports = [p for p in (intake_patches or []) if isinstance(p, dict)
             and str(p.get("type") or "").strip() in ("inlet", "outlet")]
    if not ports or not (ws / "input.stl").exists():
        return None
    if is_cad(geometry_path) and str(input_kind or "").strip() != "body-surface":
        return None
    t = stage_internal_surface(ws / "input.stl", ws / "_internal_stls",
                               intake_patches=intake_patches, input_kind=input_kind)
    shutil.copy2(t["fluid_boundary"], ws / FLUID_BOUNDARY)
    from meshpipeline.engines.region_check import record_port_openings
    record_port_openings(ws, t["openings"], intake_patches=intake_patches)
    record = {k: t[k] for k in ("openings", "interior_point", "bbox_min", "bbox_max", "wall_name")}
    record["patches"] = t["facts"]["patches"]
    (ws / "internal_surface.json").write_text(json.dumps(record, indent=1))
    return {"ports": list(t["openings"]), **record}


def _discrete_surfaces(gmsh) -> list[dict]:
    """Tag, patch name, area and centroid of every discrete surface, from its own triangles."""
    import numpy as np
    ntags, coords, _ = gmsh.model.mesh.getNodes()
    ntags = np.asarray(ntags, dtype=np.int64)
    pts = np.asarray(coords, dtype=float).reshape(-1, 3)
    index = np.zeros(int(ntags.max()) + 1 if len(ntags) else 1, dtype=np.int64)
    index[ntags] = np.arange(len(ntags))
    out = []
    for _, tag in gmsh.model.getEntities(2):
        _types, _tags, nodes = gmsh.model.mesh.getElements(2, tag)
        if not len(nodes):
            continue
        conn = np.asarray(nodes[0], dtype=np.int64).reshape(-1, 3)
        tri = pts[index[conn]]
        a = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        c = (tri.mean(axis=1) * a[:, None]).sum(axis=0) / max(float(a.sum()), 1e-300)
        out.append({"tag": tag, "name": gmsh.model.getEntityName(2, tag), "area": round(float(a.sum()), 8),
                    "centroid": [round(float(v), 6) for v in c]})
    return out


def _inspect_fluid_boundary(ws: Path) -> dict:
    import gmsh
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("inspect_surface")
        # loaded EXACTLY as the driver loads it, so every tag reported here is the tag it meshes
        from meshpipeline.engines.gmsh.driver import _load_fluid_boundary
        _load_fluid_boundary(gmsh, ws / FLUID_BOUNDARY)
        surfaces = _discrete_surfaces(gmsh)
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        gmsh.model.remove()
    finally:
        if _mine:
            gmsh.finalize()
    return {
        "volumes": 1,
        "surfaces": surfaces,
        "curves": [],
        "bbox": [xmin, ymin, zmin, xmax, ymax, zmax],
        "diag": ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5,
        "note": ("The upload is a triangle surface, staged as ONE closed fluid volume: each surface "
                 "tag above is one patch, already named for the declaration (the wall and each "
                 "inlet/outlet). Map each contracted group to the surface of the same name; the "
                 "driver fills the volume they enclose."),
    }


def _inspect_surface(ws: Path, stl: Path) -> dict:
    """The face table of a SURFACE upload, classified exactly as the driver will classify it
    (engines/gmsh/surface_volume.py), so the tags a builder binds groups to are the tags meshed."""
    import gmsh

    from meshpipeline.engines.gmsh.surface_volume import classify_closed_surface, surface_table
    from meshpipeline.engines.workspace_facts import read_flow_topology, read_input_kind
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("inspect_surface")
        try:
            tags = classify_closed_surface(gmsh, str(stl))
        except Exception as exc:  # noqa: BLE001 - reported, the builder cannot fix a surface
            tags, why = [], f" ({type(exc).__name__}: {exc})"
        else:
            why = ""
        if not tags:
            gmsh.model.remove()
            return {"error": ("the staged surface (input.stl) has no faces gmsh can close into a "
                              f"volume{why} - an empty or open surface bounds nothing. Supply a "
                              "closed surface (or a CAD solid)."),
                    "source": "surface"}
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        out = {
            "volumes": 0,
            "surfaces": surface_table(gmsh, tags),
            "curves": [],
            "bbox": [xmin, ymin, zmin, xmax, ymax, zmax],
            "diag": ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5,
            "source": "surface",
        }
        if read_flow_topology(ws) == "external" and read_input_kind(ws) in ("solid-body", "body-surface"):
            out["note"] = ("A triangulated BODY for external flow: the engine cuts it out of a "
                           "far-field box and names the groups itself (the body under the declared "
                           "wall, the box under the declared far field) - write gmsh_spec.json "
                           "without groups.")
        else:
            out["note"] = ("A triangulated surface, split into faces wherever it folds sharply "
                           "(a port lid against its wall). Bind each declared port to its face by "
                           "the centroids below, every other face to the wall - or leave groups "
                           "out and the engine binds the declared ports itself.")
        gmsh.model.remove()
        return out
    finally:
        if _mine:
            gmsh.finalize()


def inspect_stl(workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
    ws = Path(workspace)
    geom = ws / "geometry.step"
    if (ws / FLUID_BOUNDARY).exists():
        return _inspect_fluid_boundary(ws)
    if not geom.exists():
        from meshpipeline.cad.internal_surface import staging_failure
        why = staging_failure(ws)
        if why:
            # the true reason the fluid boundary was never staged, not just a missing file
            return {"error": f"the geometry could not be staged for gmsh: {why}"}
        stl = ws / geometry_file
        if stl.exists():
            return _inspect_surface(ws, stl)
        return {"error": "neither geometry.step nor a staged surface (input.stl) is in the "
                         "workspace - nothing was staged for gmsh"}
    import gmsh
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        # the size the builder is told is the surface's, not OpenCascade's loose envelope
        gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
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
        from meshpipeline.engines.workspace_facts import read_flow_topology, read_input_kind
        if read_flow_topology(ws) == "external" and read_input_kind(ws) in ("solid-body", "body-surface"):
            out["note"] = ("A BODY for external flow: the engine cuts it out of a far-field box "
                           "(box minus body) and names the groups itself - the body under the "
                           "declared wall, the box under the declared far field. The tags above "
                           "name the body BEFORE that cut; write gmsh_spec.json without groups.")
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

def _no_deck_reason(ws: Path) -> str:
    """Why there is no deck: gmsh's own reason when the driver stopped on an error (driver.run
    writes it), so the user reads what gmsh could not do - not a builder that failed to write."""
    from meshpipeline.engines.gmsh.driver import STOP_REASON
    try:
        why = (ws / STOP_REASON).read_text().strip()
    except OSError:
        why = ""
    if not why:
        return "[GMSH] no mesh.inp - the Builder did not produce a valid mesh deck"
    hint = ""
    low = why.lower()
    if "plc" in low or "intersect" in low or "recover" in low:
        hint = (" - two faces of the closed surface cross or touch, so gmsh cannot recover the "
                "boundary as a volume to fill")
    elif "no solid" in low or "no volume" in low:
        hint = " - the surface does not close a volume gmsh can fill"
    return f"[GMSH] no mesh.inp - gmsh stopped: {why}{hint}"


def finalize(workspace_dir: str, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    ws = Path(workspace_dir)
    if not (ws / "mesh.inp").exists():
        return {"success": False, "stdout": "", "stderr": "",
                "output": _no_deck_reason(ws)}
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
    # EXTERNAL: the body sits inside the far-field box the driver cut it from. The extent gate
    # measures that box around THAT body (engines/far_field.py sized it in the gate's own unit),
    # so the manifest records both, never the box as its own body.
    body_bbox = ((xmin, ymin, zmin), (xmax, ymax, zmax))
    requested_box = None
    if q.get("external") and q.get("domain_box") and q.get("body_bounds"):
        requested_box = [list(q["domain_box"][0]), list(q["domain_box"][1])]
        body_bbox = (tuple(q["body_bounds"][0]), tuple(q["body_bounds"][1]))
    from meshpipeline.engines.manifest import write_manifest
    write_manifest(
        ws,
        patch_types=groups,                       # group name → role
        patch_entities={n: [] for n in groups},
        bbox=tuple(bounds),
        quality=q,
        domain=domain or "structural FEA",
        body_bbox=body_bbox,
        requested_box=requested_box,
        reference_length=q.get("reference_length"),
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
