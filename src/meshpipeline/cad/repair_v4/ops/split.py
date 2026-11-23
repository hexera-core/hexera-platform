# Responsibility: The split family - ops that ADD topology and move no geometry: split by proximity,
#                 by angle, closed faces, continuity breaks, and its inverse, unify-same-domain.
# Owns: five `Op`s and their kernel functions (`op_split_*`, `op_unify_same_domain`).
# Boundaries: kernel functions run in a worker (OCP imported lazily); the `Op` classes run in the
#             parent and only move ids and paths. Choosing WHICH faces to split is the caller's
#             (sites, Task 17; search, Task 18).
"""Split ops. Why they matter: a split adds faces and edges but leaves every surface where it was, so
the deviation it causes is exactly zero and it survives a STEP round trip.

How the divide-family ops are scoped to chosen faces. ShapeUpgrade_ShapeDivide* act on every face
of the shape they are given and cannot be restricted. So the divider is run on a compound of ONLY
the target faces (the same TShapes as in the part), and the `ShapeBuild_ReShape` context it filled
is then `Apply`ed to the WHOLE part: neighbours are rebuilt around the split edges they share, and
every untouched face stays the same TShape, which is what `ws_kernel.history_record` needs to keep
ids. The context's own `History()` is the record.

`partners` of gap_split (finding which faces sit across a gap) is NOT ported: the pairs arrive as
parameters, chosen by the site detector. `split_plan`/`iso_edges` are reused from
`cad.repair.gap_split`, which they already serve for one face; the splitter is generalised from one
face to a list of pairs here.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops import OpDeclined, check_call, register, run_kernel_op
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

_FACE_ID = {"type": "string", "x-entity": "face"}
_EDGE_ID = {"type": "string", "x-entity": "edge"}

#: Same bound as the legacy gap split: above this many iso-curves the part is re-partitioned, not
#: refined.
MAX_ISO_EDGES = 1200


# --- kernel side --------------------------------------------------------------------------------


def _finish(old: Any, new: Any, history: Any, payload: dict, *, op: str, params: dict,
            start: float) -> dict:
    """Write `new`, then the history record - or decline when nothing changed."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    ws_kernel.write_brep(new, payload["out_path"])
    record = ws_kernel.history_record(
        old, new, history, op=op, params=params, old_path=payload["brep_path"],
        new_path=payload["out_path"], quantum=payload["quantum"],
        seconds=time.perf_counter() - start)
    if not any(r["modified"] or r["generated"] or r["removed"] or not r["now"]
               for r in record["history"]):
        os.unlink(payload["out_path"])
        return {"declined": f"no_change: {op} left the part as it was"}
    return record


def _face_compound(old: Any, indices: list[int]) -> Any:
    """A compound of the faces at `indices` of `old` (the very same TShapes)."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair_v4 import ws_kernel

    faces = ws_kernel._maps(old)["face"]
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for index in indices:
        builder.Add(compound, faces.FindKey(index))
    return compound


def _divide_faces(payload: dict, make_divider: Any, *, op: str, params: dict) -> dict:
    """Run a ShapeUpgrade_ShapeDivide* on the target faces and apply its context to the part."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    divider = make_divider(_face_compound(old, payload["faces"]))
    if not divider.Perform(True):
        return {"declined": f"no_change: {op} found nothing to divide on the target faces"}
    context = divider.GetContext()
    new = context.Apply(old)
    return _finish(old, new, context.History(), payload, op=op, params=params, start=start)


@register_kernel_fn("op_split_angle")
def _split_angle(payload: dict) -> dict:
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideAngle

    return _divide_faces(
        payload, lambda s: ShapeUpgrade_ShapeDivideAngle(math.radians(payload["max_deg"]), s),
        op="split_angle", params={"max_deg": payload["max_deg"]})


