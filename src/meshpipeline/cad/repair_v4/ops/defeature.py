# Responsibility: The defeature op of v4: Raymond Lee's hole/blend recogniser as a CANDIDATE SOURCE, and one
#                 BRepAlgoAPI_Defeaturing call over a feature set (one feature at a time when the batch fails),
#                 a feature counted removed only when its faces are gone, with his T0 measurements reported.
# Owns: `Defeature` (the op), the kernel functions `v4_recognise_features` and `op_defeature`, and the ported
#       recogniser / face-signature / removal code.
# Boundaries: the parent half never loads OCCT; every kernel function runs in a worker and returns
#             `ws_kernel.history_record(...)` (or `{"declined": ...}`). It decides no verdict: clean is layer 1 +
#             the panel, judged elsewhere. Faithfulness is reported elsewhere and gates nothing (owner decision
#             2026-11-24), so no geometry cap lives here.
"""DEFEATURE: REMOVE HOLES, BLENDS, OR ANY FACES, AND SAY WHAT WAS REALLY REMOVED.

ATTRIBUTION. The recogniser, the face signatures, the defeaturing call and the batch-then-one-at-a-time removal are
COPIED from Raymond Lee's CAD simplification work, not merged:

    branch  origin/raymond/cad-simplification  (at 4f3dad49)
    src/meshpipeline/cad/simplify/recognise.py   _Face, _faces, _adjacency, _describe, _coaxial, _bbox,
                                                 feature_size, surface_cells, recognise
    src/meshpipeline/cad/simplify/verify.py      FaceSig, face_sigs, find_faces, _face_count, volume, is_valid,
                                                 defeature
    src/meshpipeline/cad/simplify/simplify.py    remove_features, _solids_closed, t0_checks
    docs/simplification/DEVLOG.md                the measurements the rules below come from

His rules, kept as he wrote them:

* HOLE: a concave cylinder grown over the coaxial cylinders and cones touching it, a hole only when its same-radius
  cylinders turn a full circle. BLEND: a torus or a cylinder spanning under a half turn, radius under 3% of the
  diagonal, grown over same-concavity blends. A concave cylinder spanning a half turn or more but not a full one is a
  SLOT END: reported, NEVER removable - `BRepAlgoAPI_Defeaturing` cannot remove a 180-degree concave round and hands
  the input back unchanged while reporting success (DEVLOG: twelve of fifty-nine candidates on nist_ftc_09).
* ONE `BRepAlgoAPI_Defeaturing` call over the whole set; when that fails, one feature at a time on the running shape.
* `IsDone()` is TRUE when OCCT removed nothing (DEVLOG: twelve "removed" blends with dV ~ 1e-15), so an unchanged
  face count and volume is a no-op, refused; and a feature counts as REMOVED only when none of its faces is still in
  the result.

What changed in the port, and why:

* History is FILLED (`SetToFillHistory(True)`; his was off): the v4 workspace carries stable ids through the kernel's
  own history, and the removals are DECLARED from it, as `remove_small` declares its own.
* His T0 checks (valid, closed, same solid count, volume change) are MEASURED and REPORTED in the op's params, never a
  refusal: the volume cap was a geometry cap, and the owner removed those (2026-11-24). Validity is judged by layer 1.
* In the one-at-a-time fallback the faces-gone rule is applied too (his fallback trusted a non-None result).
* His FEA load-case eligibility (near_ends) and simulation verification (T2) are NOT ported: here the checker is the
  judge.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops import (  # noqa: F401  (the contract's exceptions)
    OpDeclined,
    OpFailed,
    run_kernel_op,
)
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

HOLE, BLEND, SLOT_END = "hole", "blend", "slot_end"

# =============================================================================================================
# Ported from recognise.py (Raymond Lee). Unchanged except that candidates are plain dicts (the v4 kernel boundary
# speaks JSON) instead of contracts.FeatureCandidate.
# =============================================================================================================

TWO_PI = 2.0 * math.pi
#: a blend is a round of radius under this fraction of the part diagonal
BLEND_RATIO = 0.03
#: PLAN-fea-4: a hot spot gets 6 elements per quarter-turn of its arc
ELEMENTS_PER_QUARTER_TURN = 6


@dataclass
class _Face:
    kind: str
    area: float
    centroid: tuple[float, float, float]
    radius: float = 0.0
    span: float = 0.0
    concave: bool = False
    axis: tuple = ()


def _faces(shape: Any) -> tuple[list[Any], Any]:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    return [TopoDS.Face_s(fmap.FindKey(i)) for i in range(1, fmap.Extent() + 1)], fmap


def _adjacency(shape: Any, fmap: Any) -> list[set[int]]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    em = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, em)
    adj: list[set[int]] = [set() for _ in range(fmap.Extent())]
    for i in range(1, em.Extent() + 1):
        ids = [fmap.FindIndex(f) - 1 for f in em.FindFromIndex(i)]
        for a in ids:
            adj[a].update(b for b in ids if b != a)
    return adj


def _describe(face: Any) -> _Face:
    import numpy as np
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp, BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Torus
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.GProp import GProp_GProps
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    c = g.CentreOfMass()
    kind = {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cyl", GeomAbs_Cone: "cone",
            GeomAbs_Torus: "torus"}.get(t, "other")
    info = _Face(kind, g.Mass(), (c.X(), c.Y(), c.Z()))
    if kind not in ("cyl", "cone", "torus"):
        return info
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    info.span = u1 - u0
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, n)
    pv, nv = np.array([p.X(), p.Y(), p.Z()]), np.array([n.X(), n.Y(), n.Z()])
    if kind in ("cyl", "cone"):
        geom = s.Cylinder() if kind == "cyl" else s.Cone()
        info.radius = geom.Radius() if kind == "cyl" else geom.RefRadius()
        ax = geom.Axis()
        o, d = ax.Location(), ax.Direction()
        ov, dv = np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()])
        info.axis = (tuple(ov), tuple(dv))
        w = pv - ov
        info.concave = float(nv @ (w - (w @ dv) * dv)) < 0.0
    else:
        tor = s.Torus()
        info.radius = tor.MinorRadius()
        ax = tor.Axis()
        o, d = ax.Location(), ax.Direction()
        ov, dv = np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()])
        w = pv - ov
        radial = w - (w @ dv) * dv
        rn = float(np.linalg.norm(radial))
        tube_centre = ov + (radial / rn * tor.MajorRadius() if rn > 0 else 0.0)
        info.concave = float(nv @ (tube_centre - pv)) > 0.0
    return info


def _coaxial(a: _Face, b: _Face, tol: float) -> bool:
    import numpy as np
    if not a.axis or not b.axis:
        return False
    (pa, da), (pb, db) = a.axis, b.axis
    da, db = np.asarray(da), np.asarray(db)
    if abs(abs(float(da @ db)) - 1.0) > 1e-6:
        return False
    w = np.asarray(pb) - np.asarray(pa)
    return float(np.linalg.norm(w - (w @ da) * da)) < tol


def _bbox(shapes: Any) -> tuple[float, float, float, float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    for s in shapes:
        BRepBndLib.Add_s(s, b)
    x0, y0, z0, x1, y1, z1 = (float(v) for v in b.Get())
    return (x0, y0, z0, x1, y1, z1)


def feature_size(radius: float) -> float:
    """The element edge PLAN-fea-4 asks of a round: a quarter-turn of arc over 6 elements."""
    return (0.5 * math.pi * radius) / ELEMENTS_PER_QUARTER_TURN


def surface_cells(area: float, h: float) -> int:
    """Equilateral triangles of edge h needed to cover the area: the feature's own surface cost."""
    return int(math.ceil(area / (math.sqrt(3.0) / 4.0 * h * h))) if h > 0 else 0


