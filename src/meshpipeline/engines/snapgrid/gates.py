# Responsibility: Implement the blocking gates a snap-grid (placed-parts) multi-region mesh must pass before review.
# Boundaries: each gate states what it proves in the user's words; the numbers come from the manifest finalize wrote.
from __future__ import annotations

import json
import re
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines.gates import GateCtx, GateSpec, refuse
from meshpipeline.engines.snapgrid.runner import meshed_regions

# `regions ( fluid ( air ) solid ( a b ) );` - the OpenFOAM multi-region declaration.
_REGIONS_BLOCK = re.compile(r"\bregions\s*\((.*?)\)\s*;", re.S)
_REGION_NAME = re.compile(r"\(([^()]*)\)")


def _q(ctx: GateCtx) -> dict:
    return (ctx.manifest_or_load().get("quality") or {})


def region_properties_names(ws: Path) -> list[str] | None:
    p = ws / "constant" / "regionProperties"
    if not p.exists():
        return None
    block = _REGIONS_BLOCK.search(p.read_text(errors="replace"))
    if not block:
        return None
    return [n for group in _REGION_NAME.findall(block.group(1)) for n in group.split()]


def _gate_manifest_valid(ctx: GateCtx) -> tuple[bool, str]:
    ws = Path(ctx.workspace)
    mp = ws / "mesh_manifest.json"
    if not mp.exists() or mp.stat().st_size == 0:
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing or empty - the "
                       "snap-grid build did not complete")
    try:
        manifest = json.loads(mp.read_text())
    except json.JSONDecodeError as e:
        return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json is malformed JSON: {e}"
    for key in ("schema_version", "geometry", "patches", "validation"):
        if key not in manifest:
            return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing required key: {key}"
    names = region_properties_names(ws)
    if not names:
        return False, refuse(
            "[MANIFEST_VALIDATION_FAILED] constant/regionProperties is missing or declares no "
            "regions - the case is not a multi-region case.", FailureCause.REGION_SPLIT)
    present = meshed_regions(ws)
    if sorted(names) != present:
        return False, refuse(
            "[MANIFEST_VALIDATION_FAILED] constant/regionProperties does not match the delivered "
            f"region meshes - declared {sorted(names)}, meshed {present}.",
            FailureCause.REGION_SPLIT)
    q = manifest.get("quality", {}) or {}
    if q.get("fatal"):
        return False, refuse(
            f"[MANIFEST_VALIDATION_FAILED] fatal mesh defects: {q['fatal']}",
            FailureCause.MESH_QUALITY, fatal=list(q["fatal"]))
    n = manifest.get("cell_count", 0) or 0
    if n and n > polcfg.CELL_HARD_LIMIT:
        return False, refuse(
            f"mesh_manifest.json: total cell_count={n:,} exceeds the compute budget "
            f"({polcfg.CELL_HARD_LIMIT:,}).",
            FailureCause.CELL_BUDGET, cells=int(n), limit=int(polcfg.CELL_HARD_LIMIT))
    return True, ""


def _gate_quality_floor(ctx: GateCtx) -> tuple[bool, str]:
    # This engine's own declared gating criteria (engines/snapgrid/criteria.py); no new bars here.
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    return gate_declared_criteria("snapgrid", ctx.manifest_or_load())


def _gate_patch_contract(ctx: GateCtx) -> tuple[bool, str]:
    from meshpipeline.engines.contract import check_contract, contract_applicable
    if not contract_applicable(ctx.intake_patches):
        return True, ""   # the file names its own openings; no user contract to hold them to
    manifest = ctx.manifest_or_load()
    delivered = manifest.get("patch_types", {}) if isinstance(manifest, dict) else {}
    delivered = {k: v for k, v in delivered.items() if isinstance(k, str) and isinstance(v, str)}
    ok, diag = check_contract(intake_patches=ctx.intake_patches,
                              manifest_patches=list(delivered.keys()),
                              manifest_patch_types=delivered)
    if ok:
        return True, ""
    # the names come from the file, so a rebuild writes the same ones: no retry can fix this
    from meshpipeline.engines.gates import facts_of
    return False, refuse(str(diag), FailureCause.CONTRACT_MISMATCH,
                         **{**facts_of(diag), "retry_may_fix": False})


