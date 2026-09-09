# Responsibility: Decide whether a snappyHexMesh run actually delivered, and record what it produced.
# Boundaries: delivery means every required member of the declared bundle is present.
from __future__ import annotations

import logging
import re
from pathlib import Path

from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
from meshpipeline.engines.snappy.parallel_stages import polymesh_complete

#: blockMesh is the FIRST stage of every snappy sequence and writes its log fresh each attempt, so
#: its mtime is a lower bound on when this attempt started writing `constant/polyMesh`. Absent (a
#: workspace that never ran the sequence), staleness is simply not asserted rather than guessed.
ATTEMPT_MARKER_LOG = "blockMesh.log"


def _attempt_started(ws: Path) -> float | None:
    marker = ws / ATTEMPT_MARKER_LOG
    return marker.stat().st_mtime if marker.is_file() else None

logger = logging.getLogger(__name__)


def _polymesh_patch_names(ws: Path) -> list[str]:
    b = ws / "constant" / "polyMesh" / "boundary"
    if not b.exists():
        return []
    # OpenFOAM boundary entries are `<name>` on its own line, then `{` on the next.
    names = re.findall(r"^\s*([A-Za-z_]\w*)\s*\n\s*\{", b.read_text(errors="replace"), re.M)
    return [n for n in names if n != "FoamFile"]


def folded_class_regions(ws: Path, layer_policy: dict | None) -> dict:
    """The layer policy's synthetic class regions that the delivered boundary no longer carries.

    The policy splits one declared wall into <wall>_thin / <wall>_razor triSurface regions so each
    class can carry its own layer count; snappyHexMesh makes each a patch, and createPatch folds
    them back into the declared wall after meshing (snappy_runner authors that merge). The staged
    STL still names the regions and the layer record still lists them, but the polyMesh boundary
    - the mesh the user receives - does not. Five corpus rotors reached the manifest gate with
    body_thin / body_razor declared and zero faces: the mesh was right, the manifest was stale.
    Returns {region: wall it was folded into}. Fail-safe: no boundary, or a region that still
    exists in it, folds nothing - a genuinely missing patch must keep failing the manifest gate.
    Real CAD-named solids never carry the class suffix and are never folded.
    """
    regions = (layer_policy or {}).get("region_patches") or {}
    present = set(_polymesh_patch_names(ws))
    if not regions or not present:
        return {}
    out: dict = {}
    for r in regions:
        base, _, cls = str(r).rpartition("_")
        if cls in ("thin", "razor") and base and r not in present:
            out[str(r)] = base
    return out


# Declared roles whose OpenFOAM patch TYPE is semantically load-bearing: a 2D case with a
# front/back patch of type `patch` (a failed empty-retype) or a symmetry patch of type
# `patch` SOLVES WRONG, yet has the right name and nonzero faces - name/face reconciliation
# alone cannot catch it. Case-keyed (which roles the user declared), never engine-keyed.
_ROLE_REQUIRED_FOAM_TYPE = {"empty": "empty", "symmetry": "symmetryPlane", "wall": "wall"}


def _polymesh_boundary_foam_types(ws: Path) -> dict:
    b = ws / "constant" / "polyMesh" / "boundary"
    if not b.exists():
        return {}
    txt = b.read_text(errors="replace")
    out: dict = {}
    # \s* not \s+ : cfMesh writes patch names at column 0 (snappy indents them)
    for m in re.finditer(r"^\s*([A-Za-z_]\w*)\s*\n\s*\{([^}]*)\}", txt, re.M):
        name, body = m.group(1), m.group(2)
        if name == "FoamFile":
            continue
        t = re.search(r"\btype\s+(\w+)\s*;", body)
        if t:
            out[name] = t.group(1)
    return out


def reconcile_boundary_types(ws: Path, intake_patches: list) -> str:
    actual = _polymesh_boundary_foam_types(ws)
    if not actual:
        return ""
    bad: list[str] = []
    for p in intake_patches or []:
        role = str(p.get("type", "")).strip()
        name = str(p.get("name", "")).strip()
        want = _ROLE_REQUIRED_FOAM_TYPE.get(role)
        if want and name in actual and actual[name] != want:
            bad.append(f"{name}: declared role {role!r} requires OpenFOAM type {want!r}, "
                       f"actual boundary has {actual[name]!r}")
    if bad:
        return ("[BOUNDARY_TYPE_MISMATCH] the generated polyMesh boundary contradicts the "
                "declared contract - " + "; ".join(bad) + ". The mesh would solve wrong; "
                "re-run the mesh (the type retype/emission step failed).")
    return ""


