# Responsibility: Implement the blocking gates a conjugate (coolant + metal) mesh must pass before review.
# Boundaries: each gate states what it proves in the user's words; every number comes from the manifest finalize wrote.
from __future__ import annotations

import json
import re
from pathlib import Path

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION
from meshpipeline.engines.gates import GateCtx, GateSpec, refuse

#: `regions ( fluid ( fluid ) solid ( solid ) );` - the OpenFOAM multi-region declaration.
_REGIONS_BLOCK = re.compile(r"\bregions\s*\((.*?)\)\s*;", re.S)
_REGION_NAME = re.compile(r"\(([^()]*)\)")

#: The two regions a conjugate case is, and nothing else. A split that produced a third is a
#: refusal inside the mesher, not something that reaches a gate.
DECLARED = (FLUID_REGION, SOLID_REGION)


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


def meshed_regions(ws: Path) -> list[str]:
    """The regions that actually have a mesh on disk, by their own polyMesh.

    `constant/<name>/polyMesh/owner` and not the directory: splitMeshRegions makes the directory
    before it writes into it, so a run killed between the two leaves a region that exists and
    holds nothing.
    """
    base = ws / "constant"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir()
                  if d.is_dir() and (d / "polyMesh" / "owner").is_file()
                  and d.name != "polyMesh")


def _gate_manifest_valid(ctx: GateCtx) -> tuple[bool, str]:
    ws = Path(ctx.workspace)
    mp = ws / "mesh_manifest.json"
    if not mp.exists() or mp.stat().st_size == 0:
        return False, ("[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing or empty - the "
                       "conjugate build did not complete")
    try:
        manifest = json.loads(mp.read_text())
    except json.JSONDecodeError as e:
        return False, f"[MANIFEST_VALIDATION_FAILED] mesh_manifest.json is malformed JSON: {e}"
    for key in ("schema_version", "geometry", "patches", "validation"):
        if key not in manifest:
            return False, ("[MANIFEST_VALIDATION_FAILED] mesh_manifest.json missing required "
                           f"key: {key}")
    names = region_properties_names(ws)
    if not names:
        return False, refuse(
            "[MANIFEST_VALIDATION_FAILED] constant/regionProperties is missing or declares no "
            "regions - the case is not a coupled case, whatever else is in it.",
            FailureCause.REGION_SPLIT)
    present = meshed_regions(ws)
    if sorted(names) != present:
        return False, refuse(
            "[MANIFEST_VALIDATION_FAILED] constant/regionProperties does not match the delivered "
            f"region meshes - declared {sorted(names)}, meshed {present}.",
            FailureCause.REGION_SPLIT)
    q = manifest.get("quality") or {}
    if q.get("fatal"):
        return False, refuse(f"[MANIFEST_VALIDATION_FAILED] fatal mesh defects: {q['fatal']}",
                             FailureCause.MESH_QUALITY, fatal=list(q["fatal"]))
    n = int(manifest.get("cell_count") or 0)
    if n and n > polcfg.CELL_HARD_LIMIT:
        return False, refuse(
            f"mesh_manifest.json: total cell_count={n:,} exceeds the compute budget "
            f"({polcfg.CELL_HARD_LIMIT:,}).", FailureCause.CELL_BUDGET,
            cells=n, limit=int(polcfg.CELL_HARD_LIMIT))
    return True, ""


def _gate_quality_floor(ctx: GateCtx) -> tuple[bool, str]:
    # This engine's own declared criteria (engines/cfmesh_conjugate/criteria.py); no new bars here.
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    return gate_declared_criteria("cfmesh_conjugate", ctx.manifest_or_load())


def _gate_regions(ctx: GateCtx) -> tuple[bool, str]:
    q = _q(ctx)
    missing = q.get("regions_missing") or []
    if missing:
        return False, refuse(
            f"[REGION_SPLIT_FAILED] the case has no {missing} region. A conjugate mesh is the "
            "coolant and the metal, each as its own mesh; one of them is not there.",
            FailureCause.REGION_SPLIT)
    undeclared = q.get("regions_undeclared") or []
    if undeclared:
        return False, refuse(
            f"[REGION_SPLIT_FAILED] the split produced {undeclared} as well as the coolant and "
            "the metal. More than two regions means the coolant was cut into disconnected pieces "
            "or the metal was, and neither is the part that was sent.",
            FailureCause.REGION_SPLIT)
    rows = q.get("regions") or []
    empty = [r.get("name") for r in rows if not int(r.get("cells") or 0) > 0]
    if empty:
        return False, refuse(f"[REGION_SPLIT_FAILED] the {empty} region holds no cells.",
                             FailureCause.REGION_SPLIT)
    return True, ""


