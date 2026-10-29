# Responsibility: Draw the delivered conjugate case in the viewer - the coolant and the metal, each patch under its own region.
# Boundaries: presentation only. It reads the two region meshes and judges nothing.
# Collaborates with: cfmesh_conjugate/polymesh_surface.py (reads), render/viewer_pack.py (packs), application/viewer_payload.py (calls it through the spec).
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    FLUID_REGION,
    SOLID_REGION,
    SOLID_TO_FLUID,
)

logger = logging.getLogger(__name__)

#: The coupling's own two patches. The two are the SAME surface - measured at 93,600 faces on each
#: side of the unit cell - so only the coolant's side is drawn: drawing both doubles the geometry
#: for a view that is already on screen.
_SKIP = ("solid_to_fluid",)


def conjugate_viewer(workspace, *, roles: dict, units: str,
                     skip_names: tuple[str, ...] = ()) -> dict | None:
    """Both regions' boundaries as one payload, or None when no split mesh was written.

    EVERY PATCH UNDER ITS REGION, as "fluid/coolant_in" and "solid/ymin". Two reasons, and the
    first is correctness: both regions are cut from the same block, so both own a patch called
    `ymin` and both own one called `wall`. Drawn under their bare names one would silently
    replace the other in the viewer's part roster, and which one survived would depend on the
    order the directories happened to be read in.

    The second is that the region is the thing a reader of a conjugate mesh wants to pick apart:
    the question is always whether the coolant is where the coolant should be and the metal is
    everywhere else, and that is answered by isolating one of them.

    constant/polyMesh is NOT read. splitMeshRegions leaves the pre-split mesh in place at the
    root, carrying every patch with the two regions' face counts added together (ymin: 532 at the
    root, which is the fluid's 128 plus the solid's 404), so a viewer pointed there draws a mesh
    that was never delivered and shows no split at all.
    """
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import boundary_surface
    from meshpipeline.render.viewer_pack import polymesh_response

    ws = Path(workspace)
    raw: list[dict] = []
    merged_roles = dict(roles or {})
    stats: list[dict] = []
    for region in (FLUID_REGION, SOLID_REGION):
        polymesh = ws / "constant" / region / "polyMesh"
        if not (polymesh / "boundary").is_file():
            continue
        try:
            patches, cell_stats = boundary_surface(
                polymesh, skip_names=tuple(skip_names) + _SKIP)
        except (OSError, ValueError):
            logger.warning("conjugate viewer: %s has no readable mesh", region, exc_info=True)
            continue
        for p in patches:
            bare = str(p["name"])
            p["name"] = f"{region}/{bare}"
            merged_roles[p["name"]] = (roles or {}).get(bare, p["type"])
            # THE METAL'S OUTSIDE WRAPS EVERYTHING. A cooled solid is a block with coolant inside
            # it, so the solid region's every patch except the coupling is a face of that block,
            # and a viewer that opens them shown draws six grey rectangles with the part of
            # interest sealed inside. `encloses_domain` is how an engine says this: render/
            # mesh_facts opens such a part HIDDEN and the viewer re-frames on what is left, which
            # on this engine is the coolant. Without it the first thing a reader sees of a
            # conjugate mesh is the one surface that tells them nothing.
            if region == SOLID_REGION and bare != SOLID_TO_FLUID:
                p["encloses_domain"] = True
        raw += patches
        if cell_stats:
            stats.append(cell_stats)
    if not raw:
        return None

    # THE FINER OF THE TWO REGIONS sets the scale. The viewer uses these to pick a size legend,
    # and the coolant is both the finer region and the one the resolution was chosen for, so a
    # legend drawn off the metal's coarse interior would describe neither region's cells.
    cell_stats = None
    if stats:
        cell_stats = {k: min(float(s[k]) for s in stats if s.get(k) is not None)
                      for k in ("p10", "typ") if any(s.get(k) is not None for s in stats)}
    # WHAT A BROWSER CAN ACTUALLY CARRY. Packed whole, the first real customer cold plate's
    # coolant side is 13,022,400 faces: 416.8 MB raw, 555.8 MB of base64, measured on the
    # delivered workspace on 2026-10-28. That is past the 512 MiB `sandbox.safe_extract` puts on a
    # workspace archive before a browser has been asked to parse a byte of it, and 32x the
    # 400,000-triangle budget the INPUT skin path has considered browser-safe all along. The
    # delivered path had no budget of any kind. `fit_to_budget` merges cells FOR DRAWING and
    # returns the record that says so; nothing about the mesh on disk changes.
    from meshpipeline.render.polymesh_budget import fit_to_budget
    raw, reduced = fit_to_budget(raw)

    resp = polymesh_response(raw, cell_stats or None, merged_roles, units, reduced=reduced)
    if resp and not reduced:
        # THE HEATMAP IS PER FACE, so it describes the surface only while the faces are the ones
        # the mesher wrote. Once cells have been merged for drawing, one drawn face stands for
        # several delivered ones and a colour on it was measured on none of them. A mesh big
        # enough to need merging is also the one whose quality_fields pass reads 29 million points
        # and 73 million faces, so skipping it is not only the honest answer here.
        _paint(resp, ws)
    return resp


def _paint(resp: dict, ws: Path) -> None:
    """The heatmap the viewer colours the boundary with, measured on the COOLANT's mesh.

    The bar it paints red is this engine's own non-orthogonality criterion, so the picture and the
    gate agree about what is bad. Measured on the fluid region because that is where the cut cells
    are: the metal is mostly untouched background (1.21 degrees against the coolant's 27.99 on the
    unit cell), so painting the metal's numbers would show a flawless mesh either way.
    """
    try:
        from meshpipeline.engines.openfoam_criteria import MAX_NON_ORTHO
        from meshpipeline.render.face_quality import attach_quality_fields
        bar = MAX_NON_ORTHO.threshold
        if isinstance(bar, (int, float)):
            # THE NAMES THIS VIEWER DRAWS ARE NOT THE NAMES ON DISK. Every patch here is written
            # "<region>/<patch>" so the two regions' identically named block faces cannot replace
            # one another in the roster; the polyMesh knows only the bare name. Handing the
            # prefixed names to the quality reader matched nothing, so it returned None and this
            # engine has never drawn a heatmap - silently, because a missing heatmap is swallowed
            # by design. The map is the correspondence, stated once.
            prefix = f"{FLUID_REGION}/"
            alias = {str(p["name"]): str(p["name"])[len(prefix):]
                     for p in (resp.get("patches") or [])
                     if isinstance(p, dict) and str(p.get("name", "")).startswith(prefix)}
            attach_quality_fields(resp, ws / "constant" / FLUID_REGION / "polyMesh",
                                  non_ortho_limit=float(bar), patch_of=alias)
    except Exception:                      # noqa: BLE001 - a heatmap never costs the view
        logger.warning("conjugate viewer: no quality heatmap (non-fatal)", exc_info=True)


__all__ = ["conjugate_viewer"]
