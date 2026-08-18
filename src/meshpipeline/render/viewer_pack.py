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
            } for p in raw]}
