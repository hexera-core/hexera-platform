# Responsibility: Draw the delivered conjugate case in the viewer - the coolant and the metal, each patch under its own region.
# Boundaries: presentation only. It reads the two region meshes and judges nothing.
# Collaborates with: cfmesh_conjugate/polymesh_surface.py (reads), render/viewer_pack.py (packs), application/viewer_payload.py (calls it through the spec).
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION

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
    resp = polymesh_response(raw, cell_stats or None, merged_roles, units)
    if resp:
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
            attach_quality_fields(resp, ws / "constant" / FLUID_REGION / "polyMesh",
                                  non_ortho_limit=float(bar))
    except Exception:                      # noqa: BLE001 - a heatmap never costs the view
        logger.warning("conjugate viewer: no quality heatmap (non-fatal)", exc_info=True)


__all__ = ["conjugate_viewer"]
