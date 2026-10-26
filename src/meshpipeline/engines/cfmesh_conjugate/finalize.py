# Responsibility: Turn the conjugate run's report into the manifest the gates and the review read.
# Boundaries: it reports what the run measured. It meshes nothing, and it judges nothing the gates judge.
# Collaborates with: cfmesh_conjugate/native.py (which wrote the report) and cfmesh_conjugate/gates.py.
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION

logger = logging.getLogger(__name__)

#: What the split names the coupling's own two patches. They are the engine's, never the user's,
#: so they are no part of the boundary contract the user approved at intake.
INTERFACE_MARK = "_to_"

#: The two regions a conjugate case has, and what regionProperties calls each.
REGION_TYPE = {FLUID_REGION: "fluid", SOLID_REGION: "solid"}

#: An OpenFOAM patch type read back off the delivered mesh, as a role in the user's words. Only
#: the types that carry a role: a plain `patch` could be an inlet or an outlet and the mesh cannot
#: tell which, so that one is not here.
_OF_TYPE_TO_ROLE = {"wall": "wall", "symmetryplane": "symmetry", "empty": "empty"}

#: Roles the delivered mesh genuinely witnesses. For anything else - inlet, outlet - the declared
#: role is echoed, because both are written as a plain `patch` and reporting them as "patch" would
#: lose the only record of which end the coolant goes in.
_MESH_EVIDENCED_ROLES = frozenset({"wall", "symmetry", "empty"})


def _region_boundary(ws: Path, region: str) -> list[dict]:
    """The patches of ONE region, read off that region's own boundary file.

    NEVER constant/polyMesh. The root mesh survives splitMeshRegions untouched, carrying the
    pre-split patches with their pre-split face counts, and those counts are the two regions'
    added together: measured on the unit cell, ymin has 532 faces at the root, which is the
    fluid's 128 plus the solid's 404. A check that read the root would see every patch populated
    on a case where the coolant never reached one of them.
    """
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

    polymesh = ws / "constant" / region / "polyMesh"
    if not (polymesh / "boundary").is_file():
        return []
    try:
        return read_boundary_patches(polymesh)
    except (OSError, ValueError):
        logger.warning("conjugate finalize: %s has no readable boundary file", region,
                       exc_info=True)
        return []


def delivered_boundary_types(ws: Path, intake_patches: list) -> dict[str, str]:
    """The boundaries the user gets, with the role the delivered mesh evidences for each.

    The interface patches are left out: the user never asked for them and never approved them -
    the split writes them because a coupled case needs them.
    """
    declared = {(p.get("name") or "").strip(): (p.get("type") or "").strip()
                for p in (intake_patches or []) if isinstance(p, dict) and p.get("name")}
    out: dict[str, str] = {}
    for region in (FLUID_REGION, SOLID_REGION):
        for patch in _region_boundary(ws, region):
            name = str(patch.get("name") or "")
            if not name or INTERFACE_MARK in name:
                continue
            role = _OF_TYPE_TO_ROLE.get(str(patch.get("type") or "").strip().lower())
            if not role:
                said = declared.get(name, "")
                role = said if said and said.lower() not in _MESH_EVIDENCED_ROLES else "patch"
            out.setdefault(name, role)
    return out


def empty_patches(ws: Path) -> list[str]:
    """Named patches that came out of the split with no faces, as "<region>:<patch>".

    A PATCH WITH NO FACES IS A NAME WITH NOTHING BEHIND IT. Every block face is cut into both
    regions' boundary lists whether or not either region reaches it, so a declared inlet whose
    coolant never touches that face still appears in the fluid's boundary - with nFaces 0. The
    solver then has a `coolant_in` it cannot apply a flow rate to.

    The solid legitimately has no faces on nothing here, and neither does the fluid: both regions
    touch every side of the block they were cut from in the cases this engine takes. Where one
    genuinely does not, the gate's own words say which region and which patch, so the reader can
    see whether the name was the problem or the geometry was.
    """
    out = []
    for region in (FLUID_REGION, SOLID_REGION):
        for patch in _region_boundary(ws, region):
            name = str(patch.get("name") or "")
            if name and INTERFACE_MARK not in name and int(patch.get("n_faces") or 0) <= 0:
                out.append(f"{region}:{name}")
    return out


