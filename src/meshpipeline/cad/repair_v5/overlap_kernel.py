# Responsibility: the kernel side of v5's overlap_self_intersection class (build step 8): its DETECTORS (duplicate
#                 bodies, the faces a self-intersecting solid's interference sits on) and its three new ops' kernels
#                 (`boolean_by_volume`, `fragment_drop_small`, `drop_duplicate_body`).
# Owns: `v5_overlap_facts`, `v5_boolean_by_volume`, `v5_fragment_drop_small`, `v5_drop_duplicate_body`, the
#       duplicate signature and the self-intersecting solid's rebuild (MakerVolume + winding classification).
# Boundaries: runs INSIDE a worker (OCP imported lazily). The workspace record is body_kernel's `_finish`, the
#             assembly every body op uses; the cut and the fuse are body_kernel's own kernels, called, not copied.
#             Thresholds and rule choice live in the parent (`repair_v5.overlap`); this file measures and applies.
"""Overlap and self-intersection, what the conformal pass leaves.

DUPLICATE BODY: two solids with the same volume, area, centre of mass, principal moments, box and topology counts
(all within a relative tolerance of the part's size) are one body written twice. Layer 1 reports them as a
body_overlap whose common volume is the whole body; the repair is to drop the copy, not to cut or fuse.

SELF-INTERSECTING SOLID: `BOPAlgo_ArgumentAnalyzer` (layer 1's own rule) names the interfering sub-shapes; their faces
are the defect's AIM. `ShapeAnalysis_Wire.CheckSelfIntersection` says which of them is a self-intersecting FACE (its
own boundary crosses itself), the one `rebuild_face` exists for. The FRAGMENT repair rebuilds the solid from its own
faces (`BOPAlgo_MakerVolume`, which splits them where they cross), keeps the pieces the ORIGINAL encloses (winding
number of a point inside each piece against the original's triangulation, `tess_kernel`), drops pieces under the
size threshold, and fuses what is left.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

#: relative tolerance of the duplicate signature (scaled by the part's diagonal for lengths, its powers for the rest)
DUP_REL = 1e-6
MAX_FACTS_SOLIDS = 400
#: the wall the per-pair overlap volumes may take inside one `v5_overlap_facts` call
COMMON_BUDGET_S = 30.0


def _props(solid: Any) -> dict:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    from meshpipeline.cad.repair_v4.ws_kernel import _maps

    v, a = GProp_GProps(), GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, v)
    BRepGProp.SurfaceProperties_s(solid, a)
    c = v.CentreOfMass()
    pm = v.PrincipalProperties()
    i1, i2, i3 = pm.Moments()
    box = Bnd_Box()
    BRepBndLib.Add_s(solid, box)
    maps = _maps(solid)
    return {"volume": abs(v.Mass()), "area": a.Mass(), "centre": (c.X(), c.Y(), c.Z()),
            "moments": tuple(sorted(abs(x) for x in (i1, i2, i3))), "box": tuple(box.Get()),
            "counts": (maps["face"].Extent(), maps["edge"].Extent())}


def duplicate(pa: dict, pb: dict, diag: float) -> bool:
    """The duplicate signature: equal topology counts, and volume, area, centre, box and principal moments within
    `DUP_REL` of the part's size (length x diag, area x diag^2, volume x diag^3, moments x diag^5)."""
    if pa["counts"] != pb["counts"]:
        return False
    d = max(diag, 1e-9)

    def close(x: float, y: float, scale: float) -> bool:
        return abs(x - y) <= DUP_REL * scale + 1e-12

    return (close(pa["volume"], pb["volume"], d ** 3) and close(pa["area"], pb["area"], d ** 2)
            and all(close(x, y, d) for x, y in zip(pa["centre"], pb["centre"], strict=True))
            and all(close(x, y, d) for x, y in zip(pa["box"], pb["box"], strict=True))
            and all(close(x, y, d ** 5) for x, y in zip(pa["moments"], pb["moments"], strict=True)))


def _faces_of_sub(sub: Any, solid: Any) -> list[Any]:
    """The faces of `solid` that contain `sub` (a face itself, or an edge/vertex of one)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    kind = sub.ShapeType()
    if kind == TopAbs_FACE:
        return [sub]
    if kind not in (TopAbs_EDGE, TopAbs_VERTEX):
        return []
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(solid, kind, TopAbs_FACE, owners)
    if not owners.Contains(sub):
        return []
    return _iter(owners.FindFromKey(sub))


def _iter(lst: Any) -> list[Any]:
    try:
        return list(lst)
    except TypeError:
        out = []
        it = lst.cbegin() if hasattr(lst, "cbegin") else None
        if it is None:
            return out
        while it.More():
            out.append(it.Value())
            it.Next()
        return out


def interference_faces(solid: Any) -> list[Any]:
    """The faces layer 1's self-interference analysis names on `solid` (ArgumentAnalyzer, SelfInterMode)."""
    from OCP.BOPAlgo import BOPAlgo_ArgumentAnalyzer

    an = BOPAlgo_ArgumentAnalyzer()
    an.SetShape1(solid)
    for flag in ("ArgumentTypeMode", "SmallEdgeMode", "RebuildFaceMode", "TangentMode", "MergeVertexMode",
                 "MergeEdgeMode", "ContinuityMode", "CurveOnSurfaceMode"):
        setattr(an, flag, False)
    an.SelfInterMode = True
    an.Perform()
    faces: list[Any] = []
    for res in _iter(an.GetCheckResult()):
        for sub in _iter(res.GetFaultyShapes1()):
            for f in _faces_of_sub(sub, solid):
                if not any(f.IsSame(g) for g in faces):
                    faces.append(f)
    return faces


def wire_self_intersects(face: Any, precision: float) -> bool:
    from OCP.BRepTools import BRepTools
    from OCP.ShapeAnalysis import ShapeAnalysis_Wire
    from OCP.TopoDS import TopoDS

    f = TopoDS.Face_s(face)
    wire = BRepTools.OuterWire_s(f)
    if wire.IsNull():
        return False
    return bool(ShapeAnalysis_Wire(wire, f, precision).CheckSelfIntersection())


@register("v5_overlap_facts")
def v5_overlap_facts(payload: dict) -> dict:
    """`{"duplicates": [[i, j], ...], "self_int": {"<i>": {"faces": [face idx], "wire_self_int": [face idx]}}}`.
    Kernel indices (1-based). `pairs` (optional [[i, j]]) limits the duplicate test; `self_int` lists the solids
    whose interference faces are located."""
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep

    start = time.perf_counter()
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    solids, faces = maps["solid"], maps["face"]
    diag = part_diagonal(payload, shape)
    pairs = [tuple(p) for p in payload.get("pairs") or []][:MAX_FACTS_SOLIDS]
    props: dict[int, dict] = {}
    dups = []
    for i, j in pairs:
        for k in (i, j):
            if k not in props:
                props[k] = _props(solids.FindKey(int(k)))
        if duplicate(props[i], props[j], diag):
            dups.append([int(i), int(j)])
    # round 2: each pair's OVERLAP VOLUME, so a cluster's pairs are played largest first (bounded; unmeasured = last)
    common: dict[str, float] = {}
    for i, j in pairs:
        if time.perf_counter() - start > COMMON_BUDGET_S:
            break
        try:
            common[f"{int(i)}|{int(j)}"] = _common_facts(solids.FindKey(int(i)), solids.FindKey(int(j)))[0]
        except Exception:  # noqa: BLE001, S112 - an unmeasured pair keeps its place at the end
            continue
    self_int: dict[str, Any] = {}
    precision = max(1e-7, 1e-6 * diag)
    for i in payload.get("self_int") or []:
        solid = solids.FindKey(int(i))
        try:
            found = interference_faces(solid)
        except Exception as exc:  # noqa: BLE001 - an unlocated self-intersection stays a body defect
            self_int[str(i)] = {"error": f"{type(exc).__name__}: {exc}"[:200], "faces": [], "wire_self_int": []}
            continue
        idx = [int(faces.FindIndex(f)) for f in found]
        wsi = []
        for f, k in zip(found, idx, strict=True):
            try:
                if wire_self_intersects(f, precision):
                    wsi.append(k)
            except Exception:  # noqa: BLE001, S112 - an unreadable wire is "not known to self-cross"
                continue
        self_int[str(i)] = {"faces": [k for k in idx if k > 0], "wire_self_int": [k for k in wsi if k > 0]}
    return {"duplicates": dups, "self_int": self_int, "common": common, "seconds": time.perf_counter() - start}


# --- ops ---------------------------------------------------------------------------------------------------------


def _drop_pieces(kept: list[Any], gone: list[Any]) -> Any:
    """A history stage that REMOVES what only the `gone` pieces held (a sub-shape shared with a kept piece lives on)."""
    from OCP.BRepTools import BRepTools_History

    from meshpipeline.cad.repair_v4.body_kernel import _KINDS
    from meshpipeline.cad.repair_v4.ws_kernel import _maps

    kept_maps = [_maps(s) for s in kept]
    hist = BRepTools_History()
    for piece in gone:
        maps = _maps(piece)
        for kind in _KINDS:
            for i in range(1, maps[kind].Extent() + 1):
                sub = maps[kind].FindKey(i)
                if not any(m[kind].Contains(sub) for m in kept_maps):
                    hist.Remove(sub)
    return hist


@register("v5_drop_duplicate_body")
def v5_drop_duplicate_body(payload: dict) -> dict:
    """Drop the SECOND of two bodies (kernel indices `bodies`) when the pair carries the duplicate signature."""
    from meshpipeline.cad.repair_v4.body_kernel import _declined, _finish
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep

    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    solids = _maps(old)["solid"]
    a, b = (solids.FindKey(int(i)) for i in payload["bodies"])
    pa, pb = _props(a), _props(b)
    diag = part_diagonal(payload, old)
    if not duplicate(pa, pb, diag):
        return _declined("not_duplicate", detail="volume/area/centre/box/moments/topology differ",
                         volumes=[pa["volume"], pb["volume"]])
    hist = _drop_pieces([a], [b])
    params = {"bodies": payload.get("body_ids", []),
              "measured": {"volume_mm3": pa["volume"], "dropped": (payload.get("body_ids") or [None, None])[1]}}
    return _finish(payload, old, [(hist, None)], [b], [], op="drop_duplicate_body", params=params, start=start,
                   expected_solids=solids.Extent() - 1)


#: CRS v5 round 2 - ROBUST BOOLEANS. The declines a pair's Boolean is RETRIED on: OCCT's own failures (an invalid or
#: degenerate result, a Boolean that did not complete), never a judgement about the pair (no_overlap, nothing_to_fuse)
RETRY_ON = frozenset({"invalid_result", "fuse_failed", "cut_failed", "cut_degenerate", "fuse_degenerate",
                      "fragment_failed", "fragment_degenerate", "changes_body_count", "boolean_raised"})
#: the fuzzy values a failed pair is retried at, as shares of its measured overlap THICKNESS (common volume over half
#: its area): far under the overlap, so the fuzzy only absorbs the near-coincident edges OCCT trips on (jig probe,
#: 3e7c2a1197c3 B1+B6: the fuse at fuzzy 0 is invalid, at 8.9e-5 mm valid)
FUZZY_OF_THICKNESS = (1e-3, 1e-2)
#: ... and never above this share of the part diagonal (1e-4: 0.01 mm on a 100 mm part, under the fidelity bound)
FUZZY_CAP_OF_DIAGONAL = 1e-4


def fuzzy_ladder(thickness_mm: float | None, diag_mm: float) -> list[float]:
    """The retry fuzzy values for one pair, ascending: each share of the overlap thickness, capped. Pure."""
    cap = FUZZY_CAP_OF_DIAGONAL * max(diag_mm, 1e-9)
    if not thickness_mm or thickness_mm <= 0:
        return [cap / 10, cap]
    return sorted({min(thickness_mm * k, cap) for k in FUZZY_OF_THICKNESS})


def attempts(thickness_mm: float | None, diag_mm: float) -> list[dict]:
    """The ORDERED attempts one pair's Boolean makes (the owner's order): exact; on healed bodies (ShapeFix +
    UnifySameDomain); then healed with each fuzzy step. The first that applies wins; each failure is reported. Pure."""
    return [{"fuzzy_mm": 0.0, "heal_targets": False}, {"fuzzy_mm": 0.0, "heal_targets": True},
            *({"fuzzy_mm": f, "heal_targets": True} for f in fuzzy_ladder(thickness_mm, diag_mm))]


def _common_facts(a: Any, b: Any) -> tuple[float, float]:
    """(volume, area) of the pair's common solid (0, 0 when they share none)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    from meshpipeline.cad.repair_v4 import body_kernel as BK

    op = BRepAlgoAPI_Common()
    op.SetArguments(BK._list([a]))
    op.SetTools(BK._list([b]))
    op.Build()
    if not op.IsDone():
        raise RuntimeError("common failed while measuring overlap")
    shape = op.Shape()
    if not BK._solid_count(shape):
        return 0.0, 0.0
    return abs(BK._volume(shape)), BK._area(shape)


def _with_retries(run: Any, payload: dict, ladder: list[dict]) -> tuple[dict, list[dict]]:
    """`run(payload + attempt)` down `ladder` until one is not a RETRY_ON decline: (its answer, every try made)."""
    tries: list[dict] = []
    got: dict = {}
    for a in ladder:
        try:
            got = run({**payload, **a})
        except RuntimeError as exc:
            got = {"declined": "boolean_raised", "detail": {"error": str(exc)[:200]}}
        tries.append({**a, "result": got.get("declined", "applied")})
        if got.get("declined") not in RETRY_ON:
            break
    return got, tries


@register("v5_boolean_by_volume")
def v5_boolean_by_volume(payload: dict) -> dict:
    """ONE overlapping pair resolved by a Boolean CHOSEN BY VOLUME (the conformal pass's rule, `fuse_share`): a
    duplicate -> drop the copy; overlap >= fuse_share of the smaller body -> fuse; else the smaller is cut by the
    larger (`v4_conformalise cut_by`). Declines `no_overlap` when the pair shares no volume.

    ROBUST (round 2): the chosen Boolean runs down `attempts` - exact, healed bodies, healed + fuzzy stepped from the
    overlap's thickness - and its result is ShapeFixed before it is judged (`body_kernel._finish`)."""
    from meshpipeline.cad.repair_v4 import body_kernel as BK
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep

    old = read_brep(payload["brep_path"])
    solids = _maps(old)["solid"]
    ia, ib = (int(i) for i in payload["bodies"])
    ida, idb = (payload.get("body_ids") or [f"B{ia}", f"B{ib}"])[:2]
    a, b = solids.FindKey(ia), solids.FindKey(ib)
    diag = part_diagonal(payload, old)
    pa, pb = _props(a), _props(b)
    tries: list[dict] = []
    if duplicate(pa, pb, diag):
        got = v5_drop_duplicate_body(payload)
        chosen, why = "drop_duplicate_body", "the pair is one body written twice"
    else:
        try:
            common, area = _common_facts(a, b)
        except RuntimeError:
            return BK._declined("common_failed", detail="the pair's overlap could not be measured")
        if common <= BK.zero_volume_mm3(diag):
            return BK._declined("no_overlap", common_mm3=common)
        thickness = 2.0 * common / area if area > 0 else None
        ladder = attempts(thickness, diag)
        small = min(pa["volume"], pb["volume"])
        share = common / max(small, 1e-12)
        if share >= float(payload.get("fuse_share", 0.5)):
            got, tries = _with_retries(BK._fuse, payload, ladder)
            chosen, why = "fuse", f"overlap is {share:.0%} of the smaller body: fuse"
        else:
            keeper, kid = (ia, ida) if pa["volume"] >= pb["volume"] else (ib, idb)
            got, tries = _with_retries(BK._conformalise, {**payload, "mode": f"cut_by:{kid}", "cut_by": keeper},
                                       ladder)
            chosen, why = "cut", f"overlap is {share:.0%} of the smaller body: cut it by the larger {kid}"
        why += f" (overlap {common:.4g} mm3, thickness {thickness or 0:.3g} mm)"
    if "record" in got:
        rec = got["record"]
        rec["params"] = {**(rec.get("params") or {}), "chosen": chosen, "why": why,
                         **({"tries": tries} if len(tries) > 1 else {})}
        rec["op"] = "boolean_by_volume"
    else:
        got = {**got, "detail": {**(got.get("detail") or {}), "chosen": chosen, "why": why,
                                 **({"tries": tries} if tries else {})}}
    return got


def _general_fragment(args: list[Any], fuzzy: float = 0.0) -> Any:
    """BRepAlgoAPI_BuilderAlgo WITHOUT glue: the arguments really interfere (the conformalise fragment's glue option
    assumes they only touch)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_BuilderAlgo

    from meshpipeline.cad.repair_v4.body_kernel import _list

    alg = BRepAlgoAPI_BuilderAlgo()
    alg.SetArguments(_list(args))
    alg.SetNonDestructive(True)
    alg.SetFuzzyValue(fuzzy)
    alg.SetToFillHistory(True)
    alg.Build()
    if not alg.IsDone():
        raise RuntimeError("fragment (BOPAlgo_Builder) failed")
    return alg


def _interior_points(piece: Any, n: int = 24) -> list[tuple[float, float, float]]:
    from meshpipeline.cad.repair_v5 import tess_kernel as K

    tris = K.triangles(piece)
    box = K.box_of(tris)
    if box is None:
        return []
    pts = K.halton_points(box[0], box[1], 0, 400)
    return [p for p, w in zip(pts, K.winding_numbers(pts, tris), strict=True) if K.inside(w)][:n]


def _rebuild_self_intersecting(solid: Any, fuzzy: float = 0.0) -> tuple[list[Any], list[tuple[Any, Any]], dict]:
    """(the kept solids, the history stages, measured) for ONE self-intersecting solid: MakerVolume over its faces,
    pieces the original encloses (majority of interior points at |winding| >= 0.5), the rest dropped."""
    from OCP.BOPAlgo import BOPAlgo_MakerVolume
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.repair_v4.body_kernel import _list
    from meshpipeline.cad.repair_v4.ws_kernel import _maps
    from meshpipeline.cad.repair_v5 import tess_kernel as K

    faces = []
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        faces.append(exp.Current())
        exp.Next()
    mv = BOPAlgo_MakerVolume()
    mv.SetArguments(_list(faces))
    mv.SetIntersect(True)
    mv.SetNonDestructive(True)
    mv.SetFuzzyValue(fuzzy)
    mv.SetToFillHistory(True)
    mv.Perform()
    if mv.HasErrors():
        raise RuntimeError("MakerVolume failed")
    pieces_map = _maps(mv.Shape())["solid"]
    pieces = [pieces_map.FindKey(i) for i in range(1, pieces_map.Extent() + 1)]
    original = K.triangles(solid)
    kept, gone, votes = [], [], []
    for p in pieces:
        pts = _interior_points(p)
        w = K.winding_numbers(pts, original) if pts else []
        inside = sum(1 for x in w if K.inside(x))
        votes.append([inside, len(w)])
        if pts and inside * 2 > len(w):
            # MakerVolume can hand a piece back inside out (jig/VM, b25999e0f8be: layer 1 non_positive_volume on the
            # output): the same TShape reversed, so the history still names it
            from meshpipeline.cad.repair_v4.body_kernel import _volume

            kept.append(p.Reversed() if _volume(p) < 0.0 else p)
        else:
            gone.append(p)
    stages: list[tuple[Any, Any]] = [(mv.History(), None)]
    if gone:
        stages.append((_drop_pieces(kept, gone), None))
    return kept, stages, {"pieces": len(pieces), "enclosed": len(kept), "votes": votes}


@register("v5_fragment_drop_small")
def v5_fragment_drop_small(payload: dict) -> dict:
    """FRAGMENT the target bodies and DROP the small solids: two or more bodies are split where they interfere
    (General Fuse, no glue) into pieces that share faces; ONE self-intersecting body is rebuilt from its own faces
    (`_rebuild_self_intersecting`). Pieces under `max_fraction` of the PART's volume are dropped; the remaining pieces
    of a single body are fused back into as few solids as the fuse gives (kept apart when the fuse fails).

    ROBUST (round 2): down `attempts` (exact, healed bodies, healed + fuzzy) until one is not an OCCT failure."""
    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of, read_brep

    diag = float(payload.get("part_diag_mm") or 0.0) or diagonal_of(read_brep(payload["brep_path"]))
    got, tries = _with_retries(_fragment_drop_small_once, payload, attempts(None, diag))
    if len(tries) > 1:
        if "record" in got:
            got["record"]["params"] = {**(got["record"].get("params") or {}), "tries": tries}
        else:
            got = {**got, "detail": {**(got.get("detail") or {}), "tries": tries}}
    return got


def _fragment_drop_small_once(payload: dict) -> dict:
    """One attempt of `v5_fragment_drop_small` at the payload's `fuzzy_mm` / `heal_targets`."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    from meshpipeline.cad.repair_v4 import body_kernel as BK
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep

    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    solids = _maps(old)["solid"]
    targets = [solids.FindKey(int(i)) for i in payload["bodies"]]
    fuzzy = float(payload.get("fuzzy_mm") or 0.0)
    args, heal = BK.heal_targets(targets, bool(payload.get("heal_targets")))
    part_volume = sum(abs(BK._volume(solids.FindKey(i))) for i in range(1, solids.Extent() + 1))
    threshold = float(payload["max_fraction"]) * part_volume
    diag = part_diagonal(payload, old)
    measured: dict[str, Any] = {"threshold_mm3": threshold, "part_volume_mm3": part_volume, "fuzzy_mm": fuzzy,
                                "healed_targets": heal["healed"]}
    try:
        if len(targets) == 1:
            pieces, stages, facts = _rebuild_self_intersecting(args[0], fuzzy)
            measured.update(facts)
        else:
            frag = _general_fragment(args, fuzzy)
            pm = _maps(frag.Shape())["solid"]
            pieces = [pm.FindKey(i) for i in range(1, pm.Extent() + 1)]
            bad = BK._degenerate(args, pieces, BK.zero_volume_mm3(diag))
            if bad is not None:
                return BK._declined("fragment_degenerate", **bad)
            stages = [(frag.History(), None)]
            measured["pieces"] = len(pieces)
        stages = [*heal["stages"], *stages]
    except RuntimeError as exc:
        return BK._declined("fragment_failed", detail=str(exc))
    small = [p for p in pieces if abs(BK._volume(p)) < threshold]
    kept = [p for p in pieces if not any(p.IsSame(s) for s in small)]
    measured["dropped_mm3"] = [abs(BK._volume(p)) for p in small]
    if not kept:
        return BK._declined("nothing_left", detail="every piece is under the threshold", **measured)
    if small:
        stages.append((_drop_pieces(kept, small), None))
    if len(targets) == 1 and len(kept) > 1:
        fuse = BRepAlgoAPI_Fuse()
        fuse.SetArguments(BK._list(kept[:1]))
        fuse.SetTools(BK._list(kept[1:]))
        fuse.SetToFillHistory(True)
        fuse.Build()
        if fuse.IsDone():
            fm = _maps(fuse.Shape())["solid"]
            fused = [fm.FindKey(i) for i in range(1, fm.Extent() + 1)]
            if fused and len(fused) < len(kept) and all(BK._is_valid(s) for s in fused):
                stages.append((fuse.History(), None))
                kept = fused
    measured["solids_after"] = len(kept)
    params = {"bodies": payload.get("body_ids", []), "max_fraction": float(payload["max_fraction"]),
              "measured": measured}
    return BK._finish(payload, old, stages, targets, kept, op="fragment_drop_small", params=params, start=start,
                      expected_solids=solids.Extent() - len(targets) + len(kept))


__all__ = ["duplicate", "interference_faces", "v5_boolean_by_volume", "v5_drop_duplicate_body",
           "v5_fragment_drop_small", "v5_overlap_facts", "wire_self_intersects"]
