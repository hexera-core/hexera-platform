# Responsibility: Implement the blocking gates a multi-region snappyHexMesh run must pass before review.
# Boundaries: each gate states what it proves in the user's words.
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.gates import GateCtx, GateSpec

logger = logging.getLogger(__name__)


def _q(ctx: GateCtx) -> dict:
    return (ctx.manifest_or_load().get("quality") or {})


# `regions ( fluid ( fluid ) solid ( solid ) );` - the OpenFOAM multi-region declaration.
_REGIONS_BLOCK = re.compile(r"\bregions\s*\((.*?)\)\s*;", re.S)
_REGION_NAME = re.compile(r"\(([^()]*)\)")


def region_properties_names(ws: Path) -> list[str] | None:
    p = ws / "constant" / "regionProperties"
    if not p.exists():
        return None
    block = _REGIONS_BLOCK.search(p.read_text(errors="replace"))
    if not block:
        return None
    return [n for group in _REGION_NAME.findall(block.group(1)) for n in group.split()]


def _region_properties_declares_regions(ws: Path) -> tuple[bool, str]:
    names = region_properties_names(ws)
    if names is None:
        return False, ("[MANIFEST_VALIDATION_FAILED] constant/regionProperties is missing or does "
                       "not declare a `regions ( ... );` entry - the region split did not complete; "
                       "the case is not a multi-region case yet. Re-run run_mesh.")
    if not names:
        return False, ("[MANIFEST_VALIDATION_FAILED] constant/regionProperties declares NO regions - "
                       "a multi-region case must name the regions a coupled solver is to solve. "
                       "The split produced no usable declaration; re-run run_mesh.")
    present = sorted(d.name for d in (ws / "constant").iterdir()
                     if d.is_dir() and (d / "polyMesh" / "owner").exists())
    undeclared_mesh = [n for n in present if n not in names]
    unmeshed_decl = [n for n in names if n not in present]
    if unmeshed_decl or undeclared_mesh:
        return False, (
            "[MANIFEST_VALIDATION_FAILED] constant/regionProperties does not match the delivered "
            f"region meshes - declared {sorted(names)}, meshed {present}"
            + (f"; declared but never meshed: {unmeshed_decl}" if unmeshed_decl else "")
            + (f"; meshed but never declared: {undeclared_mesh}" if undeclared_mesh else "")
            + ". A coupled solver would trip over the difference; re-run run_mesh.")
    return True, ""


def _gate_cht_manifest_valid(ctx: GateCtx) -> tuple[bool, str]:
    ws = Path(ctx.workspace)
    mp = ws / "mesh_manifest.json"
    if not mp.exists() or mp.stat().st_size == 0:
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing or empty - "
                       "run_mesh did not complete the multi-region build")
    try:
        manifest = json.loads(mp.read_text())
    except json.JSONDecodeError as e:
        return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json is malformed JSON: {e}"
    for key in ("schema_version", "geometry", "patches", "validation"):
        if key not in manifest:
            return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing required key: {key}"
    ok, why = _region_properties_declares_regions(ws)
    if not ok:
        return False, why
    q = manifest.get("quality", {}) or {}
    if q.get("fatal"):
        return False, (f"[MANIFEST_VALIDATION_FAILED] fatal mesh defects across regions: {q['fatal']} - "
                       "set quality='strict' (or coarsen the offending region) and run_mesh again")
    n = manifest.get("cell_count", 0) or 0
    if n and n > polcfg.CELL_HARD_LIMIT:
        return False, (f"mesh_manifest.json: total cell_count={n:,} exceeds the compute budget "
                       f"({polcfg.CELL_HARD_LIMIT:,}) - the multi-region mesh is TOO FINE. Lower "
                       "max_cells / surface_level and run_mesh again.")
    return True, ""


def _gate_regions_split(ctx: GateCtx) -> tuple[bool, str]:
    q = _q(ctx)
    missing = q.get("regions_missing") or []
    if missing:
        return False, (f"[REGION_SPLIT_FAILED] declared region(s) {missing} did not survive "
                       "splitMeshRegions - the enclosing surface leaked or two zones merged. "
                       "Check the region's solid assignment and raise its surface_level so the "
                       "zone seals, then run_mesh again. Do NOT raise max_cells for this.")
    regions = q.get("regions") or []
    if not regions:
        return False, ("[REGION_SPLIT_FAILED] no regions recorded after the split - "
                       "splitMeshRegions produced no per-region meshes; run_mesh again")
    empty = [r.get("name") for r in regions if not (r.get("cells") or 0) > 0]
    if empty:
        return False, (f"[REGION_SPLIT_FAILED] region(s) {empty} split to ZERO cells - "
                       "their cellZone captured no cells (leaked or fully overlapped). Fix the "
                       "region assignment / surface sealing and run_mesh again.")
    # POLICY: no undeclared region may ship. A live delivery included an undeclared 740k-cell
    # domain0 (unzoned background) whose coupled domain0_to_air patch regionProperties never
    # listed - a downstream CHT solve would trip over it. Declared plans reconcile EXACTLY.
    undeclared = q.get("regions_undeclared") or []
    if undeclared:
        return False, (f"[REGION_SPLIT_FAILED] undeclared region(s) {undeclared} appeared in "
                       "the split output - cells fell outside every declared region (typically "
                       "unzoned background). The delivered case must contain exactly the "
                       "declared regions. Re-run the mesh; if it persists, the fluid region's "
                       "surface is leaking (raise its surface_level).")

    # THE DELIVERED CASE, read back. Everything above reconciles against the PLAN the runner
    # compiled (`.regions.json`), which only proves the plan is self-consistent. The solver reads
    # `constant/regionProperties` and the region directories, so those are what must agree - and
    # a region counts only when its polyMesh is COMPLETE and non-empty. `owner` alone is what a
    # partial splitMeshRegions leaves behind.
    from meshpipeline.engines.snappy_multiregion import regions as _regions
    try:
        delivered = _regions.inventory(ctx.workspace)
    except _regions.RegionPropertiesError as exc:
        return False, (f"[REGION_SPLIT_FAILED] {exc}. The delivered case must carry a readable "
                       "constant/regionProperties naming every region it ships.")
    problem = delivered.problem()
    if problem:
        return False, (f"[REGION_SPLIT_FAILED] {problem}. constant/regionProperties and the "
                       "delivered region meshes must describe the same case - a solver reads "
                       "both and trips on any disagreement.")
    return True, ""


