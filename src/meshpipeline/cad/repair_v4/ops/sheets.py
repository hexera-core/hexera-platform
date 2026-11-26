# Responsibility: `drop_sheet` (CRS v5 round 2, owner-approved 2026-11-25): remove a STRAY SHEET - a free shell or a set
#                 of loose faces that encloses no volume, belongs to no solid, touches no solid and closes none - from
#                 the part, and report it as a removal. The parent-side `Op` plus its kernel function.
# Owns: `DropSheet` (`OPS`) and the `op_drop_sheet` kernel fn.
# Boundaries: the parent half never loads OCCT. The open_shell_volume detector (`repair_v5.open_shell`) decides when a
#             defect is a sheet; this op re-checks every condition on the shape it is given and declines rather than
#             drop anything a solid could need. The loss guard still judges the result (it measures solids only, and a
#             sheet has no volume to lose).
"""Drop a stray sheet body.

search_ai cleaned d6d8f9b3439b this way (step 13): one closed solid plus a free shell open along its own outline, which
layer 1 reports as `open_shell` and which no sew or fill can close. The owner approved dropping such a sheet; large
missing faces stay unrepairable (local re-surfacing is deferred).

A unit is dropped only when ALL hold, measured here:
- it is a free shell (outside every solid) or the part's loose faces, and its faces could never enclose a volume
  (`open_shell_kernel._closable` is False);
- it shares no edge and no vertex with any solid (disjoint, so it is not part of one);
- every solid of the part is CLOSED (no free edge), so the sheet cannot be a missing face of an open solid;
- at least one solid with positive volume remains.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops.shells import _record, _run
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace


class DropSheet:
    """Remove every stray sheet (free shell or loose faces, no volume, disjoint from closed solids). Whole part."""

    name = "drop_sheet"
    #: aimed only by v5's open_shell_volume `sheet` row (owner-approved): never offered to v4's proposer nor swept
    v5_class_only = True
    params_schema: dict = {
        "type": "object",
        "x-targets": {"kind": "any", "min": 0, "max": None},
        "properties": {},
        "additionalProperties": False,
    }

    def applies_to(self, site: Site) -> bool:
        return False

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_drop_sheet", [], params, timeout_s)


OPS: tuple[Any, ...] = (DropSheet(),)


# --- kernel side ---------------------------------------------------------------------------------


def _decline(why: str) -> dict:
    return {"declined": why}


def _map(shape: Any, kind: Any) -> Any:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, out)
    return out


def _keys(m: Any) -> list[Any]:
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _free_edges(shape: Any) -> int:
    """Edges of `shape` used by exactly one face (seams, used twice by one face, and degenerate edges are not free)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, owners)
    return sum(1 for i in range(1, owners.Extent() + 1)
               if owners.FindFromIndex(i).Size() == 1 and not BRep_Tool.Degenerated_s(TopoDS.Edge_s(owners.FindKey(i))))


def _units(shape: Any) -> list[tuple[str, Any, list[Any]]]:
    """The part's non-solid units: (name, the shell or None, its faces) for every free shell and the loose faces."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    shells, exp = [], TopExp_Explorer(shape, TopAbs_SHELL, TopAbs_SOLID)
    while exp.More():
        if not any(exp.Current().IsSame(s) for s in shells):
            shells.append(exp.Current())
        exp.Next()
    out: list[tuple[str, Any, list[Any]]] = [(f"H{k}", s, _keys(_map(s, TopAbs_FACE)))
                                             for k, s in enumerate(shells, start=1)]
    loose, exp = [], TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SHELL)
    while exp.More():
        if not any(exp.Current().IsSame(f) for f in loose):
            loose.append(exp.Current())
        exp.Next()
    if loose:
        out.append(("L", None, loose))
    return out


def _closable(faces: list[Any]) -> bool:
    """`open_shell_kernel._closable` over these faces (edge uses counted among them only)."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.repair_v5.open_shell_kernel import _closable as closable

    edges = _map(_compound(faces), TopAbs_EDGE)
    uses: dict[int, list[int]] = {}
    for k, f in enumerate(faces):
        # every USE (the explorer visits a seam twice, as `open_shell_kernel` counts it), so a closed shell with a
        # periodic face still reads closable
        mine, exp = [], TopExp_Explorer(f, TopAbs_EDGE)
        while exp.More():
            mine.append(edges.FindIndex(exp.Current()))
            exp.Next()
        uses[k] = mine
    return closable(list(uses), uses)


