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
    history_record,
    read_brep,
    write_brep,
)

#: Largest fuzzy value / overlap the ops accept, whatever the part size (mm).
ABS_BOUND_MM = 0.1
REL_BOUND = 0.001
#: Largest share of a body the cut may remove (the clean definition caps volume change at 2%).
MAX_VOLUME_CHANGE = 0.02
_KINDS = ("face", "edge", "solid")


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
                builder.Add(comp, walk(TopoDS.Compound_s(child), False))
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


def _finish(payload: dict, old: Any, stages: list[tuple[Any, Any]], targets: list[Any], new_solids: list[Any],
            *, op: str, params: dict, start: float, must_merge: list[tuple[int, int]] | None = None,
            require_shared: bool = False) -> dict:
    """Assemble the new compound, check it, write it, and build the workspace record."""
    from OCP.TopTools import TopTools_IndexedMapOfShape

    drop = TopTools_IndexedMapOfShape()
    for s in targets:
        drop.Add(s)
    new = _rebuild(old, drop, new_solids)
    if _solid_count(new) != _solid_count(old):
        return _declined("changes_body_count", before=_solid_count(old), after=_solid_count(new))
    if not _is_valid(new):
        return _declined("invalid_result")
    if require_shared and _shared_face_count(new) == 0:
        # Success that shared nothing would record progress that did not happen (e.g. a gap wider
        # than the fuzzy value after STEP rounding): refuse, and say why.
        return _declined("no_shared_interface", detail="no face is shared by two bodies afterwards")
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


@register("v4_conformalise")
def _conformalise(payload: dict) -> dict:
    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    fuzzy = float(payload.get("fuzzy_mm", 0.0))
    mode = payload["mode"]
    bound = fidelity_bound(diagonal_of(old))
    if fuzzy > bound + 1e-12:
        return _declined("exceeds_fidelity", what="fuzzy_mm", value=fuzzy, bound=bound)
    targets = _targets(old, payload["bodies"])
    cut_by = payload.get("cut_by")
    keeper = _targets(old, [cut_by])[0] if cut_by is not None else None
    if mode != "fragment" and keeper is None:
        raise ValueError(f"mode {mode!r} needs a cut_by body")

    # Every overlap is measured BEFORE anything is changed, so a decline leaves nothing half done.
    cuts: list[Any] = []
    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            volume = _common_volume(a, b, fuzzy)
            if volume <= 1e-9 * max(diagonal_of(old), 1.0) ** 3:
                continue  # touching, not overlapping
            involves_keeper = keeper is not None and (a.IsSame(keeper) or b.IsSame(keeper))
            if not involves_keeper:
                return _declined("unresolved_overlap", volume=volume)
            victim = b if a.IsSame(keeper) else a
            volume, depth, method = _cut_depth(victim, keeper, fuzzy)
            if depth > bound:
                return _declined("exceeds_fidelity", what="overlap_depth", value=depth,
                                 bound=bound, volume=volume, method=method)
            fraction = volume / max(_volume(victim), 1e-12)
            if fraction > MAX_VOLUME_CHANGE:
                return _declined("exceeds_fidelity", what="volume_change", value=fraction,
                                 bound=MAX_VOLUME_CHANGE, volume=volume)
            cuts.append(victim)

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    stages: list[tuple[Any, Any]] = []
    current = list(targets)
    for victim in cuts:
        cut = BRepAlgoAPI_Cut()
        cut.SetArguments(_list([victim]))
        cut.SetTools(_list([keeper]))
        cut.SetNonDestructive(True)
        cut.SetFuzzyValue(fuzzy)
        cut.SetToFillHistory(True)
        cut.Build()
        if not cut.IsDone() or _solid_count(cut.Shape()) != 1:
            return _declined("changes_body_count", detail="cut did not leave exactly one body")
        stages.append((cut.History(), victim))
        current = [_maps(cut.Shape())["solid"].FindKey(1) if t.IsSame(victim) else t
                   for t in current]
    frag = _builder(current, fuzzy)
    stages.append((frag.History(), None))
    result_solids = _maps(frag.Shape())["solid"]
    new_solids = [result_solids.FindKey(i) for i in range(1, result_solids.Extent() + 1)]
    if len(new_solids) != len(targets):
        return _declined("changes_body_count", before=len(targets), after=len(new_solids))
    params = {"bodies": payload.get("body_ids", []), "fuzzy_mm": fuzzy, "mode": mode}
    return _finish(payload, old, stages, targets, new_solids, op="conformalise", params=params,
                   start=start, require_shared=True)


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
    bound = fidelity_bound(diagonal_of(old))
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
