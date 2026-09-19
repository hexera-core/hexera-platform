# Responsibility: Expose the surface the browser viewer draws for a VMTK result, and the volume
# behind it so the viewer can colour that surface by cell quality.
# Boundaries: presentation of a delivered mesh; it judges nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_WALL_ID = 1   # vmtk convention: the non-cap boundary (the lumen wall)

# VTK cell type -> corner count, for the volume cells mesh.vtu may carry: tetrahedron, hexahedron,
# wedge and pyramid, and their quadratic forms (VTK lists the corner nodes first).
_VOLUME_CORNERS = {10: 4, 12: 8, 13: 6, 14: 5, 24: 4, 25: 8, 26: 6, 27: 5}


def _staged_cap_names(workspace, surf, ids) -> dict[int, str]:
    """entity id -> declared port name, for caps that sit on a staged port (nearest centroid,
    within the port's size); anything else keeps its cap_N name."""
    from meshpipeline.engines.vmtk.lumen_staging import read_staging
    staged = read_staging(workspace)
    ports = list((staged or {}).get("ports") or [])
    if not ports:
        return {}
    import numpy as np
    names: dict[int, str] = {}
    used: set[str] = set()
    for eid in sorted({int(v) for v in ids}):
        if eid in (_WALL_ID, 0):
            continue
        part = surf.extract_cells([i for i, v in enumerate(ids) if int(v) == eid])
        if part.n_points == 0:
            continue
        c = np.asarray(part.points, dtype=float).mean(axis=0)
        free = [p for p in ports if p["name"] not in used]
        if not free:
            break
        best = min(free, key=lambda p: float(np.linalg.norm(c - np.asarray(p["centroid"]))))
        if float(np.linalg.norm(c - np.asarray(best["centroid"]))) <= 0.6 * float(best["size_m"]):
            names[eid] = str(best["name"])
            used.add(str(best["name"]))
    return names


def read_vtu(workspace, *, named: bool = False) -> dict | None:
    """mesh.vtu as the viewer needs it: points (P,3), the volume cells by kind (corner nodes
    only), and the delivered boundary by vmtk entity id - wall + cap_N - as triangles of point
    indices, in the order the surface is drawn. With named=True the caps that sit on
    engine-staged ports carry the user's declared names instead (the viewer's roles are keyed by
    those names); the gate that counts caps reads the unnamed form. None when there is no mesh
    yet; {} when the file could not be read (logged)."""
    vtu = Path(workspace) / "mesh.vtu"
    if not vtu.exists() or vtu.stat().st_size == 0:
        return None
    try:
        import numpy as np
        import pyvista as pv
        grid = pv.read(str(vtu))
        points = np.asarray(grid.points, dtype=np.float64)
        cells: list = []
        for vtk_type, n_corners in _VOLUME_CORNERS.items():
            block = grid.cells_dict.get(vtk_type)
            if block is not None and len(block):
                cells.append(np.asarray(block, dtype=np.int64)[:, :n_corners])
        # The boundary as pyvista extracts it, each surface point traced back to the volume point
        # it came from, so every drawn triangle can be matched to the cell behind it.
        surf = grid.extract_surface(pass_pointid=True, pass_cellid=True).triangulate()
        if surf.n_cells == 0:
            return {"points": points, "cells": cells, "patches": []}
        original = np.asarray(surf.point_data["vtkOriginalPointIds"], dtype=np.int64)
        faces = np.asarray(surf.faces, dtype=np.int64).reshape(-1, 4)
        tris = original[faces[:, 1:4]]
        ids = surf.cell_data.get("CellEntityIds")
        if ids is None:
            return {"points": points, "cells": cells, "patches": [("wall", tris.tolist())]}
        ids = np.asarray(ids).astype(np.int64)
        cap_names = _staged_cap_names(workspace, surf, ids) if named else {}
        patches: list = []
        for eid in sorted({int(v) for v in ids}):
            name = "wall" if eid == _WALL_ID else cap_names.get(eid, f"cap_{eid}")
            sel = tris[ids == eid]
            if len(sel):
                patches.append((name, sel.tolist()))
        return {"points": points, "cells": cells, "patches": patches}
    except Exception as exc:  # noqa: BLE001 - the viewer degrades, it never fails a job
        logger.warning("vmtk viewer surface parse failed: %s", exc)
        return {}


def surface_patches_of(mesh: dict | None) -> dict[str, list]:
    """The drawn surface: patch name -> triangles as coordinate triples, in the order
    render/volume_quality counts them."""
    if not mesh:
        return {}
    points = mesh["points"]
    out: dict[str, list] = {}
    for name, polys in mesh["patches"]:
        tris: list = []
        for poly in polys:
            p = [tuple(float(c) for c in points[i]) for i in poly]
            for k in range(1, len(p) - 1):
                tris.append([p[0], p[k], p[k + 1]])
        if tris:
            out[name] = tris
    return out


def surface_patches(workspace, *, named: bool = False) -> dict[str, list]:
    return surface_patches_of(read_vtu(workspace, named=named))
