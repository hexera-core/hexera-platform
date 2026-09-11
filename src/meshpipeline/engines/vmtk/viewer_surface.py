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


def surface_patches(workspace, *, named: bool = False) -> dict[str, list]:
    """The delivered boundary by vmtk entity id: wall + cap_N. With named=True the caps that
    sit on engine-staged ports carry the user's declared names instead (the viewer's roles
    are keyed by those names); the gate that counts caps reads the unnamed form."""
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
        cap_names = _staged_cap_names(workspace, surf, ids) if named else {}
        for eid in sorted({int(v) for v in ids}):
            part = surf.extract_cells([i for i, v in enumerate(ids) if int(v) == eid])
            name = "wall" if eid == _WALL_ID else cap_names.get(eid, f"cap_{eid}")
            tris = _tris(part.extract_surface().triangulate())
            if tris:
                out[name] = tris
        return out
    except Exception as exc:  # noqa: BLE001 - the viewer degrades, it never fails a job
        logger.warning("vmtk viewer surface parse failed: %s", exc)
        return {}