def quality_of(ws: Path, report: dict) -> dict:
    """The manifest's quality dict, from the report the run left and the delivered regions.

    Everything here was MEASURED by the run: checkMesh per region, the meshed volume of each,
    the interface's face count from both sides, and the coolant's disagreement with the CAD. None
    of it is re-derived, and nothing that was not measured is reported as a number - an
    unmeasured non-orthogonality carried as 0.0 reads as a perfect mesh to every gate that
    compares it against a bar.
    """
    regions = report.get("regions") or {}
    rows: list[dict] = []
    fatal: list[str] = []
    non_ortho: list[float] = []
    skew: list[float] = []
    for name in (FLUID_REGION, SOLID_REGION):
        r = regions.get(name)
        if not isinstance(r, dict):
            continue
        fatal += [f"{name}:{f}" for f in (r.get("fatal") or [])]
        if r.get("max_non_ortho") is not None:
            non_ortho.append(float(r["max_non_ortho"]))
        if r.get("max_skewness") is not None:
            skew.append(float(r["max_skewness"]))
        rows.append({"name": name, "type": REGION_TYPE[name],
                     "cells": int(r.get("cells") or 0),
                     "volume_m3": float(r.get("volume_m3") or 0.0),
                     "pieces": int(r.get("pieces") or 0),
                     "mesh_ok": bool(r.get("mesh_ok")),
                     "fatal": list(r.get("fatal") or [])})

    fluid = regions.get(FLUID_REGION) or {}
    plan = report.get("plan") or {}
    expected = float(plan.get("expect_fluid_volume_m3") or 0.0)
    meshed = float(fluid.get("volume_m3") or 0.0)
    faces_fluid = int(report.get("interface_faces_fluid") or 0)
    faces_solid = int(report.get("interface_faces_solid") or 0)
    missing = [n for n in (FLUID_REGION, SOLID_REGION) if n not in {r["name"] for r in rows}]
    undeclared = [n for n in regions if n not in REGION_TYPE]
    blank = empty_patches(ws)

    q: dict = {
        "cells": sum(r["cells"] for r in rows),
        "fatal": fatal,
        "regions": rows,
        "regions_missing": missing,
        "regions_undeclared": undeclared,
        # BOTH SIDES OF THE COUPLING, and they must be the same number. The split cuts along faces
        # that already existed, so the two sides are the same surface: measured at 93,600 faces
        # each on the unit cell. A mismatch is a mesh the solver cannot map across.
        "interface_faces_fluid": faces_fluid,
        "interface_faces_solid": faces_solid,
        "interface_ok": faces_fluid > 0 and faces_fluid == faces_solid,
        "coolant_volume_m3": meshed,
        "coolant_expected_m3": expected,
        "coolant_pieces": int(fluid.get("pieces") or 0),
        "coolant_pieces_expected": int(plan.get("expect_fluid_pieces") or 0),
        "empty_patches": blank,
        "ports": report.get("ports"),
        "patch_names": dict(report.get("patch_names") or {}),
        "native_problems": list(report.get("problems") or []),
        "notes": list(report.get("notes") or []),
        "background_cell_m": plan.get("background_cell_m"),
        "levels": plan.get("levels"),
        "cells_across_asked": plan.get("cells_across"),
        "narrowest_passage_m": plan.get("narrowest_m"),
    }
    if expected > 0.0:
        q["coolant_error_pct"] = abs(meshed - expected) / expected * 100.0
        q["coolant_tol_pct"] = float(plan.get("volume_tol") or 0.0) * 100.0
        # THE ERROR AS A FRACTION OF WHAT THIS RESOLUTION ALLOWS, and the reason it is computed
        # here rather than compared somewhere: the bar is not a constant. It is the measured
        # staircase error for the cells-across used, with a margin, and it rides in the plan
        # (cfmesh_conjugate/plan._tolerance). A declared Criterion can only hold a static
        # threshold, so stating the bar twice - 5% in the criterion and 19.89% in the gate - gave
        # one quantity two readers that disagreed: this fixture came out 9.95% wrong at 5 cells
        # across, which the gate allows and the criterion refused. Expressed as a ratio, both read
        # the SAME number and the static bar is 1.0.
        if q["coolant_tol_pct"] > 0.0:
            q["coolant_error_over_tol"] = q["coolant_error_pct"] / q["coolant_tol_pct"]
    q["coolant_pieces_short"] = _pieces_short(q)
    q["resolution_short"] = _resolution_short(plan)
    # WHETHER THE INTERIOR CAN BE CUT AT ALL. The engine declares a REGION inspection target, so
    # the review context tells the reviewer to call inspect_region - and the slices are published
    # only when a per-region volume was exported. Without one the reviewer is sent to a tool that
    # produces no evidence, and a review with nothing to submit stalls into a non-verdict rather
    # than failing (the defect engines/manifest's own comment records, job 63ff3d42: 8 rounds, 0
    # submissions). The export never fails a mesh, so this is recorded rather than refused - but
    # it is recorded, because a stalled review with no trace of why is the worst of the three.
    q["review_volume"] = (report.get("review") or {}).get("volume") or ""
    if non_ortho:
        q["max_non_ortho"] = max(non_ortho)
    if skew:
        q["max_skewness"] = max(skew)
    q["mesh_ok"] = (bool(report.get("ok")) and not fatal and not missing and not undeclared
                    and not blank and q["interface_ok"])
    return q


