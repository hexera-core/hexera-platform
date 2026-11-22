# Responsibility: Write the boundary the mesher actually produced as a portable surface mesh.
# Boundaries: it converts what is already on disk into one file; it meshes nothing and renders nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SURFACE_MSH = "surface_mesh.msh"
#: The rule that reads the mesh on disk from the case written last (docs/RULES.md).
VER_NEWEST_CASE = "VER-8"


def _patch_files(ws: Path) -> list[Path]:
    """The .vtp files of ONE time directory - the latest.

    foamToVTK writes one .vtp per boundary patch under VTK/<case>_<time>/boundary/. Globbing across
    the tree would collect every time step it wrote, and since patches are named by file stem the
    same patch would then be added twice under one physical name - a delivered surface with the
    boundary duplicated on top of itself. Only the newest time directory describes the mesh as
    delivered, so take that one and nothing else.

    THE NEWEST CASE FIRST, THEN ITS LATEST TIME (VER-8). A pass the size brake stopped leaves its own
    VTK/<case>_0 beside the delivered one (snappy's runs are cr_ws_<random>), and every one is time 0: ordered
    by name, RM3's cr_ws_rm21egpm_0 (a stopped pass, far field only) sorted after the delivered cr_ws_9vz6v0eq_0
    and the CAD check read "the body was not read" off a 6 M-cell production-grade mesh (RM5: "100% of your CAD
    not reached"). The case whose files were written last is the mesh on disk.
    """
    dirs = {f.parent for f in ws.glob("VTK/**/boundary/*.vtp")}
    if not dirs:
        return []

    def _case(d: Path) -> str:
        return d.parent.name.rpartition("_")[0] or d.parent.name

    def _written(d: Path) -> float:
        return max((f.stat().st_mtime for f in d.glob("*.vtp")), default=0.0)

    newest: dict[str, float] = {}
    for d in dirs:
        newest[_case(d)] = max(newest.get(_case(d), 0.0), _written(d))
    case = max(newest, key=lambda c: (newest[c], c))
    mine = sorted((d for d in dirs if _case(d) == case), key=lambda d: (_time_index(d.parent.name), d.parent.name))
    return sorted(mine[-1].glob("*.vtp"))


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

    _pin_gmsh(gmsh)
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



def _pin_gmsh(gmsh) -> None:
    """One thread and a fixed seed (engines/determinism.py): threaded gmsh inserts points in the
    order threads finish, so the same surface could come out as a different staging."""
    from meshpipeline.engines.determinism import GMSH_RANDOM_SEED, GMSH_THREADS
    gmsh.option.setNumber("General.NumThreads", GMSH_THREADS)
    gmsh.option.setNumber("Mesh.RandomSeed", GMSH_RANDOM_SEED)


__all__ = ["SURFACE_MSH", "build_surface_msh"]
