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

#: What a poor-cell set is called in the viewer's part roster, and the patch type it wears. The
#: type is not an OpenFOAM patch type - there is no patch - it is what this surface IS, and the
#: roster shows it beside the regions so a reader can switch it on over either of them.
POOR_TYPE = "poorQuality"
#: A set past this many triangles is not drawn. The sets are the cells that FAILED a check, so on
#: a mesh worth delivering they are small by definition - 28,132 cells out of 102.6 million on the
#: first customer plate, 4 on a deliberately bad 4-cell test case. A set big enough to break the
#: budget is a mesh with nothing worth highlighting, because the highlight would be the mesh.
POOR_MAX_TRIANGLES = 400_000

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

    # THE POOR CELLS GO IN AFTER THE BUDGET, deliberately, so they are never merged. Corintis
    # asked for a "highlight of poor-quality cells" (2026-10-29) and the two mechanisms that
    # could have answered it both stop before the mesh they asked about: the per-face heatmap is
    # dropped once cells are merged for drawing (a colour on a merged face was measured on none
    # of the faces it stands for), and the measurement behind it refuses a mesh past
    # face_quality.MAX_FACES_FOR_FIELDS - 40 M faces, against this plate's 200 M. The sets are
    # the one answer that holds at any size: OpenFOAM wrote them in the container, on the mesh
    # itself, and a few thousand faces draw exactly whatever the rest of the mesh had to give up.
    raw += _poor_surfaces(ws)

    resp = polymesh_response(raw, cell_stats or None, merged_roles, units, reduced=reduced)
    if resp and not reduced:
        # THE HEATMAP IS PER FACE, so it describes the surface only while the faces are the ones
        # the mesher wrote. Once cells have been merged for drawing, one drawn face stands for
        # several delivered ones and a colour on it was measured on none of them. A mesh big
        # enough to need merging is also the one whose quality_fields pass reads 29 million points
        # and 73 million faces, so skipping it is not only the honest answer here.
        _paint(resp, ws)
    return resp


def _poor_surfaces(ws: Path) -> list[dict]:
    """Every poor-cell set checkMesh kept, as its own drawable surface.

    `checkMesh -writeSets vtk` writes one file per set it filled, at
    postProcessing/constant/<set>/<set>.vtp, and the engine's foam_exec.collect_poor_sets moves
    them under poor_cells/<region>/ so a later region's run cannot pick them up again. Measured in
    the mesh image on a 4-cell mesh of aspect ratio 2500: "Writing 4 cells with high aspect ratio
    to set highAspectRatioCells", then one .vtp of 18 points and 16 polys - already a SURFACE, the
    boundary of the cells that failed, which is the thing to draw.

    EMPTY ON A GOOD MESH, which is the common case and not a defect: a set holds the cells that
    failed a check, so a mesh that passes writes no files and this returns nothing.
    """
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate.foam_exec import poor_sets

    rows = poor_sets(ws)
    if not rows:
        return []
    try:
        import pyvista as pv
    except ImportError:                    # pragma: no cover - the worker has it; a probe may not
        logger.info("conjugate viewer: %d poor-cell set(s) kept but pyvista is absent", len(rows))
        return []
    out: list[dict] = []
    for row in rows:
        path = ws / str(row.get("path") or "")
        region = path.parent.name
        try:
            # ALREADY A SURFACE. `-writeSets` takes a surfaceFormat, so what lands is PolyData
            # (measured: NumberOfPoints='18' NumberOfPolys='16' on the 4-cell slab) and
            # extract_surface on PolyData is a pass that only asks pyvista which algorithm to
            # warn about. Anything else that ever lands here still gets converted.
            mesh = pv.read(path)
            if not isinstance(mesh, pv.PolyData):
                mesh = mesh.extract_surface()
            faces = np.asarray(mesh.faces, dtype=np.uint32)
            points = np.asarray(mesh.points, dtype=np.float32)
        except Exception:                  # noqa: BLE001 - a highlight never costs the view
            logger.warning("conjugate viewer: could not read the poor-cell set %s",
                           path.name, exc_info=True)
            continue
        if not mesh.n_cells or not mesh.n_points:
            continue
        if mesh.n_cells > POOR_MAX_TRIANGLES:
            logger.info("conjugate viewer: %s/%s is %d faces, past %d - not drawn",
                        region, row.get("name"), mesh.n_cells, POOR_MAX_TRIANGLES)
            continue
        out.append({
            # the set's own name, under its region, so the two regions' sets stay apart exactly
            # as their patches do
            "name": f"{region}/{row.get('name')}",
            "type": POOR_TYPE,
            "face_count": int(mesh.n_cells),
            "points": points.tobytes(),
            # [n, i0..i(n-1)] per face, which is pyvista's own face layout and the one
            # polymesh_surface writes
            "polys": faces.tobytes(),
        })
    if out:
        logger.info("conjugate viewer: %d poor-cell set(s) drawn - %s", len(out),
                    ", ".join(f"{d['name']} ({d['face_count']} faces)" for d in out))
    return out


