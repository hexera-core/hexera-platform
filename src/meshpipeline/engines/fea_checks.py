# Responsibility: Measure, on a finished solid mesh, whether the FEA plan's rules were met: elements through every planned wall, and elements per quarter-turn round every hot spot - each against the rule's own number.
# Boundaries: engine-neutral. It reads the internal mesh (engines/gmsh/abaqus_deck.SolidMesh: nodes, tetrahedra, the triangles of each CAD face) and the plan; it judges nothing else and changes nothing.
# Collaborates with: engines/gmsh/fea_adapter.py (runs it after meshing, into quality.json), engines/fea_driver.py and engines/fea_ladder.py (a zone that fell short is the ladder's LAD-fea-3), engines/gmsh/gates.py (the through-wall floor).
"""THE PLAN'S RULES, MEASURED ON THE MESH.

The element size at a face is read as the median length of the edges of the triangles meshed on
it (the length the size field sets there). Then:

* a WALL zone (PLAN-fea-3) has `thickness / size` elements through it, and needs WALL_ELEMENTS;
* a HOT SPOT (PLAN-fea-4) has `(pi / 2) * radius / size` elements per quarter-turn, and needs the
  plan's count (fewer when the user accepted fewer at the named spots), and its wall the stress
  count;
* a capped CORNER (PLAN-fea-5) is reported, not judged: it is meant to stop at its cap.

Every wall is judged. A hot spot or a capped corner is judged where its zone binds (finer than
the base size); where the base size is already finer, its rule is met by the base.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

from meshpipeline.engines import fea_plan as P

#: A rule is met within this share of its number: the median edge scatters a few per cent about
#: the size the field asked for, and a 1.96 is the 2 it was planned as.
TOLERANCE = 0.9


def face_sizes(mesh, tags_by_name: Mapping[str, int]) -> dict[str, float]:
    """Median boundary-edge length (metres) on each named face."""
    index = {int(n): i for i, n in enumerate(np.asarray(mesh.node_ids).tolist())}
    xyz = np.asarray(mesh.xyz, dtype=float)
    out: dict[str, float] = {}
    for name, tag in tags_by_name.items():
        tri = mesh.faces.get(int(tag))
        if tri is None or not len(tri):
            continue
        t = np.vectorize(index.get, otypes=[np.int64])(np.asarray(tri)[:, :3])
        e = np.vstack([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
        e = np.unique(np.sort(e, axis=1), axis=0)
        out[name] = float(np.median(np.linalg.norm(xyz[e[:, 1]] - xyz[e[:, 0]], axis=1)))
    return out


def face_tag(name: str) -> int | None:
    try:
        return int(str(name).rsplit("_", 1)[-1])
    except ValueError:
        return None


def measure(mesh, plan: Mapping) -> dict:
    """The rules as met on `mesh`: {"through_wall": {...}, "hot_spots": {...}, "corners": {...},
    "short": [zone ids that fell short]} - every count rounded to 2 decimals."""
    base = float(plan.get("base_size_m") or math.inf)
    # every wall is judged (a wall the base size crosses is the easy case, and still the rule);
    # a hot spot or a corner only where its zone binds (finer than the base)
    zones = [z for z in plan.get("zones") or [] if z["kind"] == "wall" or float(z["size_m"]) < base]
    names = sorted({f for z in zones for f in z.get("faces") or []})
    sizes = face_sizes(mesh, {n: t for n in names if (t := face_tag(n)) is not None})
    per_quarter = P.HOT_SPOT_PER_QUARTER_MIN if plan.get("relaxed") else P.HOT_SPOT_PER_QUARTER
    walls, spots, corners, short = [], [], [], []
    for z in zones:
        got = [sizes[f] for f in z.get("faces") or [] if f in sizes]
        if not got:
            continue
        h = float(np.median(got))
        if z["kind"] == "wall":
            n = float(z["thickness_m"]) / h
            ok = n >= P.WALL_ELEMENTS * TOLERANCE
            walls.append({"zone": z["id"], "face": z["faces"][0], "thickness_mm": round(z["thickness_m"] * 1e3, 4),
                          "elements": round(n, 2), "needed": P.WALL_ELEMENTS, "ok": ok})
        elif z["kind"] in ("hole", "fillet", "notch"):
            n = P.QUARTER * float(z["radius_m"]) / h
            ok = n >= per_quarter * TOLERANCE
            spots.append({"zone": z["id"], "spot": z.get("spot"), "kind": z["kind"],
                          "radius_mm": round(z["radius_m"] * 1e3, 4), "per_quarter_turn": round(n, 2),
                          "needed": per_quarter, "ok": ok})
        elif z["kind"] == "corner":
            corners.append({"zone": z["id"], "spot": z.get("spot"), "size_mm": round(h * 1e3, 4),
                            "cap_mm": round(z["size_m"] * 1e3, 4)})
            continue
        else:
            continue
        if not ok:
            short.append(z["id"])
    worst_wall = min((w["elements"] for w in walls), default=None)
    worst_spot = min((s["per_quarter_turn"] for s in spots), default=None)
    return {
        "through_wall": {"min": worst_wall, "needed": P.WALL_ELEMENTS, "walls": len(walls),
                         "short": [w for w in walls if not w["ok"]][:20]},
        "hot_spots": {"min_per_quarter_turn": worst_spot, "needed": per_quarter, "spots": len(spots),
                      "short": [s for s in spots if not s["ok"]][:20]},
        "corners": corners[:20],
        "short": short,
    }


__all__ = ["TOLERANCE", "face_sizes", "face_tag", "measure"]
