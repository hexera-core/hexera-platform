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


def polymesh_response(raw: list, cell_stats: Any, roles: dict, units: str,
                      reduced: dict | None = None, exact: dict | None = None) -> dict | None:
    """The delivered boundary as the browser receives it.

    `reduced` is carried through UNTOUCHED and is the whole of this function's honesty about size:
    when a caller has merged cells to fit a browser's budget, the payload says how many faces were
    delivered, how many are drawn, and at what resolution. Without it a reader has no way to tell a
    mesh that is coarse from a mesh that is being SHOWN coarse, and the face count is the mesh's
    own measurement of itself.

    `exact` is the same kind of record for the OTHER way of getting under a budget: merging only
    faces that already lay flat in the same plane, which changes the geometry by nothing. The two
    are different claims and the viewer words them differently, so they travel in different keys -
    and BOTH may be present, because a mesh can be merged losslessly first and still be too big,
    in which case the budget reduces what is left and the payload has to admit both steps.
    """
    if not raw:
        return None
    return {"kind": "polymesh", "mesh_units": units, "cell_stats": cell_stats,
            **({"reduced": reduced} if reduced else {}),
            **({"exact": exact} if exact else {}),
            "patches": [{
                "name": p["name"],
                "type": roles.get(p["name"], p["type"]),
                "face_count": p["face_count"],
                # the wetted area this patch delivers, when the reader measured one. On a
                # conjugate mesh the interface's area is the heat-transfer area, and it is what
                # staircasing takes away without changing a single face count.
                **({"area_m2": float(p["area_m2"])} if p.get("area_m2") is not None else {}),
                "points_b64": base64.b64encode(p["points"]).decode("ascii"),
                "polys_b64": base64.b64encode(p["polys"]).decode("ascii"),
                # the engine says when a surface wraps everything else (a domain side, an air
                # region reaching it); the part roster opens those out of the way
                **({"encloses_domain": True} if p.get("encloses_domain") else {}),
            } for p in raw]}
