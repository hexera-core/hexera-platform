# Responsibility: The shell-and-solid repair ops of v4 - remove_small, sew, make_volume and
#                 orient_and_close - each as a parent-side `Op` plus the kernel function it calls.
# Owns: `OPS` (the four ops, for registry wiring at integration) and the `op_*` kernel functions.
# Boundaries: the parent half never loads OCCT; every kernel function runs in a worker, imports
#             OCP lazily, and returns `ws_kernel.history_record(...)` (never a hand-built record).
#             Fidelity is judged elsewhere; this module only makes the declarations it owes it.
"""Remove small entities, sew, make a volume, orient and close.

Identity under each op (measured on the real kernel, see tests/native/..._ops_shells.py):

* `BRepBuilderAPI_Sewing` rebuilds topology wholesale: faces and edges it merges come back as NEW
  TShapes, and it keeps no BRepTools_History. It does answer `ModifiedSubShape(old)` for every face
  and edge it was given, so `sew` builds the history from that - a face keeps its id (1:1 Modified),
  edges merged pairwise retire and their merged edge is minted. Without that translation every id
  would be retired, because merged faces are not IsSame as the old ones.
* `BRepAlgoAPI_Defeaturing` and ShapeFix_* (via their ShapeBuild_ReShape) report their own history
  and `remove_small` hands it through.
* `orient_and_close` and `make_volume` keep faces as the same TShape where the kernel allows.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn

# Task 11's ops/__init__ owns these. This was a try/except shim while the two tasks ran in
# parallel; a swallowed ImportError would have silently installed DIFFERENT exception classes, so
# `except ops.OpDeclined` elsewhere would miss them while these tests still passed. Hard import.
from meshpipeline.cad.repair_v4.ops import OpDeclined, OpFailed
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace, apply_history

#: The fidelity bound is min(0.1 mm, 0.001 x diagonal). Duplicated from the contract, not imported,
#: because the checker (Tasks 2-5) is not a dependency of an op.
_BOUND_MM = 0.1
_BOUND_FRACTION = 0.001
_LADDER_FLOOR_FRACTION = 1e-6
_KIND_OF_PREFIX = {"F": "face", "E": "edge", "B": "solid"}

_SPLIT_ANGLES = (90, 60, 45)
#: `remove_small` widths and `sew` rungs as FRACTIONS of the kernel's bound, min(0.1 mm, 0.001 x diagonal)
#: (`fidelity_bound`): the sizes are made per part, never absolute, so one cannot fall outside the
#: window the kernel accepts. On a part of 100 mm or more these are the old 0.001 / 0.01 / 0.05 mm widths and a
#: 0.001 / 0.01 / 0.1 mm ladder.
_SLIVER_WIDTH_FRACTIONS = (0.01, 0.1, 0.5)
_SEW_RUNG_FRACTIONS = (0.01, 0.1, 1.0)


def scaled_sliver_widths(diag_mm: float) -> tuple[float, ...]:
    """`remove_small` widths (mm) for a part of this diagonal: fractions of `fidelity_bound`. Every one is in
    (0, bound]; a part with no usable diagonal has none."""
    if not math.isfinite(diag_mm) or diag_mm <= 0.0:
        return ()
    bound = fidelity_bound(diag_mm)
    return tuple(f * bound for f in _SLIVER_WIDTH_FRACTIONS)


#: `remove_small` widths for THE DET SWEEP as fractions OF THE DIAGONAL, with no absolute ceiling (LEAD-2, A11
#: step 2 priority 3, under the owner's 2026-11-24 removal of "op parameter bounds tied to the deviation budget").
#: `scaled_sliver_widths` above keeps the old fidelity-bound ladder, because the STAGED healer uses it and the
#: staged arm is the baseline every snapshot compares a search arm against: widening it would move the baseline
#: and confound the comparison. This widens only the arm under test.
#:
#: Measured, over the 16 distinct faces a `low_quality` failure located on the dev parts: only 2 were narrower than
#: the old ceiling, and 6 carry a tiny EDGE of 0.001 to 0.2 mm - 1 to 4 times that ceiling - on an otherwise
#: ordinary face, so the op could not reach what the mesher was failing on. The widest of those needs 0.202 mm on a
#: 46.9 mm part, i.e. 4.3e-3 of the diagonal; 1e-2 covers it with margin and treats a 1 m part and a 10 mm part
#: alike. THREE rungs, as before, so this changes the widths and not how many candidates an expansion gets.
_DET_SLIVER_WIDTH_RATIOS = (1e-4, 1e-3, 1e-2)


def det_sliver_widths(diag_mm: float) -> tuple[float, ...]:
    """`remove_small` widths (mm) the DET SWEEP proposes for a part of this diagonal: fractions of the diagonal
    itself, with no absolute ceiling. A part with no usable diagonal has none.

    What bounds the edit is the verdict, not the ladder: a removal that breaks layer 1 or loses a panel member
    scores worse and is never delivered (`search.deliverable`), and one that keeps both while moving the part is
    reported by the gaming flag. That is the owner's 2026-11-24 model - propose, then judge."""
    if not math.isfinite(diag_mm) or diag_mm <= 0.0:
        return ()
    return tuple(r * diag_mm for r in _DET_SLIVER_WIDTH_RATIOS)


def scaled_sew_ladder(diag_mm: float) -> list[float]:
    """A `sew` ladder (mm, tightest first) for a part of this diagonal: fractions of `fidelity_bound`, each kept
    only if it lies in the kernel's window [1e-6 x diagonal, bound]. The window is empty for a part over 100 m
    (the floor passes the 0.1 mm cap): then there is no ladder, `[]`, and the sweep proposes no sew."""
    if not math.isfinite(diag_mm) or diag_mm <= 0.0:
        return []
    floor, bound = ladder_floor(diag_mm), fidelity_bound(diag_mm)
    return [r for r in (f * bound for f in _SEW_RUNG_FRACTIONS) if floor <= r <= bound]


# --- parent side ---------------------------------------------------------------------------------

def _run(pool: WorkerPool, ws: Workspace, op: str, kernel_fn: str, targets: list[StableId],
         params: dict, timeout_s: float) -> tuple[Workspace, Diff]:
    """Call `kernel_fn` in a worker over `ws`'s BREP, fold its history record into a new workspace."""
    ws.resolve(list(targets))
    brep = Path(ws.brep_path)
    out_path = str(brep.with_name(f"{brep.stem}.{op}.{uuid4().hex[:8]}.brep"))
    payload = {
        "brep_path": ws.brep_path, "out_path": out_path, "quantum": ws.quantum, "params": params,
        "part_diag_mm": ws.part_diag_mm,
        "targets": [{"id": t, "type": _KIND_OF_PREFIX[t[0]], "index": ws.indices[t]} for t in targets],
        # kernel index -> stable id, so the kernel can name what it removes in `declared_removed`
        "ids": {f"{_KIND_OF_PREFIX[sid[0]]}:{index}": sid for sid, index in ws.indices.items()},
    }
    result = pool.call(kernel_fn, payload, timeout_s=timeout_s)
    if not result.ok or result.value is None:
        raise OpFailed(result.status, result.error or "no value")
    if "declined" in result.value:
        raise OpDeclined(str(result.value["declined"]))
    new = apply_history(ws, result.value)
    return new, new.history[-1]


class _ShellOp:
    name = ""
    params_schema: dict = {}
    _site_kinds: tuple[str, ...] = ()

    def applies_to(self, site: Site) -> bool:
        # B19: `_site_kinds` is a PREFERENCE now (see search._PREFERRED_FAMILIES), not a permission. `sew` on a
        # solid and `make_volume` without a closable shell are still refused, by `search._declines_in_advance`.
        return True


class RemoveSmall(_ShellOp):
    """Remove faces or edges narrower than `max_width_mm` and DECLARE every removal."""

    name = "remove_small"
    _site_kinds = ("sliver",)
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "any", "min": 1, "max": None},
        "properties": {
            # the real upper limit is min(0.1, 0.001 x diagonal), which needs the part; the kernel
            # enforces it and rejects anything above
            "max_width_mm": {"type": "number", "minimum": 1e-6},
        },
        "required": ["max_width_mm"],
        "additionalProperties": False,
    }

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_remove_small", targets, params, timeout_s)


class Sew(_ShellOp):
    """Sew loose faces at the TIGHTEST tolerance in `tol_ladder` that closes them."""

    name = "sew"
    _site_kinds = ("open",)
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "any", "min": 0, "max": None},
        "properties": {
            # each rung must be at least 1e-6 x diagonal (numerical noise below); no upper bound (B12). The kernel checks
            "tol_ladder": {"type": "array", "minItems": 1, "maxItems": 8,
                           "items": {"type": "number", "minimum": 1e-9}},
        },
        "required": ["tol_ladder"],
        "additionalProperties": False,
    }

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_sew", targets, params, timeout_s)


class MakeVolume(_ShellOp):
    """Build solid(s) from faces (the targets, or every face outside a solid when none given)."""

    name = "make_volume"
    _site_kinds = ("open",)
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "face", "min": 0, "max": None},
        "properties": {},
        "additionalProperties": False,
    }

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_make_volume", targets, params, timeout_s)


class OrientAndClose(_ShellOp):
    """Fix shell face orientation and make every solid positively oriented (positive volume)."""

    name = "orient_and_close"
    _site_kinds = ("invalid", "open")
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "solid", "min": 0, "max": None},
        "properties": {},
        "additionalProperties": False,
    }

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, self.name, "op_orient_and_close", targets, params, timeout_s)


OPS: tuple[Any, ...] = (RemoveSmall(), Sew(), MakeVolume(), OrientAndClose())


# --- kernel side ---------------------------------------------------------------------------------

def _entities(shape: Any, kind: str) -> list[Any]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    indexed = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, {"face": TopAbs_FACE, "edge": TopAbs_EDGE, "solid": TopAbs_SOLID}[kind],
                       indexed)
    return [indexed.FindKey(i) for i in range(1, indexed.Extent() + 1)]


def fidelity_bound(diag_mm: float) -> float:
    """The upper edge of every size this module's ops accept: min(0.1 mm, 0.001 x diagonal), in MILLIMETRES.
    The generators (`search.DetGenerator`, `stages._shells`) derive their rungs from this, so a rung they make
    cannot fall outside what the kernel accepts."""
    return min(_BOUND_MM, _BOUND_FRACTION * diag_mm)


def ladder_floor(diag_mm: float) -> float:
    """The lower edge of a sew rung: 1e-6 x diagonal, in millimetres. (`remove_small` has no floor, only > 0.)"""
    return _LADDER_FLOOR_FRACTION * diag_mm


def _bound(shape: Any, payload: dict | None = None) -> tuple[float, float]:
    """(diagonal, fidelity bound) of the PART (`ws_kernel.part_diagonal`: the lineage's original, lane C13)."""
    from meshpipeline.cad.repair_v4.ws_kernel import part_diagonal

    diag = part_diagonal(payload or {}, shape)
    return diag, fidelity_bound(diag)


def _record(old: Any, new: Any, history: Any, payload: dict, op: str, params: dict, start: float,
            declare_removed: bool = False) -> dict:
    """`history_record` for `new`, which is written to `out_path` first. With `declare_removed`,
    every old face/edge/solid the history says is gone is named (by stable id) in
    `declared_removed`."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    ws_kernel.write_brep(new, payload["out_path"])
    declared: list[str] = []
    if declare_removed:
        for kind in ("face", "edge", "solid"):
            for i, sub in enumerate(_entities(old, kind), start=1):
                sid = payload["ids"].get(f"{kind}:{i}")
                if sid is not None and history.IsRemoved(sub):
                    declared.append(sid)
    return ws_kernel.history_record(
        old, new, history, op=op, params=params, old_path=payload["brep_path"],
        new_path=payload["out_path"], quantum=payload["quantum"], seconds=time.perf_counter() - start,
        declared_removed=declared)


def _sub_kind(sub: Any) -> str:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID

    return {TopAbs_FACE: "face", TopAbs_EDGE: "edge", TopAbs_SOLID: "solid"}.get(sub.ShapeType(), "")


def _history_from_lookup(old: Any, lookup: Any) -> Any:
    """A BRepTools_History over old faces, edges and solids from `lookup(sub) -> Shape | None`:
    None or a null shape is a removal, the same TShape is no change, anything else is Modified into
    its same-kind pieces (a split edge answers a compound of edges)."""
    from OCP.BRepTools import BRepTools_History

    history = BRepTools_History()
    for kind in ("face", "edge", "solid"):
        for sub in _entities(old, kind):
            new = lookup(sub)
            if new is None or new.IsNull():
                history.Remove(sub)
            elif not new.IsSame(sub):
                pieces = [new] if _sub_kind(new) == kind else _entities(new, kind)
                if not pieces:
                    history.Remove(sub)
                for piece in pieces:
                    history.AddModified(sub, piece)
    return history


def _width(sub: Any, kind: str) -> float:
    """How narrow a target is, never UNDER-measuring (a removal is declared exempt from fidelity).
    An edge: its length. A face: the larger of (a) the inertia width sqrt(12 x I_min / area) - the
    short side of a rectangle, sqrt(3) x r of a disc - and (b) the second-largest bounding-box
    extent (a disc's full diameter, a square's side). Over-measuring a tilted face only makes the
    guard stricter."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    if kind == "edge":
        BRepGProp.LinearProperties_s(sub, props)
        return float(props.Mass())
    BRepGProp.SurfaceProperties_s(sub, props)
    area = float(props.Mass())
    if area <= 0:
        return 0.0
    i_min = min(props.PrincipalProperties().Moments())
    inertia = (12.0 * max(i_min, 0.0) / area) ** 0.5
    box = Bnd_Box()
    BRepBndLib.Add_s(sub, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    second = sorted((x1 - x0, y1 - y0, z1 - z0))[1]
    return max(inertia, second)


def _count_small(shape: Any, max_width: float) -> dict[str, int]:
    return {"faces": sum(1 for f in _entities(shape, "face") if _width(f, "face") <= max_width),
            "edges": sum(1 for e in _entities(shape, "edge") if _width(e, "edge") <= max_width)}


@register_kernel_fn("op_remove_small")
def _remove_small(payload: dict) -> dict:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    from OCP.ShapeFix import ShapeFix_FixSmallFace, ShapeFix_Wireframe
    from OCP.TopTools import TopTools_ListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    max_width = float(params["max_width_mm"])
    if not max_width > 0:
        return {"declined": f"invalid_params: max_width_mm {max_width} must be positive"}
    # no upper bound (it was the fidelity bound, min(0.1 mm, 0.001 x diagonal)); removals are declared and reported
    by_kind = {"face": _entities(old, "face"), "edge": _entities(old, "edge")}
    wanted: dict[str, list[Any]] = {"face": [], "edge": []}
    for target in payload["targets"]:
        kind = target["type"]
        if kind not in wanted:
            raise ValueError(f"{target['id']}: remove_small takes faces and edges, not {kind}s")
        sub = by_kind[kind][target["index"] - 1]
        width = _width(sub, kind)
        if width > max_width:
            raise ValueError(f"{target['id']} is {width:.6g} mm wide, over max_width_mm {max_width}; "
                             "refusing to remove an entity that is not small")
        wanted[kind].append(sub)

    shape, history, via = old, None, []
    if wanted["face"]:
        defeature = BRepAlgoAPI_Defeaturing()
        defeature.SetShape(old)
        tools = TopTools_ListOfShape()
        for face in wanted["face"]:
            tools.Append(face)
        defeature.AddFacesToRemove(tools)
        defeature.SetToFillHistory(True)
        defeature.Build()
        if defeature.IsDone():
            shape, history, via = defeature.Shape(), defeature.History(), ["defeaturing"]
        else:  # no solid to heal around (loose faces): collapse strip and spot faces instead
            fixer = ShapeFix_FixSmallFace()
            fixer.Init(old)
            fixer.SetPrecision(max_width)
            fixer.Perform()
            shape = fixer.FixShape()
            history = _history_from_lookup(old, fixer.Context().Apply)
            via = ["fix_small_face"]
    if wanted["edge"]:
        base = shape
        wire = ShapeFix_Wireframe(base)
        wire.SetPrecision(max_width)
        wire.SetMaxTolerance(max_width)
        wire.ModeDropSmallEdges = True
        wire.FixSmallEdges()
        fixed = wire.Shape()
        step = _history_from_lookup(base, wire.Context().Apply)
        shape = fixed
        if history is None:
            history = step
        else:
            history.Merge(step)
        via.append("fix_small_edges")
    if history is None or not (history.HasRemoved() or history.HasModified()):
        return {"declined": f"no_change: nothing at or under {max_width} mm to remove at {[t['id'] for t in payload['targets']]}"}
    params["via"] = via
    # Work still to do: a caller must see it, since the shape can pass BRepCheck with these left
    remaining = _count_small(shape, max_width)
    params["small_faces_remaining"] = remaining["faces"]
    params["small_edges_remaining"] = remaining["edges"]
    return _record(old, shape, history, payload, "remove_small", params, start, declare_removed=True)


def _free_edge_count(shape: Any) -> int:
    """Free (single-use) edges of `shape`, measured independently of the sewing that made it."""
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    bounds = ShapeAnalysis_FreeBounds(shape, 1e-9, False, False)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(bounds.GetClosedWires(), TopAbs_EDGE, edges)
    TopExp.MapShapes_s(bounds.GetOpenWires(), TopAbs_EDGE, edges)
    return int(edges.Extent())


@register_kernel_fn("op_sew")
def _sew(payload: dict) -> dict:
    """Sew at each rung of the ladder, TIGHTEST first, and keep the first rung that closes the
    shell (no free edge, no multiply-shared edge). If no rung closes it, keep the rung that leaves
    the fewest free edges (tightest on a tie), provided it improves on the input."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    diag, bound = _bound(old, payload)
    floor = ladder_floor(diag)
    ladder = sorted({float(t) for t in params["tol_ladder"]})
    off = [t for t in ladder if t < floor]
    if not ladder or off:
        # below the kernel's working floor (1e-6 x diagonal) a rung is numerical noise. There is NO upper bound any
        # more (it was the fidelity bound): a wide rung may move geometry, which is measured and reported.
        return {"declined": f"invalid_params: tol_ladder rungs {off or ladder} must be at least {floor} "
                            "(1e-6 x diagonal)"}
    if _entities(old, "solid"):
        return {"declined": "not_applicable: input carries a solid; sewing returns shells and would destroy it "
                            "(undeclared). Sew only open shells or loose faces"}
    before = _free_edge_count(old)
    if before == 0:
        return {"declined": "no_change: input has no free edges; nothing to sew"}
    best: tuple[int, float, Any, Any] | None = None
    tried: list[dict] = []
    for tol in ladder:
        sewing = BRepBuilderAPI_Sewing(tol)
        sewing.Add(old)
        sewing.Perform()
        result = sewing.SewedShape()
        free = _free_edge_count(result)
        closed = free == 0 and sewing.NbMultipleEdges() == 0
        tried.append({"tol": tol, "free_edges": free, "multiple_edges": int(sewing.NbMultipleEdges())})
        if closed:
            best = (free, tol, sewing, result)
            break
        if best is None or free < best[0]:
            best = (free, tol, sewing, result)
    assert best is not None
    free, tol, sewing, result = best
    params.update(tol_used=tol, free_edges_before=before, free_edges_after=free, tried=tried,
                  closed=free == 0)
    if free >= before and not params["closed"]:
        return {"declined": f"not_closed: sewing at {ladder} left {free} free edges (input had {before}); "
                            "no rung improved the shape"}
    history = _history_from_lookup(old, sewing.ModifiedSubShape)
    return _record(old, result, history, payload, "sew", params, start)


@register_kernel_fn("op_make_volume")
def _make_volume(payload: dict) -> dict:
    """BOPAlgo_MakerVolume over the target faces (or every face outside a solid). Solids already in
    the shape are kept. Faces the volume does not use are dropped - and show as undeclared
    removals, on purpose."""
    from OCP.BOPAlgo import BOPAlgo_MakerVolume
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound
    from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_ListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    faces = _entities(old, "face")
    if payload["targets"]:
        chosen = []
        for target in payload["targets"]:
            if target["type"] != "face":
                raise ValueError(f"{target['id']}: make_volume takes faces, not {target['type']}s")
            chosen.append(faces[target["index"] - 1])
    else:
        in_solid = TopTools_IndexedMapOfShape()
        solids = TopExp_Explorer(old, TopAbs_SOLID)
        while solids.More():
            inner = TopExp_Explorer(solids.Current(), TopAbs_FACE)
            while inner.More():
                in_solid.Add(inner.Current())
                inner.Next()
            solids.Next()
        chosen = [f for f in faces if not in_solid.Contains(f)]
    if not chosen:
        return {"declined": "not_applicable: no faces outside a solid to build a volume from"}

    maker = BOPAlgo_MakerVolume()
    arguments = TopTools_ListOfShape()
    for face in chosen:
        arguments.Append(face)
    maker.SetArguments(arguments)
    maker.SetIntersect(False)  # sewn faces already meet at shared edges; do not re-split them
    maker.SetToFillHistory(True)
    maker.Perform()
    if maker.HasErrors():
        return {"declined": "no_volume: MakerVolume could not form a volume from these faces"}
    volumes = _entities(maker.Shape(), "solid")
    if not volumes:
        return {"declined": "no_volume: MakerVolume found no closed volume in these faces"}

    new = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(new)
    for solid in _entities(old, "solid"):
        builder.Add(new, solid)
    for solid in volumes:
        builder.Add(new, solid)
    params["volumes"] = len(volumes)
    return _record(old, new, maker.History(), payload, "make_volume", params, start)


def _volume(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


@register_kernel_fn("op_orient_and_close")
def _orient_and_close(payload: dict) -> dict:
    """ShapeFix_Solid then BRepLib::OrientClosedSolid on each solid so it has positive volume, and
    ShapeFix_Shell on each shell that is not part of a solid (consistent face orientation). Flipping
    orientation keeps every TShape, so identity survives untouched."""
    from OCP.BRepLib import BRepLib
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.ShapeFix import ShapeFix_Shell, ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    all_solids = _entities(old, "solid")
    solids = all_solids
    if payload["targets"]:
        solids = []
        for target in payload["targets"]:
            if target["type"] != "solid":
                raise ValueError(f"{target['id']}: orient_and_close takes solids, not {target['type']}s")
            solids.append(all_solids[target["index"] - 1])
    reshape = ShapeBuild_ReShape()
    for solid in solids:
        fixer = ShapeFix_Solid(TopoDS.Solid_s(solid))
        fixer.Perform()
        fixed = TopoDS.Solid_s(fixer.Solid())
        BRepLib.OrientClosedSolid_s(fixed)
        if _volume(fixed) <= 0:
            fixed = TopoDS.Solid_s(fixed.Reversed())
        if _volume(fixed) <= 0:
            raise RuntimeError("solid has non-positive volume either way round: its shell is not "
                               "closed; sew or make_volume first")
        if not fixed.IsEqual(solid):
            reshape.Replace(solid, fixed)
    if not payload["targets"]:  # shells outside any solid
        free = TopExp_Explorer(old, TopAbs_SHELL, TopAbs_SOLID)
        while free.More():
            shell_fixer = ShapeFix_Shell(TopoDS.Shell_s(free.Current()))
            if shell_fixer.Perform() and not shell_fixer.Shell().IsEqual(free.Current()):
                reshape.Replace(free.Current(), shell_fixer.Shell())
            free.Next()
    new = reshape.Apply(old)
    history = _history_from_lookup(old, reshape.Apply)
    return _record(old, new, history, payload, "orient_and_close", params, start)
