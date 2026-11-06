# Responsibility: MESH-24 - at mesh time, give every CAD edge far shorter (and every face far narrower) than the element size the plan puts beside it elements of its own length (width) there (a size field on the edge, growing at the plan's rate), so the triangles and tetrahedra pinned to its two end nodes are well shaped instead of slivers.
# Boundaries: gmsh model in, size-field specs and a record out; it never changes the CAD, never merges anything (MESH-9 does, for the far smaller edges it can mesh through) and never sizes a curve a MESH-9 compound meshes. Its added elements are bounded by a budget read from the plan's own estimate.
# Collaborates with: engines/gmsh/fea_adapter.py (size_fields builds the fields with the plan's; quality.json `fea_short_edges`), engines/gmsh/fea_tiny.local_floor (the smallest size the plan can put on each face), docs/RULES.md MESH-24.
"""MESH-24: A SHORT CAD EDGE (A NARROW FACE) GETS ELEMENTS OF ITS OWN SIZE.

Every CAD edge is a chain of mesh edges, and both of its end points are mesh nodes. Where an edge is far
shorter than the element size beside it - a fillet that runs out a few hundredths of a millimetre before a
corner, a round that stops just short of a face - its two end nodes sit that close together inside
triangles and tetrahedra the plan sized for elements ten or a hundred times longer: those elements are
slivers (C5 knuckle, demo dry run 2 Nov: 0.027 mm edges where three fillets run out, beside 0.5 mm
elements: aspect 153, dihedral angles 1.6 and 176.8 degrees). Optimisers cannot move them: the nodes are
the CAD's own vertices.

Ansys and Abaqus resolve such an edge by meshing it at its own length (a local edge sizing); here:

* An edge is SHORT when it is under SHORT_SHARE of the smallest element size the plan can put on any face
  it bounds (fea_tiny.local_floor: the plan's zones read the way MESH-6 bounds them), and MESH-9 does not
  mesh through it (a compound curve).
* A face is NARROW when its width (twice its area over its perimeter, as MESH-9 reads it) is under the same
  share of the smallest planned size on it or its neighbours: the same sliver, drawn along a strip.
* Curves of no length (poles) and curves bounding a single face (seams) pin no two nodes together and are
  never sized.
* Each gets a gmsh Distance field on itself and a Threshold: SIZE_PER_LENGTH times its length within its own
  length of it, then growing at the plan's rate (as every zone of the plan grows) to the base size. The
  triangles at its end nodes are then about as long as they are wide, and so are the tetrahedra on them.
* A narrow face and the short edges that bound it are ONE unit: sized together or left together.
* THE BUDGET: the elements those fields add are estimated from the plan's own element density (per_h3);
  should all of them come to more than BUDGET_SHARE of the plan's estimate (and BUDGET_FLOOR elements: a part of 18,000 elements was taken to 262,000 by a floor of 200,000 on the FEA corpus, 3 Nov), the
  shortest edges against their neighbours' size are sized first, until the budget is used - the record says
  how many were left as drawn.

Nothing else moves: an edge at a normal length is untouched, and a part with no short edge has no new
field - the same mesh, to the byte.
"""
from __future__ import annotations

import math
from typing import Any

#: MESH-24: an edge shorter than this share of the smallest planned element beside it is short (an aspect
#: of about 1 / SHORT_SHARE pinned to its end nodes; the production limit is 20)
SHORT_SHARE = 0.1
#: ...and is sized at this many times its own length (the triangle on it then about equilateral)
SIZE_PER_LENGTH = 1.5
#: a curve shorter than this share of the base size (a tenth of the part) is a degenerate one (a pole, a zero-length
#: edge), never sized
DEGENERATE_SHARE = 1e-6
#: the added elements may come to this share of the plan's estimate...
BUDGET_SHARE = 0.25
#: ...and never fewer than this many elements in all
BUDGET_FLOOR = 25_000
#: the rule
RULE = "MESH-24"


def _length(gmsh, c: int) -> float:
    try:
        return float(gmsh.model.occ.getMass(1, int(c)))
    except Exception:  # noqa: BLE001 - a curve OpenCASCADE cannot measure is not sized here
        return 0.0


