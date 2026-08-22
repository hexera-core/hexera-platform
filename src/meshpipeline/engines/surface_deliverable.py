# Responsibility: Write the boundary the mesher actually produced as a portable surface mesh.
# Boundaries: it converts what is already on disk into one file; it meshes nothing and renders nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SURFACE_MSH = "surface_mesh.msh"


def _patch_files(ws: Path) -> list[Path]:
    """The .vtp files of ONE time directory - the latest.

    foamToVTK writes one .vtp per boundary patch under VTK/<case>_<time>/boundary/. Globbing across
    the tree would collect every time step it wrote, and since patches are named by file stem the
    same patch would then be added twice under one physical name - a delivered surface with the
    boundary duplicated on top of itself. Only the newest time directory describes the mesh as
    delivered, so take that one and nothing else.
    """
    dirs = sorted({f.parent for f in ws.glob("VTK/**/boundary/*.vtp")},
                  key=lambda d: (_time_index(d.parent.name), d.parent.name))
    return sorted(dirs[-1].glob("*.vtp")) if dirs else []


def _time_index(case_dir_name: str) -> int:
    # "<case>_<n>" - sort on n numerically so _10 lands after _9, not before it.
    _, _, tail = case_dir_name.rpartition("_")
    try:
        return int(tail)
    except ValueError:
        return -1


def build_surface_msh(workspace) -> str | None:
    """The faces the run generated, named by patch - the surface a user asked for.

    Deliberately NOT build_review_msh. That one re-exports the CAD the mesher snapped TO, because
    the reviewer navigates by the body, and it is the wrong thing to hand a customer: a 3.3M-cell
    NACA 0012 run shipped a 232-facet copy of the customer's own upload, at 1/735th the resolution
    of the surface it had just produced. The real boundary is already on disk, so this converts it
    rather than recomputing anything.

    Faces are written as they are - snappyHexMesh boundaries are mostly quadrangles, which the Gmsh
    format holds natively, so triangulating would double the element count for nothing. Returns the
    workspace-relative name, or None when there is no VTK boundary to convert (a non-fatal state:
    the bundle still carries the solver mesh).
    """
    ws = Path(workspace)
    files = _patch_files(ws)
    if not files:
        logger.info("surface deliverable: no VTK boundary beside the mesh, nothing to convert")
        return None

    import gmsh
    import pyvista as pv

    gmsh.initialize(interruptible=False)          # worker-thread safe
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("surface")
        written = 0
        for idx, path in enumerate(files, start=1):
            try:
                mesh = pv.read(path)
            except Exception:
                logger.exception("surface deliverable: could not read %s", path.name)
                continue
            if mesh.n_cells == 0 or mesh.n_points == 0:
                continue
            # Node tags are global in gmsh, so each patch gets its own block. Points stay SHARED
            # within a patch, which is what keeps this file a fraction of the size it would be if
            # every face carried its own three corners.
            base = idx * 100_000_000
            gmsh.model.addDiscreteEntity(2, idx)
            gmsh.model.mesh.addNodes(
                2, idx, list(range(base + 1, base + 1 + mesh.n_points)),
                mesh.points.reshape(-1).tolist())

            tris: list[int] = []
            quads: list[int] = []
            faces = mesh.faces
            i = 0
            while i < len(faces):
                n = int(faces[i])
                poly = faces[i + 1:i + 1 + n]
                i += n + 1
                if n == 3:
                    tris.extend(base + 1 + int(v) for v in poly)
                elif n == 4:
                    quads.extend(base + 1 + int(v) for v in poly)
                else:
                    # A boundary face may have more than four sides where refinement levels meet.
                    # Fan it rather than skipping it: a hole in the delivered surface is worse than
                    # a face split into triangles.
                    for k in range(1, n - 1):
                        tris.extend((base + 1 + int(poly[0]), base + 1 + int(poly[k]),
                                     base + 1 + int(poly[k + 1])))
            if tris:
                gmsh.model.mesh.addElementsByType(idx, 2, [], tris)
            if quads:
                gmsh.model.mesh.addElementsByType(idx, 3, [], quads)
            group = gmsh.model.addPhysicalGroup(2, [idx])
            gmsh.model.setPhysicalName(2, group, path.stem)
            written += 1

        if not written:
            return None
        gmsh.write(str(ws / SURFACE_MSH))
        return SURFACE_MSH
    except Exception:
        logger.exception("surface deliverable: conversion failed (non-fatal)")
        return None
    finally:
        gmsh.finalize()


__all__ = ["SURFACE_MSH", "build_surface_msh"]
