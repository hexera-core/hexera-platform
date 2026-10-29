# Responsibility: Build the two things a conjugate review opens - one msh carrying both regions' boundaries, and the coolant region's volume.
# Boundaries: conversion only. It reads what the run left on disk and writes a review aid; it never fails a mesh.
# Collaborates with: cfmesh_conjugate/finalize.py (which calls both) and sandbox/mesh_reader.py (which reads the msh).
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    FLUID_REGION,
    SOLID_REGION,
    SOLID_TO_FLUID,
)
from meshpipeline.engines.cfmesh_conjugate.foam_exec import prefer_finished

logger = logging.getLogger(__name__)

#: What finalize publishes as mesh_paths.surface. The shared manifest writer hardcodes this name,
#: so a run that does not write it raises EVIDENCE_MISSING at the review before any renderer
#: starts - which a live two-solid multi-region run hit after passing every executor gate.
REVIEW_MSH = "mesh.msh"
#: Where foamToVTK puts a region's boundary patches and its cells.
_BOUNDARY_GLOB = "boundary/*.vtp"
_INTERNAL = "internal.vtu"


def _time_index(case_dir_name: str) -> int:
    """foamToVTK names a time directory "<case>_<n>", not "<n>".

    Measured: a run in runs/review wrote VTK/fluid/review_0/, so parsing the whole name as a
    number found no time directory at all and both builders returned empty on a mesh that was
    there. The index is sorted numerically so _10 lands after _9 rather than before it. Same
    reading as engines/surface_deliverable._time_index, which this had got wrong.
    """
    _, _, tail = case_dir_name.rpartition("_")
    try:
        return int(tail)
    except ValueError:
        return -1


def _newest_time(region_dir: Path) -> Path | None:
    """The latest FINISHED time directory foamToVTK wrote for a region.

    Only one time describes the mesh as delivered. Globbing across the tree would collect every
    step it wrote, and since patches are named by file stem the same patch would be added twice
    under one region - a surface with the boundary laid on top of itself.

    FINISHED, because "the newest" on its own read a truncated export as the mesh. A live run
    delivered two fluid times: constant (the mesh, 70,361-byte inlet.vtp, complete) and 0 (the
    blank solver fields, 65,539 bytes, stopping mid-base64 with no closing tag). This picked the
    second, pv.read raised "Error parsing input file", the loop in build_region_msh logged it and
    moved on, and the review opened with patches: ['solid'] - no coolant at all - on a mesh whose
    fluid was there and correct. foam_exec.NO_FIELD_TIME stops that export being attempted;
    prefer_finished is what stops a half-written one being believed, whatever wrote it - and it
    prefers rather than requires, so a missing marker can never cost a surface that is readable.
    """
    times = prefer_finished([p for p in region_dir.iterdir() if p.is_dir()])
    if not times:
        return None
    return max(times, key=lambda d: (_time_index(d.name), d.name))


def delivered_regions(ws: Path) -> list[str]:
    """The regions the MESH has, read off the delivered polyMesh rather than the VTK export.

    The export is what fails; the polyMesh is what was built. Asking the export which regions
    exist is asking the thing that went wrong to grade itself.
    """
    root = ws / "constant"
    return [d.name for d in sorted(root.glob("*"))
            if d.is_dir() and d.name != "polyMesh" and (d / "polyMesh" / "boundary").is_file()]


def _patch_files(ws: Path, region: str) -> list[Path]:
    """Every boundary patch of one region, in a stable order.

    SOLID_TO_FLUID is skipped on purpose. It is the same surface as the fluid side's interface -
    measured at 93,600 faces on both - so carrying both doubles the interface geometry in the file
    for a view the reviewer already has from the coolant's side.
    """
    region_dir = ws / "VTK" / region
    if not region_dir.is_dir():
        return []
    latest = _newest_time(region_dir)
    if latest is None:
        return []
    return sorted(p for p in latest.glob(_BOUNDARY_GLOB) if p.stem != SOLID_TO_FLUID)