def added_elements(size: float, base: float, growth: float, per_h3: float, length: float,
                   extent: float = 0.0) -> float:
    """Roughly how many elements one MESH-24 field adds: size `size` within `length` of its edge (or face),
    then growing at `growth` - 1 per unit distance to `base`, at the plan's density `per_h3` elements per cube
    of the local size, less what the base size would have put there. An edge's field is a graded ball; a
    narrow face's (`extent`: its length) a graded tube along it."""
    g1 = max(float(growth) - 1.0, 1e-6)
    reach = float(length) + (float(base) - float(size)) / g1
    n, steps = 0.0, 64
    for k in range(steps):
        r0, r1 = reach * k / steps, reach * (k + 1) / steps
        r = 0.5 * (r0 + r1)
        h = float(size) if r <= length else min(float(base), float(size) + (r - float(length)) * g1)
        shell = (2.0 * math.pi * r * float(extent) if extent > 0 else 4.0 * math.pi * r * r) * (r1 - r0)
        n += shell * float(per_h3) * (1.0 / h ** 3 - 1.0 / float(base) ** 3)
    return max(n, 0.0)


def _width(gmsh, s: int) -> tuple[float, float]:
    """A face's width (twice its area over its perimeter, as MESH-9 reads it) and its length (half the
    perimeter)."""
    try:
        area = float(gmsh.model.occ.getMass(2, int(s)))
    except Exception:  # noqa: BLE001 - a face OpenCASCADE cannot measure is not sized here
        return 0.0, 0.0
    per = sum(_length(gmsh, abs(int(t))) for _d, t in gmsh.model.getBoundary([(2, int(s))], oriented=False,
                                                                          combined=False))
    return (2.0 * area / per, 0.5 * per) if per > 0 else (0.0, 0.0)


def plan_short(gmsh, plan: dict, *, face_tags: dict[str, list[int]] | None = None,
               skip: set[int] | None = None, skip_faces: set[int] | None = None) -> dict:
    """MESH-24's decisions on the current (synchronised, not yet meshed) model at the plan's sizes as the
    model reads them: {"edges": [{"dim": 1 | 2, "tag", "feature_m" (an edge's length, a face's width),
    "local_m", "size_m", "added"}...] (sized), "left": n (left as drawn by the budget), "budget": elements,
    "added": elements}. `skip`: curves a MESH-9 compound meshes through; `skip_faces`: faces it merged."""
    from meshpipeline.engines.gmsh.fea_tiny import local_floor

    base = float(plan["base_size_m"])
    growth = float(plan.get("growth") or 1.3)
    per_h3 = float(plan.get("per_h3") or 4.5)
    skip = {int(c) for c in skip or ()}
    skip_faces = {int(f) for f in skip_faces or ()}
    floor_len = DEGENERATE_SHARE * base

    def tags_of(name: str) -> list[int]:
        if face_tags is not None:
            return [int(t) for t in face_tags.get(str(name)) or []]
        try:
            return [int(str(name).rsplit("_", 1)[-1])]
        except ValueError:
            return []

    # candidates by the base size first (the local size is never above it): (dim, tag) -> (feature, extent,
    # the faces whose planned size it is held against)
    cand: dict[tuple[int, int], tuple[float, float, list[int]]] = {}
    for _d, c in gmsh.model.getEntities(1):
        c = int(c)
        if c in skip:
            continue
        length = _length(gmsh, c)
        if not floor_len < length < SHORT_SHARE * base:
            continue
        faces = sorted({int(s) for s in gmsh.model.getAdjacencies(1, c)[0]})
        # a curve of no length (a pole), or one bounding a single face (a seam), pins no two nodes together
        if len(faces) >= 2:
            cand[(1, c)] = (length, 0.0, faces)
    for _d, f in gmsh.model.getEntities(2):
        f = int(f)
        if f in skip_faces:
            continue
        width, extent = _width(gmsh, f)
        if not 0.0 < width < SHORT_SHARE * base:
            continue
        near = {int(n) for _dd, c in gmsh.model.getBoundary([(2, f)], oriented=False, combined=False)
                for n in gmsh.model.getAdjacencies(1, abs(int(c)))[0]}
        cand[(2, f)] = (width, extent, sorted(near | {f}))
    empty = {"edges": [], "left": 0, "budget": 0, "added": 0}
    if not cand:
        return empty
    floor = local_floor(gmsh, plan, sorted({(2, f) for _w, _e, fs in cand.values() for f in fs}), tags_of)
    rows: list[dict[str, Any]] = []
    for (d, t), (feature, extent, faces) in sorted(cand.items()):
        local = min(floor.get((2, f), base) for f in faces)
        if feature >= SHORT_SHARE * local:
            continue
        size = SIZE_PER_LENGTH * feature
        rows.append({"dim": d, "tag": t, "feature_m": feature, "local_m": local, "size_m": size,
                     "added": added_elements(size, local, growth, per_h3, feature, extent)})
    if not rows:
        return empty
    est = float((plan.get("estimate") or {}).get("elements") or 0.0)
    budget = max(BUDGET_FLOOR, BUDGET_SHARE * est)
    # ONE UNIT a narrow face and the short edges that bound it: sized together or left together (an end edge
    # sized without its strip grades the strip's own slivers into worse ones: the runout block, 2 Nov)
    narrow = {int(r["tag"]) for r in rows if r["dim"] == 2}
    unit_of: dict[tuple[int, int], int] = {}
    for r in rows:
        if r["dim"] == 1:
            owners = sorted(f for f in narrow if r["tag"] in {abs(int(t)) for _d, t in gmsh.model.getBoundary(
                [(2, f)], oriented=False, combined=False)})
            if owners:
                unit_of[(1, int(r["tag"]))] = owners[0]
    units: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        dt = (int(r["dim"]), int(r["tag"]))
        key = (2, unit_of[dt]) if dt in unit_of else dt
        units.setdefault(key, []).append(r)
    kept, used = [], 0.0
    # the smallest against their neighbours' size first (the worst slivers), then by entity
    for _key, rs in sorted(units.items(), key=lambda kv: (min(r["feature_m"] / r["local_m"] for r in kv[1]), kv[0])):
        cost = sum(r["added"] for r in rs)
        if used + cost > budget:
            continue
        kept += rs
        used += cost
    kept.sort(key=lambda r: (r["dim"], r["tag"]))
    return {"edges": kept, "left": len(rows) - len(kept), "budget": int(budget), "added": int(used)}


