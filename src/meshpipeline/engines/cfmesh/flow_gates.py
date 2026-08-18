# Responsibility: Implement the blocking gates a cfMesh run must pass before review.
# Boundaries: the flow-engine gate set: manifest validity, the patch contract, boundary typing and the quality floor.
from __future__ import annotations

import json
import logging
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.gates import GateCtx, GateSpec

logger = logging.getLogger(__name__)


#: The files an OpenFOAM polyMesh is MADE of. `owner` alone was checked, so a mesh missing its
#: points or faces - which no solver and no reader can open - passed the deliverable gate.
_POLYMESH_REQUIRED = ("owner", "neighbour", "points", "faces", "boundary")


def _polymesh_is_complete(pm: Path, engine_label: str) -> tuple[bool, str]:
    if not pm.is_dir():
        return False, (f"constant/polyMesh missing - {engine_label} produced no volume mesh")
    missing = [f for f in _POLYMESH_REQUIRED if not (pm / f).exists()]
    if missing:
        return False, (
            f"constant/polyMesh is incomplete - missing {missing}. An OpenFOAM mesh is the whole "
            f"set {list(_POLYMESH_REQUIRED)}; no solver or reader can open it without them. "
            f"{engine_label} did not finish writing the mesh - run_mesh again.")
    empty = [f for f in _POLYMESH_REQUIRED if (pm / f).stat().st_size == 0]
    if empty:
        return False, (f"constant/polyMesh is incomplete - {empty} are present but EMPTY; "
                       f"{engine_label} was interrupted mid-write. Run run_mesh again.")
    return True, ""


def _validate_manifest(workspace: Path, domain: str = "") -> tuple[bool, str]:
    manifest_path = workspace / "mesh_manifest.json"

    if not manifest_path.exists() or manifest_path.stat().st_size == 0:
        return False, "mesh_manifest.json missing or empty - Builder did not write manifest"

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"mesh_manifest.json is malformed JSON: {e}"

    logger.debug("_validate_manifest: top-level keys received: %s", list(manifest.keys()))

    for key in ["schema_version", "geometry", "patches", "validation"]:
        if key not in manifest:
            return False, f"mesh_manifest.json missing required key: {key}"

    _reviewer_fields = {
        "mesh_units":              "reviewer cannot reference physical dimensions",
        "domain":                  "reviewer system prompt domain field will be empty",
    }
    for field, reason in _reviewer_fields.items():
        if not manifest.get(field):
            logger.warning(
                "_validate_manifest: manifest missing '%s' - %s - job will continue - workspace=%s",
                field, reason, workspace,
            )

    v = manifest.get("validation", {})

    if not v.get("has_wall", False):
        return False, "mesh_manifest.json: no patch of TYPE wall - surface classification failed"
    if not (v.get("has_inflow", False) and v.get("has_outflow", False)):
        return False, (
            "mesh_manifest.json: missing inflow/outflow boundary "
            "(need a patch of TYPE inlet + TYPE outlet, or a single TYPE farfield)"
        )
    _patch_val = v.get("patch_validation", {})
    _empty = [name for name, has_faces in _patch_val.items() if not has_faces]
    if _empty:
        return False, f"mesh_manifest.json: these patches have zero faces: {_empty}"

    # Compute-feasibility gate: the executor is the SOLE limiter of mesh size (the
    # reviewer no longer judges cell count). Over the cap → reject with ACTIONABLE
    # coarsening feedback so the builder can fix it on retry.
    _cell_count = manifest.get("cell_count", 0)
    _CELL_HARD_LIMIT = polcfg.CELL_HARD_LIMIT
    if _cell_count and _cell_count > _CELL_HARD_LIMIT:
        return (
            False,
            f"mesh_manifest.json: cell_count={_cell_count:,} exceeds the compute budget "
            f"({_CELL_HARD_LIMIT:,} cells) - the mesh is TOO FINE. COARSEN it: increase "
            "maxCellSize, raise the wall localRefinement cellSize, and drop or widen any "
            "objectRefinement, then run_mesh again. Do NOT shrink the domain box to cut "
            "cells - keep the far-field extents and reduce REFINEMENT instead."
        )

    # cfMesh writes a native OpenFOAM polyMesh.
    if not manifest.get("mesh_written", False):
        return False, "mesh_manifest.json: cfMesh reported the mesh was not written"
    ok, why = _polymesh_is_complete(workspace / "constant" / "polyMesh", "cfMesh")
    if not ok:
        return False, why
    return True, ""


def _gate_manifest_valid(ctx: GateCtx) -> tuple[bool, str]:
    ok, err = _validate_manifest(Path(ctx.workspace), domain=ctx.domain)
    return ok, ("" if ok else f"[MANIFEST_VALIDATION_FAILED] {err}")


def _gate_patch_contract(ctx: GateCtx) -> tuple[bool, str]:
    manifest = ctx.manifest_or_load()
    if not (manifest and ctx.intake_patches):
        return True, ""
    from meshpipeline.engines.contract import check_contract
    ok, diag = check_contract(
        intake_patches=ctx.intake_patches,
        manifest_patches=manifest.get("patches", []),
        manifest_patch_types=manifest.get("patch_types", {}),
    )
    return ok, ("" if ok else diag)


def _gate_boundary_types(ctx: GateCtx) -> tuple[bool, str]:
    from meshpipeline.engines.cfmesh.deliverable import reconcile_boundary_types
    reason = reconcile_boundary_types(ctx.workspace, ctx.intake_patches or [])
    return (False, reason) if reason else (True, "")


def _gate_quality_floor(ctx: GateCtx) -> tuple[bool, str]:
    # This engine's OWN declared gating criteria, enforced before the reviewer can see the mesh.
    # The bars and thresholds are the engine's (engines/cfmesh/criteria.py); this gate adds none.
    # gmsh and vmtk have always had the equivalent boundary (sicn_floor / quality_floor); cfmesh
    # declared its bars, published them in the manifest, and nothing consumed them.
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    return gate_declared_criteria("cfmesh", ctx.manifest_or_load())


FLOW_GATES: tuple[GateSpec, ...] = (
    GateSpec(key="manifest_valid", check=_gate_manifest_valid, section="MANIFEST",
             proves="The mesh is structurally sound - no negative-volume, open or mis-oriented cells"),
    GateSpec(key="patch_contract", check=_gate_patch_contract, section="GROUPS",
             proves="Every boundary you named exists in the mesh, and carries real faces"),
    GateSpec(key="boundary_types", check=_gate_boundary_types, section="GROUPS",
             proves="Each boundary is typed as the solver needs it (wall / symmetry / empty)"),
    GateSpec(key="quality_floor",  check=_gate_quality_floor,       section="MESH",
             proves="The mesh clears every quality bar cfMesh requires - no fatal topology defects"),
)