def build_region_msh(workspace) -> tuple[dict[str, list[int]], tuple | None]:
    """Both regions' delivered boundaries as one msh, a physical group per REGION.

    ONE GROUP PER REGION, not per patch, which is the difference from the single-region engines.
    The reviewer's question here is whether the metal and the coolant are the two bodies the part
    actually has and whether they share their interface; the patch names are still on the
    manifest, and are what the patch targets walk.

    EVERY ELEMENT IS A TRIANGLE, which is not an aesthetic choice. sandbox/mesh_reader.py prefixes
    each face block with a corner-count column and then np.vstack()s the blocks, so a group
    holding both quadrangles and pentagons raises ValueError and the review never opens. Measured
    on a real two-region case: the mixed-type file is 28,781,401 bytes and read_mesh FAILS; all
    triangles is 35,654,271 bytes, loads in 0.8 s, and read_mesh reports the physical groups
    [('fluid', (1,)), ('solid', (2,))]. The extra 24% on disk buys a file that opens.

    Returns ({region: [entity tags]}, the model's bounding box), or ({}, None) when there is no
    VTK boundary to convert - a non-fatal state, as everywhere else in this module.
    """
    ws = Path(workspace)
    per_region = {r: _patch_files(ws, r) for r in (FLUID_REGION, SOLID_REGION)}
    if not any(per_region.values()):
        logger.info("conjugate review: no VTK boundary beside the mesh, nothing to convert")
        return {}, None

    import gmsh
    import pyvista as pv

    gmsh.initialize(interruptible=False)          # worker-thread safe
    _pin_gmsh(gmsh)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("conjugate")
        groups: dict[str, list[int]] = {}
        tag = 0
        for region, files in per_region.items():
            tags: list[int] = []
            for path in files:
                try:
                    mesh = pv.read(path)
                except Exception:
                    logger.exception("conjugate review: could not read %s", path.name)
                    continue
                if mesh.n_cells == 0 or mesh.n_points == 0:
                    continue
                tag += 1
                # Node tags are global in gmsh, so each patch gets its own block; points stay
                # SHARED within a patch, which is what keeps this a fraction of the size it would
                # be with three corners per face.
                base = tag * 100_000_000
                gmsh.model.addDiscreteEntity(2, tag)
                gmsh.model.mesh.addNodes(
                    2, tag, list(range(base + 1, base + 1 + mesh.n_points)),
                    mesh.points.reshape(-1).tolist())
                tris = _triangles(mesh.faces, base)
                if not tris:
                    continue
                gmsh.model.mesh.addElementsByType(tag, 2, [], tris)
                tags.append(tag)
            if not tags:
                # LOUD, and naming the region. A dropped region used to leave no trace here at
                # all: the fluid's patches failed to read, this continued, and the only sign was
                # the reviewer refusing two region targets it could not find. Nothing in the log
                # an operator reads said the word "fluid". The mesh stands - this module never
                # fails one - but the review will be inconclusive and the reason belongs here.
                logger.error("conjugate review: region %r contributed no readable surface from "
                             "%d boundary file(s) - the review will not see it: %s",
                             region, len(files), ", ".join(f.name for f in files) or "(none)")
                continue
            group = gmsh.model.addPhysicalGroup(2, tags)
            gmsh.model.setPhysicalName(2, group, region)
            groups[region] = tags
        if not groups:
            return {}, None
        # HALF A SURFACE IS NOT THE SURFACE, and publishing it anyway is how a conjugate mesh
        # came back PASSED on a review that never saw its coolant. Measured on two live runs of
        # the same coupon: the fluid's .vtp files failed to parse, this published a mesh.msh
        # holding the solid alone, and the reviewer opened "patches: ['solid']". The second of
        # them returned verdict=passed with concerns=[], because every gate between here and the
        # verdict tests presence and not substance - the patch obligation asks for one patch of
        # any kind, so one inspection of one region's boundary discharges "every renderable
        # boundary patch".
        #
        # The regions are read off the DELIVERED polyMesh, not off the export: the export is the
        # thing that failed, and asking it which regions exist is asking it to grade itself. A
        # workspace with no region directories at all - every unit fixture that fakes a VTK tree
        # - names no required regions and is published as before.
        want = delivered_regions(ws)
        missing = [r for r in want if r not in groups]
        if missing:
            logger.error("conjugate review: the mesh has %d region(s) (%s) and the surface would "
                         "carry %d (%s), so it is NOT published - a review of half a conjugate "
                         "mesh reads as a review of all of it", len(want), ", ".join(want),
                         len(groups), ", ".join(sorted(groups)))
            return {}, None
        box = gmsh.model.getBoundingBox(-1, -1)
        gmsh.write(str(ws / REVIEW_MSH))
        return groups, tuple(box)
    except Exception:
        logger.exception("conjugate review: conversion failed (non-fatal)")
        return {}, None
    finally:
        gmsh.finalize()


def _triangles(faces, base: int) -> list[int]:
    """Every face of a pyvista polygon array as triangles, with gmsh's node tags.

    A quadrangle is split rather than written as one, and a face with more sides is fanned. See
    build_region_msh: one physical group may not hold two corner counts.
    """
    out: list[int] = []
    i = 0
    while i < len(faces):
        n = int(faces[i])
        poly = faces[i + 1:i + 1 + n]
        i += n + 1
        if n < 3:
            continue
        for k in range(1, n - 1):
            out.extend((base + 1 + int(poly[0]), base + 1 + int(poly[k]),
                        base + 1 + int(poly[k + 1])))
    return out


def review_volume(workspace) -> str | None:
    """The COOLANT region's cells, as a workspace-relative path, or None.

    The fluid region explicitly, never VTK/case_0/internal.vtu. That merged file is the whole
    block as one body, which is the very thing a conjugate review has to be able to tell a real
    split apart from - and with both exports on disk a glob returns the fluid's only by
    alphabetical luck.
    """
    ws = Path(workspace)
    region_dir = ws / "VTK" / FLUID_REGION
    if not region_dir.is_dir():
        return None
    latest = _newest_time(region_dir)
    if latest is None:
        return None
    hit = latest / _INTERNAL
    return hit.relative_to(ws).as_posix() if hit.is_file() else None


def _pin_gmsh(gmsh) -> None:
    """One thread and a fixed seed (engines/determinism.py): threaded gmsh inserts points in the
    order threads finish, so the same surface could come out as a different staging."""
    from meshpipeline.engines.determinism import GMSH_RANDOM_SEED, GMSH_THREADS
    gmsh.option.setNumber("General.NumThreads", GMSH_THREADS)
    gmsh.option.setNumber("Mesh.RandomSeed", GMSH_RANDOM_SEED)
