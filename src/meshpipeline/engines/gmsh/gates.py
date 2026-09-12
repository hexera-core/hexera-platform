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

#: Minimum elements that must span the NARROWEST bounding-box dimension. gmsh sizes
#: from a factor of the part DIAGONAL, so a long thin duct (diagonal = length) can end
#: up with an element bigger than its bore - a handful of cells across the flow that
#: still passes every shape-quality gate. The baseline corpus shipped 14 such "passes"
#: at 0.3-5% of budget (transition_006_fluid at 6,455 cells, s_duct_001_fluid 132x
#: coarser than its solid twin) - silent under-resolution and training-label poison.
#: 6 is the floor; the driver aims for 8 with margin.
RESOLUTION_FLOOR_CELLS = 6

#: The floor for a FLUID DOMAIN measured where it matters: cells across the local passage at
#: the narrowest wall (5th percentile over the boundary points). The same bar VMTK fills to
#: (engines/vmtk/criteria.py PASSAGE_MIN_CELLS_ACROSS); industry practice is 20-40.
PASSAGE_FLOOR_CELLS = 12


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


def _gate_resolution_floor(ctx: GateCtx) -> tuple[bool, str]:
    """The mesh must resolve the NARROWEST dimension of the part, not just have
    well-shaped cells. Reads the element size and the bbox the driver already
    recorded, and rejects a mesh whose largest element spans the thin direction
    in fewer than RESOLUTION_FLOOR_CELLS cells. Self-contained: needs only fields
    quality.json already carries (size_h, bounds), so no mesh-image change is
    required for it to take effect."""
    q = (ctx.manifest_or_load().get("quality") or {})
    # THE MEASURED PASSAGE GATES when the driver could measure it: an under-resolved branch
    # or scroll cannot hide behind a well-resolved main run or a wide bounding box.
    local = q.get("passage_cells_across_local") or {}
    p05 = local.get("p05")
    if p05 is not None and float(p05) < PASSAGE_FLOOR_CELLS:
        return False, (
            f"[RESOLUTION] undermeshed: {float(p05):g} cells across the passage at the "
            f"narrowest wall (5th percentile; median {local.get('median')}) - a CFD mesh needs "
            f"at least {PASSAGE_FLOOR_CELLS} everywhere (industry practice is 20-40). Fix in "
            "gmsh_spec.json: lower size.value (the passage field only tightens gmsh's own "
            "size, it cannot refine past size.mode='absolute' values that are too large), and "
            "run_mesh again."
        )
    h = q.get("size_h")
    bounds = q.get("bounds")
    # An older deck without these fields is not judged here (the manifest/sicn
    # gates still apply); a floor cannot be enforced on data that is not present.
    if not h or not bounds or len(bounds) != 6:
        return True, ""
    extents = [bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]]
    min_ext = min(e for e in extents if e > 0) if any(e > 0 for e in extents) else 0.0
    if min_ext <= 0 or float(h) <= 0:
        return True, ""
    cells_across = min_ext / float(h)
    # prefer the driver's own count when present (it knows the meshed extent exactly, and
    # whether the limiting width is the box or a declared port the flow must cross)
    cells_across = float(q.get("cells_across_min", cells_across))
    basis = str(q.get("min_extent_basis", "bbox"))
    if basis.startswith("port:"):
        min_ext = float(q.get("min_extent", min_ext))
        what = f"the flow width of its declared port {basis[5:]}"
    else:
        what = "the part's narrowest dimension"
    if cells_across < RESOLUTION_FLOOR_CELLS:
        return False, (
            f"[RESOLUTION] the mesh spans {what} "
            f"({min_ext * 1000:.1f} mm) in only ~{cells_across:.1f} elements "
            f"(element size {float(h) * 1000:.1f} mm) - below the "
            f"{RESOLUTION_FLOOR_CELLS}-cell floor, so the flow cross-section is "
            "under-resolved even though the cells are well-shaped. Fix in "
            "gmsh_spec.json: set size.mode='absolute' with a value near "
            f"{min_ext / (RESOLUTION_FLOOR_CELLS + 2) * 1000:.1f} mm (or smaller), "
            "and run_mesh again. gmsh's default factor-of-diagonal sizing under-"
            "resolves long thin parts because the diagonal is the length, not the bore."
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
    # Well-shaped is not the same as adequately resolved: this catches a mesh whose
    # cells are clean but too big to resolve the flow cross-section (the corpus's
    # silent under-spend). After sicn (a degenerate mesh is the worse news) and
    # before naming (an under-resolved mesh is not worth patch-checking).
    GateSpec(key="resolution_floor", check=_gate_resolution_floor,   section="MESH",
             proves="The narrowest dimension of the part is resolved in enough cells to carry the flow"),
    GateSpec(key="patch_contract", check=_gate_gmsh_region_contract, section="GROUPS",
             proves="Every named group you asked for exists in the deck"),
)
