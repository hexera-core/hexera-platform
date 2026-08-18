# Responsibility: Expose the surface the browser viewer draws for a VMTK result.
# Boundaries: presentation of a delivered mesh; it judges nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_WALL_ID = 1   # vmtk convention: the non-cap boundary (the lumen wall)


def _tris(poly) -> list:
    pts = poly.points
    out: list = []
    faces = poly.faces.reshape(-1, 4) if poly.n_cells else []
    for f in faces:
        if f[0] != 3:
            continue
        out.append([tuple(float(c) for c in pts[f[k + 1]]) for k in range(3)])
    return out


def surface_patches(workspace) -> dict[str, list]:
    vtu = Path(workspace) / "mesh.vtu"
    if not vtu.exists() or vtu.stat().st_size == 0:
        return {}
    try:
        import pyvista as pv
        surf = pv.read(str(vtu)).extract_surface().triangulate()
        ids = surf.cell_data.get("CellEntityIds")
        if ids is None:
            return {"wall": _tris(surf)}
        out: dict[str, list] = {}
        for eid in sorted({int(v) for v in ids}):
            part = surf.extract_cells([i for i, v in enumerate(ids) if int(v) == eid])
            name = "wall" if eid == _WALL_ID else f"cap_{eid}"
            tris = _tris(part.extract_surface().triangulate())
            if tris:
                out[name] = tris
        return out
    except Exception as exc:  # noqa: BLE001 - the viewer degrades, it never fails a job
        logger.warning("vmtk viewer surface parse failed: %s", exc)
        return {}
