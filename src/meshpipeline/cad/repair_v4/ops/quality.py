# Responsibility: the low_quality class's dedicated topology edits (CRS v5 step 10): merge a sliver face into its
#                 neighbour, collapse a short edge into one vertex, drop a small solid - each a parent-side `Op` plus
#                 its kernel function.
# Owns: `MergeSliver`, `CollapseEdge`, `DropSolid` (`OPS`) and `op_merge_sliver` / `op_collapse_edge` /
#       `op_drop_solid`.
# Boundaries: the parent half never loads OCCT. Thresholds (what is a sliver, what deviation is allowed, which
#             edges are features) come in as parameters from `repair_v5.quality`; the kernel only measures and
#             refuses what breaks them. The shell ops' `_run`/`_record`/history helpers are reused, not copied.
"""Real topology edits for slivers, short edges and small solids, with the deviation MEASURED.

- `merge_sliver`: the face is removed and its neighbours are extended/trimmed to close over it
  (`BRepAlgoAPI_Defeaturing`). A face is a FEATURE and is protected when every neighbour it could merge into meets it
  at more than `feature_deg` (a chamfer, a step): then the op declines. The deviation (`measured.old_to_new`: the
  farthest sample of the removed face from the result) is measured and the op declines over `max_dev_mm`.
- `collapse_edge`: a short edge is removed and its two vertices merged into one (`ShapeFix_Wire::FixSmall` on every
  wire that uses it, through one shared `ShapeBuild_ReShape`, so both faces see the same merged vertex). The edge's
  own sample points' distance to the result is `measured.old_to_new`.
- `drop_solid`: a solid under `max_volume_fraction` of the part is removed from the compound. The loss guard judges
  it like any other edit (it counts a lost body), so this op is only ever kept where that rule allows it.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops.shells import _entities, _history_from_lookup, _record, _run
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

#: samples per direction on a removed face (UV grid, inside points only) for the deviation
DEVIATION_GRID = 5


class _QualityOp:
    name = ""
    kernel_fn = ""
    params_schema: dict = {}
    #: aimed only by v5's low_quality class menu (parameters from a measured cause): never offered to v4's
    #: proposer (`proposer.offered_ops`) nor swept by its det generator
    v5_class_only = True

    def applies_to(self, site: Site) -> bool:
        return False

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, self.kernel_fn, targets, params, timeout_s)


class MergeSliver(_QualityOp):
    name = "merge_sliver"
    kernel_fn = "op_merge_sliver"
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "face", "min": 1, "max": None},
        "properties": {
            "max_dev_mm": {"type": "number", "minimum": 0},
            # 180 = no edge is protected (a tiny face is small both ways: removing it moves less than its size)
            "feature_deg": {"type": "number", "minimum": 0, "maximum": 180},
        },
        "required": ["max_dev_mm", "feature_deg"],
        "additionalProperties": False,
    }


class CollapseEdge(_QualityOp):
    name = "collapse_edge"
    kernel_fn = "op_collapse_edge"
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "edge", "min": 1, "max": None},
        "properties": {"max_length_mm": {"type": "number", "minimum": 1e-9}},
        "required": ["max_length_mm"],
        "additionalProperties": False,
    }


class DropSolid(_QualityOp):
    name = "drop_solid"
    kernel_fn = "op_drop_solid"
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "solid", "min": 1, "max": None},
        "properties": {"max_volume_fraction": {"type": "number", "minimum": 0, "maximum": 0.1}},
        "required": ["max_volume_fraction"],
        "additionalProperties": False,
    }


OPS: tuple[Any, ...] = (MergeSliver(), CollapseEdge(), DropSolid())


# --- kernel side ---------------------------------------------------------------------------------


def _face_samples(face: Any) -> list[Any]:
    """Points ON `face`: its vertices and the inside points of a UV grid."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_VERTEX
    from OCP.TopoDS import TopoDS

    pts = [BRep_Tool.Pnt_s(TopoDS.Vertex_s(v)) for v in _sub(face, TopAbs_VERTEX)]
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    surface = BRep_Tool.Surface_s(face)
    for a in range(DEVIATION_GRID):
        for b in range(DEVIATION_GRID):
            u = u0 + (u1 - u0) * (a + 0.5) / DEVIATION_GRID
            v = v0 + (v1 - v0) * (b + 0.5) / DEVIATION_GRID
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-9).State() in (TopAbs_IN, TopAbs_ON):
                pts.append(surface.Value(u, v))
    return pts


def _edge_samples(edge: Any) -> list[Any]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    curve = BRepAdaptor_Curve(edge)
    t0, t1 = curve.FirstParameter(), curve.LastParameter()
    return [curve.Value(t0 + (t1 - t0) * k / 4) for k in range(5)]


def _sub(shape: Any, kind: Any) -> list[Any]:
    from OCP.TopExp import TopExp_Explorer

    out = []
    ex = TopExp_Explorer(shape, kind)
    while ex.More():
        out.append(ex.Current())
        ex.Next()
    return out


