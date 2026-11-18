# Responsibility: Write the mesh manifest that records what a completed run produced and in what units.
# Boundaries: it serialises facts the engine already established; it measures nothing and never guesses a unit.
# Collaborates with: contracts/mesh_units.py and each engine's finalize seam.
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.contracts.mesh_units import validate_completed_mesh_unit

logger = logging.getLogger(__name__)


def renderable_entities(manifest) -> list[str]:
    """The entities a review session will be able to draw, off the manifest this run wrote.

    `patches` (patch_entities) is the review surface's own map: a name with a non-empty list of
    gmsh entity tags is a physical group that was written into mesh.msh, and a name with an empty
    one is a boundary the mesh HAS that this engine's review surface does not carry. snappy's far
    field is the standing example - it lives in the blockMesh boundary, never in the triSurface
    the review msh is built from - so "has faces" is not the same question as "can be shown", and
    an obligation that named every delivered patch could never be discharged on an external run.

    WHAT THIS CANNOT TELL YOU, said plainly because an obligation built on it inherits it: these
    names come from the EXPORT, so a patch the export dropped is simply absent here and the
    obligation shrinks with it. Total loss is still caught - no renderable entity means no patch
    target, and discovery coming up empty refuses the review - but partial loss is not. Closing
    that needs the engine to declare which of its delivered boundaries the review surface is
    supposed to carry, and none of them declares it today. The engines whose entities are
    REGIONS have an independent count to compare against (checkMesh's per-region rows) and
    cfmesh_conjugate uses it; the body-patch engines have no such second reading.
    """
    patches = (manifest or {}).get("patches") or {}
    if not isinstance(patches, dict):
        return []
    names = sorted(str(name) for name, tags in patches.items()
                   if isinstance(name, str) and name.strip() and tags)
    # NEVER MORE THAN THE RENDERER WILL ADVERTISE. sandbox.render_adapter.expand_patch_targets
    # stops at MAX_PATCHES, so on a generated 2,000-part board discovery offers 512 targets and
    # no more. An obligation naming all 2,000 could not be met by any session: every such review
    # would end EXPECTED_TARGET_MISSING on a mesh that is fine. The truncation is the same one
    # the renderer makes - its patch_names() is sorted and expand_patch_targets takes them in
    # order - so these are exactly the ids it will offer.
    from meshpipeline.sandbox.render_adapter import MAX_PATCHES
    if len(names) > MAX_PATCHES:
        logger.info("manifest: %d renderable entities, naming the %d the renderer will offer",
                    len(names), MAX_PATCHES)
        names = names[:MAX_PATCHES]
    return names


def _patch_face_counts(workspace: Path) -> dict:
    """nFaces per boundary patch, off the mesh that was DELIVERED.

    THE BLOCK IS NOT THE DELIVERY. A multi-region engine keeps the pre-split block at
    constant/polyMesh and puts the regions beside it, and this read the block. splitMeshRegions
    divides the block's faces between the regions, and the port cut carves inlet and outlet out of
    a block face AFTERWARDS, on the coolant alone - so the block is the mesh as it was before two
    of the three things that decide what the customer gets. Measured on the delivered coupon,
    job 18e2f4ed, 2026-10-29:

        block      xmin 1,928  xmax 1,928  ymin 776  ymax 776  zmin 7,504  zmax 17,426
        delivered  ...the same, except zmax 14,226 - plus inlet 1,600, outlet 1,600 and the
                   interface at 346,456 faces on each side

    The roster a reader sees showed zmax 3,200 faces too big, which is exactly the two ports cut
    out of it, and gave no count at all to the ports or to the interface - the one patch a
    conjugate mesh is for.

    AND AT THE SIZES THAT NEED RANKS THERE IS NO BLOCK AT ALL. The parallel chain stays decomposed
    through the split and merges the two REGIONS, never the block, so constant/polyMesh does not
    exist on a big conjugate build and this returned {} - every boundary in the panel with its
    count blank.

    Summed per BARE name across the regions, because the manifest is keyed by the name the user
    asked for at intake and two regions can legitimately reach the same block face. A single-region
    mesh has no region directories, reads the one boundary file it always did, and is unchanged.
    """
    root = workspace / "constant"
    regions = [d / "polyMesh" / "boundary" for d in sorted(root.glob("*"))
               if d.is_dir() and d.name != "polyMesh" and (d / "polyMesh" / "boundary").is_file()]
    counts: dict[str, int] = {}
    for bnd in regions or [root / "polyMesh" / "boundary"]:
        if not bnd.exists():
            continue
        for m in re.finditer(r"(\w+)\s*\{[^}]*?nFaces\s+(\d+)",
                             bnd.read_text(errors="replace"), re.DOTALL):
            counts[m.group(1)] = counts.get(m.group(1), 0) + int(m.group(2))
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


