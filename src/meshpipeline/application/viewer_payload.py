# Responsibility: Build the payload the browser viewer renders a delivered mesh from.
# Boundaries: assembled while the pipeline owns the workspace, so the API never reads a worker's filesystem.
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT, MeshUnitsError, completed_mesh_unit

logger = logging.getLogger(__name__)

#: The one logical artifact every viewer API route resolves.
VIEWER_LOGICAL_KEY = "viewer_data"


class ViewerPayloadError(RuntimeError):
    pass


def _attach_facts(resp: dict, manifest: dict) -> None:
    from meshpipeline.render.mesh_facts import parts as _parts
    from meshpipeline.render.mesh_facts import quality as _quality
    try:
        rendered = tuple(str(p.get("name")) for p in (resp.get("patches") or [])
                         if isinstance(p, dict) and p.get("name"))
        q = _quality(manifest) if resp.get("is_mesh") else {}
        if q:
            resp["quality"] = q
        resp["parts"] = _parts(manifest, rendered=rendered)
    except Exception as exc:      # a malformed manifest must not cost the user their mesh
        logger.warning("surface: mesh facts unavailable (%s)", exc)
        resp.setdefault("parts", [])


def _build_surface_payload(ws) -> dict | None:
    import json as _json

    from meshpipeline.cad.stl_io import read_stl_solids, read_stl_triangles
    from meshpipeline.render.viewer_pack import stl_response

    # A DELIVERED mesh is identified by its manifest existing. When it does, that manifest is the
    # authority and MUST state its unit - a completed artifact that cannot say what it is measured
    # in is an integrity failure, not something to label "m" and hope. When it does not exist, what
    # follows is the tessellated INPUT skin, which is not a completed mesh at all; staging has
    # already normalised it to metres, and it is marked `is_mesh: False` below.
    manifest_path = ws / "mesh_manifest.json"
    if manifest_path.exists():
        try:
            manifest = _json.loads(manifest_path.read_text())
        except Exception as exc:
            raise MeshUnitsError(
                "the delivered mesh manifest could not be read") from exc
        units = completed_mesh_unit(manifest).value
    else:
        manifest = {}
        units = COMPLETED_MESH_UNIT.value
    roles = manifest.get("patch_types", {}) or {}
    # The TRUE cell count. The viewer only ever sees the BOUNDARY surface, so it can
    # count faces and nothing else - it was labelling those faces "cells", which in a
    # system where cell count is a budget (CELL_HARD_LIMIT) is a lie the user could act
    # on. The manifest knows, and it is engine-neutral.
    cell_count = int(manifest.get("cell_count") or 0)
    # EMPTY by default: the payload carries the real mesh structure, outer boundary included.
    # Hiding a solid farfield is presentation, and the viewer owns it (it opens an enclosing
    # boundary hidden and translucent). The mechanism stays for engines that must drop
    # genuinely internal objects.
    _skip_names: tuple[str, ...] = ()

    # ENGINE-OWNED render surface (spec.viewer_surface): the engine renders its own
    # delivered mesh and returns the finished response, or None if it hasn't produced
    # one yet. No polyMesh/foam knowledge lives here.
    try:
        from meshpipeline.engines.registry import get_spec
        _hook = get_spec(manifest.get("mesh_mode", "")).viewer_surface
        if _hook is not None:
            resp = _hook(ws, roles=roles, units=units, skip_names=_skip_names)
            if resp:
                # THE ENGINE rendered its own DELIVERED mesh. That is the fact the
                # viewer needs - not the payload's format. (gmsh and vmtk deliver a
                # surface, the OpenFOAM engines a polyMesh; both are the real mesh.)
                resp["is_mesh"] = True
                resp["cell_count"] = cell_count
                _attach_facts(resp, manifest)
                return resp
    except Exception as exc:
        logger.warning("surface: engine viewer-surface hook failed (%s) - "
                       "falling back to input STL", exc)

    # ENGINE-NEUTRAL fallback: the tessellated INPUT skin staged in the workspace. This
    # used to name cfMesh's `geom.stl` and snappy's `constant/triSurface/` explicitly -
    # engine vocabulary in a shared layer, and it meant a gmsh or vmtk job whose viewer
    # hook came up empty fell all the way through to a 404. Any staged STL will do:
    # multi-solid files carry their own solid names, single-solid files their filename.
    patches: dict[str, list] = {}
    for stl in sorted(ws.rglob("*.stl")):
        solids = read_stl_solids(stl)
        if len(solids) > 1:                    # a named multi-solid file IS the patch set
            patches.update(solids)
        else:                                  # one solid per file → the file names it
            patches.setdefault(stl.stem, read_stl_triangles(stl))
    resp = stl_response(patches, roles, units, _skip_names)
    if resp is None:
        return None
    resp["is_mesh"] = False                    # this is the INPUT skin, and it says so
    resp["cell_count"] = 0                     # …so it has no cells to report
    # An input skin has no DELIVERED mesh to describe: no quality, and only the parts the
    # manifest already declares. Saying nothing is the truthful answer, not zeroes.
    _attach_facts(resp, manifest)
    return resp




def _quality_block(manifest: dict) -> dict:
    qc = manifest.get("quality_criteria")
    if not qc:
        # pre-registry mesh: evaluate now from the manifest's quality block
        from meshpipeline.engines.quality_criteria import evaluate as _qc_evaluate
        q = manifest.get("quality", {}) or {}
        mode = manifest.get("mesh_mode", "cfmesh")
        qc = {"engine": mode, "production_grade": None,
              "criteria": _qc_evaluate(mode, {
                  "fatal": q.get("fatal", []), "skew_fraction": q.get("skew_fraction"),
                  "max_non_ortho": q.get("max_non_ortho")})}
    # This block only ever describes a DELIVERED mesh, so the manifest must state its unit.
    return {"cell_count": manifest.get("cell_count"),
            "mesh_units": completed_mesh_unit(manifest).value, **qc}


def _last_review(ws: Path) -> dict:
    reviews = sorted(ws.glob("review_*/verdict.json"))
    if not reviews:
        return {}
    try:
        return json.loads(reviews[-1].read_text())
    except Exception:  # noqa: BLE001 - an unreadable verdict is simply absent evidence
        return {}


def build_viewer_payload(workspace: Path) -> dict[str, Any]:
    ws = Path(workspace)
    try:
        manifest = json.loads((ws / "mesh_manifest.json").read_text())
    except Exception as exc:  # noqa: BLE001
        raise ViewerPayloadError("mesh_manifest.json is missing or unreadable") from exc

    surface = _build_surface_payload(ws)
    if surface is None:
        raise ViewerPayloadError("no renderable surface in the workspace")
    return {
        "engine": str(manifest.get("mesh_mode", "") or ""),
        "mesh_available": True,
        "surface": surface,
        "quality": _quality_block(manifest),
        "review": _last_review(ws),
    }
