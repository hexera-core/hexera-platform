# Responsibility: Implement the blocking gates a VMTK run must pass before review.
# Boundaries: each gate states what it proves in the user's words.
from __future__ import annotations

import json
import logging
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines.gates import GateCtx, GateSpec, refuse
from meshpipeline.engines.region_check import gate_port_areas
from meshpipeline.engines.vmtk.criteria import QUALITY_FLOOR

logger = logging.getLogger(__name__)


def _gate_vmtk_manifest_valid(ctx: GateCtx) -> tuple[bool, str]:
    ws = Path(ctx.workspace)
    mp = ws / "mesh_manifest.json"
    if not mp.exists() or mp.stat().st_size == 0:
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing or empty - "
                       "run_mesh did not complete")
    try:
        manifest = json.loads(mp.read_text())
    except json.JSONDecodeError as e:
        return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json is malformed JSON: {e}"
    for key in ("schema_version", "geometry", "patches", "validation"):
        if key not in manifest:
            return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing required key: {key}"
    if not (ws / "mesh.vtu").exists():
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh.vtu missing - the tetrahedral volume "
                       "mesh (the deliverable) was not written; run_mesh again")
    q = manifest.get("quality", {}) or {}
    if q.get("fatal"):
        return False, refuse(
            f"[MANIFEST_VALIDATION_FAILED] fatal mesh defects: {q['fatal']} - inverted or "
            "degenerate tetrahedra. Raise edge_length_factor slightly, or reduce "
            "boundary_layers if the layers are inverting, then run_mesh again",
            FailureCause.MESH_QUALITY, fatal=list(q["fatal"]))
    if not (q.get("cells") or 0) > 0:
        return False, ("[MANIFEST_VALIDATION_FAILED] the volume mesh has ZERO cells - the lumen "
                       "surface was not closed, so there was no interior to fill. Set "
                       "cap_openings=true and run_mesh again")
    n = manifest.get("cell_count", 0) or 0
    if n and n > polcfg.CELL_HARD_LIMIT:
        return False, refuse(
            f"mesh_manifest.json: cell_count={n:,} exceeds the compute budget "
            f"({polcfg.CELL_HARD_LIMIT:,}) - the mesh is TOO FINE. RAISE edge_length_factor "
            "(coarser cells relative to the local radius) and run_mesh again.",
            FailureCause.CELL_BUDGET, cells=int(n), limit=int(polcfg.CELL_HARD_LIMIT))
    return True, ""


def _gate_tet_quality_floor(ctx: GateCtx) -> tuple[bool, str]:
    q = (ctx.manifest_or_load().get("quality") or {})
    mq = q.get("min_quality")
    if mq is None:
        return False, refuse("[QUALITY] min_quality missing from the quality report - the mesh "
                             "was not quality-checked; run_mesh again",
                             FailureCause.MESH_QUALITY, unmeasured=["tetrahedron quality"])
    if float(mq) < QUALITY_FLOOR:
        return False, refuse(
            f"[QUALITY] min tet quality {mq} is below the {QUALITY_FLOOR} floor - sliver "
            "tetrahedra. In vmtk the fix is the SIZING, not a local knob: raise "
            "edge_length_factor a little (very fine cells relative to the radius produce "
            "slivers at bifurcations), and if boundary_layers are inflating into a tight "
            "bend reduce them or thin boundary_layer_thickness_factor; then run_mesh again.",
            FailureCause.MESH_QUALITY, checks=[{
                "key": "min_quality", "label": "tetrahedron quality", "measured": float(mq),
                "op": ">=", "threshold": QUALITY_FLOOR}])
    return True, ""


def _gate_vmtk_patch_contract(ctx: GateCtx) -> tuple[bool, str]:
    manifest = ctx.manifest_or_load()
    if not (manifest and ctx.intake_patches):
        return True, ""
    types = manifest.get("patch_types") or {}
    if not types:
        return True, ""   # nothing measured to compare against
    from meshpipeline.engines.contract import check_contract
    from meshpipeline.engines.gates import facts_of
    ok, diag = check_contract(
        intake_patches=ctx.intake_patches,
        manifest_patches={n: (manifest.get("patches") or {}).get(n, []) for n in types},
        manifest_patch_types=types,
    )
    if not ok:
        # the openings are bound to the declared names by staging the lumen, with the builder's
        # capping choices - a new attempt may bind them differently, so the retries stay
        return False, refuse(str(diag), FailureCause.CONTRACT_MISMATCH,
                             **{**facts_of(diag), "retry_may_fix": True})
    # ARTIFACT reconciliation: the manifest's patch_types echo the declaration (circular),
    # so also reconcile the ACTUAL .vtu boundary structure recorded by finalize - the
    # produced mesh must have a real wall and exactly the capped openings the strategy
    # promised (input had N open profiles -> output must have N caps).
    q = manifest.get("quality") or {}
    actual = q.get("actual_boundaries")
    if actual is not None:
        if "wall" not in actual:
            return False, refuse("[VTU_BOUNDARY_MISMATCH] the produced mesh.vtu has no wall "
                                 f"boundary region (actual boundaries: {actual}) - the surface "
                                 "remesh/capping lost the lumen wall; re-run the mesh.",
                                 FailureCause.PATCH_NOT_CAPTURED, patches=["wall"])
        expected_caps = q.get("expected_caps")
        # vmtk CellEntityIds: 1 = wall, real opening caps are numbered FROM 2; entity 0
        # (surfaced as cap_0) is the unclassified remainder, not an opening - counting it
        # false-rejected a known-good aorta (3 openings -> cap_2..cap_4 plus a cap_0).
        actual_caps = sum(1 for n in actual
                          if str(n).startswith("cap_") and str(n) != "cap_0")
        if expected_caps is not None and actual_caps != int(expected_caps):
            return False, refuse(
                "[VTU_BOUNDARY_MISMATCH] the produced mesh.vtu has "
                f"{actual_caps} capped opening(s) but the strategy seeded "
                f"{expected_caps} (source+target openings). An opening was lost "
                "or an extra cap appeared during remesh/capping; re-run the mesh.",
                FailureCause.PATCH_NOT_CAPTURED,
                detail=(f"the mesh has {actual_caps} capped openings where {expected_caps} "
                        "were expected"))
    return True, ""


VMTK_GATES: tuple[GateSpec, ...] = (
    GateSpec(key="manifest_valid",  check=_gate_vmtk_manifest_valid, section="MANIFEST",
             proves="The volume mesh was written and parses back - no fatal defects",
             cause=FailureCause.ENGINE_CRASHED),
    # Soundness BEFORE naming: run_gates stops at the first blocking failure, so with the
    # floor last a patch-name mismatch refused the run while leaving its quality unmeasured.
    # Deliverability, then is-it-sound, then is-it-what-was-asked-for.
    GateSpec(key="quality_floor",   check=_gate_tet_quality_floor,   section="MESH",
             proves="No inverted or degenerate tetrahedra - the mesh clears the quality floor",
             cause=FailureCause.MESH_QUALITY),
    GateSpec(key="patch_contract",  check=_gate_vmtk_patch_contract, section="GROUPS",
             proves="The wall and every inlet/outlet cap are present, and match what you declared",
             cause=FailureCause.CONTRACT_MISMATCH),
    # SHARED with every engine (engines/region_check.py): each declared inlet/outlet delivered at
    # about the size declared. finalize already holds each cap to its STAGED opening, tighter;
    # this is the same rule every engine answers to, against what the user said.
    GateSpec(key="port_areas", check=gate_port_areas, section="GROUPS",
             proves="Every inlet and outlet is the opening you declared, at its size",
             cause=FailureCause.PATCH_NOT_CAPTURED),
)