def _pieces_short(q: dict) -> list[str]:
    """The piece-count disagreement in words, or [] when the counts agree.

    A LIST AND NOT A BOOLEAN, because the declared criterion is `op="empty"` and the gate must be
    able to tell "they agree" from "nobody counted": an absent key reads as unmeasured and
    refuses, where False would read as a pass.
    """
    want, got = int(q["coolant_pieces_expected"]), int(q["coolant_pieces"])
    if want <= 0 or got == want:
        return []
    return [f"the coolant came out as {got} piece(s) where the part's geometry has {want}"]


def _resolution_short(plan: dict) -> list[str]:
    """Where the mesh is coarser across the passages than the product's flow rule asks.

    The rule is engines/passage.PASSAGE_CELLS_ACROSS, and this engine does not reach it on a real
    cold plate inside the cell budget (engines/cfmesh_conjugate/budget has the measurement). The
    shortfall is REPORTED here and gated nowhere: the bar that decides delivery is the coolant's
    measured volume error, because the staircase measurements show the cell count does not
    predict that error for any one part.
    """
    from meshpipeline.engines.passage import PASSAGE_CELLS_ACROSS

    asked = int(plan.get("cells_across") or 0)
    if asked <= 0 or asked >= PASSAGE_CELLS_ACROSS:
        return []
    narrowest = float(plan.get("narrowest_m") or 0.0)
    where = f" ({narrowest * 1e6:.4g} um wide)" if narrowest > 0 else ""
    return [f"the narrowest passage{where} has {asked} cells across it, where the flow rule asks "
            f"for {PASSAGE_CELLS_ACROSS}"]