@register_kernel_fn("op_split_closed")
def _split_closed(payload: dict) -> dict:
    """Closed FACES first (a full cylinder, cone, sphere, torus: ShapeDivideClosed). If none of the
    targets is closed, closed EDGES on them (a circular rim: ShapeDivideClosedEdges)."""
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideClosed, ShapeUpgrade_ShapeDivideClosedEdges

    first = _divide_faces(payload, ShapeUpgrade_ShapeDivideClosed, op="split_closed", params={})
    if "declined" not in first:
        return first
    return _divide_faces(payload, ShapeUpgrade_ShapeDivideClosedEdges, op="split_closed", params={})


@register_kernel_fn("op_split_continuity")
def _split_continuity(payload: dict) -> dict:
    from OCP.GeomAbs import GeomAbs_C1, GeomAbs_C2
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideContinuity

    criterion = {1: GeomAbs_C1, 2: GeomAbs_C2}[payload["order"]]

    def make(shape: Any) -> Any:
        divider = ShapeUpgrade_ShapeDivideContinuity(shape)
        divider.SetBoundaryCriterion(criterion)
        divider.SetPCurveCriterion(criterion)
        divider.SetSurfaceCriterion(criterion)
        return divider

    return _divide_faces(payload, make, op="split_continuity", params={"order": payload["order"]})


