# Responsibility: Decide whether a cfMesh run actually delivered, and record what it produced.
# Boundaries: delivery means every required member of the declared bundle is present.
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
from meshpipeline.engines.cfmesh.deliverable import (
    boundary_patch_names,
    delivery_problem,
)

logger = logging.getLogger(__name__)


def finalize(workspace_dir: str, intake_patches: list, engine: str, domain: str = "",
                     internal_flow: bool = False, engine_params: dict | None = None,
                     flow_topology: str = "") -> dict:
    from meshpipeline.engines.runtime import get_engine
    R = get_engine(engine)
    ws = Path(workspace_dir)
    # THE DELIVERABLE GATE. `owner` alone was the old test and it let a partial mesh through:
    # cartesianMesh writes incrementally, so a run killed by a timeout, an OOM or a cancellation
    # routinely leaves an `owner` beside nothing else. checkMesh cannot read such a case, so it
    # reports no fatal classes - and `success = not fatal` then read "no fatal classes" as a good
    # mesh, wrote the manifest and sent the case on to delivery.
    incomplete = delivery_problem(ws)
    if incomplete:
        return {"success": False, "stdout": "", "stderr": "", "output": incomplete}
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
                if _lc.get("overall_pct") is not None:
                    q["layer_coverage_pct"] = _lc["overall_pct"]
                elif _walls:
                    q["layer_coverage_pct"] = min(v["coverage_pct"] for v in _walls.values())
                if _walls:
                    q["per_patch_layers"] = {
                        n: {"layers": v.get("layers"), "target": v.get("layers_target"),
                            "coverage_pct": v.get("coverage_pct")} for n, v in _walls.items()}
            except Exception:
                logger.exception("finalize: layer-coverage parse failed (non-fatal)")
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
    if _solids:
        _review_tris = _solids
        try:
            patch_entities, bbox = R.build_review_msh(ws, _solids)
        except Exception:
            logger.exception("Executor: review-mesh build failed (non-fatal)")
    # When an engine's review surface covers only the BODY (snappy: the far-field / symmetry
    # patches live in the blockMesh boundary, not the triSurface), declare the mesh's real
    # patches so the manifest matches the mesh AND the contract (the added ones carry no
    # review geometry). The engine declares this via review_surface_is_body_only.
    if getattr(R, "review_surface_is_body_only", False):
        for _pn in boundary_patch_names(ws):
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
    # the domain-extent gate measures this, not the octree-padded mesh bounds.
    requested_box = None
    _gb = ws / "geom_box.json"
    if _gb.exists():
        try:
            import json as _json
            _d = _json.loads(_gb.read_text())
            requested_box = [_d["domain_min"], _d["domain_max"]]
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
                     # STATED explicitly. Every engine receives prepared metre geometry and
                     # writes metres; the constant says so rather than a default assuming it.
                     mesh_units=COMPLETED_MESH_UNIT.value,
                     mesh_mode=getattr(R, "name", "") or engine or "cfmesh",
                     engine_params=engine_params or {}, flow_topology=flow_topology)
    fatal = q.get("fatal", [])
    out = (f"[CFMESH] polyMesh cells={q.get('cells')} fatal={fatal} "
           f"non_ortho={q.get('max_non_ortho')} skew={q.get('max_skewness')}")
    return {"success": not fatal, "output": out, "stdout": out, "stderr": ""}