def _paint(resp: dict, ws: Path) -> None:
    """The heatmap the viewer colours the boundary with, measured on BOTH regions' meshes.

    The bar it paints red is this engine's own non-orthogonality criterion, so the picture and the
    gate agree about what is bad.

    BOTH, because the metal is the worse region on a real part and it was never painted. This read
    the coolant alone, on the grounds that "the metal is mostly untouched background (1.21 degrees
    against the coolant's 27.99 on the unit cell), so painting the metal's numbers would show a
    flawless mesh either way". True of the unit cell. On the customer coupon the engine's own
    checkMesh says the opposite, in the same report:

        fluid   max non-ortho 15.82   avg 1.52   max skew 0.296   max aspect 1.61
        solid   max non-ortho 26.98   avg 2.03   max skew 0.514   max aspect 2.44

    The metal is worse on every one of those, and on the 336 M-cell plate it holds 27,932 of the
    28,132 poor cells. A reader who opens the heatmap and sees the coolant clean is being told the
    mesh is clean by the half of it that is.

    One response carries one `quality_fields`, so the two reads are merged: the per-patch arrays
    union (the names are region-prefixed, so nothing collides), the hotspots concatenate, and each
    metric keeps the WORSE max and the sum of what is over - the figure a reader wants from a
    two-region mesh is the two-region figure.
    """
    try:
        from meshpipeline.engines.openfoam_criteria import MAX_NON_ORTHO
        from meshpipeline.render.face_quality import attach_quality_fields
        bar = MAX_NON_ORTHO.threshold
        if not isinstance(bar, (int, float)):
            return
        merged: dict = {}
        for region in (FLUID_REGION, SOLID_REGION):
            polymesh = ws / "constant" / region / "polyMesh"
            if not (polymesh / "boundary").is_file():
                continue
            # THE NAMES THIS VIEWER DRAWS ARE NOT THE NAMES ON DISK. Every patch here is written
            # "<region>/<patch>" so the two regions' identically named block faces cannot replace
            # one another in the roster; the polyMesh knows only the bare name. Handing the
            # prefixed names to the quality reader matched nothing, so it returned None and this
            # engine has never drawn a heatmap - silently, because a missing heatmap is swallowed
            # by design. The map is the correspondence, stated once.
            prefix = f"{region}/"
            # A POOR-CELL SET IS NOT A PATCH. It wears a region prefix so the two regions' sets
            # stay apart, but the polyMesh has never heard of it and there is nothing to colour:
            # it already IS the highlight.
            mine = [q for q in (resp.get("patches") or [])
                    if isinstance(q, dict) and str(q.get("name", "")).startswith(prefix)
                    and q.get("type") != POOR_TYPE]
            if not mine:
                continue
            alias = {str(q["name"]): str(q["name"])[len(prefix):] for q in mine}
            # ONLY THIS REGION'S PATCHES GO IN. The other region's names would travel unaliased
            # and be looked for, by their prefixed names, in a polyMesh that has never heard of
            # them - harmless, but it asks a reader of this code to work that out.
            one: dict = {"patches": mine}
            attach_quality_fields(one, polymesh, non_ortho_limit=float(bar), patch_of=alias)
            _merge_quality(merged, one.get("quality_fields"))
        if merged:
            resp["quality_fields"] = merged
    except Exception:                      # noqa: BLE001 - a heatmap never costs the view
        logger.warning("conjugate viewer: no quality heatmap (non-fatal)", exc_info=True)


#: Per metric, how the two regions' figures become one. `limit` is the same bar on both sides, so
#: it is taken as it comes; `floor` and `scale_to` span the viewer's colours and have to cover
#: both.
_WORSE = ("max", "scale_to")
_SUMMED = ("n_over", "n_faces")


def _merge_quality(into: dict, one: dict | None) -> None:
    """Fold one region's quality fields into the response's, in place."""
    if not isinstance(one, dict):
        return
    if not into:
        into.update({k: v for k, v in one.items() if k not in ("patches", "hotspots", "metrics")})
        into["patches"] = dict(one.get("patches") or {})
        into["hotspots"] = list(one.get("hotspots") or [])
        into["metrics"] = {k: dict(v) for k, v in (one.get("metrics") or {}).items()
                           if isinstance(v, dict)}
        return
    into["patches"].update(one.get("patches") or {})
    into["hotspots"].extend(one.get("hotspots") or [])
    for name, m in (one.get("metrics") or {}).items():
        if not isinstance(m, dict):
            continue
        have = into["metrics"].get(name)
        if not isinstance(have, dict):
            into["metrics"][name] = dict(m)
            continue
        for k in _WORSE:
            if k in m:
                have[k] = max(float(have.get(k) or 0.0), float(m[k]))
        for k in _SUMMED:
            if k in m:
                have[k] = int(have.get(k) or 0) + int(m[k])


__all__ = ["conjugate_viewer"]