@register_kernel_fn("op_split_by_proximity")
def _split_by_proximity(payload: dict) -> dict:
    """Split each face of each pair along iso-curves fine enough that a chord across a patch sags
    less than `target_ratio` of the gap between the pair (gap_split's plan), all in one splitter."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopTools import TopTools_ListOfShape

    from meshpipeline.cad.repair import gap_split
    from meshpipeline.cad.repair.localize import _face_area
    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    faces = ws_kernel._maps(old)["face"]
    diagonal = ws_kernel.diagonal_of(old)
    size = gap_split._mesh_size(diagonal)
    area = sum(float(_face_area(faces.FindKey(i))) for i in range(1, faces.Extent() + 1))
    edges: list = []
    done: set[int] = set()
    for a, b in payload["pairs"]:
        fa, fb = faces.FindKey(a), faces.FindKey(b)
        extrema = BRepExtrema_DistShapeShape(fa, fb)
        if not extrema.IsDone():
            return {"declined": f"unmeasurable: no distance between faces {a} and {b}"}
        gap = float(extrema.Value())
        for index, face in ((a, fa), (b, fb)):
            if index in done:
                continue
            done.add(index)
            plan = gap_split.split_plan(face, gap_mm=max(gap, 1e-9), mesh_size=size,
                                        part_area_mm2=area, diagonal_mm=diagonal,
                                        sag_share=payload["target_ratio"])
            edges.extend(gap_split.iso_edges(face, plan))
        if len(edges) > MAX_ISO_EDGES:
            return {"declined": f"too_fine: more than {MAX_ISO_EDGES} iso-curves needed"}
    if not edges:
        return {"declined": "no_change: no face of the pairs is curved enough to sag across its gap"}
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(old)
    for edge in edges:
        tools.Append(edge)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.SetToFillHistory(True)
    splitter.SetRunParallel(False)  # identical runs must give identical shapes
    splitter.Build()
    if not splitter.IsDone():
        return {"declined": "split_failed: the splitter reported errors"}
    return _finish(old, splitter.Shape(), splitter.History(), payload, op="split_by_proximity",
                   params={"target_ratio": payload["target_ratio"]}, start=start)


@register_kernel_fn("op_unify_same_domain")
def _unify_same_domain(payload: dict) -> dict:
    """Merge same-domain faces and edges, except the `keep` edges and, when `faces` is given, any
    edge that touches a face outside it."""
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    maps = ws_kernel._maps(old)
    unifier = ShapeUpgrade_UnifySameDomain(old, True, True, False)
    for index in payload["keep"]:
        unifier.KeepShape(maps["edge"].FindKey(index))
    if payload["faces"]:
        inside = set(payload["faces"])
        ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(old, TopAbs_EDGE, TopAbs_FACE, ancestors)
        for i in range(1, ancestors.Extent() + 1):
            owners = [maps["face"].FindIndex(TopoDS.Face_s(f)) for f in ancestors.FindFromIndex(i)]
            if any(o not in inside for o in owners):
                unifier.KeepShape(ancestors.FindKey(i))
    unifier.Build()
    return _finish(old, unifier.Shape(), unifier.History(), payload, op="unify_same_domain",
                   params={"scope": payload["scope"], "keep": payload["keep_ids"]}, start=start)


# --- parent side --------------------------------------------------------------------------------


def _indices(ws: Workspace, ids: list[StableId]) -> list[int]:
    return [ws.indices[i] for i in ids]


@register
class SplitByProximity:
    name = "split_by_proximity"
    params_schema = {
        "type": "object",
        "properties": {
            "face_pairs": {"type": "array", "minItems": 1, "maxItems": 16,
                           "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                     "items": _FACE_ID}},
            #: the share of the pair's gap a chord's sag may use; 0.25 is gap_split's first rung,
            #: 0.125 and 0.06 its finer ones
            "target_ratio": {"type": "number", "minimum": 0.05, "maximum": 1},
        },
        "required": ["face_pairs", "target_ratio"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 0, "max": 0},  # the faces ride in face_pairs
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind == "proximity"

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        pairs = [_indices(ws, pair) for pair in params["face_pairs"]]
        return run_kernel_op(pool, ws, kernel_fn="op_split_by_proximity", op=self.name,
                             payload={"pairs": pairs, "target_ratio": params["target_ratio"]},
                             timeout_s=timeout_s)


@register
class SplitAngle:
    name = "split_angle"
    params_schema = {
        "type": "object",
        "properties": {"max_deg": {"type": "number", "minimum": 30, "maximum": 180}},
        "required": ["max_deg"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 1},  # the faces to split
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind in ("periodic", "proximity", "self_overlap")

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        return run_kernel_op(pool, ws, kernel_fn="op_split_angle", op=self.name,
                             payload={"faces": _indices(ws, targets), "max_deg": params["max_deg"]},
                             timeout_s=timeout_s)


@register
class SplitClosed:
    name = "split_closed"
    params_schema = {
        "type": "object", "properties": {}, "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 1},
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind == "periodic"

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        return run_kernel_op(pool, ws, kernel_fn="op_split_closed", op=self.name,
                             payload={"faces": _indices(ws, targets)}, timeout_s=timeout_s)


@register
class SplitContinuity:
    name = "split_continuity"
    params_schema = {
        "type": "object",
        "properties": {"order": {"type": "integer", "enum": [1, 2]}},
        "required": ["order"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 1},
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind == "invalid"

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        return run_kernel_op(pool, ws, kernel_fn="op_split_continuity", op=self.name,
                             payload={"faces": _indices(ws, targets), "order": params["order"]},
                             timeout_s=timeout_s)


@register
class UnifySameDomain:
    """Merge coplanar/co-surface faces and collinear edges. `scope` "all" merges across the whole
    part; "targets" only among the target faces. `keep` edges are never merged away - pass the
    edges a split just added to keep them."""

    name = "unify_same_domain"
    params_schema = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["all", "targets"]},
            "keep": {"type": "array", "maxItems": 4096, "items": _EDGE_ID},
        },
        "required": ["scope", "keep"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 0},  # faces to merge among, when scope is "targets"
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind == "sliver"

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        if params["scope"] == "targets" and not targets:
            raise OpDeclined("scope 'targets' needs target faces; none were given")
        if params["scope"] == "all" and targets:
            raise OpDeclined("scope 'all' takes no targets; use scope 'targets' to restrict it")
        return run_kernel_op(
            pool, ws, kernel_fn="op_unify_same_domain", op=self.name,
            payload={"faces": _indices(ws, targets), "keep": _indices(ws, params["keep"]),
                     "keep_ids": list(params["keep"]), "scope": params["scope"]},
            timeout_s=timeout_s)