def _without(shape: Any, drop_shells: list[Any], drop_faces: list[Any]) -> Any:
    """`shape` rebuilt without the dropped free shells and loose faces; everything else is re-added as the SAME TShape.
    A sub-compound left empty is not written (`body_kernel._is_empty`)."""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_FACE, TopAbs_SHELL
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator

    from meshpipeline.cad.repair_v4.body_kernel import _is_empty

    def gone(child: Any) -> bool:
        kind = child.ShapeType()
        pool = drop_shells if kind == TopAbs_SHELL else drop_faces if kind == TopAbs_FACE else []
        return any(child.IsSame(x) for x in pool)

    def walk(node: Any) -> Any:
        comp = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(comp)
        it = TopoDS_Iterator(node)
        while it.More():
            child = it.Value()
            if child.ShapeType() == TopAbs_COMPOUND:
                sub = walk(TopoDS.Compound_s(child))
                if not _is_empty(sub):
                    builder.Add(comp, sub)
            elif not gone(child):
                builder.Add(comp, child)
            it.Next()
        return comp

    return walk(shape)


@register_kernel_fn("op_drop_sheet")
def _drop_sheet(payload: dict) -> dict:
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools_History
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload.get("params") or {})
    solids = _keys(_map(old, TopAbs_SOLID))

    def volume(s: Any) -> float:
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(s, props)
        return float(props.Mass())

    if not any(volume(s) > 0 for s in solids):
        return _decline("no_solid: dropping the sheet would leave no solid with volume")
    open_solids = [k for k, s in enumerate(solids, start=1) if _free_edges(s)]
    if open_solids:
        return _decline(f"open_solid: solid(s) {open_solids[:5]} have free edges; the sheet may be their missing face")
    solid_edges, solid_vertices = _map(_compound(solids), TopAbs_EDGE), _map(_compound(solids), TopAbs_VERTEX)
    drop_shells, drop_faces, kept_units, dropped = [], [], [], []
    for name, shell, faces in _units(old):
        unit = shell if shell is not None else _compound(faces)
        touches = any(solid_edges.Contains(e) for e in _keys(_map(unit, TopAbs_EDGE))) or any(
            solid_vertices.Contains(v) for v in _keys(_map(unit, TopAbs_VERTEX)))
        if touches:
            kept_units.append(f"{name}: shares an edge or vertex with a solid")
            continue
        if _closable(faces):
            kept_units.append(f"{name}: its faces could enclose a volume (make_volume's case, not a sheet)")
            continue
        if shell is not None:
            drop_shells.append(shell)
        else:
            drop_faces.extend(faces)
        area = 0.0
        for f in faces:
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(f, props)
            area += float(props.Mass())
        dropped.append({"unit": name, "faces": len(faces), "area_mm2": round(area, 6)})
    if not dropped:
        return _decline("no_sheet: " + ("; ".join(kept_units) or "no free shell or loose face"))
    new = _without(old, drop_shells, drop_faces)
    kept = {kind: _map(new, kind) for kind in (TopAbs_FACE, TopAbs_EDGE)}
    history = BRepTools_History()
    for unit in [*drop_shells, *drop_faces]:
        for kind in (TopAbs_FACE, TopAbs_EDGE):
            for sub in _keys(_map(unit, kind)):
                if not kept[kind].Contains(sub):
                    history.Remove(sub)
    params["dropped"] = dropped
    if kept_units:
        params["kept"] = kept_units
    return _record(old, new, history, payload, "drop_sheet", params, start, declare_removed=True)


def _compound(shapes: list[Any]) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for s in shapes:
        builder.Add(comp, s)
    return comp