def finalize(workspace_dir: str, intake_patches: list, engine: str, domain: str = "",
                     internal_flow: bool = False, engine_params: dict | None = None,
                     flow_topology: str = "") -> dict:
    from meshpipeline.engines.runtime import get_engine
    R = get_engine(engine)
    ws = Path(workspace_dir)
    # THE DELIVERABLE GATE. `owner` alone was the old test, and it shipped partial meshes:
    # snappyHexMesh writes the polyMesh incrementally, so a run killed by a timeout, an OOM or a
    # cancellation leaves an `owner` beside nothing else. checkMesh cannot read such a case, so it
    # reports no fatal classes - and `success = not fatal` read that as a good mesh, wrote the
    # manifest from empty measurements and sent the case to delivery.
    # `polymesh_complete` is snappy's own reconstruction check, already used by `classify()`; the
    # gate simply asks it instead of asking about one file. `since` makes it reject a previous
    # attempt's mesh: blockMesh runs first and unconditionally in every snappy sequence, so its
    # log is a lower bound on when this attempt began writing constant/polyMesh.
    ok, problems = polymesh_complete(ws, since=_attempt_started(ws))
    if not ok:
        return {"success": False, "stdout": "", "stderr": "",
                "output": ("[SNAPPY] constant/polyMesh is incomplete - " + ", ".join(problems)
                           + ". snappyHexMesh did not finish writing the mesh (a killed, "
                             "timed-out or cancelled run leaves a partial polyMesh); "
                             "re-run the mesh.")}
    q = R.check_mesh(ws)
    # NEAR-WALL PRISM LAYER COVERAGE - a MEASURED number, not a visual: a ~1e-4 m layer band
    # on a metre-scale body is invisible in any whole-body render, so the reviewer must judge
    # the layer axis from this figure (it otherwise mistook an unresolvable near-wall region
    # for "no layers"). Capability by declaration: an engine parses it only if it exposes
    # parse_layer_coverage AND actually wrote a layer log - no engine-name check.
    if hasattr(R, "parse_layer_coverage"):
        _log = ws / "snappyHexMesh.log"
        if _log.exists():
            try:
                _lc = R.parse_layer_coverage(_log.read_text(errors="replace"))
                _per = _lc.get("per_patch", {}) or {}
                # coverage on the WALL patches (exclude the box faces that carry no layers)
                _walls = {n: v for n, v in _per.items() if n not in ("farfield", "symmetry")}
                # PROVENANCE travels with the number: 'overall' is the AREAL cells-added
                # headline; 'per_patch_min' is a THICKNESS-percent fallback measuring a
                # different thing. Downstream policy (layer-coverage caveat eligibility)
                # accepts only the areal figure and fails closed on the fallback or an
                # absent key - so the source is stated, never inferred.
                if _lc.get("overall_pct") is not None:
                    q["layer_coverage_pct"] = _lc["overall_pct"]
                    q["layer_coverage_source"] = "overall"
                    if _lc.get("cells_with_layers") is not None:
                        q["layer_cells_with"] = _lc["cells_with_layers"]
                        q["layer_cells_targeted"] = _lc["cells_targeted"]
                elif _walls:
                    q["layer_coverage_pct"] = min(v["coverage_pct"] for v in _walls.values())
                    q["layer_coverage_source"] = "per_patch_min"
                if _walls:
                    q["per_patch_layers"] = {
                        n: {"layers": v.get("layers"), "target": v.get("layers_target"),
                            "coverage_pct": v.get("coverage_pct")} for n, v in _walls.items()}
            except Exception:
                logger.exception("finalize: layer-coverage parse failed (non-fatal)")
    # THE HONEST LAYER-POLICY RECORD (engines/snappy/layer_policy.py). When the thin-feature
    # classifier locally reduced or dropped prism layers, the delivered quality data must SAY so
    # - per class: the layer count and the wall-area fraction it covers - so the reviewer and the
    # caveat machinery judge the measured coverage against the policy that was actually authored,
    # not against the global request it deliberately replaced.
    _lp = ws / "layer_policy.json"
    if _lp.exists():
        try:
            import json as _json
            _pol = _json.loads(_lp.read_text())
            if isinstance(_pol, dict):
                q["layer_policy"] = _pol
        except Exception:
            logger.exception("finalize: layer-policy record unreadable (non-fatal)")
    patch_entities, bbox = {}, (0.0,) * 6
    _review_tris: dict = {}   # per-patch triangles → precomputed reviewer camera views
    # The review surface the vision reviewer renders is built from the geometry THIS ENGINE
    # staged - snappy names a body triSurface, cfMesh assembles geom.stl. The engine DECLARES
    # its source STL(s) via review_geometry_stls (default: geom.stl); the shared code just
    # reads them and builds the mesh. No engine-name branches - capability by declaration.
    _stls = (R.review_geometry_stls(ws, internal_flow)
             if hasattr(R, "review_geometry_stls") else [ws / "geom.stl"])
    _solids: dict = {}
    for _stl in _stls:
        if not Path(_stl).exists():
            continue
        try:
            _solids.update(R.read_stl_solids(_stl))
        except Exception:
            logger.exception("Executor: reading review geometry %s failed", _stl)
    # Class regions the boundary no longer has are drawn as the wall they became, so the
    # review surface, the manifest's patch map and the delivered boundary all say the same thing.
    _folded = folded_class_regions(ws, q.get("layer_policy"))
    for _reg, _base in _folded.items():
        if _reg in _solids:
            _solids[_base] = list(_solids.get(_base) or []) + list(_solids.pop(_reg))
    if _solids:
        _review_tris = _solids
        try:
            patch_entities, bbox = R.build_review_msh(ws, _solids)
        except Exception:
            logger.exception("Executor: review-mesh build failed (non-fatal)")
    # INTERNAL CARVE LEAK AUDIT - defense in depth behind the port-mouth seal in
    # cad_tessellate.tessellate_internal. An internal carve may only deliver the patches
    # the engine itself staged as triSurface STLs (inlet/outlet*/wall, under whatever
    # names port binding chose). Any OTHER patch with faces is the blockMesh
    # background-box skin ('outer') surviving the carve: the channel leaked to the
    # exterior void - a hollow part whose annular port mouths were not sealed - and the
    # delivered mesh contains a spurious outside region (jobs 95bd0197 and 0de57541
    # shipped 'outer' patches of 58,348 and 5,827 faces beside inlet/outlet/wall).
    # Recorded as a QUALITY KEY (not a criteria row: gate_declared_criteria fails closed
    # on any gating key absent from the quality report, so a new row would demand this
    # measurement of every snappy job, external aero included) and shouted in the builder
    # output - re-planning cannot fix a leak born in tessellation.
    internal_leak_msg = ""
    if internal_flow:
        from meshpipeline.engines.manifest import _patch_face_counts
        _staged = {Path(_s).stem for _s in _stls if Path(_s).exists()}
        _leaked = {n: c for n, c in _patch_face_counts(ws).items()
                   if c > 0 and n not in _staged} if _staged else {}
        if _leaked:
            q["internal_unexpected_patches"] = _leaked
            _detail = ", ".join(f"{n}={c}" for n, c in sorted(_leaked.items()))
            internal_leak_msg = (
                f" [INTERNAL_CARVE_LEAK] boundary patch(es) beyond the staged "
                f"inlet/outlet/wall set carry faces: {_detail} - the carve kept the "
                f"exterior void (a hollow part's port mouths were not sealed), so the "
                f"mesh includes a spurious outside region and is physically wrong for "
                f"internal flow. This is a geometry-prep defect, not a plan problem - "
                f"do not re-plan; the port STLs must seal the mouths.")
            logger.error("finalize: internal-flow mesh delivered unexpected boundary "
                         "patch(es) with faces (%s) - carve leaked to the exterior void",
                         _detail)
    # When an engine's review surface covers only the BODY (snappy: the far-field / symmetry
    # patches live in the blockMesh boundary, not the triSurface), declare the mesh's real
    # patches so the manifest matches the mesh AND the contract (the added ones carry no
    # review geometry). The engine declares this via review_surface_is_body_only.
    if getattr(R, "review_surface_is_body_only", False):
        for _pn in _polymesh_patch_names(ws):
            patch_entities.setdefault(_pn, [])
    # export the VOLUME mesh (foamToVTK) so the reviewer can slice it to see boundary
    # layers / internal refinement; body bbox from the input STL for inspection regions.
    volume_path = None
    try:
        volume_path = R.export_volume_vtk(ws)
    except Exception:
        logger.exception("Executor: foamToVTK volume export failed (non-fatal)")
    # The DELIVERABLE surface: the faces this run actually produced, converted from the VTK
    # boundary the export above leaves beside the volume. Distinct from the review mesh built
    # earlier, which re-exports the CAD the mesher snapped TO - the right thing for the reviewer to
    # navigate by, and the wrong thing to hand a user, who asked for the surface that was generated.
    try:
        from meshpipeline.engines.surface_deliverable import build_surface_msh
        build_surface_msh(ws)
    except Exception:
        logger.exception("Executor: surface deliverable export failed (non-fatal)")
    body_bbox = None
    try:
        _bi = R.inspect_stl(ws)
        if "bbox_min" in _bi and "bbox_max" in _bi:
            body_bbox = (_bi["bbox_min"], _bi["bbox_max"])
    except Exception:
        logger.exception("Executor: body bbox read failed (non-fatal)")
    patch_types = {p.get("name"): p.get("type")
                   for p in (intake_patches or []) if p.get("name")}
    if not patch_types and internal_flow:
        # internal flow: derive roles from the (deterministic) patch NAMES, not an external-aero
        # index fallback - that mislabels inlet→wall and calls the wall 'farfield' (there is no
        # farfield inside a cavity), corrupting the reviewer's + contract's view of the boundary.
        def _role(n: str) -> str:
            ln = n.lower()
            if "inlet" in ln:
                return "inlet"
            if "outlet" in ln:
                return "outlet"
            return "wall"
        patch_types = {n: _role(n) for n in patch_entities.keys()}
    elif not patch_types:
        patch_types = {n: ("wall" if i == 0 else "farfield")
                       for i, n in enumerate(patch_entities.keys())}
    # Synthetic thin/razor class patches ARE the wall - the layer policy split one declared wall
    # into class regions, and the manifest must role every one of them as wall (the index fallback
    # above would otherwise call them 'farfield' and the manifest-derived wall_faces would lie).
    _pol_regions = (q.get("layer_policy") or {}).get("region_patches") or {}
    for _rp in _pol_regions:
        if _rp in _folded:
            continue          # folded back into the declared wall by createPatch - not a patch here
        if _rp not in patch_types or patch_types.get(_rp) == "farfield":
            patch_types[_rp] = "wall"
    # SURFACE-CAPTURE ANCHOR - the OBJECTIVE 'surface capture' number (a vision reviewer
    # cannot read snap quality from a render; it confabulates 'good' from face counts or
    # 'staircased' from a blur). The ENGINE DECLARES which surfaces to compare - its snapped
    # wall vs its reference CAD - via an optional surface_capture_reference hook; the shared
    # metric just measures the deviation. Engines that do not body-fit a reference simply
    # don't define the hook, so there is NO engine-name list here (capability by declaration).
    _ref_fn = getattr(R, "surface_capture_reference", None)
    if callable(_ref_fn) and volume_path:
        try:
            from meshpipeline.cad.surface_checks import surface_deviation
            _pair = _ref_fn(ws, patch_types)
            if _pair:
                _sd = surface_deviation(*_pair)
                if _sd:
                    q["surface_deviation"] = _sd
        except Exception:
            logger.exception("Executor: surface-deviation metric failed (non-fatal)")
    # The far-field box the builder PREPARED (recorded by prepare_surface) -
    # the A1 domain-extent gate measures this, not the octree-padded mesh bounds.
    requested_box = None
    reference_length = None
    _gb = ws / "geom_box.json"
    if _gb.exists():
        try:
            import json as _json
            _d = _json.loads(_gb.read_text())
            requested_box = [_d["domain_min"], _d["domain_max"]]
            reference_length = _d.get("reference_length_m")
        except Exception:
            logger.warning("Executor: could not read geom_box.json (non-fatal)")
    # mesh_mode must record the engine that ACTUALLY built this mesh: the dispute
    # flow pins the rebuild engine from it, and the quality report evaluates the
    # engine's own criteria set. (It previously defaulted to "cfmesh" for every
    # engine - a snappy mesh's dispute would have rebuilt with the wrong mesher.)
    R.write_manifest(ws, patch_types=patch_types, patch_entities=patch_entities,
                     bbox=bbox, quality=q, domain=domain or "",
                     body_bbox=body_bbox, mesh_bounds=q.get("bounds"),
                     requested_box=requested_box, volume_path=volume_path,
                     reference_length=reference_length,
                     # STATED explicitly. Every engine receives prepared metre geometry and
                     # writes metres; the constant says so rather than a default assuming it.
                     mesh_units=COMPLETED_MESH_UNIT.value,
                     mesh_mode=getattr(R, "name", "") or engine or "cfmesh",
                     engine_params=engine_params or {}, flow_topology=flow_topology)
    fatal = q.get("fatal", [])
    out = (f"[CFMESH] polyMesh cells={q.get('cells')} fatal={fatal} "
           f"non_ortho={q.get('max_non_ortho')} skew={q.get('max_skewness')}"
           + internal_leak_msg)
    return {"success": not fatal, "output": out, "stdout": out, "stderr": ""}
