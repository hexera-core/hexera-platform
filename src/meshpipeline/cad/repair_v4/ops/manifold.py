# Responsibility: v5's open_shell_volume ops that the four shell ops (shells.py) do not cover: `sew_gap` (sew the OPEN
#                 part of a shape at ONE measured tolerance, closed solids untouched, closed shells made solids) and
#                 `split_non_manifold` (a shell with multiply-shared edges split into manifold shells, each closed one
#                 a solid). Each is a parent-side `Op` plus the kernel function it calls.
# Owns: `OPS` (both ops, picked up by the ops package) and the `op_sew_gap` / `op_split_non_manifold` kernel fns.
# Boundaries: the parent half never loads OCCT; the kernel half returns `ws_kernel.history_record(...)` through
#             `shells._record`. Parameters come from `repair_v5.open_shell` (the measured gap), never a ladder here.
"""Sew at the measured gap; split non-manifold shells.

`sew` (shells.py) declines any input that carries a solid, because sewing a whole shape returns shells and destroys
its solids. Real open parts are mostly an OPEN SOLID (a solid whose shell has free edges) beside closed ones, so
`sew_gap` sews only the faces outside the closed solids - the open solids' faces and every face outside a solid - at
the ONE tolerance the open_shell_volume detector measured, and rebuilds a solid from every sewn shell that closed or
that came from an open solid (it was a solid before; an open solid stays a solid, now with fewer free edges).

`split_non_manifold` takes each unit with an edge shared by more than two faces, rebuilds its faces into manifold
shells (`ShapeFix_Shell.FixFaceOrientation` with multi-connexity on and non-manifold creation off), and makes each
closed shell a positive solid. Faces and edges keep their TShapes; the old solid is Modified into its pieces.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops.shells import (
    _bound,
    _entities,
    _free_edge_count,
    _history_from_lookup,
    _record,
    _run,
    _ShellOp,
    _volume,
    ladder_floor,
)
from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace


class SewGap(_ShellOp):
    """Sew the open part of the shape at the measured `tol_mm`; closed solids are untouched."""

    name = "sew_gap"
    _site_kinds = ("open",)
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "any", "min": 0, "max": None},
        "properties": {"tol_mm": {"type": "number", "minimum": 1e-9}},
        "required": ["tol_mm"],
        "additionalProperties": False,
    }

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_sew_gap", [], params, timeout_s)


class SplitNonManifold(_ShellOp):
    """Split every unit with a non-manifold edge (or the target solids) into manifold shells/solids."""

    name = "split_non_manifold"
    _site_kinds = ("invalid",)
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "any", "min": 0, "max": None},
        "properties": {},
        "additionalProperties": False,
    }

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_split_non_manifold", [], params, timeout_s)


OPS: tuple[Any, ...] = (SewGap(), SplitNonManifold())


# --- kernel side ---------------------------------------------------------------------------------

def _compound(parts: list[Any]) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    out = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(out)
    for p in parts:
        builder.Add(out, p)
    return out


def _solid_of(shell: Any, closed: bool) -> Any:
    """A solid on `shell`; when closed, oriented to a positive volume (ShapeFix_Solid + OrientClosedSolid)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.BRepLib import BRepLib
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopoDS import TopoDS

    if not closed:
        return BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(shell)).Solid()
    solid = TopoDS.Solid_s(ShapeFix_Solid().SolidFromShell(TopoDS.Shell_s(shell)))
    BRepLib.OrientClosedSolid_s(solid)
    if _volume(solid) < 0:
        solid = TopoDS.Solid_s(solid.Reversed())
    return solid


def _faces_in(shapes: list[Any]) -> Any:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    seen = TopTools_IndexedMapOfShape()
    for s in shapes:
        for kind in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX):
            TopExp.MapShapes_s(s, kind, seen)
    return seen