def _published_volume(ws: Path, volume_path: str | None) -> str | None:
    """The volume the manifest may publish: a FILE a reader can open, or None.

    `mesh_paths.volume` is a file artifact for every engine (a VTU/VTP the review slices, the
    deliverable deck for gmsh, the lumen mesh for vmtk). A directory published there (an
    OpenFOAM `constant/`) is no volume any renderer reads: the review dropped it as absent while
    the manifest still promised slices through it, so every such review ended before the model
    was asked (EXPECTED_TARGET_MISSING). Absence is a real, stated state; a stand-in path is not.
    """
    if not volume_path:
        return None
    p = Path(volume_path)
    full = p if p.is_absolute() else ws / p
    if full.is_dir():
        logger.warning("manifest: volume %r is a directory, not a volume file - none published",
                       p.name)
        return None
    # AND A PATH TO A FILE THAT IS NOT THERE IS NOT A VOLUME EITHER, which this guard asserted and
    # did not check. `_published_surface` below has had the same rule as a real test since it was
    # written - "if not p.is_file(): return None" - and this one only ever rejected a directory,
    # so the one case that actually happens went straight through.
    #
    # MEASURED, job 0ff6f9e5, 2026-11-16. `engines/return_scope` left the fluid interior behind to
    # get the archive under its ceiling and UNLINKED it, and its own record says so:
    # left_out VTK/fluid/cr_ws_vgcm8hv7_0/internal.vtu, 10,366,611,525 bytes. The report still
    # carried the path it had been written at, this guard still published it because it was not a
    # directory, and the manifest therefore promised a volume that had been deleted and three cuts
    # through it. The review then refused with "3 region(s) not offered - no volume artifact": the
    # engine looked as though it had produced evidence right up to the point of being asked for it.
    #
    # Absence is a real, stated state; a stand-in path is not.
    if not full.is_file():
        logger.warning("manifest: volume %r is not a file in %s - none published (it was most "
                       "likely left behind to get the archive home; see return_scope.json)",
                       p.name, ws)
        return None
    return volume_path


def _published_surface(ws: Path, name: str = "mesh.msh") -> str | None:
    """The surface the manifest may publish: a FILE a reader can open, or None.

    THE SAME RULE AS THE VOLUME, applied to the key beside it. `mesh_paths.surface` was an
    unconditional `str((ws / "mesh.msh").resolve())` one line below the guard whose own docstring
    states the standard - "Absence is a real, stated state; a stand-in path is not" - so a
    workspace with no mesh.msh still published a path to one. Every engine writes that file on a
    successful run, which is why nothing caught it; what it costs is on the paths where the file is
    NOT there, because a reader then gets a dangling name instead of a stated absence and
    `review_session.resolve_raw` raises the same EVIDENCE_MISSING for both while saying the wrong
    thing about which happened.

    `mesh_paths.surface` is `required=True` in all seven review bundles, so None is not a quiet
    downgrade: it is the absence those bundles already know how to refuse on, stated once instead
    of discovered by opening it.
    """
    p = (ws / name).resolve()
    if not p.is_file():
        logger.warning("manifest: no %s in %s - no surface published", name, ws)
        return None
    return str(p)