def _deviation(points: list[Any], onto: Any) -> float | None:
    """The farthest of `points` from the shape `onto` (mm); None when nothing could be measured."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    worst = None
    for p in points:
        dist = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(p).Vertex(), onto)
        if dist.IsDone():
            worst = dist.Value() if worst is None else max(worst, dist.Value())
    return None if worst is None else float(worst)


def _compound(shapes: list[Any]) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    out, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(out)
    for s in shapes:
        builder.Add(out, s)
    return out


def _valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


def _decline(why: str) -> dict:
    return {"declined": why}


#: two faces whose outward normals at the sliver are within this (deg) are parallel (a step's top and floor)
STEP_PARALLEL_DEG = 1.0
#: a step's wall meets both sides at more than this (deg) - also when the caller set no feature protection
FEATURE_WALL_DEG = 30.0


def _step_floor(sliver: Any, near: list[dict], fmap: Any, feature_deg: float) -> tuple[int, Any] | None:
    """(index, face) of the SMALLER of the two faces along the sliver's long sides when the sliver meets both
    above the feature angle (a wall, not an in-plane strip) and those two are parallel and face the same way (a
    step: top and floor), else None."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v5.quality_kernel import _normal_at

    sides = sorted(near, key=lambda n: -n["shared_mm"])[:2]
    if len(sides) < 2 or any(n["dihedral_deg"] is None or n["dihedral_deg"] <= min(feature_deg, FEATURE_WALL_DEG)
                             for n in sides):
        return None
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(sliver, props)
    mid = props.CentreOfMass()
    faces = [TopoDS.Face_s(fmap.FindKey(n["face"])) for n in sides]
    normals = [_normal_at(f, mid) for f in faces]
    if any(n is None for n in normals) or normals[0].Angle(normals[1]) * 57.29577951308232 > STEP_PARALLEL_DEG:
        return None

    def area(f: Any) -> float:
        p = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, p)
        return float(p.Mass())

    i = min(range(2), key=lambda j: area(faces[j]))
    return sides[i]["face"], faces[i]


@register_kernel_fn("op_merge_sliver")
def _merge_sliver(payload: dict) -> dict:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_ListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v5.quality_kernel import _ancestors, neighbours

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    max_dev, feature = float(params["max_dev_mm"]), float(params["feature_deg"])
    fmap, emap = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(old, TopAbs_FACE, fmap)
    TopExp.MapShapes_s(old, TopAbs_EDGE, emap)
    efmap = _ancestors(old, TopAbs_EDGE, TopAbs_FACE)
    faces, into, partners, protected = [], {}, [], []
    for target in payload["targets"]:
        if target["type"] != "face":
            raise ValueError(f"{target['id']}: merge_sliver takes faces, not {target['type']}s")
        face = TopoDS.Face_s(fmap.FindKey(target["index"]))
        near = neighbours(face, fmap, emap, efmap)
        # merge INTO the neighbour it continues most smoothly into, along the longest shared boundary
        ok = [n for n in near if n["dihedral_deg"] is not None and n["dihedral_deg"] <= feature]
        # a WALL between two parallel faces is a step whatever the angles: removing the wall alone leaves two
        # parallel faces that can never be extended to meet (VM, injected step: merged "alone", mesh still failed)
        floor = _step_floor(face, near, fmap, feature)
        if floor is not None or not ok:
            if floor is None:
                angles = [n["dihedral_deg"] for n in near]
                protected.append(f"{target['id']} meets every neighbour above the {feature:g} deg feature angle "
                                 f"({angles}) and is no step between parallel faces")
                continue
            # A STEP lower than e between two parallel faces (the sliver is its wall): a narrow region, not a feature
            # at this mesh size. The wall and the smaller side go; the other side is extended over them, so the
            # part moves by the step's height - measured below and bounded by max_dev_mm like any merge.
            k, floor_face = floor
            into[target["id"]] = "step:" + payload["ids"].get(f"face:{k}", f"face:{k}")
            faces.extend([face, floor_face])
            partners.extend(fmap.FindKey(n["face"]) for n in near)
            continue
        pick = max(ok, key=lambda n: n["shared_mm"])
        into[target["id"]] = payload["ids"].get(f"face:{pick['face']}", f"face:{pick['face']}")
        faces.append(face)
        partners.extend(fmap.FindKey(n["face"]) for n in near)
    if not faces:
        return _decline("protected: " + "; ".join(protected) + " - a feature, not a sliver")
    params["protected"] = protected
    tools = TopTools_ListOfShape()
    for face in faces:
        tools.Append(face)
    defeature = BRepAlgoAPI_Defeaturing()
    defeature.SetShape(old)
    defeature.AddFacesToRemove(tools)
    defeature.SetToFillHistory(True)
    defeature.Build()
    if not defeature.IsDone():
        return _decline("defeaturing_failed: the neighbours could not be extended over the sliver")
    new, history = defeature.Shape(), defeature.History()
    if not _valid(new):
        return _decline("invalid_result: the merged shape fails BRepCheck")
    grown = [g for p in partners for g in [*history.Modified(p)] if g.ShapeType() == p.ShapeType()]
    grown += [p for p in partners if not history.IsRemoved(p) and not history.Modified(p).Size()]
    dev = _deviation([q for f in faces for q in _face_samples(f)], _compound(grown) if grown else new)
    if dev is None:
        return _decline("unmeasured: the deviation of the merged faces could not be measured")
    if dev > max_dev:
        return _decline(f"deviation {dev:.4g} mm over the {max_dev:.4g} mm limit")
    params.update(into=into, measured={"old_to_new": dev})
    return _record(old, new, history, payload, "merge_sliver", params, start, declare_removed=True)


