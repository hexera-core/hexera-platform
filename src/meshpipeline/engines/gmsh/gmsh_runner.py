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


# finite-volume quality bars (the fleet-audit follow-up: gmsh fluid domains shipped at
# 73.9 deg max non-orthogonality past checkMesh's 70 deg severe line, while every snappy
# mesh respected its own 65 deg gate)

def fv_applicability(quality: dict, workspace) -> str:
    """Whether the FV bars BLOCK this mesh: "hard" | "advisory" | "not_applicable".

    The metrics judge finite-volume solvability, so they block a FLUID-VOLUME deliverable
    (the case declares a flow topology, or a purpose that requires a fluid volume) and stay
    advisory-but-reported for a structural FE deck; a 2D planar FEA triangle mesh has no FV
    face metrics at all. Applicability is decided by the CASE, never by the engine name
    (the same rule the spec's conformance coverage states for the domain-extent check).
    """
    if str((quality or {}).get("dimensionality", "3D")).upper() == "2D":
        return "not_applicable"
    from meshpipeline.engines.workspace_facts import read_flow_topology, read_purpose
    if read_flow_topology(workspace):
        return "hard"
    purpose = read_purpose(workspace)
    if purpose:
        from meshpipeline.engines.purposes import PURPOSES
        p = PURPOSES.get(purpose)
        req = p.requires_mesh_kind if p is not None else ""
        kinds = {req} if isinstance(req, str) else set(req)
        if "fluid-volume" in kinds:
            return "hard"
    return "advisory"


def fv_quality_check(quality: dict, workspace) -> tuple[str, bool, str]:
    """(mode, ok, why) against the declared hard FV bars (engines/gmsh/settings.py).

    Fail-closed where it applies: a fluid-volume mesh whose quality report lacks the FV
    metrics FAILS as unmeasured rather than passing unjudged.
    """
    mode = fv_applicability(quality, workspace)
    if mode == "not_applicable":
        return mode, True, ""
    import meshpipeline.engines.gmsh.settings as gq
    from meshpipeline.engines.gmsh.fv_metrics import fv_verdict
    ok, why = fv_verdict(quality or {},
                         nonortho_hard=gq.GMSH_FV_NONORTHO_HARD,
                         skew_internal_hard=gq.GMSH_FV_SKEW_INTERNAL_HARD,
                         skew_boundary_hard=gq.GMSH_FV_SKEW_BOUNDARY_HARD)
    return mode, ok, why


def run_enricher(R, workspace, res: dict, q: dict, out: dict) -> None:
    """run_mesh guidance: fold the FV verdict in so the builder iterates BEFORE submitting
    into a gate bounce (spec._load_run_enricher; same seam snappy and vmtk use)."""
    from meshpipeline.engines.registry import get_spec
    policy = get_spec("gmsh").run_policy
    if policy is None:  # pragma: no cover - the gmsh spec always declares a run policy
        return
    fatal = out.get("fatal_defects") or []
    if not out.get("success"):
        out["guidance"] = f"Not valid ({fatal or policy.fail_label}). {policy.fail_hint}"
        return
    mode, fv_ok, why = fv_quality_check(q, workspace)
    if mode == "hard" and not fv_ok:
        out["success"] = False
        out["mesh_ok"] = False
        out["guidance"] = ("MESH VALID BUT OVER THE FINITE-VOLUME QUALITY BARS - do not "
                           "submit. " + why)
        return
    if mode == "advisory" and not fv_ok:
        out["guidance"] = (policy.ok_guidance
                           + f" (FV-metric note, advisory for a structural deck: {why})")
        return
    out["guidance"] = policy.ok_guidance


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
           f"max_non_ortho={q.get('max_non_ortho')} "
           f"max_skewness={q.get('max_skewness')} "
           f"fatal={q.get('fatal', [])}")
    # ELEMENT-ORDER CONTRACT (defence in depth behind the driver's own enforcement): the
    # DELIVERED order in the quality report must be the user's declared param. Catches a
    # stale mesh surviving from before the declaration changed - the audit's finding was
    # order-1 declarations delivered as tet10.
    _declared = str((engine_params or {}).get("element_order") or "")
    _delivered_order = q.get("element_order")
    if _declared in ("1", "2") and _delivered_order is not None \
            and str(_delivered_order) != _declared:
        msg = (f"[GMSH] delivered element order {_delivered_order} does not match the "
               f"user's declared element_order {_declared} - the deck violates the intake "
               "contract. Run run_mesh again (the driver enforces the declared order).")
        return {"success": False, "stdout": out, "stderr": "", "output": f"{out}\n{msg}"}
    return {"success": not q.get("fatal"), "stdout": out, "stderr": "", "output": out}