def fields(gmsh, short: dict, base: float, growth: float) -> tuple[list[int], list[dict]]:
    """The gmsh fields of MESH-24's sized edges and faces, and their MESH-6 bounds rows
    (fea_adapter.reach_restricted)."""
    from meshpipeline.engines.gmsh.fea_adapter import _sampling

    g1 = max(float(growth) - 1.0, 1e-6)
    ids, bounds = [], []
    for r in short.get("edges") or []:
        size, feature, d, t = float(r["size_m"]), float(r["feature_m"]), int(r["dim"]), int(r["tag"])
        dist = gmsh.model.mesh.field.add("Distance")
        n = 20 if d == 1 else _sampling(gmsh, [t], size)
        gmsh.model.mesh.field.setNumbers(dist, "CurvesList" if d == 1 else "SurfacesList", [t])
        gmsh.model.mesh.field.setNumber(dist, "Sampling", n)
        th = gmsh.model.mesh.field.add("Threshold")
        dist_max = feature + (float(base) - size) / g1
        for key, val in (("InField", dist), ("SizeMin", size), ("SizeMax", float(base)), ("DistMin", feature),
                         ("DistMax", dist_max)):
            gmsh.model.mesh.field.setNumber(th, key, float(val))
        ids.append(th)
        bounds.append({"field": th, "dim": d, "tags": [t], "size": size, "reach": feature,
                       "dist_max": dist_max, "sampling": int(n)})
    return ids, bounds


def record(short: dict, scale: float = 1.0) -> dict:
    """quality.json's `fea_short_edges`: how many edges were sized, the shortest (mm), how many the budget
    left as drawn."""
    rows = short.get("edges") or []
    mm = 1000.0 / float(scale or 1.0)
    edges = [r for r in rows if int(r["dim"]) == 1]
    faces = [r for r in rows if int(r["dim"]) == 2]
    return {"rule": RULE, "edges": len(edges), "faces": len(faces), "left": int(short.get("left") or 0),
            "shortest_mm": round(min(float(r["feature_m"]) for r in edges) * mm, 6) if edges else None,
            "narrowest_mm": round(min(float(r["feature_m"]) for r in faces) * mm, 6) if faces else None,
            "added_estimate": int(short.get("added") or 0)}


__all__ = ["BUDGET_FLOOR", "BUDGET_SHARE", "RULE", "SHORT_SHARE", "SIZE_PER_LENGTH", "added_elements", "fields",
           "plan_short", "record"]
