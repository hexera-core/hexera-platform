# Responsibility: Implement the blocking gates a Gmsh run must pass before review.
# Boundaries: each gate states what it proves in the user's words.
from __future__ import annotations

import json
import logging
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.gates import GateCtx, GateSpec
from meshpipeline.engines.gmsh.gmsh_runner import SICN_FLOOR

logger = logging.getLogger(__name__)


def _gate_gmsh_manifest_valid(ctx: GateCtx) -> tuple[bool, str]:
    ws = Path(ctx.workspace)
    mp = ws / "mesh_manifest.json"
    if not mp.exists() or mp.stat().st_size == 0:
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing "
                       "or empty - Builder did not write manifest")
    try:
        manifest = json.loads(mp.read_text())
    except json.JSONDecodeError as e:
        return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json is malformed JSON: {e}"
    for key in ["schema_version", "geometry", "patches", "validation"]:
        if key not in manifest:
            return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing required key: {key}"
    if not (ws / "mesh.inp").exists():
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh.inp missing - the FEA "
                       "deck (the deliverable) was not written; run_mesh again")
    q = manifest.get("quality", {}) or {}
    if q.get("fatal"):
        return False, (f"[MANIFEST_VALIDATION_FAILED] fatal mesh defects: {q['fatal']} - "
                       "coarsen or repair the geometry approach and run_mesh again")
    _n = manifest.get("cell_count", 0)
    if _n and _n > polcfg.CELL_HARD_LIMIT:
        return False, (
            f"mesh_manifest.json: cell_count={_n:,} exceeds the compute budget "
            f"({polcfg.CELL_HARD_LIMIT:,} elements) - the mesh is TOO FINE. COARSEN it: "
            "raise size.value (element size factor) in gmsh_spec.json and run_mesh again."
        )
    return True, ""


def _gate_sicn_floor(ctx: GateCtx) -> tuple[bool, str]:
    q = (ctx.manifest_or_load().get("quality") or {})
    sicn = q.get("min_sicn")
    if sicn is None:
        return False, ("[QUALITY] min_sicn missing from quality report - the mesh "
                       "was not quality-checked; run_mesh again")
    if float(sicn) < SICN_FLOOR:
        return False, (
            f"[QUALITY] min SICN {sicn} is below the {SICN_FLOOR} floor - near-"
            "degenerate elements. Fix in gmsh_spec.json: reduce size.value near "
            "small features (or lower curvature_nodes), keep optimize=true, and "
            "consider element_order 1 to isolate whether high-order snapping is "
            "the cause; then run_mesh again."
        )
    return True, ""


def _gate_gmsh_region_contract(ctx: GateCtx) -> tuple[bool, str]:
    manifest = ctx.manifest_or_load()
    if not (manifest and ctx.intake_patches):
        return True, ""
    types = {n: r for n, r in (manifest.get("patch_types") or {}).items()
             if r != "free"}
    contracted = [p for p in ctx.intake_patches if p.get("type") != "free"]
    if not contracted:
        return True, ""
    from meshpipeline.engines.contract import check_contract
    ok, diag = check_contract(
        intake_patches=contracted,
        manifest_patches={n: (manifest.get("patches") or {}).get(n, [])
                          for n in types},
        manifest_patch_types=types,
    )
    return ok, ("" if ok else diag)


GMSH_GATES: tuple[GateSpec, ...] = (
    GateSpec(key="manifest_valid", check=_gate_gmsh_manifest_valid, section="MANIFEST",
             proves="The element deck was written and parses back - no fatal defects"),
    # Soundness BEFORE naming: run_gates stops at the first blocking failure, so with the
    # floor last a patch-name mismatch refused the run while leaving its quality unmeasured.
    # Deliverability, then is-it-sound, then is-it-what-was-asked-for.
    GateSpec(key="sicn_floor",     check=_gate_sicn_floor,          section="MESH",
             proves="No degenerate elements - every element clears the quality floor for FE assembly"),
    GateSpec(key="patch_contract", check=_gate_gmsh_region_contract, section="GROUPS",
             proves="Every named group you asked for exists in the deck"),
)
