# Responsibility: The delivered snap-grid mesh as the viewer draws it: every part's meshed surface under its region name, and the domain's sides.
# Owns: the viewer file the mesher writes beside the case (VIEWER_NAME) and the read of it into the viewer response.
# Boundaries: presentation only; it reads no polyMesh (the mesh is binary and split into regions) - the mesher writes the surfaces it already holds.
# Collaborates with: engines/snapgrid/mesher.py (writes), application/viewer_payload.py (calls the spec's viewer_surface), render/viewer_pack.py (packs).
from __future__ import annotations

from pathlib import Path

import numpy as np

#: The file beside the case holding the surfaces the viewer draws.
VIEWER_NAME = "snapgrid_viewer.npz"


def write_viewer(case: Path, *, points: np.ndarray, keys: np.ndarray, topo, cell_zone: np.ndarray,
                 zone_names: list[str], fluid: set[str]) -> dict:
    """Each solid region's whole boundary (its interfaces and any domain side it touches) as one
    patch under its name, and each fluid region's domain sides - the faces as meshed (snapped
    round parts included). Returns {name: faces} written."""
    ids_i = np.searchsorted(keys, topo.face_pts)
    ids_b = np.searchsorted(keys, topo.b_pts)
    zo, zn = cell_zone[topo.owner], cell_zone[topo.neighbour]
    zb = cell_zone[topo.b_owner]
    iface = np.flatnonzero(zo != zn)
    sizes_i = np.diff(topo.face_off)
    sizes_b = np.diff(topo.b_off)
    from meshpipeline.engines.snapgrid.curved import face_geometry
    area_i = np.linalg.norm(face_geometry(points, topo.face_off, ids_i)[0], axis=1)
    area_b = np.linalg.norm(face_geometry(points, topo.b_off, ids_b)[0], axis=1)
    data: dict = {}
    names: list[str] = []
    types: list[str] = []
    counts: list[int] = []
    areas_all: list[np.ndarray] = []
    for z, name in enumerate(zone_names):
        faces_i = iface[(zo[iface] == z) | (zn[iface] == z)] if name not in fluid else \
            np.zeros(0, dtype=np.int64)
        faces_b = np.flatnonzero(zb == z)
        loops = [ids_i[topo.face_off[f]:topo.face_off[f + 1]] for f in faces_i.tolist()]
        loops += [ids_b[topo.b_off[f]:topo.b_off[f + 1]] for f in faces_b.tolist()]
        if not loops:
            continue
        sz = np.concatenate([sizes_i[faces_i], sizes_b[faces_b]])
        flat = np.concatenate(loops)
        uniq, inv = np.unique(flat, return_inverse=True)
        polys = np.empty(len(flat) + len(sz), dtype=np.uint32)
        starts = np.concatenate(([0], np.cumsum(sz + 1)[:-1]))
        polys[starts] = sz
        mask = np.ones(len(polys), dtype=bool)
        mask[starts] = False
        polys[mask] = inv
        k = len(names)
        data[f"p{k}"] = points[uniq].astype(np.float32)
        data[f"f{k}"] = polys
        names.append(name)
        types.append("patch" if name in fluid else "wall")
        counts.append(len(sz))
        areas_all.append(np.sqrt(np.concatenate([area_i[faces_i], area_b[faces_b]])))
    sizes_all = np.concatenate(areas_all) if areas_all else np.zeros(0)
    np.savez_compressed(Path(case) / VIEWER_NAME, names=np.array(names), types=np.array(types),
                        counts=np.array(counts, dtype=np.int64),
                        cell_size=sizes_all.astype(np.float32), **data)
    return dict(zip(names, counts))


def snapgrid_viewer(workspace, *, roles: dict, units: str,
                    skip_names: tuple[str, ...] = ()) -> dict | None:
    """The viewer response for a delivered snap-grid case, or None before one is written."""
    path = Path(workspace) / VIEWER_NAME
    if not path.exists():
        return None
    from meshpipeline.render.viewer_pack import polymesh_response

    with np.load(path, allow_pickle=False) as z:
        raw = []
        for k, (name, ptype, count) in enumerate(zip(z["names"].tolist(), z["types"].tolist(),
                                                     z["counts"].tolist())):
            if name in skip_names:
                continue
            raw.append({"name": name, "type": ptype, "face_count": int(count),
                        "points": z[f"p{k}"].astype(np.float32).tobytes(),
                        "polys": z[f"f{k}"].astype(np.uint32).tobytes()})
        cs = z["cell_size"]
        stats = ({"p10": float(np.percentile(cs, 10)), "typ": float(np.median(cs))}
                 if len(cs) else None)
    return polymesh_response(raw, stats, roles, units)