@register_kernel_fn("op_collapse_edge")
def _collapse_edge(payload: dict) -> dict:
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Wire
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v5.quality_kernel import _ancestors, _faces_of, _length

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    limit = float(params["max_length_mm"])
    edges = _entities(old, "edge")
    efmap = _ancestors(old, TopAbs_EDGE, TopAbs_FACE)
    context = ShapeBuild_ReShape()
    targets, fixed = [], 0
    for target in payload["targets"]:
        if target["type"] != "edge":
            raise ValueError(f"{target['id']}: collapse_edge takes edges, not {target['type']}s")
        edge = TopoDS.Edge_s(edges[target["index"] - 1])
        length = _length(edge)
        if length > limit:
            raise ValueError(f"{target['id']} is {length:.6g} mm long, over max_length_mm {limit}; "
                             "refusing to collapse an edge that is not short")
        targets.append(edge)
        for face in _faces_of(efmap, edge):
            for wire in _sub(face, TopAbs_WIRE):
                fix = ShapeFix_Wire(TopoDS.Wire_s(wire), face, limit)
                fix.SetContext(context)
                fix.ModifyTopologyMode = True
                k = fix.WireData().Index(edge)
                if k and fix.FixSmall(k, False, limit):
                    fixed += 1
    if not fixed:
        return _decline(f"no_change: ShapeFix_Wire collapsed none of {[t['id'] for t in payload['targets']]}")
    merged = context.Apply(old)
    # the faces round the merged vertex keep their old pcurve ends: ShapeFix_Shape re-settles them (measured on the
    # prototype sliver: invalid straight after FixSmall, valid after this); its own replacements are chained
    settle = ShapeFix_Shape(merged)
    settle.Perform()
    new = settle.Shape()
    if not _valid(new):
        return _decline("invalid_result: the collapsed shape fails BRepCheck")

    def lookup(sub: Any) -> Any:
        mid = context.Apply(sub)
        return mid if mid.IsNull() else settle.Context().Apply(mid)

    history = _history_from_lookup(old, lookup)
    if not (history.HasRemoved() or history.HasModified()):
        return _decline("no_change: the collapse changed no face, edge or solid")
    dev = _deviation([q for e in targets for q in _edge_samples(e)], _compound(_sub(new, TopAbs_EDGE)))
    params["measured"] = {"old_to_new": dev} if dev is not None else {}
    params["wires_fixed"] = fixed
    return _record(old, new, history, payload, "collapse_edge", params, start, declare_removed=True)


@register_kernel_fn("op_drop_solid")
def _drop_solid(payload: dict) -> dict:
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools_History
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    solids = _entities(old, "solid")

    def volume(s: Any) -> float:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(s, props)
        return abs(float(props.Mass()))

    total = sum(volume(s) for s in solids)
    drop = []
    for target in payload["targets"]:
        if target["type"] != "solid":
            raise ValueError(f"{target['id']}: drop_solid takes solids, not {target['type']}s")
        s = solids[target["index"] - 1]
        share = volume(s) / total if total > 0 else 1.0
        if share > float(params["max_volume_fraction"]):
            raise ValueError(f"{target['id']} is {share:.3g} of the part, over max_volume_fraction "
                             f"{params['max_volume_fraction']}; refusing to drop a solid that is not small")
        drop.append(s)
    if len(drop) >= len(solids):
        return _decline("would_empty: dropping these would leave no solid")
    keep = [s for s in solids if not any(s.IsSame(d) for d in drop)]
    new = _compound(keep) if len(keep) > 1 else keep[0]
    kept = TopTools_IndexedMapOfShape()
    for kind in (TopAbs_FACE, TopAbs_EDGE):
        TopExp.MapShapes_s(new, kind, kept)
    history = BRepTools_History()
    for d in drop:
        history.Remove(d)
        for sub in [*_sub(d, TopAbs_FACE), *_sub(d, TopAbs_EDGE)]:
            if not kept.Contains(sub):          # a face or edge the dropped solid shared with a kept one stays
                history.Remove(sub)
    params["dropped_share"] = round(sum(volume(d) for d in drop) / total, 9) if total > 0 else None
    return _record(old, new, history, payload, "drop_solid", params, start, declare_removed=True)
