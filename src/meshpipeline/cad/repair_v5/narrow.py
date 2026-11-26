# Responsibility: v5's NARROW-REGION detector (round 2, fix E): aim a low_quality mesher defect at the geometry
#                 where the mesher's WORST elements are, not at the smallest entity anywhere on the site's faces; and
#                 the local check's "the worst element left this region" judgement.
# Owns: `worst_points`, `measure`, `region_of`, `attach_regions`, `moved` and their constants (PROVISIONAL).
# Boundaries: pure except `measure` (one kernel call through the pool; the kernel half is `narrow_kernel.py`). The
#             faults' thresholds and merge parameters are `quality.py`'s (e = a tenth of the shipped element size);
#             the class menu rows are `loop.CLASS_REPAIRS`; `seq.params_for` reads what this module writes on the cause.
"""Why: step 13's dev wave removed 16 of 36 low_quality defects locally, and the misses were `still_there` at the local
mesh after the repair APPLIED (merge_sliver, remove_small, collapse_edge all changed the part). The cause the class
measured was the site's single smallest entity - on any face the site's cluster touched - and the element that failed
was elsewhere. The mesher says where: `mesh_child` locates a low-quality mesh at its WORST tets (`site.graph["points"]`,
tag "nodes ..."), but those points never reached the defect (`seq._points` reads `p[0]` off a dict and drops them).

THE RULE: read the failing member's worst-element points off the site; measure (`v5_narrow_facts`) what lies within
`REACH_OF_E` x e of them; the NEAREST candidate's kind is the cause, and every candidate of that kind near the points
(nearest first, at most `quality.MAX_AIM`) is aimed:
- a face narrower than e -> `sliver` (merge_sliver, feature-protected as in quality.py), under e^2 -> `small_face`;
- an edge shorter than e -> `small_edge` (collapse_edge);
- a NARROW REGION - two edges sharing no vertex, or two faces sharing no vertex, closer than e - -> `narrow_region`:
  the face the two edges both bound (a neck), else the narrowest face at them; the smaller of two close faces; plus
  the short edges there. Menu: merge_sliver (deviation <= e, no feature protection: the strip is narrower than e),
  defeature of those faces, collapse_edge, remove_small.
Nothing near the points (or no points: Netgen locates no low-quality element) leaves the defect as `quality.py` made it.

MOVED (the local check): a low-quality defect IS its worst elements. After a repair, when the failing member still
fails on the region but every worst element is now more than `MOVED_OF_E` x e from where they were AND the region's
min SICN is strictly better, this defect is gone and what is left is another one (the next round finds it). Reported
as `moved` in the detail, never as a pass; the end-of-round full check (min SICN is a key of the score) keeps or
reverts the round as for every other defect.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v5 import quality as QL

#: the detector looks this many e round each worst-element point. PROVISIONAL
REACH_OF_E = 3.0
#: a worst element farther than this many e from every earlier one has MOVED. PROVISIONAL
MOVED_OF_E = 2.0
#: at most this many low_quality defects per diagnosis are measured (one kernel call each)
MAX_DEFECTS = 6
#: the mesher kinds whose failure is located at its worst elements
QUALITY_KINDS = frozenset({"low_quality", "inverted_elements"})
NARROW = "narrow_region"
#: candidate kinds, in tie order (the same distance: the face first, its edges ride with it)
KIND_ORDER = ("sliver", "small_face", "small_edge", NARROW)
#: the cause keys a quality fault wrote; replaced wholesale when the region re-aims the defect
FAULT_KEYS = frozenset({"kind", "aim", "fault", "fault_why", "faces", "max_dev_mm", "feature_deg", "max_width_mm",
                        "refit_tol_mm", "neighbour", "neighbour_dihedral_deg", "protected", "max_length_mm",
                        "partner", "max_volume_fraction"})
BUDGET_S = 20.0


def worst_points(site: Any, member: str | None) -> list[tuple[float, float, float]]:
    """The failing member's worst-element points on `site` (mm): `site.graph["points"]` entries this member located
    from mesh nodes. Pure."""
    out = []
    for p in (getattr(site, "graph", None) or {}).get("points") or []:
        if not isinstance(p, dict) or p.get("xyz") is None or not str(p.get("tag", "")).startswith("nodes"):
            continue
        if member and p.get("mesher") not in (None, member):
            continue
        try:
            out.append(tuple(float(c) for c in p["xyz"][:3]))
        except (TypeError, ValueError):
            continue
    return out  # type: ignore[return-value]


def measure(pool: Any, ws: Any, points: list, *, h_mm: float) -> dict | None:
    """One `v5_narrow_facts` call round `points` at e = `quality.E_OF_H` x `h_mm`; None when nothing was measured."""
    from meshpipeline.cad.repair_v4.sites import _call

    if not points or not h_mm or h_mm <= 0:
        return None
    e = QL.e_of(h_mm)
    return _call(pool, "v5_narrow_facts", {"brep_path": ws.brep_path, "points": [list(p) for p in points],
                                           "e_mm": e, "radius_mm": REACH_OF_E * e, "budget_s": BUDGET_S})


@dataclasses.dataclass(frozen=True)
class Region:
    kind: str
    #: faces and/or edges to aim at, nearest first
    aim: tuple[StableId, ...]
    #: the ops' parameters (merge_sliver, collapse_edge, remove_small)
    params: dict
    why: str
    #: the nearest candidate's distance from a worst element, mm
    dist_mm: float
    measured: dict


def _candidates(facts: dict, reverse: dict, e: float) -> list[tuple[float, int, str, StableId, dict]]:
    """(distance, kind rank, kind, entity, row) for every small/narrow thing near any point; an entity the reverse
    index cannot place is skipped (never guessed)."""
    best: dict[tuple[str, StableId], tuple[float, int, str, StableId, dict]] = {}

    def add(d: float, kind: str, sid: StableId | None, row: dict) -> None:
        if sid is None:
            return
        key = (kind, sid)
        cand = (float(d), KIND_ORDER.index(kind), kind, sid, row)
        if key not in best or cand[:2] < best[key][:2]:
            best[key] = cand

    for pt in facts.get("points") or []:
        widths = {int(f["face"]): f for f in pt.get("faces") or []}
        for f in pt.get("faces") or []:
            area, width = float(f["area_mm2"]), float(f["width_mm"])
            if area < e * e:
                add(f["dist_mm"], "small_face", reverse.get(("F", int(f["face"]))), f)
            elif width < e:
                add(f["dist_mm"], "sliver", reverse.get(("F", int(f["face"]))), f)
        edges = {int(r["edge"]): r for r in pt.get("edges") or []}
        for r in pt.get("edges") or []:
            if 0 < float(r["length_mm"]) < e:
                add(r["dist_mm"], "small_edge", reverse.get(("E", int(r["edge"]))), r)
        for pr in pt.get("edge_pairs") or []:
            # the face both edges bound (a neck), else the narrowest measured face at either edge
            around = list(pr.get("common") or []) or [*pr.get("faces_a", []), *pr.get("faces_b", [])]
            measured = [widths[i] for i in around if i in widths]
            face = (min(measured, key=lambda f: float(f["width_mm"]))["face"] if measured
                    else (around[0] if around else None))
            if face is None:
                continue
            d = min(float(edges.get(int(pr["a"]), {}).get("dist_mm", math.inf)),
                    float(edges.get(int(pr["b"]), {}).get("dist_mm", math.inf)))
            row = {**widths.get(int(face), {"face": int(face)}), "pair": pr}
            add(d if math.isfinite(d) else REACH_OF_E * e, NARROW, reverse.get(("F", int(face))), row)
        for pr in pt.get("face_pairs") or []:
            fa, fb = widths.get(int(pr["a"])), widths.get(int(pr["b"]))
            if fa is None or fb is None:
                continue
            small = fa if float(fa["area_mm2"]) <= float(fb["area_mm2"]) else fb
            add(min(float(fa["dist_mm"]), float(fb["dist_mm"])), NARROW, reverse.get(("F", int(small["face"]))),
                {**small, "pair": pr})
    return sorted(best.values(), key=lambda c: (c[0], c[1], c[3]))


def region_of(facts: dict | None, reverse: dict, *, h_mm: float) -> Region | None:
    """The region at the worst elements, or None when nothing small or narrow is near them. Pure."""
    if not facts or not facts.get("points"):
        return None
    e = float(facts.get("e_mm") or QL.e_of(h_mm))
    cands = _candidates(facts, reverse, e)
    if not cands:
        return None
    kind = cands[0][2]
    mine = [c for c in cands if c[2] == kind]
    aim: list[StableId] = []
    for c in mine:
        if c[3] not in aim:
            aim.append(c[3])
    aim = aim[:QL.MAX_AIM]
    rows = [c[4] for c in mine if c[3] in aim]
    first = rows[0]
    d0 = cands[0][0]
    params: dict[str, Any]
    if kind == "small_edge":
        longest = max(float(r["length_mm"]) for r in rows)
        params = {"max_length_mm": QL.SIZE_MARGIN * longest, "max_width_mm": QL.SIZE_MARGIN * longest}
        why = (f"{len(aim)} edge(s) under e = {e:.4g} mm at the worst elements (nearest {aim[0]}, "
               f"{float(first['length_mm']):.4g} mm long, {d0:.3g} mm away)")
    else:
        params = QL._merge_params(first, reverse, e, protect=kind == "sliver")
        params["max_width_mm"] = QL.SIZE_MARGIN * max(float(r.get("rs_width_mm") or 0.0) for r in rows)
        if kind == NARROW:
            # a strip narrower than e is no feature at this mesh size: no protection, the deviation bound decides
            params["feature_deg"] = QL.NO_PROTECTION_DEG
            # the short edges in the region ride along (collapse_edge aims the cause's edges)
            short = [c for c in cands if c[2] == "small_edge"][:QL.MAX_AIM]
            if short:
                aim = [*aim, *(c[3] for c in short)]
                longest = max(float(c[4]["length_mm"]) for c in short)
                params["max_length_mm"] = QL.SIZE_MARGIN * longest
            pr = first.get("pair") or {}
            why = (f"a narrow region at the worst elements: {'edges' if 'common' in pr else 'faces'} "
                   f"{pr.get('gap_mm', 0):.4g} mm apart (under e = {e:.4g} mm), aimed at {aim[0]} ({d0:.3g} mm away)")
        else:
            what = (f"{float(first['area_mm2']):.4g} mm2" if kind == "small_face"
                    else f"{float(first['width_mm']):.4g} mm wide")
            why = (f"{len(aim)} {kind.replace('_', ' ')}(s) under e = {e:.4g} mm at the worst elements (nearest "
                   f"{aim[0]}, {what}, {d0:.3g} mm away)")
    measured = {"e_mm": e, "dist_mm": d0, "candidates": len(cands),
                "kinds": sorted({c[2] for c in cands}), "points": len(facts.get("points") or [])}
    return Region(kind, tuple(aim), params, why, d0, measured)


def attach_regions(defects: list[Defect], regions: dict[str, tuple[list, Region | None]]) -> list[Defect]:
    """`defects` with each measured low_quality defect re-aimed at its region (`regions`: defect id -> (its worst
    points, the region or None)). Pure. A defect with points but no region keeps its cause and says so."""
    out = []
    for d in defects:
        if d.id not in regions or d.cls != QL.LOW_QUALITY:
            out.append(d)
            continue
        points, region = regions[d.id]
        base = {**(d.cause or {}), "worst_points": [list(p) for p in points]}
        if region is None:
            out.append(dataclasses.replace(d, cause=base, rule=f"{d.rule}; nothing small or narrow within "
                                                                f"{REACH_OF_E:g} e of the worst elements"))
            continue
        cause = {k: v for k, v in base.items() if k not in FAULT_KEYS}
        cause.update(kind=region.kind, aim=list(region.aim), fault=region.measured, fault_why=region.why,
                     located_by="worst_elements", **region.params)
        out.append(dataclasses.replace(d, cause=cause, rule=f"{d.rule.split('; measured ', 1)[0]}; at the worst "
                                                            f"elements: {region.why}"))
    return out


def moved(defect: Defect, detail: dict) -> str | None:
    """Why the local mesh shows THIS low-quality defect gone though the member still fails, or None. Pure.

    Gone here = the member still fails with a quality kind on the repaired region, the region failed before the repair
    too, its min SICN is strictly better, and every worst element is more than `MOVED_OF_E` x e from every one before
    (the region's own before mesh, else the full check's points)."""
    if defect.kind != "mesher" or defect.code not in QUALITY_KINDS or defect.cls != QL.LOW_QUALITY:
        return None
    if detail.get("member_status") != "fail" or detail.get("member_error_kind") not in QUALITY_KINDS:
        return None
    if detail.get("region_passed_before", True):
        return None
    after, before = detail.get("member_min_sicn"), detail.get("region_min_sicn_before")
    if after is None or before is None or not float(after) > float(before):
        return None
    now = [p for p in detail.get("member_points") or [] if p]
    was = [p for p in detail.get("region_points_before") or [] if p] or list(
        (defect.cause or {}).get("worst_points") or [])
    h = QL.h_of(defect) or float((defect.cause or {}).get("h_mm") or 0.0)
    if not now or not was or not h:
        return None
    reach = MOVED_OF_E * QL.e_of(h)
    nearest = min(math.dist(a[:3], b[:3]) for a in now for b in was)
    if nearest <= reach:
        return None
    return (f"moved: the worst element is {nearest:.3g} mm from where it was (> {reach:.3g} mm) and min SICN "
            f"{float(before):.3g} -> {float(after):.3g}; the member still fails elsewhere")
