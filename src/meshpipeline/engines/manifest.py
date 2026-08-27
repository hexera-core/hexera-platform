# Responsibility: Write the mesh manifest that records what a completed run produced and in what units.
# Boundaries: it serialises facts the engine already established; it measures nothing and never guesses a unit.
# Collaborates with: contracts/mesh_units.py and each engine's finalize seam.
from __future__ import annotations

import json
import re
from pathlib import Path

from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.mesh_units import validate_completed_mesh_unit


def _patch_face_counts(workspace: Path) -> dict:
    bnd = workspace / "constant" / "polyMesh" / "boundary"
    counts: dict[str, int] = {}
    if bnd.exists():
        for m in re.finditer(r"(\w+)\s*\{[^}]*?nFaces\s+(\d+)",
                             bnd.read_text(errors="replace"), re.DOTALL):
            counts[m.group(1)] = int(m.group(2))
    return counts


def _inspection_regions(body_bbox) -> list[dict]:
    (x0, y0, z0), (x1, y1, z1) = body_bbox
    c = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]
    L = [x1 - x0, y1 - y0, z1 - z0]
    box = [[x0, x1], [y0, y1], [z0, z1]]
    m = 1.5 * (max(L) or 1.0)                                  # frame slices on the body + near-field
    body_frame = [x0 - m, x1 + m, y0 - m, y1 + m, z0 - m, z1 + m]
    regions: list[dict] = []
    for i, tag in enumerate("xyz"):                           # 3 orthogonal slices through the body
        n = [0, 0, 0]; n[i] = 1
        regions.append({"name": f"slice_{tag}_centre", "kind": "slice",
                        "normal": n, "origin": c, "clip_box": body_frame})
    for i in sorted(range(3), key=lambda k: L[k])[:2]:        # near-wall on the two thinnest (flat) faces
        others = [k for k in range(3) if k != i]
        sn = min(others, key=lambda k: L[k])                  # slice plane CONTAINS the wall axis i
        n = [0, 0, 0]; n[sn] = 1
        b = [list(p) for p in box]
        pad_i = (L[i] or 0.01) * 0.8
        b[i] = [box[i][1] - pad_i, box[i][1] + pad_i]         # straddle the body's +face (the wall)
        for k in others:                                      # keep the long axes to a readable strip
            ck = (box[k][0] + box[k][1]) / 2; hk = (box[k][1] - box[k][0]) * 0.35
            b[k] = [ck - hk, ck + hk]
        regions.append({"name": f"nearwall_{'xyz'[i]}", "kind": "nearwall", "normal": n,
                        "origin": c,
                        "clip_box": [b[0][0], b[0][1], b[1][0], b[1][1], b[2][0], b[2][1]]})
    return regions



def _delivered_mesh_exists(ws: Path, mesh_mode: str) -> bool:
    # The engine's own declared marker, never a path guessed here. An unknown engine falls back to
    # the OpenFOAM layout rather than claiming a mesh exists.
    try:
        from meshpipeline.engines.registry import get_spec

        deliverable = get_spec(mesh_mode).deliverable
        marker = deliverable.marker if deliverable is not None else "constant/polyMesh/owner"
    except Exception:  # noqa: BLE001 - an unregistered engine must not crash manifest writing
        marker = "constant/polyMesh/owner"
    return (ws / marker).exists()


def _engine_serves_region_slices(mesh_mode: str) -> bool:
    """Whether this engine's declared review surface can REDEEM an interior-slice promise.

    `inspection_regions` is a promise to the reviewer: the review context renders it as
    "call inspect_region(<name>)". The runtime kind-gates that tool on the session's
    REGION inspection targets, so on an engine that declares none (gmsh, vmtk) every call
    is refused - and a reviewer sent to a tool that never produces evidence stalls the
    review into a no-progress non-verdict (job 63ff3d42: 8 rounds, 0 submissions,
    reviewer_evidence_missing). The promise is published ONLY when the engine's own spec
    declares a REGION target. Fail closed: an unknown engine promises nothing.
    """
    try:
        from meshpipeline.contracts.review_evidence import TargetKind
        from meshpipeline.engines.registry import get_spec

        return any(t.kind is TargetKind.REGION
                   for t in get_spec(mesh_mode).inspection_targets)
    except Exception:  # noqa: BLE001 - an unregistered engine must not crash manifest writing
        return False


def _validation_block(types: set, owner: bool, fc: dict, roles: dict) -> dict:
    return {
        "has_wall": "wall" in types,
        "has_inflow": ("inlet" in types) or ("farfield" in types),
        "has_outflow": ("outlet" in types) or ("farfield" in types),
        "patch_validation": {p: fc.get(p, 0) > 0 for p in roles},
        "body_has_faces": any(fc.get(p, 0) > 0 for p, r in roles.items() if r == "wall"),
        "volume_exists": owner,
        "warnings": [],
    }


def write_manifest(workspace, *, patch_types: dict, patch_entities: dict,
                   bbox: tuple, quality: dict, domain: str = "",
                   body_bbox=None, mesh_bounds=None, volume_path: str | None = None,
                   requested_box=None, mesh_units: str,
                   mesh_mode: str = "cfmesh",
                   engine_params: dict | None = None, flow_topology: str = "",
                   reference_length=None) -> dict:
    # STATED, NOT DEFAULTED. `mesh_units` is keyword-only and has no default, so an engine that
    # forgets it fails here - before any deliverable is announced - rather than writing a manifest
    # whose unit each consumer then guessed differently. Validated at the point of writing because
    # this is where the artifact becomes durable.
    mesh_units = validate_completed_mesh_unit(
        mesh_units.value if isinstance(mesh_units, LengthUnit) else mesh_units).value
    ws = Path(workspace)
    fc = _patch_face_counts(ws)
    roles = dict(patch_types)
    types = set(roles.values())
    # "the mesh physically exists" - asked of THE ARTIFACT THIS ENGINE DELIVERS, which each spec
    # already declares as its Deliverable marker. This used to be a hardcoded
    # constant/polyMesh/owner probe: true for the three OpenFOAM engines and structurally false for
    # gmsh (mesh.inp) and vmtk (mesh.vtu), so a 35,994-tetrahedron vmtk mesh published
    # mesh_written: false. Two engines were publishing a fact about themselves that was never true.
    owner = _delivered_mesh_exists(ws, mesh_mode)
    # box_* keys = the ACTUAL meshed extent (octree-padded) for INFO/reference.
    gb = list(mesh_bounds) if (mesh_bounds and len(mesh_bounds) == 6) else list(bbox)
    x0, y0, z0, x1, y1, z1 = gb
    geometry = {"box_xmin": x0, "box_xmax": x1, "box_ymin": y0,
                "box_ymax": y1, "box_zmin": z0, "box_zmax": z1}
    # domain_box = the REQUESTED far-field box (what the builder prepared). This is
    # what the A1 domain-extent gate measures. Falls back to the meshed bounds only
    # if the prepared box is unavailable (octree padding then adds tolerance noise).
    if requested_box and len(requested_box) == 2:
        (dmnx, dmny, dmnz), (dmxx, dmxy, dmxz) = requested_box
        geometry["domain_box"] = {"xmin": float(dmnx), "xmax": float(dmxx),
                                  "ymin": float(dmny), "ymax": float(dmxy),
                                  "zmin": float(dmnz), "zmax": float(dmxz)}
    else:
        geometry["domain_box"] = {"xmin": x0, "xmax": x1, "ymin": y0,
                                  "ymax": y1, "zmin": z0, "zmax": z1}
    if body_bbox:
        (bx0, by0, bz0), (bx1, by1, bz1) = body_bbox
        geometry["body_box"] = {"xmin": bx0, "xmax": bx1, "ymin": by0,
                                "ymax": by1, "zmin": bz0, "zmax": bz1}
        # chord = streamwise (x) body extent - the unit the planner's margins are natively
        # in (flow is along +x here). The user's "Nc" requests are in THEIR unit, which is
        # reference_length when they stated one: a CRM brief quotes MACs, and judging those
        # against body lengths rejected a correct domain twice.
        geometry["chord"] = max(float(bx1) - float(bx0), 1e-9)
        geometry["reference_length"] = (float(reference_length) if reference_length
                                        else geometry["chord"])
        geometry["reference_length_source"] = ("user_stated" if reference_length
                                               else "body_streamwise_extent")
    # Evidence-backed production-grade report card (engines.quality_criteria):
    # per-criterion threshold vs measured vs verdict, each with rationale + a
    # curated citation - so the reviewer/intake can JUSTIFY quality to the
    # user instead of asserting it. Build-time-only keys (rc/timed_out) are absent
    # at manifest time and show as not-evaluated; the build judge already gated them.
    from meshpipeline.engines.quality_criteria import evaluate as _qc_evaluate
    from meshpipeline.engines.quality_criteria import measurements_from_manifest
    # The engine's WHOLE quality dict feeds the criteria (each engine's rows pick their own keys;
    # absent keys read as not-evaluated) + wall_faces, which is manifest-derived rather than
    # engine-reported. The mapping lives in quality_criteria so the pre-review quality gate judges
    # the SAME measurements this report card is built from - a manifest cannot publish a grade its
    # own numbers contradict.
    _qc_rows = _qc_evaluate(mesh_mode, measurements_from_manifest(
        {"quality": quality, "patch_face_counts": fc, "patch_types": roles}))
    _gating = [r for r in _qc_rows if r["gating"] and r["passed"] is not None]
    manifest = {
        "schema_version": "2.1", "mesh_mode": mesh_mode, "mesh_written": owner,
        "engine_params": dict(engine_params or {}),
        "flow_topology": flow_topology or "",   # neutral flow-regime fact (NOT an engine_param)
        "cell_count": quality.get("cells", 0), "mesh_units": mesh_units,
        "domain": domain,
        "quality_criteria": {
            "engine": mesh_mode,
            "production_grade": bool(all(r["passed"] for r in _gating)) if _gating else None,
            "criteria": _qc_rows,
        },
        "geometry": geometry,
        "patches": patch_entities or {p: [] for p in roles},
        "patch_types": roles,
        "patch_face_counts": fc, "quality": quality,
        "mesh_paths": {"surface": str((ws / "mesh.msh").resolve()), "volume": volume_path},
        "inspection_regions": (_inspection_regions(body_bbox)
                               if body_bbox and _engine_serves_region_slices(mesh_mode)
                               else []),
        "validation": _validation_block(types, owner, fc, roles),
    }
    (ws / "mesh_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest
