# Responsibility: THE USER'S MESH REQUIREMENTS ON THE SOLID FEA MESH (PLAN-fea-33, 34): turn the checked set read from their notes (engines/mesh_requirements) into what the solid plan already takes - a size for the whole mesh as the sizes-per-part the plan holds (engines/fea_plan.user_sized, PLAN-fea-29) - and, on the built tetrahedra, say for every requirement what was asked and what was delivered.
# Boundaries: pure over plain data and numpy; no engine, no file, no model. Nothing here can fail a mesh. A quality limit has no lever on the solid route yet (the fill and its optimiser take no target): it is measured and told, never said to be met when it is not. With no requirement every function returns its input unchanged or None.
# Collaborates with: engines/fea_driver.py (merges the whole-mesh size into the sizes per part), engines/gmsh/fea_adapter.py (measures the report into quality.json), engines/verdict_limits.py and engines/fea_results.py (the rows), engines/mixed_run.py (the solid part of a mixed mesh).
"""HOW EACH FIGURE OF A USER'S LIMIT IS MEASURED ON A TETRAHEDRON when they name no tool:

* aspect ratio: longest edge over shortest edge (the corners' straight edges).
* Jacobian: the smallest over the largest determinant of the Jacobian at the element's nodes (1 for a
  straight-sided tetrahedron; under 1 as its sides curve).
* corner angles (degrees): the corner angles of the four faces, the smallest and the largest.
* edge lengths (mm): each element's shortest and longest straight edge.
Warpage, skew and taper are measures of four-sided elements: a tetrahedron has none.
Percentages are of all the elements of the mesh.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np

from meshpipeline.engines import mesh_requirements as MR

RULE_SIZES = "PLAN-fea-33"
RULE_REPORT = "PLAN-fea-34"
#: what a whole-mesh entry is called in the plan's words
WHOLE = "the whole mesh"
#: PLAN-fea-33: with a global size G and no shortest edge given, no zone is held coarser than BAND_LOW x G (the
#: same band the shell uses, engines/shell_requirements.BAND)
BAND_LOW = 0.5
DEFINITIONS = ("Aspect ratio: longest edge over shortest edge. Jacobian: the smallest over the largest determinant "
               "at the element's nodes. Angles: the corner angles of the faces. Percentages are of all the "
               "elements of the mesh.")
#: requirement (measure, op) -> the figure of fea_quality.element_measures it reads, and how
FIGURES = {("aspect_ratio", "<="): "edge_ratio", ("jacobian", ">="): "jacobian",
           ("min_angle", ">="): "min_face_angle", ("max_angle", "<="): "max_face_angle"}
_CHUNK = 200_000


def whole_sizes(sizes: Mapping[str, Mapping] | None, req: Mapping | None, part_ids: Iterable[str], *,
                base_m: float) -> dict[str, dict]:
    """PLAN-fea-33: the sizes per part (PLAN-fea-29's {part id: {"min_m", "max_m"}}) with the user's size for the
    WHOLE mesh added for every part they did not size themselves: a global size G is the part's own size, no
    zone under their shortest edge (half of G when they gave none); a longest edge alone caps the rules' size; a
    shortest edge alone holds every zone over it. `base_m`: the rules' own base size. Unchanged with no size."""
    out = {k: dict(v) for k, v in (sizes or {}).items()}
    s = (req or {}).get("sizes") or {}
    G, lo, hi = (s.get(k) for k in ("global_mm", "min_edge_mm", "max_edge_mm"))
    if G is None and lo is None and hi is None:
        return out
    if G is not None:
        mid = float(G) * 1e-3
        low = (float(lo) if lo is not None else BAND_LOW * float(G)) * 1e-3
    else:
        low = float(lo) * 1e-3 if lo is not None else 0.0
        mid = max(float(base_m), low)
        if hi is not None:
            mid = max(min(mid, float(hi) * 1e-3), low)
        if lo is None and mid >= float(base_m):
            return out                      # a longest edge the rules' own sizes are already under: nothing to hold
    low = min(low, mid)
    entry = {"min_m": float(f"{low:.6g}"), "max_m": float(f"{2.0 * mid - low:.6g}"), "name": WHOLE, "whole": True}
    for pid in part_ids:
        out.setdefault(str(pid), dict(entry))
    return out


def part_ids(zone_list: Iterable[Mapping]) -> list[str]:
    """The part ids a plan's zones belong to, as engines/fea_plan.user_sized keys them (a single part: Part_1)."""
    seen: list[str] = []
    for z in zone_list:
        if z.get("kind") == "part":
            key = str(z.get("part") or "")
        else:
            head, sep, _tail = str((z.get("faces") or [""])[0]).rpartition("_Face_")
            key = head if sep else "Part_1"
        if key and key not in seen:
            seen.append(key)
    return seen or ["Part_1"]


def _measures(mesh) -> dict[str, np.ndarray]:
    from meshpipeline.engines.fea_quality import element_measures

    ids = np.asarray(mesh.node_ids)
    index = np.full(int(ids.max()) + 1, -1, dtype=np.int64)
    index[ids] = np.arange(len(ids))
    xyz = np.asarray(mesh.xyz, dtype=float)
    els = index[np.asarray(mesh.elements)]
    parts: dict[str, list[np.ndarray]] = {}
    pairs = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))
    for s in range(0, len(els), _CHUNK):
        X = xyz[els[s:s + _CHUNK]]
        m = element_measures(X)
        with np.errstate(divide="ignore", invalid="ignore"):
            jac = np.where(np.isfinite(m["jac_ratio"]) & (m["jac_ratio"] > 0), 1.0 / m["jac_ratio"], 0.0)
        edges = np.stack([np.linalg.norm(X[:, b] - X[:, a], axis=1) for a, b in pairs])
        for k, v in (("edge_ratio", m["edge_ratio"]), ("jacobian", jac), ("min_face_angle", m["min_face_angle"]),
                     ("max_face_angle", m["max_face_angle"]), ("min_edge", edges.min(axis=0)),
                     ("max_edge", edges.max(axis=0))):
            parts.setdefault(k, []).append(np.asarray(v, dtype=float))
    return {k: np.concatenate(v) if v else np.zeros(0) for k, v in parts.items()}