def _gate_interfaces_conformal(ctx: GateCtx) -> tuple[bool, str]:
    q = _q(ctx)
    if q.get("interface_ok") is True:
        return True, ""
    bad = q.get("interface_mismatch") or []
    if bad:
        return False, (f"[INTERFACE_NON_CONFORMAL] the fluid-solid interface(s) {bad} do not "
                       "match across their coupled patches (unequal face counts, or one side "
                       "missing). The regions must share CONFORMAL faces; check the assembly's "
                       "solids actually touch there, then run_mesh again.")
    return False, ("[INTERFACE_NON_CONFORMAL] no conformal fluid-solid interface was created - "
                   "the fluid and solid regions do not share faces. A coupled case needs the "
                   "regions to touch; verify the assembly and run_mesh again.")


def _gate_multiregion_patch_contract(ctx: GateCtx) -> tuple[bool, str]:
    from meshpipeline.engines.contract import check_contract, contract_applicable
    if not contract_applicable(ctx.intake_patches):
        return True, ""   # no user boundary contract to enforce (direct dispatch)
    manifest = ctx.manifest_or_load()
    delivered = manifest.get("patch_types", {}) if isinstance(manifest, dict) else {}
    delivered = {k: v for k, v in delivered.items() if isinstance(k, str) and isinstance(v, str)}
    ok, diag = check_contract(
        intake_patches=ctx.intake_patches,
        manifest_patches=list(delivered.keys()),   # the user-boundary names finalize delivered
        manifest_patch_types=delivered,
    )
    return ok, ("" if ok else diag)


def _gate_cht_region_contract(ctx: GateCtx) -> tuple[bool, str]:
    q = _q(ctx)
    declared = ctx.engine_params.get("_regions") if isinstance(ctx.engine_params, dict) else None
    regions = q.get("regions") or []
    if not declared or not regions:
        return True, ""   # nothing to compare against (direct dispatch) - skip, other gates cover validity
    want = {str(r.get("name")): str(r.get("type")) for r in declared if isinstance(r, dict)}
    got = {str(r.get("name")): str(r.get("type")) for r in regions}
    missing = sorted(set(want) - set(got))
    mistyped = sorted(n for n in set(want) & set(got) if want[n] != got[n])
    if missing:
        return False, (f"[REGION_CONTRACT_MISMATCH] declared region(s) {missing} are absent from the "
                       "delivered case - every region the user declared must be meshed.")
    if mistyped:
        detail = "; ".join(f"{n}: declared {want[n]!r}, delivered {got[n]!r}" for n in mistyped)
        return False, f"[REGION_CONTRACT_MISMATCH] region role mismatch - {detail}."
    return True, ""


def _gate_quality_floor(ctx: GateCtx) -> tuple[bool, str]:
    # This engine's OWN declared gating criteria, enforced before the reviewer can see the mesh.
    # The bars and thresholds are the engine's (engines/snappy_multiregion/criteria.py); this gate adds none.
    # gmsh and vmtk have always had the equivalent boundary (sicn_floor / quality_floor); snappy_multiregion
    # declared its bars, published them in the manifest, and nothing consumed them.
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    return gate_declared_criteria("snappy_multiregion", ctx.manifest_or_load())


MULTIREGION_GATES: tuple[GateSpec, ...] = (
    GateSpec(key="manifest_valid",   check=_gate_cht_manifest_valid,     section="MANIFEST",
             proves="The multi-region case is sound - no fatal defects in any region"),
    GateSpec(key="patch_contract",   check=_gate_multiregion_patch_contract, section="GROUPS",
             proves="The delivered mesh carries exactly the boundaries you approved at intake - "
                    "none merged, renamed, dropped, or re-roled"),
    GateSpec(key="regions_split",    check=_gate_regions_split,          section="MESH",
             proves="Every region you declared was meshed - and no undeclared region was invented"),
    GateSpec(key="interfaces",       check=_gate_interfaces_conformal,   section="GEOMETRY",
             proves="The fluid-solid interfaces are conformal - faces match one-to-one across them"),
    GateSpec(key="region_contract",  check=_gate_cht_region_contract,    section="GROUPS",
             proves="Each region carries the boundaries you named for it"),
    GateSpec(key="quality_floor",  check=_gate_quality_floor,       section="MESH",
             proves="Every region clears the quality bars this engine requires - skewness is localized across all regions"),
)