def finalize(workspace_dir, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    """The manifest for a delivered conjugate case, or a failure the executor can read.

    The seven positional arguments and the `success` key are the executor's, not this engine's.
    """
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    from meshpipeline.engines.cfmesh_conjugate.native import read_report
    from meshpipeline.engines.cfmesh_conjugate.slices import review_slices
    from meshpipeline.engines.manifest import write_manifest

    ws = Path(workspace_dir)
    report = read_report(ws)
    if not report:
        out = (f"[CFMESH_CONJUGATE] no readable {PL.REPORT_NAME} - the mesher did not finish")
        return {"success": False, "stdout": "", "stderr": "", "output": out}
    if report.get("refused"):
        out = f"[CFMESH_CONJUGATE] the mesher refused this case: {report['refused']}"
        return {"success": False, "stdout": "", "stderr": "", "output": out}
    if not report.get("regions"):
        out = ("[CFMESH_CONJUGATE] the report carries no region measurements - checkMesh never "
               "read this mesh, and it cannot be delivered unchecked")
        return {"success": False, "stdout": "", "stderr": "", "output": out}

    q = quality_of(ws, report)
    patch_types = delivered_boundary_types(ws, intake_patches)
    review = report.get("review") or {}
    # THE TWO NAMED BODIES the reviewer opens the surface by (cfmesh_conjugate/review_surface):
    # the coolant and the metal, each one physical group. Published beside the patch names so the
    # manifest's entity map covers everything the renderer can be asked for.
    entities = {str(k): list(v) for k, v in (review.get("groups") or {}).items()}

    lo, hi = _block(ws, report)
    write_manifest(
        ws,
        patch_types=patch_types,
        patch_entities={**{n: [] for n in patch_types}, **entities},
        bbox=(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]),
        quality=q,
        domain=domain or "conjugate heat transfer (a cooled solid)",
        body_bbox=(tuple(lo), tuple(hi)),
        mesh_bounds=(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]),
        # The COOLANT's volume, never the merged block: that merged export is the one thing a
        # conjugate review has to tell a real split apart from.
        volume_path=review.get("volume") or None,
        mesh_units=COMPLETED_MESH_UNIT.value,
        mesh_mode="cfmesh_conjugate",
        flow_topology=flow_topology,
        engine_params={**(engine_params or {}),
                       "_regions": [{"name": r["name"], "type": r["type"]} for r in q["regions"]]},
        inspection_regions=review_slices(lo, hi, inlet=_port(q, "inlet"),
                                         outlet=_port(q, "outlet")),
    )
    out = _said(q)
    return {"success": bool(q["mesh_ok"]), "stdout": out, "stderr": "", "output": out}


def _port(quality: dict, which: str) -> str:
    """The block face the coolant enters or leaves by, which is what names the cut across the flow.

    The BLOCK FACE and not the user's name for it: the slice needs the axis, and `coolant_in`
    carries none. `patch_names` maps one to the other.
    """
    ports = quality.get("ports") or {}
    named = str(ports.get(which) or "")
    if not named:
        return ""
    for face, name in (quality.get("patch_names") or {}).items():
        if name == named:
            return str(face)
    return named


def _block(ws: Path, report: dict):
    """The block the mesh fills, from the plan the host wrote.

    Falls back to the delivered points when a plan written before this field existed is read, and
    to the unit box only when neither is there - a bbox of zeros would publish a mesh with no
    extent, which every downstream reader of the manifest treats as a degenerate case.
    """
    from meshpipeline.engines.cfmesh_conjugate import plan as PL

    try:
        p = PL.read_plan(ws)
        if p.block_lo and p.block_hi:
            return list(p.block_lo), list(p.block_hi)
    except (OSError, ValueError):
        logger.info("conjugate finalize: no plan to read the block from")
    bounds = (report.get("review") or {}).get("bbox")
    if bounds and len(bounds) == 6:
        return [float(v) for v in bounds[:3]], [float(v) for v in bounds[3:]]
    logger.warning("conjugate finalize: neither the plan nor the review surface carries the "
                   "block - the manifest's extent is a unit box")
    return [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]


def _said(q: dict) -> str:
    """One line, in the numbers a reader of the build log would want."""
    bits = [f"cells={q['cells']:,}"]
    for r in q["regions"]:
        bits.append(f"{r['name']}={r['cells']:,}")
    if q.get("coolant_error_pct") is not None:
        bits.append(f"coolant={q['coolant_volume_m3'] * 1e9:.5g} mm3 "
                    f"({q['coolant_error_pct']:.2f}% from the CAD)")
    bits.append(f"interface={q['interface_faces_fluid']:,} faces both sides")
    if q["fatal"]:
        bits.append(f"fatal={q['fatal'][:6]}")
    if q["empty_patches"]:
        bits.append(f"empty={q['empty_patches'][:6]}")
    if q["native_problems"]:
        bits.append("problems: " + "; ".join(q["native_problems"][:4]))
    if not q.get("review_volume"):
        bits.append("NO per-region volume was exported: the reviewer cannot cut interior slices")
    return "[CFMESH_CONJUGATE] " + " ".join(bits)


__all__ = ["delivered_boundary_types", "empty_patches", "finalize", "quality_of"]