def report(mesh, req: Mapping | None, *, mm_per_unit: float = 1000.0, order: int = 2) -> dict | None:
    """PLAN-fea-34: for every requirement read from the notes, what was asked and what the built solid mesh
    delivers (the shape of engines/shell_requirements.report). None with no requirement. `mm_per_unit`: the
    mesh's coordinates in millimetres each (the solid is meshed in metres)."""
    if not req or not req.get("rows"):
        return None
    m = _measures(mesh)
    n = int(len(m.get("edge_ratio", ())))
    lo, hi = m["min_edge"] * mm_per_unit, m["max_edge"] * mm_per_unit
    limits = {lim["id"]: lim for lim in req.get("limits") or []}
    s = req.get("sizes") or {}
    rows = []
    for row in req["rows"]:
        field = str(row["field"])
        if field.startswith("limit:"):
            got = _limit_row(limits[row["id"]], m, n)
        elif field == "global":
            typical = float(np.median(0.5 * (lo + hi))) if n else 0.0
            got = {"applied": True, "measured": float(f"{typical:.6g}"),
                   "met": bool(n) and 0.5 * s["global_mm"] <= typical <= 1.5 * s["global_mm"],
                   "delivered": f"elements are about {MR._g(typical)} mm"}
        elif field in ("min_edge", "max_edge"):
            limit = float(s[f"{field}_mm"])
            v = lo if field == "min_edge" else hi
            outside = int((v < limit).sum() if field == "min_edge" else (v > limit).sum())
            worst = float(v.min() if field == "min_edge" else v.max()) if n else 0.0
            got = {"applied": True, "outside": outside, "outside_pct": MR.pct(outside, n), "target": True,
                   "worst": float(f"{worst:.6g}"), "met": True if outside == 0 else None,
                   "delivered": "every element is inside it" if not outside else
                   MR.edge_words(field, outside, n, limit, worst)}
        elif field == "quadratic":
            got = ({"applied": True, "met": True, "delivered": "the solid elements are quadratic tetrahedra (10-node)"}
                   if order == 2 else
                   {"applied": False, "met": None, "delivered": "not applied - this mesh is linear tetrahedra"})
        elif field == "linear":
            got = {"applied": False, "met": None, "delivered": "not applied - a solid is meshed with quadratic "
                                                               "tetrahedra (10-node); linear ones are not offered yet"}
        elif field == "growth":
            got = {"applied": False, "met": None, "delivered": "not applied - on a solid, sizes grow by our own "
                                                               "fixed rate"}
        elif field == "thickness":
            got = {"applied": False, "met": None, "delivered": "not applied - a solid keeps the thickness of your CAD"}
        else:  # quads_only, triangles_only, max_triangle_percent
            got = {"applied": False, "met": None, "delivered": "not applied - a solid is meshed with tetrahedra"}
        rows.append({"id": row["id"], "field": field, "asked": row["asked"], **got})
    return MR.finish_report(rows, n, DEFINITIONS)


def _limit_row(lim: Mapping, m: Mapping[str, np.ndarray], n: int) -> dict:
    _d, _range, unit, name = MR.MEASURES[str(lim["measure"])]
    key = FIGURES.get((str(lim["measure"]), str(lim["op"])))
    if key is None or str(lim.get("shape") or "any") not in ("tet", "any"):
        return {"applied": False, "met": None,
                "delivered": f"not applied - {name} of this kind is not a measure of tetrahedra"}
    return MR.limit_row(lim, m[key], n)


def summary(rep: Mapping | None, not_applied: int = 0) -> str:
    return MR.summary(rep, kind="solid", not_applied=not_applied)


__all__ = ["BAND_LOW", "DEFINITIONS", "FIGURES", "RULE_REPORT", "RULE_SIZES", "WHOLE", "part_ids", "report",
           "summary", "whole_sizes"]