def _published_cuts(ws: Path, regions: list | None) -> tuple[list[dict], int]:
    """The inspection regions, with every cut frame whose PICTURE is actually on disk kept and
    every one whose picture is not dropped. Returns (regions, how many pictures survived).

    THE SAME RULE AS THE SURFACE AND THE VOLUME, applied to the frames: a frame that names an
    image file that is not there is a dangling name, and the reader gets EVIDENCE_MISSING while
    being told the wrong thing about which of the two happened.

    WHY THE PICTURES EXIST AT ALL. The cuts used to be a promise to the home side to slice the
    volume itself, and the volume is the fluid interior - 10,366,611,525 bytes on the Corintis
    plate. Sending that home to be cut there is not available: the mesh VM has 512 GiB and the
    worker that would open it is capped at 16 GiB, and `sandbox/backend._load_volume` reads the
    file whole. Measured 2026-11-17 on this plate's own VTK tree, a pyvista read runs at
    250,591,972 bytes a second and holds 0.973 GB of RSS per 1.24 GB of file, so that interior is
    about 41 s and 8 GB in a process with 16 to spend. The cut is therefore made where the mesh
    already is and only the picture travels, which is kilobytes.

    A frame keeps its window and its um/px whether or not the picture survived, because those are
    what a claim about a cut is checked against and they are true of the cut either way.
    """
    out: list[dict] = []
    kept = 0
    for r in regions or []:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        frames = []
        for f in row.get("frames") or []:
            if not isinstance(f, dict):
                continue
            fr = dict(f)
            img = fr.get("image")
            if img:
                p = Path(img)
                if (p if p.is_absolute() else ws / p).is_file():
                    kept += 1
                else:
                    logger.warning("manifest: cut %r frame %r names %s, which is not there - "
                                   "the frame is published without a picture",
                                   row.get("name"), fr.get("scope"), img)
                    fr.pop("image", None)
            frames.append(fr)
        if frames:
            row["frames"] = frames
        out.append(row)
    return out, kept


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
                   reference_length=None, symmetry_faces: list | None = None,
                   inspection_regions: list | None = None) -> dict:
    # STATED, NOT DEFAULTED. `mesh_units` is keyword-only and has no default, so an engine that
    # forgets it fails here - before any deliverable is announced - rather than writing a manifest
    # whose unit each consumer then guessed differently. Validated at the point of writing because
    # this is where the artifact becomes durable.
    mesh_units = validate_completed_mesh_unit(
        mesh_units.value if isinstance(mesh_units, LengthUnit) else mesh_units).value
    ws = Path(workspace)
    volume_path = _published_volume(ws, volume_path)
    fc = _patch_face_counts(ws)
    # THE BUILD'S OWN EXIT, carried to the one place every engine's manifest is written.
    #
    # `rc` and `timed_out` are DECLARED GATING BARS on seven engines, and until this they could
    # never fail: `finalize` is reached through one fixed contract that carries no native result
    # (executor calls it with workspace, patches, engine, domain, internal_flow, params,
    # topology), so each engine rebuilt `quality` by re-reading checkMesh output or its own report
    # from disk, and both keys arrived absent. `gate_declared_criteria` then skipped them via
    # `BUILD_TIME_KEYS`, whose comment asserted they "CANNOT be measured after the build". The
    # process has exited; its RECORD has not. `contracts.mesh_execution` keeps every native run of
    # the attempt and publishes it through `native_runs`, which outlives the process by design.
    #
    # Here rather than in seven finalizes: the first one to forget would revert to the old
    # behaviour silently, with no test able to see it.
    from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE, native_runs
    _runs = native_runs(ws)
    _build: dict | None = None
    if _runs is not None:
        # THE LAST RUN THAT ACTUALLY LAUNCHED, which is the same selector the executor classifies
        # failures with - so the manifest and the executor can never disagree about which run this
        # was. A refusal before launch is not the build.
        _launched = [r for r in _runs if not r.get("refused_before_launch")]
        _build = _launched[-1] if _launched else None
    # AN OUTAGE IS NOT A VERDICT ON THE MESH, and this is the half of "the same selector" that is
    # easy to miss: the executor does not trust the selected run's rc either, it branches on
    # `never_started` and `uncollected` first and returns an INFRASTRUCTURE result rather than a
    # mesh verdict. RC_INFRASTRUCTURE comes back as an ordinary result, so it passes the filter
    # above and would be stamped here as the build's exit code - after which the quality floor
    # refuses with MESH_QUALITY, the fallback ladder calls that fixable, and a storage blip on a
    # perfectly good mesh is told to the user as cells too badly shaped to use and buys a second
    # engine run. A `None` rc is the same hazard from the other side: at schema 2.2 it reads as
    # not-measured, which the gate turns into a refusal.
    #
    # In all of those the mesher's own exit is genuinely UNKNOWN, so both keys stay absent and the
    # schema stays 2.1 - the same answer as an unopened record.
    if _build is not None and (_build.get("never_started") or _build.get("uncollected")
                               or _build.get("rc") == RC_INFRASTRUCTURE
                               or _build.get("rc") is None):
        _build = None
    if _build is not None:
        # COPIED, never mutated in place: the caller reuses its own dict after this returns
        # (cfmesh's finalize reads `fatal` back out of it, snapgrid formats from it).
        # `bool()` on timed_out because `0 == False` in Python and a bare 0 would read to
        # Criterion.evaluate as a build that finished inside its budget.
        quality = {**quality, "rc": _build.get("rc"), "timed_out": bool(_build.get("timed_out"))}
    # AN UNOPENED RECORD IS UNKNOWN, NOT CLEAN. `native_runs` returns None when this process never
    # opened the attempt's record; writing rc 0 there would publish a clean build on no evidence,
    # which is worse than publishing nothing. Both keys stay absent and the schema stays 2.1, so
    # the gate goes on skipping them for exactly the manifests that cannot carry them.
    _schema = "2.2" if _build is not None else "2.1"
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
    # the box faces that are symmetry planes ([{patch, axis, side}], as the builder placed them):
    # they lie on the body by design, so the extent gate owes them no margin. Empty for a box
    # whose every face is far field (or floor).
    geometry["symmetry_faces"] = [dict(f) for f in (symmetry_faces or []) if isinstance(f, dict)]
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
    # user instead of asserting it. `rc` and `timed_out` are in here too, from the block near the
    # top of this function - they used to be absent, and the claim that they could not be known
    # at manifest time was false: the process has exited, its record has not.
    from meshpipeline.engines.quality_criteria import evaluate as _qc_evaluate
    from meshpipeline.engines.quality_criteria import measurements_from_manifest
    # The engine's WHOLE quality dict feeds the criteria (each engine's rows pick their own keys;
    # absent keys read as not-evaluated) + wall_faces, which is manifest-derived rather than
    # engine-reported. The mapping lives in quality_criteria so the pre-review quality gate judges
    # the SAME measurements this report card is built from - a manifest cannot publish a grade its
    # own numbers contradict.
    _qc_rows = _qc_evaluate(mesh_mode, measurements_from_manifest(
        {"quality": quality, "patch_face_counts": fc, "patch_types": roles,
         "flow_topology": flow_topology or ""}))
    _gating = [r for r in _qc_rows if r["gating"] and r["passed"] is not None]
    # WHAT THE CUTS ARE, before the manifest dict is built, because whether any of them has a
    # picture decides whether they are promised at all.
    _regions_in = ([dict(r) for r in inspection_regions] if inspection_regions
                   else _inspection_regions(body_bbox) if body_bbox else [])
    _regions_cut, _cut_images = _published_cuts(ws, _regions_in)
    _regions_published = (_regions_cut
                          if _engine_serves_region_slices(mesh_mode)
                          and (volume_path or _cut_images)
                          else [])
    manifest = {
        "schema_version": _schema, "mesh_mode": mesh_mode, "mesh_written": owner,
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
        "mesh_paths": {"surface": _published_surface(ws), "volume": volume_path},
        # the engine's own slices when it names them (a thermal model's: across the board and
        # through the hottest parts), else the generic ones through the body.
        #
        # A CUT IS PROMISED ON WHAT IT CAN BE MADE FROM, which is now either of two things. It used
        # to be the volume alone - "with none published there is nothing to cut" - and that was
        # right while cutting was something the home side did. An engine that cuts on the mesh VM
        # and sends the PICTURES home can keep its promise with no volume at all, and on the
        # biggest meshes that is the only way it can: the Corintis plate's interior is 10.4 GB
        # against a 16 GiB worker, so the volume is exactly what cannot come home.
        #
        # The order matters. `_published_cuts` drops frames whose picture is missing, so a run
        # that rendered nothing and published no volume still promises nothing.
        "inspection_regions": _regions_published,
        "validation": _validation_block(types, owner, fc, roles),
    }
    (ws / "mesh_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest
