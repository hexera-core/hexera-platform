# Responsibility: v5 DIAGNOSE: give every defect of one full check (a v4 Verdict) a CLASS and a CAUSE measurement,
#                 in repair priority order. The ONE classifier and the ONE class list of v5.
# Owns: `DefectClass`, `PRIORITY`, `classify_site` (the pure rule), `attach_faults` (the invalid_brep_tolerance
#       detectors' faults on the defects, pure), `diagnose` (the driver the loop calls), `order`.
# Boundaries: no OCCT here. The defects are the loop's own `local_verify.Defect`s (`seq.defects_of`: one per
#             code and site, body_overlap clustered), so their LOCATION is already on them; this module fills
#             `cls`, `cause`, `rule` and `evidence`. The extra measurements are the `v5_pair_facts` kernel fn
#             (diagnose_kernel.py) and `v5_brep_facts` (brep_facts_kernel.py, judged by brep_faults.py). A defect no rule places is `unknown`; `describe_ai.py` may describe it.
"""Defect diagnosis: every failure becomes a defect with a class, a location and a cause.

THE RULES (spec "Defect classes", owner's list of 2026-11-24), applied to one defect's own evidence at its site,
first match wins:

0. a `body_overlap` whose every Boolean failed (`boolean_failed`): the checker's failure, `CHECKER_FAILED`.
1. `overlap_self_intersection` (validity): layer 1 reported `body_overlap` or `self_intersection`. Cause: the
   overlap volume and thickness layer 1 measured, per body pair.
2-5. `BY_CODE` names the class outright for EVERY code layer 1 can emit (`verdict.valid.HARD_CODES`/`SOFT_CODES`)
   and for the mesher kinds that name one: `open_shell_volume` (open/inside-out/non-manifold shells, no volume,
   open loop), `invalid_brep_tolerance` (invalid B-rep, tolerance, unbounded, round trip, poles), `low_quality`
   (small faces/edges), `closed_periodic_face` (periodic surfaces). Cause: the codes and the site's free loops (none
   left -> the volume is made, not the loop filled). A code BY_CODE does not know is `unknown`.
6. `close_faces`: a crossing kind (overlapping_facets, edge_through_face, HXT `other`, a mesher crash) or Netgen's
   `netgen_meshing_failed` AND a non-adjacent face pair within `h`, apart (gap > contact), whose facets the PREDICTOR
   (`close_faces_kernel.v5_facet_cross`: the pair meshed in 2D by each gmsh member's own settings) finds crossing - or,
   when the predictor could not answer, that is near-coaxial, near-tangent, a thin wall in one body, or whose facet
   sag at `h` bridges the gap. A failure that names NO face is given the pairs of ONE whole-part scan per file
   (`v5_close_scan`, time-capped; step 7, owner-approved). Cause: gap, axis angle/offset, radii, facet sag, the
   segments that would close it, the predicted crossings per member, and the pairs/faces the repair aims at.
7. `surface_crossing`: `edge_through_face`, or a crossing kind at a CONTACT pair (gap ~ 0) or at a point the mesher
   printed. Cause: gap, normal angle at the closest points, the point.
8. `low_quality`: `low_quality` / `inverted_elements`, or a sliver site. Cause: min SICN per mesher, the site's
   soft findings, its distance over h.
9. a mesher crash or timeout with nothing located: NOT a defect - `CHECKER_FAILED`, an outcome the loop records.
6b. `facet_crossing_local` (round 2, AFTER the rules, `facet_local.attach_local`): a mesher defect of a crossing kind
   that close_faces did not confirm, whose faces (the whole part when none is named) the gmsh members' own 2D meshes
   show crossing WITHIN one face, ACROSS a shared edge or at TOUCHING faces. Cause: the relation, the faces' type,
   curvature over h, span, spline breaks, parameter speed, analytic fit, width, the shared edge's angle.
10. anything else is a failure nobody could locate: `unknown` (netgen_meshing_failed, HXT `other` with no close
    pair). `describe_ai` may describe it; it never picks a class.

THEN THE DETECTORS (v5 step 5, `attach_faults`): one `v5_brep_facts` measurement per diagnosis; an
invalid_brep_tolerance defect gets its fault KIND (large_tolerance, unbounded, pole, bad_spline - the kind
picks the rule seed), an `unknown` mesher failure on a measured pole/spline/tolerance fault becomes
invalid_brep_tolerance, and layer 1's soft large_tolerance becomes a defect of its own. A suspected units mismatch is
REPORTED only (owner decision A, 2026-11-25): never a defect, never rescaled.

THEN THE LOW_QUALITY DETECTORS (v5 step 10, `quality.attach_quality`): one `v5_quality_facts` measurement at
e = a tenth of `h`, only when a low_quality defect or a soft small_face/small_edge finding is there; a low_quality
defect gets its worst small edge / small face / sliver / small solid as cause (the kind picks the rule seed), and each
soft small-entity finding the detector confirms becomes a low_quality defect of its own (ordered after the mesh
classes: it is not a validity failure).
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Literal

from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.seq import CHECKER_FAILED
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v5 import brep_faults as F
from meshpipeline.cad.repair_v5 import crossing as X
from meshpipeline.cad.repair_v5 import facet_local as FL
from meshpipeline.cad.repair_v5 import narrow as N
from meshpipeline.cad.repair_v5 import open_shell as S
from meshpipeline.cad.repair_v5 import quality as QL

DefectClass = Literal[
    "overlap_self_intersection", "open_shell_volume", "invalid_brep_tolerance", "close_faces", "facet_crossing_local",
    "surface_crossing", "closed_periodic_face", "low_quality", "unknown",
]
#: THE class list, in repair order: validity first, then the mesh classes. `facet_crossing_local` (round 2, owner
#: 2026-11-25): facets crossing within a face / across a shared edge / at touching faces (`facet_local`)
PRIORITY: tuple[DefectClass, ...] = (
    "overlap_self_intersection", "open_shell_volume", "invalid_brep_tolerance", "close_faces", "facet_crossing_local",
    "surface_crossing", "closed_periodic_face", "low_quality", "unknown",
)
OVERLAP_CODES = frozenset({"body_overlap", "self_intersection"})
#: layer-1 codes and mesher kinds that name a class outright
BY_CODE: dict[str, str] = {
    # layer 1 (`verdict.valid.HARD_CODES` + `SOFT_CODES`; the test pins that every one is here)
    "body_overlap": "overlap_self_intersection", "self_intersection": "overlap_self_intersection",
    "open_shell": "open_shell_volume", "no_solids": "open_shell_volume", "non_manifold": "open_shell_volume",
    "non_positive_volume": "open_shell_volume",
    "brep_invalid": "invalid_brep_tolerance", "large_tolerance": "invalid_brep_tolerance",
    "unbounded_geometry": "invalid_brep_tolerance", "round_trip_invalid": "invalid_brep_tolerance",
    "units_changed": "invalid_brep_tolerance",
    "small_face": "low_quality", "small_edge": "low_quality",
    # a skipped signed-volume check: the checker's, never a defect
    "orientation_unchecked": CHECKER_FAILED,
    # meshers
    "no_volume": "open_shell_volume", "open_loop": "open_shell_volume", "pole": "invalid_brep_tolerance",
    "periodic_surface": "closed_periodic_face",
}
#: mesher kinds where two faces' discretisations cross or an edge pierces a face. `other` is HXT's "HXT 3D mesh
#: failed" (D28: the crossing-triangle kind); a crash on these files is the same geometry failing harder.
CROSSING_KINDS = frozenset({"overlapping_facets", "edge_through_face", "other", "gmsh_crash", "netgen_crash"})
QUALITY_KINDS = frozenset({"low_quality", "inverted_elements"})
#: the checker itself failed: a crash, a timeout, the pool. With nothing located it says nothing about WHERE.
CHECKER_KINDS = frozenset({"gmsh_crash", "netgen_crash", "timeout", "netgen_timeout", "infrastructure",
                           "capped_after_doom"})
NEAR_COAXIAL_DEG = 1.0
NEAR_COAXIAL_OFFSET_OF_RADIUS = 0.05
NEAR_TANGENT_DEG = 1.0
#: a pair farther apart than this many element sizes cannot make facets cross
GAP_REACH_H = 1.0
CONTACT_MM = 1e-7      # = sites_kernel.CONTACT_MM; not imported (parent modules never import a kernel module)
#: the gmsh default for a full circle when no size field is set (Mesh.MinimumCirclePoints)
MIN_CIRCLE_SEGMENTS = 7
#: v5 step 7: the mesher kinds a close pair can explain (Netgen's own word for a boundary it cannot recover)
CLOSE_KINDS = CROSSING_KINDS | {"netgen_meshing_failed"}
#: the whole-part scan (once per file): pairs within this share of the part's diagonal (and within GAP_REACH_H x h),
#: under this time cap, at most this many. CALIBRATED on the baseline's overlapping_facets dev files (step 7 report).
SCAN_GAP_OF_DIAG = 0.01
SCAN_BUDGET_S = 60.0
SCAN_MAX_PAIRS = 64
#: at most this many close candidates per site are meshed by the predictor, in this much time
MAX_PREDICTED = 8
PREDICT_BUDGET_S = 60.0
#: a predictor answer CONFIRMS the class (step 7): a close pair whose facets cross at no member is not close_faces
CONFIRM_BY_PREDICTOR = True


def facet_sag_mm(radius: float, h_mm: float) -> tuple[int, float]:
    """(segments a mesher puts round a full circle of `radius` at size `h_mm`, the chord's sag at that count)."""
    if radius <= 0 or h_mm <= 0:
        return MIN_CIRCLE_SEGMENTS, 0.0
    n = max(MIN_CIRCLE_SEGMENTS, math.ceil(2 * math.pi * radius / h_mm))
    return n, radius * (1.0 - math.cos(math.pi / n))


def _radii(p: dict) -> list[float]:
    return [float(r) for r in (p.get("radius_a_mm"), p.get("radius_b_mm"), p.get("radius_mm")) if r]


def _near_coaxial(p: dict) -> bool:
    if p.get("axis_angle_deg") is None:
        return False
    r = min(p.get("radius_a_mm") or 0.0, p.get("radius_b_mm") or 0.0)
    return p["axis_angle_deg"] <= NEAR_COAXIAL_DEG and p["axis_offset_mm"] <= NEAR_COAXIAL_OFFSET_OF_RADIUS * max(r, 1e-9)


def _near_tangent(p: dict, h_mm: float) -> bool:
    return (p.get("plane_axis_tilt_deg") is not None and p["plane_axis_tilt_deg"] <= NEAR_TANGENT_DEG
            and p["plane_axis_gap_mm"] <= GAP_REACH_H * h_mm)


def _same_body(p: dict) -> bool:
    return p.get("solid_a") is not None and p.get("solid_a") == p.get("solid_b")


def _sag_bridges(p: dict, h_mm: float) -> bool:
    return sum(facet_sag_mm(r, h_mm)[1] for r in _radii(p)) >= p["gap_mm"]


def _why_close(p: dict, h_mm: float) -> str | None:
    """Why this pair's facets can cross at `h`, or None when they cannot."""
    if _near_coaxial(p):
        return "near-coaxial"
    if _near_tangent(p, h_mm):
        return "near-tangent"
    if _same_body(p):
        return "thin wall"
    if _sag_bridges(p, h_mm):
        return "facet sag bridges the gap"
    return None


def _crossing_members(p: dict) -> list[str] | None:
    """The members whose facets the predictor found crossing on this pair; None when it was not measured."""
    cross = p.get("cross")
    if not isinstance(cross, dict) or not cross:
        return None
    return sorted(m for m, n in cross.items() if n)


def _close_reason(p: dict, h_mm: float) -> str | None:
    """Why this pair is a close_faces cause, or None: the predictor's answer when it gave one, else the geometry."""
    hit = _crossing_members(p)
    if hit is None or not CONFIRM_BY_PREDICTOR:
        return _why_close(p, h_mm)
    if not hit:
        return None
    geometry = _why_close(p, h_mm)
    return f"facets cross at {'/'.join(hit)}" + (f" ({geometry})" if geometry else "")


def _summed(rows: list[dict]) -> dict[str, int] | None:
    """Predicted crossings per member over `rows` (None when none was measured)."""
    got = [r["cross"] for r in rows if isinstance(r.get("cross"), dict)]
    if not got:
        return None
    return {m: sum(int(c.get(m) or 0) for c in got) for m in sorted({m for c in got for m in c})}


def _repair_aim(close: list[dict], best: dict) -> dict:
    """What the class's repairs are aimed at: the best pair's faces, every close pair (split_by_proximity), and the
    near-coaxial faces round the best pair (split_aligned's shared-angle split of BOTH faces)."""
    pairs = [[p["a"], p["b"]] for p in close][:MAX_PREDICTED]
    coaxial = _near_coaxial(best)
    aligned: list = []
    if coaxial:
        group = {best["a"], best["b"]}
        for p in close:
            if _near_coaxial(p) and {p["a"], p["b"]} & group:
                group |= {p["a"], p["b"]}
        aligned = sorted(group)
    return {"aim": [best["a"], best["b"]], "face_pairs": pairs, "coaxial": coaxial, "aligned_faces": aligned,
            "predicted_before": _summed(close), "crossing_pairs": sum(bool(_crossing_members(p)) for p in close)}


def _close_cause(p: dict, h_mm: float) -> dict:
    gap = p["gap_mm"]
    radius = min(_radii(p), default=None)
    n, sag = facet_sag_mm(radius, h_mm) if radius else (None, 0.0)
    # segments at which the sag falls under half the gap: what an aligned split must at least provide
    need = (math.ceil(math.pi / math.acos(max(-1.0, 1.0 - gap / (2 * radius)))) if radius and gap > 0 else None)
    keys = ("a", "b", "solid_a", "solid_b", "body_a", "body_b", "surface_a", "surface_b", "gap_mm", "axis_angle_deg", "axis_offset_mm",
            "radius_a_mm", "radius_b_mm", "radius_mm", "plane_axis_tilt_deg", "plane_axis_gap_mm",
            "split_aligned_coaxial", "point_a", "point_b", "cross")
    return {**{k: p[k] for k in keys if k in p}, "h_mm": h_mm, "segments_at_h": n, "sag_at_h_mm": round(sag, 6),
            "gap_over_sag": round(gap / sag, 4) if sag > 0 else None, "segments_needed": need,
            "same_body": _same_body(p)}


def classify_site(site: Site, *, pairs: list[dict], h_mm: float, hard: list[dict], min_sicn: dict[str, float | None]
                  ) -> tuple[str, str, dict]:
    """(class or `CHECKER_FAILED`, rule in words, cause) for one site. Pure.

    `pairs` are `v5_pair_facts` rows translated to stable ids (`a`/`b` face ids, `body_a`/`body_b`); `hard` the
    layer-1 hard findings on this site's entities as `{code, entities (stable), detail}`; `min_sicn` per failing
    mesher."""
    failures = site.graph.get("failures") or []
    codes = {f["error_kind"] for f in failures if f.get("mesher") == "valid"} | set(site.graph.get("hard_codes") or [])
    codes |= {h["code"] for h in hard}
    mesh_kinds = {str(f.get("error_kind")) for f in failures if f.get("mesher") != "valid"}

    overlaps = [h for h in hard if h["code"] == "body_overlap"]
    if codes == {"body_overlap"} and overlaps and all(h["detail"].get("boolean_failed") for h in overlaps):
        return (CHECKER_FAILED, "every overlap Boolean failed: the checker could not decide, not a defect",
                {"findings": overlaps})
    if codes & OVERLAP_CODES:
        return ("overlap_self_intersection", f"layer 1 reported {', '.join(sorted(codes & OVERLAP_CODES))}",
                {"findings": [h for h in hard if h["code"] in OVERLAP_CODES],
                 "overlap_volume_mm3": sum(float(h["detail"].get("volume") or 0.0) for h in hard
                                           if h["code"] == "body_overlap") or None,
                 "max_overlap_thickness_mm": max((float(h["detail"].get("thickness") or 0.0) for h in hard
                                                  if h["code"] == "body_overlap"), default=None),
                 "self_intersecting": sorted({e for h in hard if h["code"] == "self_intersection"
                                              for e in h["entities"]})})
    rank = {c: i for i, c in enumerate((*PRIORITY, CHECKER_FAILED))}
    named = sorted({BY_CODE[c] for c in codes | mesh_kinds if c in BY_CODE}, key=rank.__getitem__)
    if named:
        which = sorted(c for c in codes | mesh_kinds if BY_CODE.get(c) == named[0])
        return (named[0], f"{'/'.join(which)} -> {named[0]}",
                {"codes": which, "findings": [h for h in hard if h["code"] in which],
                 "free_loops": len(site.graph.get("free_loops") or [])})
    if codes:
        return ("unknown", f"layer-1 code(s) {', '.join(sorted(codes))} are outside the v5 class list",
                {"codes": sorted(codes), "findings": hard})

    kinds = "/".join(sorted(mesh_kinds)) or site.kind
    crossing = mesh_kinds & CROSSING_KINDS or (site.kind in ("intersection", "self_overlap") and not mesh_kinds)
    near = [p for p in pairs if p.get("gap_mm") is not None and p["gap_mm"] <= GAP_REACH_H * h_mm]
    if crossing or mesh_kinds & CLOSE_KINDS:
        close = [p for p in near if p["gap_mm"] > CONTACT_MM and _close_reason(p, h_mm)]
        if close:
            best = min(close, key=lambda p: (not _crossing_members(p), not p.get("split_aligned_coaxial"),
                                             p["gap_mm"]))
            return ("close_faces",
                    f"{kinds} at {_close_reason(best, h_mm)}: faces {best['a']}-{best['b']} "
                    f"{best['gap_mm']:.4g} mm apart (h {h_mm:.4g} mm)",
                    {**_close_cause(best, h_mm), **_repair_aim(close, best), "close_pairs": len(close)})
    if crossing:
        contacts = [p for p in near if p["gap_mm"] <= CONTACT_MM]
        printed = [pt for pt in site.graph.get("points") or [] if "point" in str(pt.get("tag", ""))]
        if contacts or printed or "edge_through_face" in mesh_kinds:
            why = ("faces in contact" if contacts else "a point the mesher printed" if printed
                   else "edge_through_face")
            return ("surface_crossing", f"{kinds}: {why}",
                    {"pairs": [{k: p.get(k) for k in ("a", "b", "gap_mm", "normal_angle_deg", "point_a", "surface_a",
                                                         "surface_b", "body_a", "body_b")} for p in contacts[:3]],
                     "contacts": len(contacts), "printed_points": printed[:4]})
    if mesh_kinds & QUALITY_KINDS or site.kind == "sliver":
        soft = site.graph.get("soft") or []
        return ("low_quality", f"{'/'.join(sorted(mesh_kinds)) or 'sliver'} at site {site.id}",
                {"min_sicn": {m: v for m, v in min_sicn.items() if m in site.meshers_failing},
                 "dist_over_h": site.dist_over_h, "h_mm": h_mm,
                 "soft": [{k: s.get(k) for k in ("code", "entity", "detail")} for s in soft][:6],
                 "nearest_pair": min(near, key=lambda p: p["gap_mm"]) if near else None})
    if mesh_kinds and mesh_kinds <= CHECKER_KINDS:
        return (CHECKER_FAILED, f"{kinds} with nothing located: the checker failed, not a defect",
                {"kinds": sorted(mesh_kinds)})
    return ("unknown", f"nothing locates {kinds}",
            {"kinds": sorted(mesh_kinds), "site_kind": site.kind, "nearest_pair": near[0] if near else None})


#: CRS v5 r2: inside the MESH tier (everything that is not a hard validity failure) the cheap, deterministic classes
#: go first, so a slow class cannot starve them of the class loop's wall. Measured per-class tournament wall on the
#: step-13 wave (4a263b160), median / p90 s: open_shell_volume 0/0, overlap 8/79, invalid_brep 11/165, low_quality
#: 17/120, close_faces 63/329, closed_periodic_face 100 (n=1), surface_crossing 256 (n=1). The validity tier keeps
#: `PRIORITY` (a repair of one there can only help the next).
#: CRS v5 r2h: the two RULE-BASED classes (a seam rebuilt or a face split at its period, a tolerance tightened) go
#: first: they are cheap, and round 2 C found periodic defects never reached behind the measured classes.
COST_ORDER: tuple[str, ...] = (
    "closed_periodic_face", "invalid_brep_tolerance",
    "open_shell_volume", "overlap_self_intersection", "low_quality", "close_faces",
    # round 2 B's class sits between close_faces and surface_crossing, as in PRIORITY (it takes the crossings
    # close_faces did not confirm, before the surface_crossing fallback)
    "facet_crossing_local", "surface_crossing", "unknown",
)


def code_class(d: Defect) -> str | None:
    """The class a defect's CODE alone names, without any measurement: `BY_CODE`, a crossing kind ->
    facet_crossing_local (its general row aims at the defect's own faces), a quality kind -> low_quality. None when
    the code names nothing (a checker failure stays what it is). Pure."""
    cls = BY_CODE.get(d.code)
    if cls is not None:
        return None if cls == CHECKER_FAILED else cls
    if d.kind == "mesher" and d.code in FL.KINDS_EXPLAINED:
        return FL.CLASS
    if d.code in QUALITY_KINDS:
        return QL.LOW_QUALITY
    return None


def by_code_when_cut(defects: list[Defect], why: str) -> list[Defect]:
    """CRS v5 r2h (owner decision 2026-11-26: never degrade to unknown because of time): when the diagnosis share of
    the wall cut its measurements short (`why`), every `unknown` defect keeps the class its code names
    (`code_class`) - the cheap, rule-based classification - with `cause["cut_short"]` saying so, instead of `unknown`
    (fix A: 34 defects in 17 files of the step-14 wave were `unknown`, 32 of them in files whose diagnosis was cut).
    Re-ordered. Pure."""
    out = []
    for d in defects:
        cls = code_class(d) if d.cls == "unknown" else None
        if cls is None:
            out.append(d)
            continue
        out.append(dataclasses.replace(d, cls=cls, cause={**(d.cause or {}), "cut_short": why},
                                       rule=f"{d.code} -> {cls} by its code ({why}; was: {d.rule})"))
    return order(out)


def order(defects: list[Defect]) -> list[Defect]:
    """Validity first, by class priority; then the mesh tier cheapest class first (`COST_ORDER`), a
    `CHECKER_FAILED` last; stable within a class. A low_quality defect made from a layer-1 SOFT finding (step 10) is
    not a validity failure: it goes with the mesh classes."""
    rank = {c: i for i, c in enumerate(PRIORITY)}
    cost = {c: i for i, c in enumerate(COST_ORDER)}

    def key(d: Defect) -> tuple[bool, int]:
        mesh_tier = d.kind != "validity" or d.code in QL.SOFT_CODES
        table = cost if mesh_tier else rank
        return mesh_tier, table.get(d.cls or "unknown", len(table))  # type: ignore[call-overload]
    return sorted(defects, key=key)


def _translate_pairs(raw: list[dict], reverse: dict) -> list[dict]:
    out = []
    for p in raw:
        a, b = reverse.get(("F", int(p["a"]))), reverse.get(("F", int(p["b"])))
        if a is None or b is None:
            continue
        out.append({**p, "a": a, "b": b,
                    "body_a": reverse.get(("B", int(p["solid_a"]))) if p.get("solid_a") else None,
                    "body_b": reverse.get(("B", int(p["solid_b"]))) if p.get("solid_b") else None})
    return out


def _site_of(defect: Defect, sites: dict[str, Site]) -> Site | None:
    """The site a loop defect was built from: its id is the site id (`S@...`), or `<site id>:overlap:<root>` for a
    body_overlap cluster."""
    return sites.get(defect.id) or sites.get(defect.id.split(":overlap:", 1)[0])


def _own_view(site: Site, defect: Defect) -> Site:
    """The site as THIS defect sees it: only the failures of its own code (a mesher defect is not classified by a
    layer-1 code at the same place, nor a validity defect by a mesher's)."""
    graph = site.graph or {}
    valid = defect.kind == "validity"
    failures = [f for f in graph.get("failures") or []
                if (f.get("mesher") == "valid") == valid and str(f.get("error_kind")) == defect.code]
    hard_codes = [c for c in graph.get("hard_codes") or [] if valid and c == defect.code]
    return dataclasses.replace(site, graph={**graph, "failures": failures, "hard_codes": hard_codes})


# --- invalid_brep_tolerance: the detectors' faults (brep_faults.py) on the defects (v5 step 5) ----------------------

#: when a located failure sits on more than one kind of fault, the one repaired first (the most local cause)
FAULT_PRIORITY = ("missing_edge", "bad_seam", "pole", "bad_spline", "large_tolerance")
PERIODIC_CLASS = "closed_periodic_face"
#: round 2: at most this many closed faces aim a periodic_surface defect that names no face
MAX_PERIODIC_AIM = 16
#: a defect code that names its fault kind outright
KIND_OF_CODE = {"unbounded_geometry": "unbounded", "pole": "pole", "large_tolerance": "large_tolerance",
                "brep_invalid": "brep_invalid"}
#: the codes of the defects the detectors add (layer 1 reports large_tolerance only as a SOFT finding)
FAULT_CODE = {"large_tolerance": "large_tolerance"}
TOLERANCE_CLASS = "invalid_brep_tolerance"


def _periodic(d: Defect, where: set[str], faults: list[F.Fault],
              closed_faces: list[tuple[str, str | None]]) -> Defect:
    """Round 2: a closed_periodic_face defect AIMED. A bad seam on its faces (or, when it names no face, anywhere on
    its bodies) makes it a `bad_seam` (rebuild_seam is the rule seed); a defect that names no face is aimed at the
    closed faces of its bodies (of the part when it names none) - the faces split_closed works on. Pure."""
    own = [e for e in d.entities if e[:1] == "F"]
    faces = {e for e in where if e[:1] == "F"}
    bodies = {e for e in where if e[:1] in ("B", "S")}
    closed = [f for f, _b in closed_faces]
    mine = [f for f, b in closed_faces if not bodies or b in bodies]
    # the site's closed faces first (the mesher located them), else its bodies' closed faces, else the site's faces
    located = [f for f in closed if f in faces]
    scope = set(own) or set(located) or set(mine) or faces
    seam = next((f for f in faults if f.kind == "bad_seam" and set(f.entities) & scope), None)
    if seam is not None:
        aim = [e for e in seam.entities if e in scope]
        return dataclasses.replace(d, cause={**d.cause, "kind": "bad_seam", "fault": seam.measured,
                                             "fault_why": seam.why, "aim": aim},
                                   rule=f"{d.rule}; measured bad_seam: {seam.why}")
    if own or not scope:
        return d
    aim = located or mine or sorted(faces)
    where_from = ("the site's closed faces" if located else
                  f"the closed faces of {'its bodies' if bodies else 'the part'}" if mine else "the site's faces")
    return dataclasses.replace(d, cause={**d.cause, "aim": aim[:MAX_PERIODIC_AIM], "closed_faces": len(aim)},
                               rule=f"{d.rule}; aimed at {len(aim)} face(s): {where_from}")


def attach_faults(defects: list[Defect], faults: list[F.Fault], sites: dict[str, Site], *,
                  soft_codes: frozenset[str] = frozenset(),
                  closed_faces: list[tuple[str, str | None]] = ()) -> list[Defect]:  # type: ignore[assignment]
    """`defects` with the detectors' `faults` (stable ids) put on them. Pure.

    - an invalid_brep_tolerance defect gets its fault KIND (which picks the rule seed: `brep_faults.KIND_REPAIRS`),
      the measurement, and an `aim` (the fault's entities at this defect) - from its code, or for a `brep_invalid`/
      `pole` defect from the most local fault on its entities;
    - an `unknown` MESHER defect whose faces carry a pole/spline/tolerance fault is that fault's: invalid_brep_tolerance,
      with the measured cause (it was a failure nobody could explain, and now something measured explains it);
    - a fault no defect took becomes its own defect only where layer 1 reported it (large_tolerance, a soft code)
      (units are report-only, never a defect). A pole or spline fault no failure is located on is not a defect: clean is
      valid + meshes, and a part that meshes round a bad spline is not broken."""
    taken: set[str] = set()
    out: list[Defect] = []
    for d in defects:
        site = _site_of(d, sites)
        where = set(d.entities) | set(site.entities if site is not None else ())
        near = sorted((f for f in faults if f.kind in FAULT_PRIORITY and set(f.entities) & where),
                      key=lambda f: FAULT_PRIORITY.index(f.kind))
        by_code = KIND_OF_CODE.get(d.code)
        if d.cls == TOLERANCE_CLASS:
            pick = next((f for f in near if f.kind == by_code), None) if by_code not in (None, "brep_invalid") else (
                near[0] if near else None)
            kind = pick.kind if pick is not None else by_code
            cause = {**d.cause, "kind": kind}
            if pick is not None:
                taken.add(pick.kind)
                aim = [e for e in pick.entities if e in where] or list(pick.entities)
                cause.update(fault=pick.measured, fault_why=pick.why, aim=aim)
                rule = f"{d.rule}; measured {pick.kind}: {pick.why}"
            else:
                rule = d.rule
            out.append(dataclasses.replace(d, cause=cause, rule=rule))
        elif d.cls == "unknown" and d.kind == "mesher" and near:
            pick = near[0]
            taken.add(pick.kind)
            aim = [e for e in pick.entities if e in where]
            out.append(dataclasses.replace(
                d, cls=TOLERANCE_CLASS,
                rule=f"{d.code} on faces with a measured {pick.kind} ({pick.why}) -> {TOLERANCE_CLASS}",
                cause={**d.cause, "kind": pick.kind, "fault": pick.measured, "fault_why": pick.why, "aim": aim,
                       "was": d.rule}))
        elif d.cls == PERIODIC_CLASS:
            out.append(_periodic(d, where, faults, list(closed_faces)))
        else:
            out.append(d)
    for f in faults:
        if f.kind in taken or f.kind not in FAULT_CODE or (f.kind == "large_tolerance"
                                                            and "large_tolerance" not in soft_codes):
            continue
        out.append(Defect(id=f"T@{f.kind}", kind="validity", code=FAULT_CODE[f.kind], entities=f.entities,
                          measurement={"diagonal_mm": f.measured.get("diag_mm")} if f.measured.get("diag_mm") else {},
                          cls=TOLERANCE_CLASS, rule=f"detector: {f.why}",
                          cause={"kind": f.kind, "fault": f.measured, "fault_why": f.why, "aim": list(f.entities)}))
    return out


def measure_faults(pool: Any, ws: Any, *, header_unit: str | None = None) -> tuple[list[F.Fault], dict | None]:
    """(the faults on `ws`, the raw facts) - one `v5_brep_facts` call; ([], None) when the kernel gave no answer
    (a measurement that failed degrades the causes, never loses a defect)."""
    from meshpipeline.cad.repair_v4.sites import _call, _reverse

    facts = _call(pool, "v5_brep_facts", {"brep_path": ws.brep_path, "budget_s": FACTS_BUDGET_S})
    if facts is None:
        return [], None
    return F.faults_of(facts, _reverse(ws), header_unit=header_unit), facts


#: the detectors' share of one diagnosis (the kernel stops and says `truncated` past it)
FACTS_BUDGET_S = 30.0
_UNITS_CACHE: dict[str, str | None] = {}


def header_unit_of(step_path: str | None) -> str | None:
    """The source STEP's declared length unit (`brep_faults.step_length_unit`), read once per path."""
    if not step_path:
        return None
    if step_path not in _UNITS_CACHE:
        try:
            with open(step_path, encoding="utf-8", errors="replace") as fh:
                _UNITS_CACHE[step_path] = F.step_length_unit(fh.read())
        except OSError:
            _UNITS_CACHE[step_path] = None
    return _UNITS_CACHE[step_path]


# --- close_faces (v5 step 7): the whole-part scan and the facet-crossing predictor -----------------------------

#: the whole-part scan's pairs (stable ids) and what it reported, ONCE per file (owner: "scan the whole part once per
#: file"), keyed by the file (`source_step`, else the lineage's original)
_SCAN_CACHE: dict[str, tuple[list[dict], dict]] = {}
_PREDICT_CACHE: dict[tuple, dict | None] = {}
_CACHE_MAX = 512
#: CRS v5 r2: a site's `v5_pair_facts` answer, keyed by (original, the site's faces, reach), with the state it was
#: measured on, in a cache the CALLER owns (one per repair run: `diagnose(pair_cache=...)`). A later ROUND reuses it
#: when no op since touched (modified, generated from or removed) any of those faces or of the partners it found: the
#: region did not change, so neither did its close pairs.
PairCache = dict[tuple, tuple[Any, list[dict]]]


def _touched_since(ws_from: Any, ws_to: Any) -> set | None:
    """Every stable id an op between the two states touched; None when `ws_to` does not descend from `ws_from`."""
    from meshpipeline.cad.repair_v4.local_verify import _new_diffs

    try:
        diffs = _new_diffs(ws_from, ws_to)
    except (ValueError, AttributeError, TypeError):
        return None
    if not diffs and getattr(ws_from, "brep_path", None) != getattr(ws_to, "brep_path", None):
        return None                      # no recorded op between two different shapes: not one lineage
    out: set = set()
    for d in diffs:
        out |= set(d.modified or {}) | set(d.generated or {}) | set(d.removed or ())
    return out


def cached_pairs(cache: PairCache, ws: Any, key: tuple, faces: list) -> list[dict] | None:
    """The cached pairs for `key` when still valid on `ws` (see `PairCache`), else None."""
    got = cache.get(key)
    if got is None:
        return None
    on, pairs = got
    if on is ws:
        return pairs
    touched = _touched_since(on, ws)
    region = set(faces) | {p[k] for p in pairs for k in ("a", "b")}
    live = set(getattr(ws, "ids", None) or ())
    if touched is None or region & touched or (live and not region <= live):
        return None
    return pairs


def store_pairs(cache: PairCache, ws: Any, key: tuple, pairs: list[dict]) -> None:
    if len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = (ws, pairs)


def whole_part_pairs(pool: Any, ws: Any, *, h_mm: float, file_key: str | None) -> tuple[list[dict], dict]:
    """(close non-adjacent pairs of the whole part, translated to stable ids and live on `ws`, the scan's report).
    One `v5_close_scan` per file; later states reuse it (ids that died are dropped)."""
    from meshpipeline.cad.repair_v4.sites import _call, _reverse

    key = file_key or str(getattr(ws, "original", "") or ws.brep_path)
    if key not in _SCAN_CACHE:
        diag = float(getattr(ws, "part_diag_mm", 0.0) or 0.0)
        limit = min(GAP_REACH_H * h_mm, SCAN_GAP_OF_DIAG * diag) if diag > 0 else GAP_REACH_H * h_mm
        answer = _call(pool, "v5_close_scan", {"brep_path": ws.brep_path, "max_gap_mm": limit,
                                                "budget_s": SCAN_BUDGET_S, "max_pairs": SCAN_MAX_PAIRS})
        info = ({k: v for k, v in answer.items() if k != "pairs"} if answer is not None else {"failed": True})
        info["max_gap_mm"] = round(limit, 6)
        if len(_SCAN_CACHE) >= _CACHE_MAX:
            _SCAN_CACHE.pop(next(iter(_SCAN_CACHE)))
        _SCAN_CACHE[key] = (_translate_pairs((answer or {}).get("pairs", []), _reverse(ws)), info)
    pairs, info = _SCAN_CACHE[key]
    live = set(getattr(ws, "ids", None) or ())
    return [p for p in pairs if not live or (p["a"] in live and p["b"] in live)], info


def predicted(pool: Any, ws: Any, pairs: list[dict], *, h_mm: float) -> list[dict]:
    """`pairs` with `cross` (predicted crossings per member, `v5_facet_cross`) on the closest `MAX_PREDICTED` apart
    pairs within reach. A pair the predictor could not measure keeps no `cross` (the geometry decides it)."""
    from meshpipeline.cad.repair_v4.sites import _call

    reach = [p for p in pairs if p.get("gap_mm") is not None and CONTACT_MM < p["gap_mm"] <= GAP_REACH_H * h_mm
             and p["a"] in ws.indices and p["b"] in ws.indices]
    reach = sorted(reach, key=lambda p: p["gap_mm"])[:MAX_PREDICTED]
    todo = [p for p in reach if (ws.brep_path, p["a"], p["b"]) not in _PREDICT_CACHE]
    if todo:
        answer = _call(pool, "v5_facet_cross", {
            "brep_path": ws.brep_path, "pairs": [[[ws.indices[p["a"]]], [ws.indices[p["b"]]]] for p in todo],
            "budget_s": PREDICT_BUDGET_S})
        rows = (answer or {}).get("pairs") or []
        for i, p in enumerate(todo):
            row = rows[i] if i < len(rows) else None
            measured = {m: n for m, n in (row or {}).get("by_member", {}).items() if n is not None}
            if len(_PREDICT_CACHE) >= _CACHE_MAX:
                _PREDICT_CACHE.pop(next(iter(_PREDICT_CACHE)))
            _PREDICT_CACHE[(ws.brep_path, p["a"], p["b"])] = measured or None
    out = []
    for p in pairs:
        cross = _PREDICT_CACHE.get((ws.brep_path, p["a"], p["b"])) if p in reach else None
        out.append({**p, "cross": cross} if cross is not None else p)
    return out


def predicted_after(pool: Any, ws_before: Any, ws_after: Any, defect: Defect) -> dict[str, int] | None:
    """Predicted crossings per member on `ws_after` between what each of the defect's close pairs became (each face
    carried through the repair's history: a split face is the group of its pieces). None when nothing could be
    measured. The close_faces class's FAST LOCAL CHECK."""
    from meshpipeline.cad.repair_v4.local_verify import _forward, _new_diffs
    from meshpipeline.cad.repair_v4.sites import _call

    cause = defect.cause or {}
    pairs = cause.get("face_pairs") or []
    if not pairs:
        return None
    diffs = _new_diffs(ws_before, ws_after)
    groups = []
    for a, b in pairs[:MAX_PREDICTED]:
        ga = sorted(ws_after.indices[s] for s in _forward([a], diffs) if s in ws_after.indices)
        gb = sorted(ws_after.indices[s] for s in _forward([b], diffs) if s in ws_after.indices)
        if ga and gb:
            groups.append([ga, gb])
    if not groups:
        return None
    answer = _call(pool, "v5_facet_cross", {"brep_path": ws_after.brep_path, "pairs": groups,
                                            "budget_s": PREDICT_BUDGET_S})
    rows = (answer or {}).get("pairs") or []
    if not rows:
        return None
    members = sorted({m for r in rows for m, n in r.get("by_member", {}).items() if n is not None})
    if not members:
        return None
    return {m: sum(int(r["by_member"].get(m) or 0) for r in rows) for m in members}


def diagnose(pool: Any, ws: Any, verdict: Any, sites: list[Site], defects: list[Defect], *, h_mm: float,
             source_step: str | None = None, report: Any = None, pair_cache: PairCache | None = None) -> list[Defect]:
    """`defects` (the loop's, on `ws`, from `verdict` and its `sites`) with `cls`, `cause`, `rule` and `evidence`
    filled, in repair order. One `v5_pair_facts` call per site, shared by its defects. A site's extra measurement
    failing degrades its cause (pairs = []), never loses the defect. Then ONE `v5_brep_facts` call puts the
    invalid_brep_tolerance detectors' faults on the defects (`attach_faults`); the units verdict is handed to
    `report(units=...)` whatever it is (the owner: report it either way)."""
    from meshpipeline.cad.repair_v4.sites import _call, _reverse

    by_id = {s.id: s for s in sites}
    reverse = _reverse(ws)
    min_sicn = {m.name: getattr(m, "min_sicn", None) for m in getattr(verdict, "panel", []) or []
                if getattr(m, "status", "pass") != "pass"}
    # layer-1 names (S/F/E kernel indices) -> stable ids, by the ONE translator the defects were built with
    hard_all = [{"code": f.code, "entities": Q.layer1_ids(ws, list(f.entities))[0], "detail": dict(f.detail)}
                for f in verdict.valid.hard]
    pairs_of: dict[str, list[dict]] = {}
    scanned: dict | None = None
    site_faces: dict[str, list[str]] = {}
    out: list[Defect] = []
    for d in defects:
        site = _site_of(d, by_id)
        if site is None:
            out.append(dataclasses.replace(d, cls="unknown", rule=f"no site for defect {d.id}"))
            continue
        faces = [e for e in site.entities if e[:1] == "F" and e in ws.indices]
        site_faces[d.id] = faces
        if site.id not in pairs_of:
            lineage = str(getattr(ws, "original", "") or "")
            ckey = (lineage, tuple(sorted(faces)), round(GAP_REACH_H * h_mm, 9))
            cacheable = pair_cache is not None and bool(faces and lineage)
            hit = cached_pairs(pair_cache, ws, ckey, faces) if cacheable else None  # type: ignore[arg-type]
            if hit is not None:
                pairs_of[site.id] = hit
            else:
                answer = _call(pool, "v5_pair_facts", {"brep_path": ws.brep_path, "max_gap_mm": GAP_REACH_H * h_mm,
                                                        "faces": [ws.indices[f] for f in faces]}) if faces else None
                pairs_of[site.id] = _translate_pairs((answer or {}).get("pairs", []), reverse)
                if answer is not None and cacheable:
                    store_pairs(pair_cache, ws, ckey, pairs_of[site.id])  # type: ignore[arg-type]
        pairs = pairs_of[site.id]
        if d.kind == "mesher" and d.code in CLOSE_KINDS and not faces:
            # step 7: a failure that names no face is given the whole part's close pairs (one scan per file)
            pairs, scanned = whole_part_pairs(pool, ws, h_mm=h_mm, file_key=source_step)
        if d.kind == "mesher" and d.code in CLOSE_KINDS and pairs:
            pairs = predicted(pool, ws, pairs, h_mm=h_mm)
        mine = set(site.entities) | set(d.entities)
        hard = [h for h in hard_all if d.kind == "validity" and h["code"] == d.code
                and (mine & set(h["entities"]) or not h["entities"])]
        view = _own_view(site, d)
        cls, rule, cause = classify_site(view, pairs=pairs, h_mm=h_mm, hard=hard, min_sicn=min_sicn)
        if cls == "close_faces" and not faces:
            cause = {**cause, "located_by": "whole_part_scan"}
        out.append(dataclasses.replace(d, cls=cls, cause=cause, rule=rule,
                                       evidence=tuple(dict(f) for f in view.graph["failures"])))
    # round 2: a crossing close_faces did not confirm, measured where it is - within a face, across a shared edge, at
    # touching faces (`facet_local`: each gmsh member's own 2D mesh of the site's faces, the whole part if none)
    out = FL.attach_local(pool, ws, out, site_faces, h_mm=h_mm)
    shells = [d for d in out if d.cls == S.CLASS]
    if shells:
        # v5 step 11: ONE shell measurement puts the open_shell_volume cause kind (gap / hole / sheet / ...) on them
        faces = sorted({ws.indices[e] for d in shells for e in d.entities if e[:1] == "F" and e in ws.indices})
        out = S.attach_shell_causes(out, S.measure_shells(pool, ws, faces), reverse)  # type: ignore[arg-type]
    unit = header_unit_of(source_step)
    faults, facts = measure_faults(pool, ws, header_unit=unit)
    soft = frozenset(str(getattr(f, "code", "")) for f in getattr(verdict.valid, "soft", ()) or ())
    out = attach_faults(out, faults, by_id, soft_codes=soft, closed_faces=F.closed_faces_of(facts, reverse))
    # v5 step 10: the low_quality detectors - only when a low_quality defect or a soft small-entity finding is there
    small_soft = {sid: f.code for f in getattr(verdict.valid, "soft", ()) or () if f.code in QL.SOFT_CODES
                  for sid in Q.layer1_ids(ws, list(f.entities))[0]}
    quality: dict | None = None
    if small_soft or any(d.cls == QL.LOW_QUALITY for d in out):
        qfacts = QL.measure(pool, ws, h_mm=h_mm)
        qfaults = QL.faults_of(qfacts, reverse, h_mm=h_mm) if qfacts else []
        out = QL.attach_quality(out, qfaults, by_id, soft=small_soft, site_of=_site_of,
                                diagonal_mm=getattr(ws, "part_diag_mm", None))
        quality = {"measured": qfacts is not None, "e_mm": QL.e_of(h_mm),
                   "counts": (qfacts or {}).get("counts"), "truncated": bool((qfacts or {}).get("truncated")),
                   "faults": [{"kind": f.kind, "entity": f.entity, "why": f.why} for f in qfaults[:8]]}
    # round 2 fix E (`narrow.py`): a low_quality MESHER defect is re-aimed at what lies at the member's WORST
    # elements (a small edge/face, a sliver, a narrow region), one `v5_narrow_facts` call per defect
    regions: dict[str, tuple[list, Any]] = {}
    for d in [d for d in out if d.cls == QL.LOW_QUALITY and d.kind == "mesher"
              and d.code in N.QUALITY_KINDS][:N.MAX_DEFECTS]:
        points = N.worst_points(_site_of(d, by_id), d.member)
        if points:
            regions[d.id] = (points, N.region_of(N.measure(pool, ws, points, h_mm=h_mm), reverse, h_mm=h_mm))
    if regions:
        out = N.attach_regions(out, regions)
    if report is not None:
        report(units=F.units_verdict(unit, (facts or {}).get("diag_mm"), (facts or {}).get("max_tolerance")),
               faults=[{"kind": f.kind, "entities": list(f.entities)[:8], "why": f.why} for f in faults],
               facts_truncated=bool((facts or {}).get("truncated")), facts_measured=facts is not None,
               **({"close_scan": scanned} if scanned is not None else {}), quality=quality)
    # v5 step 8: the overlap_self_intersection detectors (duplicate body, the self-intersection's faces)
    from meshpipeline.cad.repair_v5 import overlap as O

    out = O.attach_overlap_facts(out, O.measure_overlap(pool, ws, out), reverse)
    # v5 step 9: a surface_crossing defect's KIND (duplicate faces, true crossing, near-coincident) from one
    # measurement of the faces at it (`crossing.measure_crossings`); the kind picks the rule seed and aims the menu
    return order(X.measure_crossings(pool, ws, out))