def recognise(shape: Any) -> tuple[list[dict], float]:
    """Candidates (1-based face indices in TopExp face order, the order v4's workspace indexes faces in) and the part
    diagonal they are scaled against. Raymond's `recognise`, returning dicts."""
    import numpy as np
    faces, fmap = _faces(shape)
    info = [_describe(f) for f in faces]
    adj = _adjacency(shape, fmap)
    box = _bbox([shape])
    diag = math.dist(box[:3], box[3:])
    tol = 1e-4 * diag
    used: set[int] = set()
    groups: list[tuple[str, set[int], _Face]] = []

    for i, fi in enumerate(info):
        if i in used or fi.kind != "cyl" or not fi.concave:
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k in adj[j]:
                fk = info[k]
                if k in group or k in used:
                    continue
                if fk.kind in ("cyl", "cone") and _coaxial(fi, fk, tol) and (fk.concave or fk.kind == "cone"):
                    group.add(k)
                    stack.append(k)
        span = sum(info[j].span for j in group if info[j].kind == "cyl" and abs(info[j].radius - fi.radius) < tol)
        if span >= TWO_PI - 1e-3:
            used |= group
            groups.append((HOLE, group, fi))
        elif span >= math.pi - 1e-3:
            used |= group
            groups.append((SLOT_END, group, fi))

    def is_blend(f: _Face) -> bool:
        return (f.kind == "torus" or (f.kind == "cyl" and f.span < math.pi - 1e-3)) and f.radius < BLEND_RATIO * diag

    for i, fi in enumerate(info):
        if i in used or not is_blend(fi):
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k in adj[j]:
                if k not in group and k not in used and is_blend(info[k]) and info[k].concave == fi.concave:
                    group.add(k)
                    stack.append(k)
        used |= group
        groups.append((BLEND, group, fi))

    out: list[dict] = []
    for n, (kind, group, seed) in enumerate(groups):
        ids = sorted(group)
        areas = [info[j].area for j in ids]
        area = float(sum(areas))
        cx, cy, cz = (float(v) for v in np.average([info[j].centroid for j in ids], axis=0,
                                                   weights=areas if area > 0 else None))
        radius = max(info[j].radius for j in ids) if kind == BLEND else seed.radius
        h = feature_size(radius)
        out.append({
            "id": f"{kind}{n}", "kind": kind, "faces": [j + 1 for j in ids], "radius": float(radius),
            "radius_ratio": float(radius / diag) if diag else 0.0, "concave": bool(seed.concave), "area": area,
            "centroid": [cx, cy, cz], "bbox": list(_bbox([faces[j] for j in ids])), "feature_size": h,
            "est_surface_cells": surface_cells(area, h), "removable": kind != SLOT_END})
    return out, diag