@register_kernel_fn("op_sew_gap")
def _sew_gap(payload: dict) -> dict:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    tol = float(params["tol_mm"])
    diag, _ = _bound(old, payload)
    if tol < ladder_floor(diag):
        return {"declined": f"invalid_params: tol_mm {tol} is under the kernel's floor {ladder_floor(diag)} "
                            "(1e-6 x diagonal)"}
    solids = _entities(old, "solid")
    closed = [s for s in solids if _topo_free(s) == 0]
    opened = [s for s in solids if _topo_free(s) != 0]
    kept = _faces_in(closed)
    loose = [f for f in _entities(old, "face") if not kept.Contains(f)]
    if not loose:
        return {"declined": "no_change: every face is in a closed solid; nothing to sew"}
    before = _topo_free(_compound(loose))
    if before == 0 and not opened:
        return {"declined": "no_change: the faces outside the closed solids have no free edge (make_volume)"}
    sewing = BRepBuilderAPI_Sewing(tol)
    for f in loose:
        sewing.Add(f)
    sewing.Perform()
    sewn = sewing.SewedShape()
    after = _topo_free(sewn)
    if after >= before:
        return {"declined": f"not_closed: sewing at {tol:.4g} mm left {after} free edges (had {before})"}

    from_solid = _faces_in(opened)
    parts: list[Any] = []
    made = closed_made = 0
    used = _faces_in([])
    shells = TopExp_Explorer(sewn, TopAbs_SHELL)
    while shells.More():
        shell = shells.Current()
        shells.Next()
        faces = _entities(shell, "face")
        for f in faces:
            used.Add(f)
        is_closed = _topo_free(shell) == 0
        was_solid = any(from_solid.Contains(f) or from_solid.Contains(_origin(sewing, f, loose)) for f in faces)
        if is_closed or was_solid:
            parts.append(_solid_of(shell, is_closed))
            made += 1
            closed_made += int(is_closed)
        else:
            parts.append(shell)
    faces = TopExp_Explorer(sewn, TopAbs_FACE)
    while faces.More():
        if not used.Contains(faces.Current()):
            parts.append(faces.Current())
        faces.Next()
    new = _compound([*closed, *parts])
    image = {}
    for p in parts:
        for f in _entities(p, "face"):
            image[f.__hash__()] = p

    def lookup(sub: Any) -> Any:
        if kept.Contains(sub):
            return sub
        if sub.ShapeType() == TopAbs_SOLID:
            first = _entities(sub, "face")
            moved = sewing.ModifiedSubShape(first[0]) if first else None
            return image.get(moved.__hash__()) if moved is not None else None
        return sewing.ModifiedSubShape(sub) if sewing.IsModifiedSubShape(sub) else sub

    params.update(free_edges_before=before, free_edges_after=after, solids_made=made, closed_solids_made=closed_made)
    return _record(old, new, _history_from_lookup(old, lookup), payload, "sew_gap", params, start)


def _topo_free(shape: Any) -> int:
    """Edges bordering exactly one face (a seam's two uses of one face count as two). TOPOLOGICAL, unlike
    `shells._free_edge_count`, whose FreeBounds analysis sews coincident edges virtually: measured on the dev files,
    most open parts are exactly that - coincident edges never joined - and read 0 free edges there."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    return sum(1 for i in range(1, ancestors.Extent() + 1)
               if ancestors.FindFromIndex(i).Size() == 1
               and not BRep_Tool.Degenerated_s(TopoDS.Edge_s(ancestors.FindKey(i))))


def _origin(sewing: Any, face: Any, originals: list[Any]) -> Any:
    """The original face `face` was sewn from (identity when sewing kept it)."""
    for f in originals:
        if sewing.ModifiedSubShape(f).IsSame(face):
            return f
    return face


def _non_manifold(unit: Any) -> int:
    from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk

    return len(vk._non_manifold_edges(unit))


@register_kernel_fn("op_split_non_manifold")
def _split_non_manifold(payload: dict) -> dict:
    from OCP.BRep import BRep_Builder
    from OCP.ShapeFix import ShapeFix_Shell
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Shell

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    units = _entities(old, "solid")
    free_shells = []
    explorer = TopExp_Explorer(old, TopAbs_SHELL, TopAbs_SOLID)
    while explorer.More():
        free_shells.append(explorer.Current())
        explorer.Next()
    bad = [u for u in (*units, *free_shells) if _non_manifold(u)]
    if not bad:
        return {"declined": "no_change: no unit has an edge shared by more than two faces"}
    replaced: dict[int, list[Any]] = {}
    for unit in bad:
        shell = TopoDS_Shell()
        builder = BRep_Builder()
        builder.MakeShell(shell)
        for f in _entities(unit, "face"):
            builder.Add(shell, f)
        fixer = ShapeFix_Shell()
        fixer.FixFaceOrientation(shell, True, False)
        pieces = _entities_shells(fixer.Shape())
        if len(pieces) < 2:
            continue
        replaced[unit.__hash__()] = [_solid_of(p, _free_edge_count(p) == 0) if unit.ShapeType() == TopAbs_SOLID
                                     else p for p in pieces]
    if not replaced:
        return {"declined": "not_split: ShapeFix_Shell found no manifold split of the non-manifold unit(s)"}
    parts: list[Any] = []
    for u in (*units, *free_shells):
        parts.extend(replaced.get(u.__hash__(), [u]))
    new = _compound(parts + _loose_faces(old))
    made = dict(replaced)

    def lookup(sub: Any) -> Any:
        if sub.ShapeType() == TopAbs_SOLID and sub.__hash__() in made:
            return _compound(made[sub.__hash__()])
        return sub

    params.update(units_split=len(replaced), pieces=sum(len(p) for p in replaced.values()),
                  non_manifold_left=sum(_non_manifold(p) for ps in replaced.values() for p in ps))
    return _record(old, new, _history_from_lookup(old, lookup), payload, "split_non_manifold", params, start)


def _entities_shells(shape: Any) -> list[Any]:
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    indexed = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SHELL, indexed)
    return [indexed.FindKey(i) for i in range(1, indexed.Extent() + 1)]


def _loose_faces(shape: Any) -> list[Any]:
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer

    out = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SHELL)
    while explorer.More():
        out.append(explorer.Current())
        explorer.Next()
    return out