def _gate_regions(ctx: GateCtx) -> tuple[bool, str]:
    q = _q(ctx)
    missing = q.get("regions_missing") or []
    if missing:
        return False, refuse(f"[REGION_SPLIT_FAILED] region(s) {missing} of the file have no mesh.",
                             FailureCause.REGION_SPLIT)
    regions = q.get("regions") or []
    if not regions:
        return False, refuse("[REGION_SPLIT_FAILED] no regions recorded for the mesh.",
                             FailureCause.REGION_SPLIT)
    empty = [r.get("name") for r in regions if not (r.get("cells") or 0) > 0]
    if empty:
        return False, refuse(f"[REGION_SPLIT_FAILED] region(s) {empty} hold no cells.",
                             FailureCause.REGION_SPLIT)
    undeclared = q.get("regions_undeclared") or []
    if undeclared:
        return False, refuse(f"[REGION_SPLIT_FAILED] region(s) {undeclared} were meshed but the "
                             "file's model never declared them.", FailureCause.REGION_SPLIT)
    return True, ""


def _gate_interfaces(ctx: GateCtx) -> tuple[bool, str]:
    q = _q(ctx)
    if q.get("interface_ok") is True:
        return True, ""
    bad = q.get("interface_mismatch") or []
    return False, refuse(
        "[INTERFACE_NON_CONFORMAL] "
        + (f"the interface(s) {bad} do not carry the grid's face count on both sides."
           if bad else "no interface between regions was written."), FailureCause.REGION_SPLIT)


def _gate_file_fidelity(ctx: GateCtx) -> tuple[bool, str]:
    # THE FILE'S MODEL, checked by the mesher before anything was written (box volumes exact to
    # round-off, the domain filled, every active object placed, contacts neither invented nor
    # lost). The mesher refuses to write a mesh that fails them; this gate holds the delivered
    # case to the record of that check, so a case without it is never reviewed as one.
    q = _q(ctx)
    if q.get("file_checked") is True:
        return True, ""
    return False, refuse("[FIDELITY] the delivered case carries no record that the mesh was "
                         "checked against the file's model (snapgrid_report.json).",
                         FailureCause.ENGINE_CRASHED)


SNAPGRID_GATES: tuple[GateSpec, ...] = (
    GateSpec(key="manifest_valid", check=_gate_manifest_valid, section="MANIFEST",
             proves="The multi-region case is sound - every region declared and meshed, no fatal "
                    "defects",
             cause=FailureCause.ENGINE_CRASHED),
    GateSpec(key="quality_floor", check=_gate_quality_floor, section="MESH",
             proves="Every region clears the quality bars this engine requires",
             cause=FailureCause.MESH_QUALITY),
    GateSpec(key="file_fidelity", check=_gate_file_fidelity, section="GEOMETRY",
             proves="The mesh is the file's model - every part where the file puts it, box "
                    "volumes exact, contacts as the file makes them",
             cause=FailureCause.ENGINE_CRASHED),
    GateSpec(key="patch_contract", check=_gate_patch_contract, section="GROUPS",
             proves="The delivered mesh carries exactly the boundaries you approved at intake",
             cause=FailureCause.CONTRACT_MISMATCH),
    GateSpec(key="regions_split", check=_gate_regions, section="MESH",
             proves="Every part and air space of the file has its own mesh - and no other region "
                    "was invented",
             cause=FailureCause.REGION_SPLIT),
    GateSpec(key="interfaces", check=_gate_interfaces, section="GEOMETRY",
             proves="The interfaces between parts are conformal - faces match one-to-one",
             cause=FailureCause.REGION_SPLIT),
)