def _gate_interface(ctx: GateCtx) -> tuple[bool, str]:
    """The coupling, which is the whole point of a conjugate mesh.

    BOTH SIDES MUST BE THE SAME NUMBER. The split cuts along faces that already existed in one
    mesh, so the coolant's side of the interface and the metal's side are the same surface -
    measured at 93,600 faces each on the unit cell. If they differ, the solver has nowhere to map
    the heat across and the two regions are solved as if they never touched.
    """
    q = _q(ctx)
    fluid = int(q.get("interface_faces_fluid") or 0)
    solid = int(q.get("interface_faces_solid") or 0)
    if fluid > 0 and fluid == solid:
        return True, ""
    if fluid == 0 and solid == 0:
        return False, refuse(
            "[INTERFACE_NON_CONFORMAL] the coolant and the metal share no faces at all: there is "
            "no surface for heat to cross, so nothing couples them.", FailureCause.REGION_SPLIT)
    return False, refuse(
        f"[INTERFACE_NON_CONFORMAL] the coolant's side of the interface has {fluid:,} faces and "
        f"the metal's has {solid:,}. They are the same surface and must carry the same faces.",
        FailureCause.REGION_SPLIT)


def _gate_coolant_volume(ctx: GateCtx) -> tuple[bool, str]:
    """The meshed coolant against the CAD it was cut out of.

    THIS IS THE GATE THIS ENGINE STANDS ON. The mesh is cut out of a block by tagging cells, so
    the one way it can be wrong without being broken is tagging the wrong ones - and a mesh that
    tagged the wrong cells is a perfectly valid mesh of the wrong part. checkMesh passes it.
    `splitMeshRegions` passes it. Measured on purpose: the unit cell meshed with its coolant
    surface wound inside-out came out 95.16% wrong by volume with checkMesh reporting "Mesh OK".

    The bar is the staircase error for the resolution used, with a margin (plan._tolerance),
    because a cut-cell mesh genuinely does not enclose the CAD's volume exactly and the expected
    disagreement is measured (engines/staircase_error). What is not expected is a part-sized
    disagreement.
    """
    q = _q(ctx)
    expected = float(q.get("coolant_expected_m3") or 0.0)
    if expected <= 0.0:
        return False, refuse(
            "[FIDELITY] the case carries no coolant volume from the CAD to compare the mesh "
            "against, so nothing proves the mesh enclosed the coolant rather than the metal.",
            FailureCause.ENGINE_CRASHED)
    err = q.get("coolant_error_pct")
    tol = float(q.get("coolant_tol_pct") or 0.0)
    if err is None:
        return False, refuse("[FIDELITY] the coolant's meshed volume was not measured.",
                             FailureCause.ENGINE_CRASHED)
    if float(err) <= tol:
        return True, ""
    meshed = float(q.get("coolant_volume_m3") or 0.0)
    return False, refuse(
        f"[FIDELITY] the meshed coolant is {meshed * 1e9:.5g} mm3 against the CAD's "
        f"{expected * 1e9:.5g} mm3, which is {float(err):.2f}% out where this resolution allows "
        f"{tol:.2f}%. A mesh this far from the part's own coolant has taken in metal or left out "
        "passage, and it is a valid mesh of something that was not sent.",
        FailureCause.MESH_QUALITY)


def _gate_coolant_pieces(ctx: GateCtx) -> tuple[bool, str]:
    """As many pieces of coolant as the part has passages.

    A SECOND, INDEPENDENT READING of the same question the volume gate asks, and it catches what
    the volume cannot: a mesh that found the right total volume in the wrong number of places. One
    network meshed as two disconnected halves has lost the slot that joins them, and the solver
    will run on it and report no flow between the levels.
    """
    q = _q(ctx)
    want = int(q.get("coolant_pieces_expected") or 0)
    got = int(q.get("coolant_pieces") or 0)
    if want <= 0 or got == want:
        return True, ""
    return False, refuse(
        f"[REGION_SPLIT_FAILED] the coolant came out as {got} separate piece(s) where the part's "
        f"own geometry has {want}. "
        + ("Passages that should join have been cut apart by the mesh."
           if got > want else "Passages that are separate in the part have been merged."),
        FailureCause.REGION_SPLIT)


