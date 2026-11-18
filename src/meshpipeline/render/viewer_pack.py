# Responsibility: Pack a delivered mesh into the response the browser viewer downloads.
# Boundaries: serialisation only.
from __future__ import annotations

import base64
import struct
from typing import Any


def stl_response(patches: dict[str, list], roles: dict, units: str,
                 skip_names: tuple[str, ...] = ()) -> dict | None:
    patches = {n: t for n, t in (patches or {}).items() if t and n not in skip_names}
    if not patches:
        return None
    out = []
    for name, tris in patches.items():
        flat = [c for tri in tris for v in tri for c in v]
        out.append({"name": name, "type": roles.get(name, ""), "tri_count": len(tris),
                    "positions_b64": base64.b64encode(
                        struct.pack(f"<{len(flat)}f", *flat)).decode("ascii")})
    # NB: `is_mesh` is NOT set here. kind=stl serves two very different things - gmsh's
    # DELIVERED surface groups and the API's fallback INPUT skin - and this function
    # cannot tell them apart. Whoever calls it knows, and says so.
    return {"kind": "stl", "mesh_units": units, "patches": out}


def polygon_response(mesh: dict | None, roles: dict, units: str,
                     skip_names: tuple[str, ...] = ()) -> dict | None:
    """A delivered surface whose elements are not all triangles (a shell's quads), drawn as its REAL
    elements: each patch its own points and polygon records [n, v0 .. vn-1], so a quad is one cell and
    the viewer's edges are the element edges - never the two triangles a quad is filled with. `mesh`
    is engines/gmsh/viewer_surface.read_msh's by-face form ({"points", "patches": [(name, polys)]})."""
    import numpy as np

    if not mesh or not mesh.get("patches"):
        return None
    points = np.asarray(mesh["points"], dtype=np.float64)
    out = []
    for name, polys in mesh["patches"]:
        if not polys or name in skip_names:
            continue
        flat = np.fromiter((int(i) for poly in polys for i in poly), dtype=np.int64)
        used, local = np.unique(flat, return_inverse=True)
        recs = np.empty(len(flat) + len(polys), dtype=np.uint32)
        at = k = 0
        for poly in polys:
            n = len(poly)
            recs[at] = n
            recs[at + 1:at + 1 + n] = local[k:k + n]
            at += n + 1
            k += n
        out.append({"name": name, "type": roles.get(name, ""), "face_count": len(polys),
                    "points_b64": base64.b64encode(points[used].astype("<f4").tobytes()).decode("ascii"),
                    "polys_b64": base64.b64encode(recs.astype("<u4").tobytes()).decode("ascii")})
    if not out:
        return None
    # the same kind the triangle surface of a tetrahedral engine is served as: a delivered surface,
    # not a polyMesh (whose cell statistics and heatmap it does not carry); each patch says its form
    return {"kind": "stl", "mesh_units": units, "patches": out}


def polymesh_response(raw: list, cell_stats: Any, roles: dict, units: str) -> dict | None:
    if not raw:
        return None
    return {"kind": "polymesh", "mesh_units": units, "cell_stats": cell_stats,
            "patches": [{
                "name": p["name"],
                "type": roles.get(p["name"], p["type"]),
                "face_count": p["face_count"],
                "points_b64": base64.b64encode(p["points"]).decode("ascii"),
                "polys_b64": base64.b64encode(p["polys"]).decode("ascii"),
                # the engine says when a surface wraps everything else (a domain side, an air
                # region reaching it); the part roster opens those out of the way
                **({"encloses_domain": True} if p.get("encloses_domain") else {}),
            } for p in raw]}