# =============================================================================================================
# Ported from verify.py (Raymond Lee): face signatures, finding faces again, the one defeaturing call. Changed:
# history is filled and returned (v4 carries stable ids through it).
# =============================================================================================================

def volume(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return float(g.Mass())


def is_valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer
    return bool(BRepCheck_Analyzer(shape).IsValid())


@dataclass(frozen=True, slots=True)
class FaceSig:
    """A face named by what it IS, so it can be found again in another shape that contains it unchanged."""
    kind: str
    area: float
    centroid: tuple[float, float, float]


def face_sigs(shape: Any) -> list[tuple[FaceSig, Any]]:
    faces, _ = _faces(shape)
    out = []
    for f in faces:
        d = _describe(f)
        cx, cy, cz = (float(v) for v in d.centroid)
        out.append((FaceSig(d.kind, float(d.area), (cx, cy, cz)), f))
    return out


def find_faces(shape: Any, sigs: Any, tol: float, *, index: Any = None) -> list | None:
    """The faces of `shape` matching every signature, or None when any one is missing."""
    index = face_sigs(shape) if index is None else index
    found = []
    for s in sigs:
        hit = None
        for t, f in index:
            if (t.kind == s.kind and abs(t.area - s.area) <= 1e-5 * max(abs(s.area), 1e-12)
                    and math.dist(t.centroid, s.centroid) <= tol):
                hit = f
                break
        if hit is None:
            return None
        found.append(hit)
    return found


def _face_count(shape: Any) -> int:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return m.Extent()


def defeature(shape: Any, faces: Any) -> tuple[Any | None, Any | None, str]:
    """One BRepAlgoAPI_Defeaturing call over `faces`; (result, history), or (None, None, why).

    IsDone() is TRUE when OCCT could not remove the faces and handed the input back (it records a warning instead) -
    measured on nist_ftc_09, twelve "removed" blends with dV ~ 1e-15. So an unchanged face count and volume is a
    no-op, refused here, not a free simplification."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    d = BRepAlgoAPI_Defeaturing()
    d.SetShape(shape)
    for f in faces:
        d.AddFaceToRemove(f)
    d.SetRunParallel(False)
    d.SetToFillHistory(True)          # PORT: his was False; v4 needs the history for stable ids
    d.Build()
    if not d.IsDone():
        return None, None, "defeaturing failed"
    out = d.Shape()
    if not is_valid(out):
        return None, None, "the defeatured shape is not valid"
    v0 = volume(shape)
    if _face_count(out) == _face_count(shape) and abs(volume(out) - v0) <= 1e-9 * abs(v0):
        return None, None, "defeaturing left the shape unchanged"
    return out, d.History(), ""


# =============================================================================================================
# Ported from simplify.py (Raymond Lee): remove a feature set, batch first, one at a time on failure; a feature is
# removed only when its faces are gone. T0 measured and reported, not refused.
# =============================================================================================================

def _gone(shape: Any, sigs: Any, tol: float, index: Any) -> bool:
    """None of a feature's faces is still in `shape`."""
    return not any(find_faces(shape, [s], tol, index=index) is not None for s in sigs)


def remove_features(shape: Any, features: list[tuple[str, Any]], tol: float
                    ) -> tuple[Any, Any, list[str], dict[str, str], str]:
    """Remove each (id, face signatures) from `shape`. Returns (shape, history or None, removed ids,
    {id: why not}, "batch" | "one_at_a_time" | "none").

    One call first. The kernel can report success for a set in which some features did not move, so afterwards each
    feature counts as removed only when none of its faces is still there."""
    index = face_sigs(shape)
    located = []
    failed: dict[str, str] = {}
    for fid, sigs in features:
        faces = find_faces(shape, sigs, tol, index=index)
        if faces is None:
            failed[fid] = "its faces were not found in the part"
        else:
            located.append((fid, sigs, faces))
    if not located:
        return shape, None, [], failed, "none"

    out, history, why = defeature(shape, [f for _, _, faces in located for f in faces])
    if out is not None:
        after = face_sigs(out)
        removed = []
        for fid, sigs, _ in located:
            if _gone(out, sigs, tol, after):
                removed.append(fid)
            else:
                failed[fid] = "the kernel left it in place"
        return out, history, removed, failed, "batch"

    cur, hist, removed = shape, None, []
    for fid, sigs, _ in located:
        faces = find_faces(cur, sigs, tol)
        if faces is None:
            failed[fid] = "its faces were not found after an earlier removal"
            continue
        nxt, step, why_one = defeature(cur, faces)
        if nxt is None:
            failed[fid] = why_one
            continue
        if not _gone(nxt, sigs, tol, face_sigs(nxt)):   # PORT: the faces-gone rule in the fallback too
            failed[fid] = "the kernel left it in place"
            continue
        if hist is None:
            hist = step
        else:
            hist.Merge(step)
        cur = nxt
        removed.append(fid)
    return cur, hist, removed, failed, "one_at_a_time"


def _solids_closed(shape: Any) -> tuple[int, bool]:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    solids = 0
    e = TopExp_Explorer(shape, TopAbs_SOLID)
    while e.More():
        solids += 1
        e.Next()
    closed = True
    e = TopExp_Explorer(shape, TopAbs_SHELL)
    while e.More():
        closed = closed and bool(BRep_Tool.IsClosed_s(TopoDS.Shell_s(e.Current())))
        e.Next()
    return solids, closed


def t0_report(before: Any, after: Any) -> dict:
    """His T0 checks, MEASURED: valid, closed, same solid count, volume change. Reported, never a refusal (the volume
    cap was a geometry cap; the owner removed those). `t0_pass` says whether his gate would have passed it, cap aside."""
    v0, v1 = volume(before), volume(after)
    solids0, _ = _solids_closed(before)
    solids1, closed1 = _solids_closed(after)
    m = {"volume_before": v0, "volume_after": v1, "volume_change": (v1 - v0) / v0 if v0 else 0.0,
         "solids_before": solids0, "solids_after": solids1, "closed": closed1, "valid": is_valid(after)}
    m["t0_pass"] = bool(m["valid"] and solids1 == solids0 and solids1 > 0 and closed1)
    return m


# =============================================================================================================
# The v4 kernel functions
# =============================================================================================================

@register_kernel_fn("v4_recognise_features")
def _recognise_features(payload: dict) -> dict:
    """The recogniser over a workspace BREP, faces named by stable id (`payload["ids"]`: "face:<index>" -> id)."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    shape = ws_kernel.read_brep(payload["brep_path"])
    features, diag = recognise(shape)
    ids = payload.get("ids") or {}
    for f in features:
        f["face_ids"] = [ids.get(f"face:{i}") for i in f["faces"]]
    return {"diagonal": diag, "features": features}


@register_kernel_fn("op_defeature")
def _op_defeature(payload: dict) -> dict:
    """Defeature the target faces, grouped into features by `params["groups"]` (indices into the targets; default:
    every target is its own feature). Declines when nothing was removed. Declares every face the history removed."""
    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    targets = payload["targets"]
    faces, fmap = _faces(old)
    sigs_all = face_sigs(old)
    groups = params.get("groups") or [[i] for i in range(len(targets))]
    features: list[tuple[str, tuple]] = []
    for g, members in enumerate(groups):
        sigs = tuple(sigs_all[targets[m]["index"] - 1][0] for m in members)
        features.append((f"g{g}", sigs))
    box = _bbox([old])
    diag = math.dist(box[:3], box[3:])
    shape, history, removed, failed, via = remove_features(old, features, 1e-5 * diag)
    if not removed or history is None:
        why = "; ".join(f"{k}: {v}" for k, v in failed.items()) or "nothing to remove"
        return {"declined": f"no_change: defeaturing removed none of the {len(features)} feature(s) ({why})"}
    params.update(via=via, removed_groups=removed, not_removed=failed, t0=t0_report(old, shape))
    ws_kernel.write_brep(shape, payload["out_path"])
    declared: list[str] = []
    for i in range(1, fmap.Extent() + 1):
        sid = payload["ids"].get(f"face:{i}")
        if sid is not None and history.IsRemoved(fmap.FindKey(i)):
            declared.append(sid)
    return ws_kernel.history_record(
        old, shape, history, op="defeature", params=params, old_path=payload["brep_path"],
        new_path=payload["out_path"], quantum=payload["quantum"], seconds=time.perf_counter() - start,
        declared_removed=declared)


# =============================================================================================================
# The parent-side op
# =============================================================================================================

class Defeature:
    """Remove the target faces with BRepAlgoAPI_Defeaturing, grouped into features. Applies to any site that names
    faces: slivers, small faces at a mesher's failure, and (from the recogniser) holes and blends."""

    name = "defeature"
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "face", "min": 1, "max": None},
        "properties": {
            # which targets form one feature (indices into `targets`); a feature counts as removed only when all of
            # its faces are gone. Default: each target is its own feature.
            "groups": {"type": "array", "minItems": 1, "maxItems": 256,
                       "items": {"type": "array", "minItems": 1, "maxItems": 256,
                                 "items": {"type": "integer", "minimum": 0, "maximum": 100000}}},
            # who proposed it, for the trace: the recogniser, a mesher's located failure (D16), or the site's faces
            "source": {"type": "string", "enum": ["recognised", "located", "site"]},
        },
        "additionalProperties": False,
    }

    def applies_to(self, site: Site) -> bool:
        return any(e[:1] == "F" for e in site.entities) and (site.kind == "sliver" or bool(site.meshers_failing))

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        ws.resolve(list(targets))
        groups = params.get("groups")
        if groups is not None:
            flat = [i for g in groups for i in g]
            if any(i >= len(targets) for i in flat) or len(set(flat)) != len(flat):
                raise OpDeclined(f"invalid_params: groups {groups} must index the {len(targets)} targets, each once")
        payload = {
            "params": dict(params),
            "targets": [{"id": t, "type": "face", "index": ws.indices[t]} for t in targets],
            "ids": {f"face:{index}": sid for sid, index in ws.indices.items() if sid[:1] == "F"},
        }
        return run_kernel_op(pool, ws, kernel_fn="op_defeature", op=self.name, payload=payload, timeout_s=timeout_s)


def recognised_features(pool: WorkerPool, ws: Workspace, *, timeout_s: float = 120) -> list[dict]:
    """The recogniser on `ws`, faces by stable id; [] when the kernel cannot say (a candidate source never crashes)."""
    ids = {f"face:{index}": sid for sid, index in ws.indices.items() if sid[:1] == "F"}
    result = pool.call("v4_recognise_features", {"brep_path": ws.brep_path, "ids": ids}, timeout_s=timeout_s)
    if not result.ok or result.value is None:
        return []
    return list(result.value.get("features") or [])


def defeature_rows(features: list[dict], site_faces: list[StableId] | None = None
                   ) -> list[tuple[str, list[StableId], dict, str]]:
    """Det candidate rows from recognised features: holes and blends only, NEVER slot ends (OCCT hands a 180-degree
    concave round back unchanged). With `site_faces`, only features touching those faces; one row removes the whole
    set in one call, grouped by feature."""
    chosen = [f for f in features if f.get("removable") and f.get("kind") in (HOLE, BLEND)
              and all(f.get("face_ids") or [None])
              and (site_faces is None or set(f["face_ids"]) & set(site_faces))]
    if not chosen:
        return []
    targets: list[StableId] = []
    groups: list[list[int]] = []
    for f in sorted(chosen, key=lambda f: (-int(f.get("est_surface_cells") or 0), f["id"])):
        fresh = [sid for sid in f["face_ids"] if sid not in targets]
        if not fresh:
            continue
        groups.append(list(range(len(targets), len(targets) + len(fresh))))
        targets += fresh
    holes = sum(f["kind"] == HOLE for f in chosen)
    blends = sum(f["kind"] == BLEND for f in chosen)
    where = "at the site" if site_faces is not None else "on the part"
    return [("defeature", targets, {"groups": groups, "source": "recognised"},
             f"remove the recognised features {where} ({holes} holes, {blends} blends) in one defeaturing call")]


OPS: tuple[Any, ...] = (Defeature(),)