def _gate_named_patches(ctx: GateCtx) -> tuple[bool, str]:
    """Every named boundary has faces in the region that owns it.

    READ PER REGION, never off constant/polyMesh. The pre-split mesh survives the split at the
    root with every patch carrying the two regions' faces added together - ymin has 532 at the
    root, which is the coolant's 128 plus the metal's 404 - so a check there sees every patch
    populated even when the coolant never reaches the face the customer named as its inlet.
    """
    blank = _q(ctx).get("empty_patches") or []
    if not blank:
        return True, ""
    return False, refuse(
        f"[CONTRACT_MISMATCH] {blank} came out of the split with no faces. The name is there and "
        "there is no surface behind it, so nothing can be applied to it: the coolant does not "
        "reach the face it was named on.", FailureCause.CONTRACT_MISMATCH)


def _gate_patch_contract(ctx: GateCtx) -> tuple[bool, str]:
    """The boundaries the user named, delivered under those names.

    THE BLOCK'S OWN FACES ARE NOT PART OF IT, and this is the one place in the row where that had
    to be decided. A conjugate mesh is cut out of the part's bounding box, so the metal comes back
    with six outer faces whatever the user asked for - they are the outside of their plate, and
    they are real boundaries that a solve will put ambient convection on. What the user names at
    intake is the PORTS: where the coolant goes in and comes out. Measured on the manifold plate:
    the contract was coolant_in and coolant_out, the mesh delivered those two plus xmin, xmax,
    zmin and zmax, and the contract gate refused a mesh that had done exactly what was asked.

    A face the user named carries THEIR name, because renameBoundary put it there before the
    split (cfmesh_conjugate/conjugate.render_meshdict). A face still called xmin is one the engine
    named, and it is engine-owned in the same way the fluid_to_solid interface is.
    """
    from meshpipeline.engines.cfmesh_conjugate.conjugate import BLOCK_FACES
    from meshpipeline.engines.contract import check_contract, contract_applicable
    if not contract_applicable(ctx.intake_patches):
        return True, ""
    manifest = ctx.manifest_or_load()
    delivered = manifest.get("patch_types", {}) if isinstance(manifest, dict) else {}
    delivered = {k: v for k, v in delivered.items() if isinstance(k, str) and isinstance(v, str)}
    delivered = {k: v for k, v in delivered.items() if k not in set(BLOCK_FACES)}
    ok, diag = check_contract(intake_patches=ctx.intake_patches,
                              manifest_patches=list(delivered.keys()),
                              manifest_patch_types=delivered)
    if ok:
        return True, ""
    from meshpipeline.engines.gates import facts_of
    return False, refuse(str(diag), FailureCause.CONTRACT_MISMATCH, **facts_of(diag))


CONJUGATE_GATES: tuple[GateSpec, ...] = (
    GateSpec(key="manifest_valid", check=_gate_manifest_valid, section="MANIFEST",
             proves="The coupled case is sound - both regions declared and meshed, no fatal "
                    "defects, inside the cell budget",
             cause=FailureCause.ENGINE_CRASHED),
    GateSpec(key="quality_floor", check=_gate_quality_floor, section="MESH",
             proves="Both the coolant and the metal clear the quality bars this engine requires",
             cause=FailureCause.MESH_QUALITY),
    GateSpec(key="coolant_volume", check=_gate_coolant_volume, section="GEOMETRY",
             proves="The meshed coolant is the coolant in your CAD, to the accuracy this "
                    "resolution buys",
             cause=FailureCause.MESH_QUALITY),
    GateSpec(key="coolant_pieces", check=_gate_coolant_pieces, section="GEOMETRY",
             proves="The coolant is in as many connected pieces as your part has passages - "
                    "nothing joined that should not be, nothing cut that should not be",
             cause=FailureCause.REGION_SPLIT),
    GateSpec(key="regions_split", check=_gate_regions, section="MESH",
             proves="The coolant and the metal each have their own mesh, and there is no third "
                    "region",
             cause=FailureCause.REGION_SPLIT),
    GateSpec(key="interface", check=_gate_interface, section="GEOMETRY",
             proves="Heat can cross between the coolant and the metal - the two sides of their "
                    "shared surface carry the same faces",
             cause=FailureCause.REGION_SPLIT),
    GateSpec(key="named_patches", check=_gate_named_patches, section="GROUPS",
             proves="Every boundary you named has a surface behind it in the region that owns it",
             cause=FailureCause.CONTRACT_MISMATCH),
    GateSpec(key="patch_contract", check=_gate_patch_contract, section="GROUPS",
             proves="The delivered mesh carries exactly the boundaries you approved at intake",
             cause=FailureCause.CONTRACT_MISMATCH),
)

__all__ = ["CONJUGATE_GATES", "DECLARED", "meshed_regions", "region_properties_names"]
