# Responsibility: The kernel side of the body ops - fragment touching bodies into one conformal set
#                 (optionally cutting a tiny overlap first) and merge coincident duplicate faces.
# Owns: `conformalise` and `remove_duplicate_faces` kernel functions (registered by name), and the
#       history composition that lets an untouched or merely re-wired entity keep its stable id.
# Boundaries: runs INSIDE a worker; OCP is imported lazily so the parent never loads it. The parent
#             side (Op protocol objects, schemas, applying the record) is ops/bodies.py.
"""Body-level healing: share faces between touching bodies without changing the body count.

Constraints (Global Constraints of the v4 plan): no `fuse`; `cut_by` only when the overlap is within
the fidelity bound `min(0.1 mm, 0.001 * diagonal)`, otherwise the call DECLINES (it never clamps).
"Within the bound" is read as overlap DEPTH (a length, like the bound; a volume would be
dimensionally meaningless): HOW FAR THE CUT MOVES THE VICTIM'S SURFACE, measured (`_cut_depth`). A
second guard stops a thin-but-vast cut passing on depth alone: the removed volume may not exceed
2% of the body it is cut from (`volume_change`). `conformalise` also declines
`no_shared_interface` if, afterwards, no face is shared by two bodies;
the number of solids is unchanged. A decline is a normal result: `{"declined": <reason>, ...}`.

Identity: BOPAlgo's non-destructive Builder leaves an argument's untouched faces the same TShape, but
re-wires the neighbours of a merged face (their shared edge becomes one edge), so those are reported
as 1:1 Modified. When two coincident faces/edges collapse into one, the lowest-index member
survives (keeps its id) and the others are reported REMOVED and declared, rather than as an N:M
merge that would retire every id involved.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import (
    _maps,
    diagonal_of,
    entities_of,
    fingerprint,
    history_record,
    part_diagonal,
    read_brep,
    shape_signature,
    write_brep,
)

#: The fidelity bound min(ABS_BOUND_MM, REL_BOUND x diagonal), REPORTED beside measurements; it refuses nothing (B12).
ABS_BOUND_MM = 0.1
REL_BOUND = 0.001
_KINDS = ("face", "edge", "solid")
#: A volume this small, as a fraction of the part's bounding-box diagonal CUBED, is ZERO to the body ops. Relative, not
#: absolute: the part's own size decides what is negligible (a 1e-6 mm3 sliver is noise on a 300 mm part and not on a
#: 0.3 mm one). Cubing the diagonal gives a volume scale; 1e-9 of it is far below any feature a mesher resolves (on a
#: 100 mm part, 1e-3 mm3 - a 0.1 mm cube) and far above the 1e-13 to 1e-10 mm3 OCCT round-off that lane C11 measured on
#: the degenerate pieces a cut left on dev 0051_00518960. The SAME threshold says when two bodies merely touch
#: (`_conformalise`'s overlap test) and when a piece a cut leaves has no volume, so the op judges both alike.
ZERO_VOLUME_FRACTION = 1e-9
#: `_degenerate`'s union bounds hold within this fraction of the targets' total volume (Boolean volume rounding)
UNION_REL_TOL = 1e-6


def zero_volume_mm3(diagonal: float) -> float:
    return ZERO_VOLUME_FRACTION * max(diagonal, 1.0) ** 3


def fidelity_bound(diagonal: float) -> float:
    return min(ABS_BOUND_MM, REL_BOUND * diagonal)


def _declined(reason: str, **detail: Any) -> dict:
    return {"declined": reason, "detail": detail}


def _volume(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def _area(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return float(props.Mass())


def _extents(shape: Any) -> tuple[float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return (x1 - x0, y1 - y0, z1 - z0)


def _is_valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


def _list(shapes: list[Any]) -> Any:
    from OCP.TopTools import TopTools_ListOfShape

    out = TopTools_ListOfShape()
    for s in shapes:
        out.Append(s)
    return out


def _solid_count(shape: Any) -> int:
    return int(_maps(shape)["solid"].Extent())


def _common_volume(a: Any, b: Any, fuzzy: float) -> float:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    op = BRepAlgoAPI_Common()
    op.SetArguments(_list([a]))
    op.SetTools(_list([b]))
    op.SetFuzzyValue(fuzzy)
    op.Build()
    if not op.IsDone():
        raise RuntimeError("common failed while measuring overlap")
    return _volume(op.Shape()) if _solid_count(op.Shape()) else 0.0


#: sample points over the victim's surface inside the keeper, and the wall-clock the measurement may take
DEPTH_SAMPLES = 400
DEPTH_BUDGET_S = 20.0


def _cut_depth(victim: Any, keeper: Any, fuzzy: float) -> tuple[float, float, str]:
    """(overlap volume, depth, method) for cutting `victim` by `keeper`.

    THE DEPTH IS HOW FAR THE CUT MOVES THE VICTIM'S SURFACE. The overlap C = victim n keeper is bounded by two kinds
    of face: pieces of the victim's surface that lie inside the keeper (the cut removes them) and pieces of the
    keeper's surface that lie inside the victim (the cut leaves the victim bounded by them). The depth is the largest
    distance from the first to the second, sampled - a slab of thickness t reads t, a corner cube of side s reads s,
    in any orientation. (It replaced max(2V/A, thinnest AXIS-ALIGNED bbox extent of C), which read millimetres for a
    thin INCLINED slab and declined cuts well inside the bound: lane B6 measured it 5-12x over on real files.)

    Falls back to the mean thickness 2V/A of C (method "mean_thickness") when there is no movement to measure: a
    duplicate or contained body (no victim surface lies inside the keeper alone), a face set that cannot be
    classified, or a measurement over its budget. Never 0 for a real overlap."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    op = BRepAlgoAPI_Common()
    op.SetArguments(_list([victim]))
    op.SetTools(_list([keeper]))
    op.SetFuzzyValue(fuzzy)
    op.Build()
    if not op.IsDone():
        raise RuntimeError("common failed while measuring overlap")
    common = op.Shape()
    if not _solid_count(common):
        return 0.0, 0.0, "none"
    volume, area = _volume(common), _area(common)
    if volume <= 0.0 or area <= 0.0:
        return 0.0, 0.0, "none"
    mean = 2.0 * volume / area
    try:
        measured = _surface_movement(common, victim, keeper)
    except Exception:  # noqa: BLE001 - a measurement that broke is not a reason to admit or refuse a cut
        measured = None
    if measured is None:
        return volume, mean, "mean_thickness"
    return volume, measured, "measured"


def _surface_movement(common: Any, victim: Any, keeper: Any) -> float | None:
    """The largest distance from C's victim-side faces to its keeper-side faces, or None (see `_cut_depth`)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.repair_v4.verdict.faithful_kernel import _compound, _face_points, _faces

    deadline = time.perf_counter() + DEPTH_BUDGET_S
    on_victim, on_keeper = _compound(_faces(victim)), _compound(_faces(keeper))
    tol = max(1e-7, 1e-6 * max(_extents(common)))

    def dist(point: tuple[float, float, float], target: Any) -> float:
        d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex(), target)
        return float(d.Value()) if d.IsDone() else float("inf")

    victim_side, keeper_side = [], []
    for face in _faces(common):
        probe = _face_points(face, 1)[0]
        if dist(probe, on_keeper) <= tol:
            keeper_side.append(face)       # on the keeper's surface (including faces the two bodies share)
        elif dist(probe, on_victim) <= tol:
            victim_side.append(face)       # the victim's own surface, inside the keeper: what the cut removes
        else:
            return None                    # a face on neither surface: the classification cannot be trusted
        if time.perf_counter() > deadline:
            return None
    if not victim_side or not keeper_side:
        return None                        # a duplicate or contained body: no movement to measure
    target = _compound(keeper_side)
    per_face = max(4, DEPTH_SAMPLES // len(victim_side))
    worst = 0.0
    for face in victim_side:
        for point in _face_points(face, per_face):
            worst = max(worst, dist(point, target))
            if time.perf_counter() > deadline:
                return None
    return worst


def _builder(args: list[Any], fuzzy: float) -> Any:
    """BRepAlgoAPI_BuilderAlgo (the History-keeping wrapper of BOPAlgo_Builder) as a fragment:
    non-destructive, glue-shift (bodies only touch or coincide), the caller's fuzzy value."""
    from OCP.BOPAlgo import BOPAlgo_GlueShift
    from OCP.BRepAlgoAPI import BRepAlgoAPI_BuilderAlgo

    alg = BRepAlgoAPI_BuilderAlgo()
    alg.SetArguments(_list(args))
    alg.SetNonDestructive(True)
    alg.SetGlue(BOPAlgo_GlueShift)
    alg.SetFuzzyValue(fuzzy)
    alg.SetToFillHistory(True)
    alg.Build()
    if not alg.IsDone():
        raise RuntimeError("fragment (BOPAlgo_Builder) failed")
    return alg


def _is_empty(compound: Any) -> bool:
    """True when `compound` has no children. A rebuilt sub-compound whose every solid an op dropped is EMPTY, and it must
    not be written: netgen_occ cannot read a STEP holding one ('Bnd_Box is void', reported `step_unreadable`). Lane C11
    on two overlapping boxes: cut_by / fuse output passed 3 of 4 members with the empty compound, 4 of 4 and CLEAN
    without it, same TShapes and ids. XCAF reads nest every solid one compound down, so on real files this was the
    common case, and no body op could end clean."""
    from OCP.TopoDS import TopoDS_Iterator

    return not TopoDS_Iterator(compound).More()


def _rebuild(shape: Any, drop: Any, replacements: list[Any]) -> Any:
    """`shape` with the solids in `drop` (an indexed map) removed and `replacements` added. Anything
    else is re-added as the SAME TShape, which is what keeps untouched ids alive."""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator

    def walk(node: Any, top: bool) -> Any:
        comp = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(comp)
        it = TopoDS_Iterator(node)
        while it.More():
            child = it.Value()
            if child.ShapeType() == TopAbs_SOLID and drop.Contains(child):
                pass
            elif child.ShapeType() == TopAbs_COMPOUND:
                sub = walk(TopoDS.Compound_s(child), False)
                if not _is_empty(sub):  # lane C11: see `_is_empty`
                    builder.Add(comp, sub)
            else:
                builder.Add(comp, child)
            it.Next()
        if top:
            for r in replacements:
                builder.Add(comp, r)
        return comp

    return walk(shape, True)


def _after(hist: Any, sub: Any) -> list[Any]:
    """What `sub` became under one operation: its Modified shapes, nothing if removed, else itself."""
    mods = list(hist.Modified(sub))
    if mods:
        return mods
    return [] if hist.IsRemoved(sub) else [sub]


def _composed_history(old: Any, new: Any, stages: list[tuple[Any, Any]], targets: list[Any]) -> tuple[Any, dict]:
    """One BRepTools_History old -> new from the sequential `stages`, each `(history, scope)`: a
    stage with a scope solid speaks ONLY about that solid's entities (a Cut's History reports its
    tool as removed, which must not kill the tool body); `scope=None` is the whole shape. Coincident entities that
    collapse into one reported as: lowest old index survives, the rest REMOVED.

    Returns (history, removed_by_kind) where removed_by_kind[kind] lists the OLD indices the op took
    out - merged away or cut away (those, and only those, are what the op declares)."""
    from OCP.BRepTools import BRepTools_History

    old_maps, new_maps = _maps(old), _maps(new)
    in_targets: dict[str, set[int]] = {k: set() for k in _KINDS}
    for solid in targets:
        sub_maps = _maps(solid)
        for kind in _KINDS:
            for i in range(1, sub_maps[kind].Extent() + 1):
                in_targets[kind].add(int(old_maps[kind].FindIndex(sub_maps[kind].FindKey(i))))

    scopes = [None if scope is None else _maps(scope) for _, scope in stages]

    def in_scope(scope_maps: Any, kind: str, sub: Any) -> bool:
        return scope_maps is None or bool(scope_maps[kind].Contains(sub))

    def finals(kind: str, index: int) -> list[Any]:
        imgs = [old_maps[kind].FindKey(index)]
        for (stage, _), scope_maps in zip(stages, scopes, strict=True):
            imgs = [m for x in imgs
                    for m in (_after(stage, x) if in_scope(scope_maps, kind, x) else [x])]
        return imgs

    hist = BRepTools_History()
    merged: dict[str, list[int]] = {k: [] for k in _KINDS}
    for kind in _KINDS:
        groups: dict[int, list[int]] = {}
        for index in sorted(in_targets[kind]):
            imgs = finals(kind, index)
            if len(imgs) == 1 and new_maps[kind].FindIndex(imgs[0]) > 0:
                groups.setdefault(int(new_maps[kind].FindIndex(imgs[0])), []).append(index)
        for members in groups.values():
            merged[kind].extend(members[1:])  # members are in ascending old-index order
        away = set(merged[kind])
        for index in sorted(in_targets[kind]):
            sub = old_maps[kind].FindKey(index)
            if index in away:
                hist.Remove(sub)
                continue
            imgs = finals(kind, index)
            if not imgs:
                hist.Remove(sub)
                merged[kind].append(index)  # cut away: also this op's own removal, so declared
            elif not (len(imgs) == 1 and imgs[0].IsSame(sub)):
                for m in imgs:
                    hist.AddModified(sub, m)
            for (stage, _), scope_maps in zip(stages, scopes, strict=True):
                if in_scope(scope_maps, kind, sub):
                    for g in stage.Generated(sub):
                        hist.AddGenerated(sub, g)
    return hist, merged


def _ids(payload: dict, kind: str, indices: list[int]) -> list[str]:
    table = payload.get("ids", {}).get(kind, {})
    return [table[str(i)] for i in indices if str(i) in table]



#: B24 (ii), INSTRUMENTATION ONLY, off unless CAD_REPAIR_V4_DEBUG_RECORD names an index. No behaviour change,
#: and nothing is computed when it is unset: wave 202611242110 refused a `conformalise` record whose row for one
#: edge carried a fingerprint held by NO workspace in the lineage, and the surviving explanation is that the
#: rows are computed over an INTERMEDIATE stage shape rather than the `old` the workspace holds. This prints the
#: same index's fingerprint from `old`, from `new`, and from every stage, which separates those directly.
def _debug_record_shapes(payload: dict, old: Any, new: Any, stages: list[tuple[Any, Any]]) -> None:
    import os

    from meshpipeline.settings.env import optional_env

    want = optional_env("CAD_REPAIR_V4_DEBUG_RECORD", "")
    if not want:
        return
    import sys

    kind, _, raw = want.partition(":")
    try:
        index = int(raw)
    except ValueError:
        return
    quantum = float(payload["quantum"])

    def at(shape: Any, label: str) -> str:
        indexed = _maps(shape).get(kind)
        if indexed is None or not (1 <= index <= indexed.Extent()):
            return f"{label}: no {kind} at {index} (extent {0 if indexed is None else indexed.Extent()})"
        sub = indexed.FindKey(index)
        return (f"{label}: {fingerprint(kind, sub, quantum, index)}"
                f"{'' if sub.Location().IsIdentity() else ' [LOCATED]'} (extent {indexed.Extent()})")

    print(f"DEBUG_RECORD pid {os.getpid()} {kind} {index} for op on {payload.get('brep_path')}",
          file=sys.stderr)
    print(f"  {at(old, 'old   ')}", file=sys.stderr)
    print(f"  {at(new, 'new   ')}", file=sys.stderr)
    for i, entry in enumerate(stages, start=1):
        shape = entry[0] if isinstance(entry, tuple) else entry
        try:
            print(f"  {at(shape, f'stage{i}')}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - instrumentation never breaks the op
            print(f"  stage{i}: not measurable ({type(exc).__name__}: {exc})", file=sys.stderr)

def _finish(payload: dict, old: Any, stages: list[tuple[Any, Any]], targets: list[Any], new_solids: list[Any],
            *, op: str, params: dict, start: float, must_merge: list[tuple[int, int]] | None = None,
            require_shared: bool = False, expected_solids: int | None = None) -> dict:
    """Assemble the new compound, check it, write it, and build the workspace record. `expected_solids`: the solid
    count the op means to leave (a fuse, a cut that splits or removes its victim); None means unchanged."""
    from OCP.TopTools import TopTools_IndexedMapOfShape

    drop = TopTools_IndexedMapOfShape()
    for s in targets:
        drop.Add(s)
    # CRS v5 round 2: a Boolean's own output is ShapeFixed BEFORE it is judged (the owner's order: heal, Boolean,
    # ShapeFix the result); only the solids that fail BRepCheck are touched, and the fix is a history stage
    new_solids, fixed = fix_solids(new_solids)
    if fixed["stages"]:
        stages = [*stages, *fixed["stages"]]
        params = {**params, "result_fixed": fixed["fixed"]}
    new = _rebuild(old, drop, new_solids)
    want = _solid_count(old) if expected_solids is None else expected_solids
    if _solid_count(new) != want:
        return _declined("changes_body_count", before=_solid_count(old), after=_solid_count(new), expected=want)
    if not _is_valid(new):
        # JUDGE WHAT THE OP MADE, NOT THE PART IT WAS GIVEN. On 3e7c2a1197c3 (step 13) the staged healer's
        # split_closed left an UNTOUCHED body (B6, 247 faces) BRepCheck-invalid, so every Boolean on every other pair
        # read `invalid_result` (150 candidates) although its own solids were valid (jig probe). A part that was
        # already invalid is declined only when the op's own solids are invalid.
        bad = sum(1 for s in new_solids if not _is_valid(s))
        if bad or _is_valid(old):
            return _declined("invalid_result", invalid_solids=bad, of=len(new_solids))
    if _same_state(old, new, float(payload["quantum"])):
        # Lane C10: a fragment of bodies that already share faces (or do not touch) rebuilds the SAME shape and, until
        # this, returned it as a success: the search found the state already seen, recorded a duplicate, and spent an
        # op (11 ops on one dev file in v2, 4 by search_det and 7 by search_ai, which nothing told it was a no-op).
        return _declined("no_change", detail="the result is the input: same entities, same shape signature")
    if require_shared and _shared_face_count(new) == 0:
        # Success that shared nothing would record progress that did not happen (e.g. a gap wider
        # than the fuzzy value after STEP rounding): refuse, and say why.
        return _declined("no_shared_interface", detail="no face is shared by two bodies afterwards")
    _debug_record_shapes(payload, old, new, stages)
    hist, merged = _composed_history(old, new, stages, targets)
    gone = set(merged["face"])
    for a_i, b_i in must_merge or []:
        if a_i not in gone and b_i not in gone:
            return _declined("not_merged", faces=[a_i, b_i])
    write_brep(new, payload["out_path"])
    removed = _ids(payload, "face", merged["face"]) + _ids(payload, "edge", merged["edge"])
    record = history_record(
        old, new, hist, op=op, params=params, old_path=payload["brep_path"],
        new_path=payload["out_path"], quantum=payload["quantum"],
        seconds=time.perf_counter() - start, declared_removed=removed)
    return {"record": record, "merged_faces": merged["face"], "merged_edges": merged["edge"]}


def _only_solid(shape: Any) -> Any | None:
    solids = _maps(shape)["solid"]
    return solids.FindKey(1) if solids.Extent() == 1 else None


def heal_solid(solid: Any, *, unify: bool = True) -> tuple[Any, list[tuple[Any, Any]]]:
    """(`solid` healed, its history stages) - ShapeFix_Shape, then (`unify`) UnifySameDomain on faces and edges -
    or (`solid`, []) when the heal does not give back exactly one VALID solid (it never makes a body worse)."""
    from OCP.ShapeFix import ShapeFix_Shape
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    try:
        fix = ShapeFix_Shape(solid)
        fix.Perform()
        out, stages = _only_solid(fix.Shape()), [(fix.Context().History(), None)]
        if out is not None and unify:
            usd = ShapeUpgrade_UnifySameDomain(out, True, True, False)
            usd.Build()
            unified = _only_solid(usd.Shape())
            if unified is not None and _is_valid(unified):
                out, stages = unified, [*stages, (usd.History(), None)]
    except Exception:  # noqa: BLE001 - a heal that throws leaves the body as it was
        return solid, []
    if out is None or not _is_valid(out) or out.IsSame(solid):
        return solid, []
    return out, stages


def heal_targets(targets: list[Any], heal: bool) -> tuple[list[Any], dict]:
    """(the Boolean's arguments, `{"stages", "healed"}`): `targets` themselves, or (`heal`) each healed by `heal_solid`
    with the heal's history stages, which the op's composed history runs before its own."""
    if not heal:
        return list(targets), {"stages": [], "healed": 0}
    out, stages = [], []
    for t in targets:
        healed, mine = heal_solid(t)
        out.append(healed)
        stages.extend(mine)
    return out, {"stages": stages, "healed": sum(1 for a, b in zip(out, targets, strict=True) if a is not b)}


def fix_solids(solids: list[Any]) -> tuple[list[Any], dict]:
    """`solids` with each BRepCheck-INVALID one replaced by its ShapeFix_Shape result when that is one valid solid,
    and `{"stages": the fixes' history stages, in order, "fixed": how many}`. Valid solids are untouched."""
    out: list[Any] = []
    stages: list[tuple[Any, Any]] = []
    for s in solids:
        if _is_valid(s):
            out.append(s)
            continue
        healed, mine = heal_solid(s, unify=False)
        out.append(healed)
        stages.extend(mine)
    return out, {"stages": stages, "fixed": sum(1 for a, b in zip(out, solids, strict=True) if a is not b)}


def _same_state(old: Any, new: Any, quantum: float) -> bool:
    """True when `new` is the state `old` already is, by the very test the search dedupes on (`search._fingerprint`:
    the sorted entity fingerprints and the shape signature), so an op never reports as applied what the search would
    only discard as a duplicate."""
    def key(shape: Any) -> tuple:
        return sorted(f"{e['type']}:{e['fp']}" for e in entities_of(shape, quantum)), shape_signature(shape)

    return key(old) == key(new)


def _shared_face_count(shape: Any) -> int:
    """Faces owned by two solids at once (one TShape): the conformal interface."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SOLID, owners)
    return sum(1 for i in range(1, owners.Extent() + 1) if owners.FindFromIndex(i).Size() >= 2)


def _targets(old: Any, indices: list[int]) -> list[Any]:
    solids = _maps(old)["solid"]
    return [solids.FindKey(i) for i in indices]


@register("v4_overlap_depths")
def _overlap_depths(payload: dict) -> dict:
    """For each body pair `[i, j]` (1-based solid indices), BOTH cuts measured by `_cut_depth`, the measure
    `conformalise(cut_by)` declines on: `a_by_b` cuts solid i by solid j, `b_by_a` the reverse. With the part's diagonal
    and fidelity bound. For the evaluation's report (the owner's "both ways" overlap ruling); changes no shape."""
    old = read_brep(payload["brep_path"])
    diag = diagonal_of(old)
    solids = _maps(old)["solid"]
    pairs = []
    for i, j in payload["pairs"]:
        a, b = solids.FindKey(int(i)), solids.FindKey(int(j))
        entry: dict[str, Any] = {"bodies": [f"S{i}", f"S{j}"]}
        for name, (victim, keeper) in (("a_by_b", (a, b)), ("b_by_a", (b, a))):
            try:
                volume, depth, method = _cut_depth(victim, keeper, 0.0)
                entry[name] = {"depth_mm": depth, "method": method, "volume_mm3": volume}
            except Exception as exc:  # noqa: BLE001 - one pair that cannot be measured says so; the others stand
                entry[name] = {"depth_mm": None, "method": f"error: {type(exc).__name__}", "volume_mm3": None}
        pairs.append(entry)
    return {"diag_mm": diag, "bound_mm": fidelity_bound(diag), "pairs": pairs}


def _drop_zero_volume(solids: list[Any], eps: float) -> tuple[list[Any], dict]:
    """`solids` without the pieces whose |volume| <= `eps`, and a history stage that REMOVES what only those pieces
    held. Lane C11 on dev 0051_00518960: cut_by:B4 left two 2-face 'solids' of 1e-13 and 1e-10 mm3, each
    BRepCheck-valid alone, which layer 1 reports as `non_positive_volume`, so the whole output was invalid. Dropped,
    the part is VALID. This is NOT a repair win: that file stays 0/4 on the panel, and the cut itself cost netgen_occ
    the pass it had on the input. It only stops the op emitting a shape layer 1 must reject.
    A sub-shape a dropped piece SHARES with a kept solid is not removed: it lives on in the kept one."""
    from OCP.BRepTools import BRepTools_History

    kept, volumes = [], []
    gone = []
    for solid in solids:
        v = _volume(solid)
        if abs(v) <= eps:
            gone.append(solid)
            volumes.append(v)
        else:
            kept.append(solid)
    if not gone:
        return solids, {"history": None, "volumes": []}
    kept_maps = [_maps(s) for s in kept]
    hist = BRepTools_History()
    for piece in gone:
        maps = _maps(piece)
        for kind in _KINDS:
            for i in range(1, maps[kind].Extent() + 1):
                sub = maps[kind].FindKey(i)
                if not any(m[kind].Contains(sub) for m in kept_maps):
                    hist.Remove(sub)
    return kept, {"history": hist, "volumes": volumes}


@register("v4_conformalise")
def _conformalise(payload: dict) -> dict:
    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    fuzzy = float(payload.get("fuzzy_mm", 0.0))
    mode = payload["mode"]
    part = part_diagonal(payload, old)  # lane C13: the part as it arrived, not this shape's pole box
    bound = fidelity_bound(part)
    # B12: no fuzzy cap. The bound is computed only to be REPORTED beside each measured overlap.
    originals = _targets(old, payload["bodies"])
    cut_by = payload.get("cut_by")
    keeper = _targets(old, [cut_by])[0] if cut_by is not None else None
    if mode != "fragment" and keeper is None:
        raise ValueError(f"mode {mode!r} needs a cut_by body")
    # CRS v5 round 2: `heal_targets` runs the Boolean on healed bodies (ShapeFix + UnifySameDomain, as history)
    targets, heal = heal_targets(originals, bool(payload.get("heal_targets")))
    if keeper is not None:
        keeper = next((h for o, h in zip(originals, targets, strict=True) if o.IsSame(keeper)), keeper)

    # Every overlap is measured BEFORE anything is changed, so a decline leaves nothing half done. The depth, method
    # and volume are RECORDED, never a reason to decline: the owner's 2026-11-24 goal removed the fidelity gate (lane C7;
    # clean = valid + panel, faithfulness reported), so a cut at any depth is the verdict's to judge.
    cuts: list[Any] = []
    measured: list[dict] = []
    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            volume = _common_volume(a, b, fuzzy)
            if volume <= zero_volume_mm3(part):
                continue  # touching, not overlapping
            involves_keeper = keeper is not None and (a.IsSame(keeper) or b.IsSame(keeper))
            if not involves_keeper:
                return _declined("unresolved_overlap", volume=volume)
            victim = b if a.IsSame(keeper) else a
            volume, depth, method = _cut_depth(victim, keeper, fuzzy)
            measured.append({"depth_mm": depth, "method": method, "volume_mm3": volume, "bound_mm": bound,
                             "volume_fraction": volume / max(_volume(victim), 1e-12)})
            cuts.append(victim)

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    stages: list[tuple[Any, Any]] = list(heal["stages"])
    current = list(targets)
    for victim in cuts:
        cut = BRepAlgoAPI_Cut()
        cut.SetArguments(_list([victim]))
        cut.SetTools(_list([keeper]))
        cut.SetNonDestructive(True)
        cut.SetFuzzyValue(fuzzy)
        cut.SetToFillHistory(True)
        cut.Build()
        if not cut.IsDone():
            return _declined("cut_failed", detail="BRepAlgoAPI_Cut did not complete")
        # a deep cut may leave the victim in several pieces, or in none (a body inside its keeper): both are what the
        # cut means, and the body count is reported by layer 3, not refused here (lane C7)
        pieces = [_maps(cut.Shape())["solid"].FindKey(k) for k in range(1, _solid_count(cut.Shape()) + 1)]
        stages.append((cut.History(), victim))
        current = [p for t in current for p in (pieces if t.IsSame(victim) else [t])]
    frag = _builder(current, fuzzy)
    stages.append((frag.History(), None))
    result_solids = _maps(frag.Shape())["solid"]
    new_solids = [result_solids.FindKey(i) for i in range(1, result_solids.Extent() + 1)]
    new_solids, dropped = _drop_zero_volume(new_solids, zero_volume_mm3(part))
    bad = _degenerate(targets, new_solids, zero_volume_mm3(part), fuzzy_mm=fuzzy)
    if bad is not None:
        return _declined("cut_degenerate" if keeper is not None else "fragment_degenerate", **bad)
    if dropped["history"] is not None:
        stages.append((dropped["history"], None))
    params = {"bodies": payload.get("body_ids", []), "fuzzy_mm": fuzzy, "mode": mode,
              "measured": {"overlaps": measured, "solids_before": len(targets), "solids_after": len(new_solids),
                           "zero_volume_pieces_dropped": dropped["volumes"]},
              **({"healed_targets": heal["healed"]} if heal["healed"] else {})}
    return _finish(payload, old, stages, originals, new_solids, op="conformalise", params=params,
                   start=start, require_shared=len(new_solids) >= 2,
                   expected_solids=_solid_count(old) - len(targets) + len(new_solids))


def _faces_coincide(a: Any, b: Any, tol: float) -> bool:
    """Same extent, same area, same centre - to within `tol` (mm)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    def box(s: Any) -> tuple[float, ...]:
        bx = Bnd_Box()
        BRepBndLib.Add_s(s, bx)
        return tuple(bx.Get())

    ba, bb = box(a), box(b)
    if any(abs(x - y) > tol + 1e-6 for x, y in zip(ba, bb, strict=True)):
        return False
    return abs(_area(a) - _area(b)) <= max(tol, 1e-6) * max(_extents(a))


@register("v4_remove_duplicate_faces")
def _remove_duplicate_faces(payload: dict) -> dict:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    bound = fidelity_bound(part_diagonal(payload, old))  # lane C13
    faces = _maps(old)["face"]
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(old, TopAbs_FACE, TopAbs_SOLID, owners)
    parents: list[Any] = []
    pairs = [tuple(p) for p in payload["pairs"]]
    for a_i, b_i in pairs:
        fa, fb = faces.FindKey(a_i), faces.FindKey(b_i)
        if not _faces_coincide(fa, fb, bound):
            return _declined("not_coincident", faces=[a_i, b_i])
        owner_a = list(owners.FindFromKey(fa))
        owner_b = list(owners.FindFromKey(fb))
        if not owner_a or not owner_b or owner_a[0].IsSame(owner_b[0]):
            return _declined("same_body", faces=[a_i, b_i])
        for owner in (owner_a[0], owner_b[0]):
            if not any(owner.IsSame(p) for p in parents):
                parents.append(owner)
    frag = _builder(parents, 0.0)
    result_solids = _maps(frag.Shape())["solid"]
    new_solids = [result_solids.FindKey(i) for i in range(1, result_solids.Extent() + 1)]
    if len(new_solids) != len(parents):
        return _declined("changes_body_count", before=len(parents), after=len(new_solids))
    params = {"pairs": [list(p) for p in payload.get("pair_ids", [])]}
    return _finish(payload, old, [(frag.History(), None)], parents, new_solids,
                   op="remove_duplicate_faces", params=params, start=start, must_merge=pairs)


def _degenerate(targets: list[Any], result: list[Any], eps: float, fuzzy_mm: float = 0.0) -> dict | None:
    """Why a Boolean's result cannot be the UNION of `targets`, or None. A union is never smaller than its largest
    member nor larger than the sum of its members, and no solid has negative signed volume (an inside-out solid).

    Lane C15, dev 0051_00510567 on v4: OCCT's BRepAlgoAPI_Fuse, run on eight overlapping bodies after the staged
    healer's split_closed, returned 4 solids of +0.14, +19.6, +0.02 and -13.5 mm3 from bodies totalling 47,870 mm3,
    and reported no failure. The fuse passed BRepCheck (an inside-out solid does), the STEP round trip re-oriented the
    solid so layer 1 called it VALID, and validity-first ranking delivered 0.07% of the part. This detects a broken
    Boolean; it is not a fidelity bound: any real union passes it, whatever it does to the part."""
    vols = [_volume(t) for t in targets]
    got = [_volume(x) for x in result]
    neg = [v for v in got if v < -eps]
    if neg:
        return {"why": "a result solid has negative signed volume (inside out)", "negative_mm3": neg[:4]}
    total, lo, hi = sum(got), max(vols), sum(vols)
    # C23: the union bounds are compared within the Boolean's own volume rounding, which scales with the volumes
    # involved (relative ~1e-7 measured), not with the zero-volume floor: an exact fragment of f9501251fb30's 7 bodies
    # came out 0.11 mm3 over their 927,163.93 mm3 sum (1.2e-7) and was refused as "not a union" by the 0.084 mm3 floor
    bound = max(eps, UNION_REL_TOL * hi)
    # A FUZZY Boolean may move each surface by up to `fuzzy_mm` to glue what lies within it (closing a 1e-4 mm STEP
    # rounding gap adds that gap's volume), so its real union can differ from the bounds by at most fuzzy x the
    # targets' area. C15's broken fuse lost 99.9% of the part, far outside that; a gap closed within the fidelity
    # bound is not. Zero fuzzy adds nothing, so every exact Boolean is judged exactly as before.
    if fuzzy_mm > 0.0:
        bound += fuzzy_mm * sum(_area(t) for t in targets)
    if total < lo - bound or total > hi + bound:
        return {"why": "the result is not a union of the targets", "result_mm3": total, "largest_target_mm3": lo,
                "sum_of_targets_mm3": hi}
    return None


@register("v4_fuse")
def _fuse(payload: dict) -> dict:
    """Fuse the target bodies into one (BRepAlgoAPI_Fuse, non-destructive, with history). The owner's 2026-11-24 goal
    removed the no-fuse rule (lane C7): an overlap the cut cannot or should not resolve - a body inside another, two
    copies of one body, two halves meant to be one - becomes one solid, and the panel judges it. The body count change
    is reported by layer 3, not refused. Bodies that do not touch decline `nothing_to_fuse` (a fuse that merged nothing
    would read as progress)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    targets = _targets(old, payload["bodies"])
    if len(targets) < 2:
        raise ValueError("fuse needs at least two bodies")
    fuzzy = float(payload.get("fuzzy_mm", 0.0))
    args, heal = heal_targets(targets, bool(payload.get("heal_targets")))
    measured = []
    for i, a in enumerate(args):
        for b in args[i + 1:]:
            measured.append({"common_volume_mm3": _common_volume(a, b, fuzzy)})
    op = BRepAlgoAPI_Fuse()
    op.SetArguments(_list(args[:1]))
    op.SetTools(_list(args[1:]))
    op.SetNonDestructive(True)
    op.SetFuzzyValue(fuzzy)
    op.SetToFillHistory(True)
    op.Build()
    if not op.IsDone():
        return _declined("fuse_failed", detail="BRepAlgoAPI_Fuse did not complete")
    solids = _maps(op.Shape())["solid"]
    new_solids = [solids.FindKey(i) for i in range(1, solids.Extent() + 1)]
    if not new_solids:
        return _declined("fuse_failed", detail="the fuse produced no solid")
    if len(new_solids) >= len(targets):
        return _declined("nothing_to_fuse", detail="the bodies do not touch or overlap: nothing merged",
                         bodies=len(targets))
    bad = _degenerate(targets, new_solids, zero_volume_mm3(part_diagonal(payload, old)))
    if bad is not None:
        return _declined("fuse_degenerate", **bad)
    params = {"bodies": payload.get("body_ids", []), "fuzzy_mm": fuzzy,
              "measured": {"pairs": measured, "solids_before": len(targets), "solids_after": len(new_solids)},
              **({"healed_targets": heal["healed"]} if heal["healed"] else {})}
    return _finish(payload, old, [*heal["stages"], (op.History(), None)], targets, new_solids, op="fuse",
                   params=params, start=start,
                   expected_solids=_solid_count(old) - len(targets) + len(new_solids))


#: a coordinate this far from the origin is OpenCascade's Precision::Infinite (2e100) or a surface standing in for it,
#: never a part (the threshold `units.UNBOUNDED_DIAGONAL_MM` applies to the diagonal; the same value per coordinate)
_UNBOUNDED_COORD = 1e9


def _bounded_extent(shape: Any) -> tuple[Any, int, int]:
    """(the box of the faces that HAVE a finite extent, how many faces were left out as unbounded, how many faces in
    all). After the rule in raymond/cad-simplification `cad/occ_box.surface_box` (A. Patil): a face on an untrimmed
    surface has an infinite box and is no part of the surface any engine meshes, so it is left out of the size."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    box, skipped, total = Bnd_Box(), 0, 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = exp.Current()
        exp.Next()
        total += 1
        own = Bnd_Box()
        BRepBndLib.AddOptimal_s(face, own, False, False)
        if own.IsVoid() or any(abs(float(v)) >= _UNBOUNDED_COORD for v in own.Get()):
            skipped += 1
            continue
        box.Add(own)
    return box, skipped, total


@register("v4_trim_unbounded")
def _trim_unbounded(payload: dict) -> dict:
    """Bound a part's infinite surfaces to its real extent: each UNBOUNDED piece of the part (a solid, shell or face whose
    own extent is infinite) is intersected (BRepAlgoAPI_Common) with the part's finite-face box enlarged by
    `pad_fraction` of the diagonal; every other piece is left as the very same shape. The approach is Raymond Lee's
    (raymond/cad-simplification, devtools/simplify: `Common(shape, MakeBox(lo - pad, hi + pad))`), copied, not merged.

    PIECE BY PIECE, not the whole part at once (lane C8, sha bfc8d0b5): one Common of the whole 30-solid compound ran a
    General Fuse over every piece, merged and split the overlapping solids (30 -> 25) and dropped 21 of 23 loose faces,
    while only 2 faces were unbounded. The faces the box contributes are DECLARED added. Declines `nothing_unbounded`
    when every face already has a finite extent, and `no_bounded_extent` when no face does."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools_History
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_COMPOUND
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Iterator
    from OCP.TopTools import TopTools_IndexedMapOfShape

    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    box, skipped, total = _bounded_extent(old)
    if skipped == 0:
        return _declined("nothing_unbounded", faces=total)
    if box.IsVoid():
        return _declined("no_bounded_extent", faces=total, unbounded_faces=skipped)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    pad = max(float(payload["pad_fraction"]) * diag, 1e-6)
    tool = BRepPrimAPI_MakeBox(gp_Pnt(x0 - pad, y0 - pad, z0 - pad), gp_Pnt(x1 + pad, y1 + pad, z1 + pad)).Shape()

    hist = BRepTools_History()
    trimmed = 0

    def trim(piece: Any) -> Any:
        """The piece intersected with the box, its history folded into `hist`; None when nothing of it is inside."""
        nonlocal trimmed
        op = BRepAlgoAPI_Common()
        op.SetArguments(_list([piece]))
        op.SetTools(_list([tool]))
        op.SetNonDestructive(True)
        op.SetToFillHistory(True)
        op.Build()
        if not op.IsDone():
            raise RuntimeError("BRepAlgoAPI_Common did not complete on an unbounded piece")
        h = op.History()
        maps = _maps(piece)
        for kind in _KINDS:
            for i in range(1, maps[kind].Extent() + 1):
                sub = maps[kind].FindKey(i)
                mods = list(h.Modified(sub))
                if mods:
                    for m in mods:
                        hist.AddModified(sub, m)
                elif h.IsRemoved(sub):
                    hist.Remove(sub)
        trimmed += 1
        return op.Shape() if _maps(op.Shape())["face"].Extent() else None

    def walk(node: Any) -> Any:
        comp = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(comp)
        it = TopoDS_Iterator(node)
        while it.More():
            child = it.Value()
            it.Next()
            if child.ShapeType() == TopAbs_COMPOUND:
                sub = walk(TopoDS.Compound_s(child))
                if not _is_empty(sub):  # lane C11: see `_is_empty`
                    builder.Add(comp, sub)
                continue
            _, unbounded, _ = _bounded_extent(child)
            if not unbounded:
                builder.Add(comp, child)  # the SAME shape: its ids survive and nothing of it is touched
                continue
            kept = trim(child)
            if kept is not None:
                builder.Add(comp, kept)
        return comp

    if old.ShapeType() != TopAbs_COMPOUND:
        new = trim(old)
        if new is None:
            return _declined("trim_failed", detail="nothing of the part is inside its own box")
    else:
        new = walk(old)
    new_faces = _maps(new)["face"]
    if new_faces.Extent() == 0:
        return _declined("trim_failed", detail="nothing of the part is inside its own box")
    if diagonal_of(new) >= _UNBOUNDED_COORD:
        return _declined("trim_failed", detail="the result is still unbounded")
    images = TopTools_IndexedMapOfShape()
    old_faces = _maps(old)["face"]
    for i in range(1, old_faces.Extent() + 1):
        f = old_faces.FindKey(i)
        for m in [f, *list(hist.Modified(f))]:
            images.Add(m)
    added = [i for i in range(1, new_faces.Extent() + 1) if not images.Contains(new_faces.FindKey(i))]
    write_brep(new, payload["out_path"])
    params = {"pad_fraction": float(payload["pad_fraction"]), "added_face_indices": added,
              "measured": {"unbounded_faces": skipped, "faces_before": total, "faces_after": new_faces.Extent(),
                           "pieces_trimmed": trimmed, "bounded_diagonal_mm": diag, "pad_mm": pad}}
    record = history_record(old, new, hist, op="trim_unbounded", params=params, old_path=payload["brep_path"],
                            new_path=payload["out_path"], quantum=payload["quantum"],
                            seconds=time.perf_counter() - start)
    return {"record": record}


#: the factors `rescale` accepts: the units rule's own (`repair_v5.brep_faults.UNIT_MISTAKES`), nothing in between
RESCALE_FACTORS = (1000.0, 25.4, 10.0, 0.1, 1 / 25.4, 0.001)


@register("v5_rescale")
def _rescale(payload: dict) -> dict:
    """Scale the whole part by `factor` about the origin (a units mistake undone: v5's units rule decides the factor,
    this only applies it). BRepBuilderAPI_Transform with a scale copies every entity, so the op reports its own
    mapping: entity i of the old part IS entity i of the new one (a similarity keeps the topology and its map order),
    every one of them modified."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepTools import BRepTools_History
    from OCP.gp import gp_Pnt, gp_Trsf

    start = time.perf_counter()
    factor = float(payload["factor"])
    if not any(abs(factor - f) <= 1e-9 * f for f in RESCALE_FACTORS):
        return _declined("invalid_params", detail=f"factor {factor!r} is not a units factor {RESCALE_FACTORS}")
    old = read_brep(payload["brep_path"])
    trsf = gp_Trsf()
    trsf.SetScale(gp_Pnt(0.0, 0.0, 0.0), factor)
    maker = BRepBuilderAPI_Transform(old, trsf, True)
    if not maker.IsDone():
        return _declined("transform_failed", detail="BRepBuilderAPI_Transform did not complete")
    new = maker.Shape()
    hist = BRepTools_History()
    old_maps, new_maps = _maps(old), _maps(new)
    for kind in _KINDS:
        if old_maps[kind].Extent() != new_maps[kind].Extent():
            return _declined("topology_changed", detail=f"{kind} count {old_maps[kind].Extent()} -> "
                                                        f"{new_maps[kind].Extent()}")
        for i in range(1, old_maps[kind].Extent() + 1):
            hist.AddModified(old_maps[kind].FindKey(i), new_maps[kind].FindKey(i))
    write_brep(new, payload["out_path"])
    params = {"factor": factor,
              "measured": {"diagonal_before_mm": diagonal_of(old), "diagonal_after_mm": diagonal_of(new)}}
    record = history_record(old, new, hist, op="rescale", params=params, old_path=payload["brep_path"],
                            new_path=payload["out_path"], quantum=float(payload["quantum"]),
                            seconds=time.perf_counter() - start)
    return {"record": record}
