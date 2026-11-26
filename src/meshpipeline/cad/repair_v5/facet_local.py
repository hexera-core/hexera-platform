# Responsibility: v5's FACET_CROSSING_LOCAL class (round 2, owner-approved 2026-11-25): a mesher's facets crossing
#                 WITHIN ONE FACE, between ADJACENT faces (a shared edge) or between TOUCHING faces (gap ~ 0, no
#                 shared edge) - located and explained by the gmsh-faithful predictor, and the rule seed its cause picks.
# Owns: `CLASS`, `KIND_REPAIRS` (cause kind -> rule seed + menu), `self_crossings` (the pure triangle sweep the child
#       runs), `relation_of`, `cause_kind`, `local_cause`, `classify_local` (pure: which mesher defects are this
#       class), `measure` / `attach_local` (the driver diagnose calls) and `predicted_after` (the fast local check).
# Boundaries: the 2D meshes are facet_local_gmsh.py's (a child process per member, `mesh_child`'s profiles, never
#             tuned); the per-face/per-edge measurement is facet_local_kernel.py's `v5_local_cross`. close_faces
#             keeps every crossing between faces actually APART; this class takes what close_faces cannot explain.
"""Facets crossing where no gap explains it.

Step 7 measured gmsh's own whole-part surface meshes on 22 overlapping_facets dev files: only 12 had a crossing
between faces that are actually apart (the close_faces class). The rest cross WITHIN one face (a coarse curved or
badly parameterised face folding its own triangles), between ADJACENT faces (a knife edge, or a near-tangent shared
edge whose two sides' facets sag through each other) or between TOUCHING faces (in contact with no shared edge).

DETECTOR: the faces at the defect (the whole part when the failure names none) meshed in 2D by each gmsh member's
own settings, every pair of triangles that intersect without sharing a node counted per (face, face), each pair given
its RELATION (same face / adjacent / touching / apart) and each face its CAUSE measurement (surface type, closed,
curvature radius over the element size, angular span, B-spline continuity breaks, parameter-speed ratio, whether the
spline is really analytic, sliver width) and each adjacent pair its shared edge's angle.

RULE SEED BY CAUSE (`KIND_REPAIRS`, menus = the others + defeature): split the face (split_angle on a curved/closed
face, split_continuity at a C1 break), reapproximate a badly parameterised surface, to_canonical a spline that is really
analytic, unify_same_domain across a near-tangent shared edge, imprint/unify touching faces. Parameters from the
measurement (`op_params`).

LOCAL CHECK: the predictor again on what the touched faces became (fast); none crossing at the failing member -> the
failing mesher on the touched bodies (`local_verify`).
"""

from __future__ import annotations

import bisect
import dataclasses
import math
from collections.abc import Sequence
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v5 import close_faces as CF

CLASS = "facet_crossing_local"
RELATIONS = ("same_face", "adjacent", "touching", "apart")
#: the mesher kinds this class can explain (a crossing, a boundary Netgen cannot recover, a crash on them)
KINDS_EXPLAINED = frozenset({"overlapping_facets", "edge_through_face", "other", "gmsh_crash", "netgen_crash",
                             "netgen_meshing_failed"})

# --- the cause thresholds (each a measured property against the element size h, or a plain geometric limit) -------
#: a face whose curvature turns more than this many degrees over one element is too coarse for its own facets
CURVED_DEG_PER_H = 30.0
#: shared-edge angle (between the two faces' normals at the edge): under this the faces continue smoothly
NEAR_TANGENT_EDGE_DEG = 10.0
#: over this the faces fold back onto each other (a knife edge)
KNIFE_EDGE_DEG = 150.0
#: a face narrower than this share of h - and either planar or this many times longer than wide - is a sliver its
#: own and its neighbours' facets overrun (a narrow curved face that is not long is a small hole or fillet: curved)
SLIVER_OF_H = 0.25
SLIVER_ASPECT = 10.0
#: parameter speed max/min over the face: over this the (u, v) grid is badly distributed over the surface
BAD_PARAM_RATIO = 20.0
#: split_angle's schema window and the ladder the tournament steps along
SPLIT_DEG_MIN, SPLIT_DEG_MAX = 30.0, 90.0
#: split_turn (fix G): each piece's facet sag R(1 - cos(theta/2)) stays under 1/SAG_DIVISOR of the local feature size
#: (the face's radius, or its width when narrower); at f = R that is ~29 deg - on f3819472cf82 the 2.54 mm pipes at
#: h 40.9 mm still crossed in the predictor split at 60 deg and not at 30. Never under TURN_DEG_MIN.
SAG_DIVISOR = 32.0
TURN_DEG_MIN, TURN_DEG_MAX = 2.0, 90.0
#: the curved faces of the local crossing pairs split_turn is aimed at (the most crossed first)
MAX_TURN_FACES = 24
#: the fidelity bound (= crossing.tolerance_mm): what to_canonical / reapproximate may move a surface by
FIDELITY_MAX_MM, FIDELITY_OF_DIAG = 0.1, 1e-3

#: THE ROWS: cause kind -> (rule seed, the rest of the menu). The loop keys them `facet_crossing_local:<kind>`.
#: Fix G (round 2): `split_turn` (the fine angular split, any surface, any angle - `ops/local_facets.py`) leads every
#: curvature row and `cut_knife` the knife row; on fix B's targets the old seeds' ops could not do it (split_angle
#: floors at 30 deg and divides only surfaces of revolution; unify/defeature found nothing to change).
KIND_REPAIRS: dict[str, tuple[tuple[str, ...], ...]] = {
    "curved_face": (("split_turn",), ("split_angle",), ("split_closed",), ("reapproximate",), ("defeature",)),
    "closed_face": (("split_turn",), ("split_closed",), ("split_angle",), ("reapproximate",), ("defeature",)),
    "c1_break": (("split_continuity",), ("split_turn",), ("split_angle",), ("reapproximate",), ("defeature",)),
    "bad_param": (("reapproximate",), ("to_canonical",), ("split_turn",), ("split_angle",), ("defeature",)),
    "spline_analytic": (("to_canonical",), ("reapproximate",), ("split_turn",), ("split_angle",), ("defeature",)),
    "near_tangent_edge": (("split_turn",), ("unify_same_domain",), ("split_angle",), ("reapproximate",),
                          ("defeature",)),
    "knife_edge": (("cut_knife",), ("split_turn",), ("defeature",), ("unify_same_domain",), ("reapproximate",)),
    "sliver": (("defeature",), ("unify_same_domain",), ("split_angle",)),
    "touching": (("imprint_crossing",), ("defeature",), ("unify_same_domain",), ("split_angle",),
                 ("reapproximate",)),
}
#: the general row (a crossing no measured property explains)
GENERAL_ROW: tuple[tuple[str, ...], ...] = (("split_turn",), ("split_angle",), ("reapproximate",),
                                            ("unify_same_domain",), ("defeature",))
#: when the two faces of a pair show different properties, the most specific cause is repaired first
FACE_KIND_ORDER = ("sliver", "spline_analytic", "bad_param", "c1_break", "closed_face", "curved_face")
#: the kinds whose repair is a property of ONE face (the aim is that face), else both faces of the pair
ONE_FACE_KINDS = frozenset({"curved_face", "closed_face", "c1_break", "bad_param", "spline_analytic", "sliver"})
#: at most this many crossing pairs ride on a cause; at most this many faces are aimed at
MAX_CAUSE_PAIRS = 6
MAX_AIM_FACES = 8


# --- the child's pure half: every crossing triangle pair of one mesh, by owner face ------------------------------

Vec = tuple[float, float, float]
Tri = tuple[Vec, Vec, Vec]


def _box(t: Tri) -> tuple[float, float, float, float, float, float]:
    xs, ys, zs = zip(*t, strict=True)
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


def _unit_normal(t: Tri) -> Vec | None:
    n = CF._cross(CF._sub(t[1], t[0]), CF._sub(t[2], t[0]))
    length = CF._dot(n, n) ** 0.5
    return None if length <= 0 else (n[0] / length, n[1] / length, n[2] / length)


def _shrunk(t: Tri, by: float) -> Tri:
    c = tuple(sum(p[k] for p in t) / 3.0 for k in range(3))
    return tuple(tuple(c[k] + (p[k] - c[k]) * (1.0 - by) for k in range(3)) for p in t)  # type: ignore[return-value]


#: two triangles sharing mesh nodes OVERLAP (gmsh's "overlapping facets") when they are nearly coplanar - within
#: this angle - and lie on the same side of what they share (a fold), or repeat the same three nodes
FOLD_DEG = 3.0
_FOLD_COS = math.cos(math.radians(FOLD_DEG))
#: a node-sharing pair is tested shrunk by this share about each centroid (touching at the shared node stops counting)
SHRINK = 1e-3


def overlap_sharing(p: Tri, q: Tri, np_: tuple[int, ...], nq: tuple[int, ...]) -> bool:
    """Do two triangles that SHARE mesh nodes overlap beyond what they share? The same three nodes (a duplicated
    facet), or nearly coplanar (`FOLD_DEG`) and folded onto each other. Pure."""
    shared = set(np_) & set(nq)
    if len(shared) == 3:
        return True
    a, b = _unit_normal(p), _unit_normal(q)
    if a is None or b is None or abs(CF._dot(a, b)) < _FOLD_COS:
        return False
    if len(shared) == 2:
        i, j = (k for k in range(3) if np_[k] in shared)
        r = next(p[k] for k in range(3) if np_[k] not in shared)
        s = next(q[k] for k in range(3) if nq[k] not in shared)
        e = CF._sub(p[j], p[i])
        return CF._dot(CF._cross(e, CF._sub(r, p[i])), CF._cross(e, CF._sub(s, p[i]))) > 0.0
    return CF.tri_tri_intersect(_shrunk(p, SHRINK), _shrunk(q, SHRINK))


def self_crossings(tris: Sequence[Tri], nodes: Sequence[tuple[int, int, int]], owner: Sequence[int], *,
                   focus: set[int] | None = None, cap: int = 20_000) -> tuple[dict[tuple[int, int], int],
                                                                             dict[tuple[int, int], Vec]]:
    """({(owner i, owner j) with i <= j: crossing triangle pairs}, {same key: one crossing's centroid}) over ONE mesh:
    every pair of triangles that intersect without sharing a mesh node, or that share nodes and still overlap
    (`overlap_sharing`: a duplicated or folded facet), at least one of them on a `focus` owner (None = all).
    Sweep-and-prune on x; stops after `cap` crossings."""
    boxes = [_box(t) for t in tris]
    order = sorted(range(len(tris)), key=lambda k: boxes[k][0])
    starts = [boxes[k][0] for k in order]
    counts: dict[tuple[int, int], int] = {}
    where: dict[tuple[int, int], Vec] = {}
    found = 0
    for pos, i in enumerate(order):
        bi = boxes[i]
        hi = bisect.bisect_right(starts, bi[3])
        ni = set(nodes[i])
        for k in order[pos + 1:hi]:
            if focus is not None and owner[i] not in focus and owner[k] not in focus:
                continue
            bk = boxes[k]
            if bk[1] > bi[4] or bk[4] < bi[1] or bk[2] > bi[5] or bk[5] < bi[2]:
                continue
            if ni.intersection(nodes[k]):
                if not overlap_sharing(tris[i], tris[k], nodes[i], nodes[k]):
                    continue
            elif not CF.tri_tri_intersect(tris[i], tris[k]):
                continue
            key = (min(owner[i], owner[k]), max(owner[i], owner[k]))
            counts[key] = counts.get(key, 0) + 1
            if key not in where:
                where[key] = tuple(sum(p[c] for p in tris[i]) / 3.0 for c in range(3))  # type: ignore[assignment]
            found += 1
            if found >= cap:
                return counts, where
    return counts, where


# --- the cause: relation and kind -------------------------------------------------------------------------------


def relation_of(a: int, b: int, *, shared_edge: bool, shared_vertex: bool, gap_mm: float | None,
                tol_mm: float) -> str:
    """same_face / adjacent (a shared edge) / touching (a shared vertex only, or within `tol_mm`) / apart."""
    if a == b:
        return "same_face"
    if shared_edge:
        return "adjacent"
    if shared_vertex or (gap_mm is not None and gap_mm <= tol_mm):
        return "touching"
    return "apart"


def face_kind(facts: dict, h_mm: float) -> str | None:
    """The property of ONE face that explains its facets crossing, in repair order, or None. Pure."""
    if not facts:
        return None
    width = facts.get("width_mm")
    if (width is not None and h_mm > 0 and width < SLIVER_OF_H * h_mm
            and (facts.get("type") == "Plane" or (facts.get("aspect") or 0.0) >= SLIVER_ASPECT)):
        return "sliver"
    if facts.get("canonical_gap_mm") is not None:
        return "spline_analytic"
    ratio = facts.get("param_speed_ratio")
    if ratio is not None and ratio > BAD_PARAM_RATIO:
        return "bad_param"
    if facts.get("c1_breaks"):
        return "c1_break"
    if facts.get("closed"):
        return "closed_face"
    if facts.get("deg_per_h") is not None and facts["deg_per_h"] >= CURVED_DEG_PER_H:
        return "curved_face"
    return None


def cause_kind(pair: dict, faces: dict[Any, dict], h_mm: float) -> tuple[str | None, list]:
    """(kind, the faces it is about) for one measured crossing pair. Pure. `pair` carries `a`, `b`, `relation` and
    (adjacent) `edge_angle_deg`; `faces` the per-face facts by the same ids."""
    rel = pair.get("relation")
    a, b = pair["a"], pair["b"]
    if rel == "touching":
        return "touching", [a, b]
    own = [(f, face_kind(faces.get(f) or {}, h_mm)) for f in dict.fromkeys((a, b))]
    order = list(FACE_KIND_ORDER)
    if rel == "adjacent" and pair.get("edge_angle_deg") is not None:
        angle = float(pair["edge_angle_deg"])
        if angle >= KNIFE_EDGE_DEG:
            return "knife_edge", [a, b]
        if angle <= NEAR_TANGENT_EDGE_DEG and not any(k in ("sliver", "spline_analytic", "bad_param") for _, k in own):
            return "near_tangent_edge", [a, b]
    named = sorted(((f, k) for f, k in own if k), key=lambda fk: order.index(fk[1]))
    if named:
        kind = named[0][1]
        return kind, [f for f, k in named if k == kind]
    return None, [a, b]


def split_deg(facts: dict) -> float:
    """split_angle's max_deg from the face's measurement: half its angular span (at least two pieces), at most
    the curvature over one element, inside the op's window."""
    span = facts.get("span_deg")
    per_h = facts.get("deg_per_h")
    want = min(v for v in (0.5 * span if span else None, per_h, SPLIT_DEG_MAX) if v)
    return float(max(SPLIT_DEG_MIN, min(SPLIT_DEG_MAX, round(want / 15.0) * 15.0)))


def turn_deg(facts: dict) -> float | None:
    """split_turn's max_deg from ONE face's measurement: the angle whose chord sags 1/SAG_DIVISOR of the local
    feature size f = min(radius, width) - theta = 2 acos(1 - f / (SAG_DIVISOR R)) - and at most half the face's own
    angular span (two pieces at least), inside [TURN_DEG_MIN, TURN_DEG_MAX]. None for a face with no radius."""
    radius = facts.get("min_radius_mm")
    if not radius or radius <= 0:
        return None
    feature = min(float(radius), float(facts.get("width_mm") or radius))
    theta = 2.0 * math.degrees(math.acos(max(-1.0, 1.0 - feature / (SAG_DIVISOR * float(radius)))))
    if facts.get("span_deg"):
        theta = min(theta, 0.5 * float(facts["span_deg"]))
    return snap_turn(max(TURN_DEG_MIN, min(TURN_DEG_MAX, theta)))


def snap_turn(deg: float) -> float:
    """`deg` rounded DOWN to 90/n: the quarter, half and full faces of one revolved surface then split on common
    angles. Measured on f3819472cf82 (2.54 mm pipes at h 40.9 mm): at 30 and 22.5 deg the predictor saw no crossing,
    at 28.7 (half-cylinders in 7 pieces, quarters in 4) 20 crossings at the pipe joints where the pieces disagree."""
    return round(90.0 / math.ceil(90.0 / float(deg) - 1e-9), 3)


def turn_faces(rows: list[dict], faces: dict[Any, dict]) -> list:
    """The curved faces (a measured radius) among the local crossing `rows`, the most crossed first."""
    crossed: dict[Any, int] = {}
    for r in rows:
        for f in dict.fromkeys((r["a"], r["b"])):
            crossed[f] = crossed.get(f, 0) + _member_count(r, None)
    return [f for f in sorted(crossed, key=lambda f: -crossed[f]) if turn_deg(faces.get(f) or {}) is not None
            ][:MAX_TURN_FACES]


def fidelity_mm(diag_mm: float | None) -> float:
    return min(FIDELITY_MAX_MM, FIDELITY_OF_DIAG * float(diag_mm)) if diag_mm else FIDELITY_MAX_MM



def op_params(kind: str | None, aim_facts: list[dict], *, diag_mm: float | None) -> dict[str, dict]:
    """The parameters the cause gives the menu's ops (merged over seq's defaults by `params_for`)."""
    out: dict[str, dict] = {}
    curved = [f for f in aim_facts if f.get("span_deg") or f.get("deg_per_h")]
    if curved:
        out["split_angle"] = {"max_deg": min(split_deg(f) for f in curved)}
    fid = fidelity_mm(diag_mm)
    gaps = [float(f["canonical_gap_mm"]) for f in aim_facts if f.get("canonical_gap_mm") is not None]
    if gaps:
        out["to_canonical"] = {"tol_mm": float(min(fid, max(1e-6, 1.5 * max(gaps))))}
    degrees = [int(f["degree"]) for f in aim_facts if f.get("degree")]
    out["reapproximate"] = {"tol_mm": float(min(fid, 0.025)),
                            "max_degree": int(min(9, max(3, max(degrees, default=9))))}
    if kind == "c1_break":
        out["split_continuity"] = {"order": 1 if any(f.get("c0_breaks") for f in aim_facts) else 2}
    return out


def _member_count(pair: dict, member: str | None) -> int:
    by = pair.get("by_member") or {}
    return int(by.get(member) or 0) if member else sum(int(v or 0) for v in by.values())


def local_cause(rows: list[dict], faces: dict[Any, dict], *, h_mm: float, member: str | None,
                diag_mm: float | None) -> dict | None:
    """The cause of a defect from the measured crossing `rows` (stable ids): the pair crossing most at the failing
    `member` (any member when it has none), its relation, kind, aim and parameters. None when no row is local
    (same face / adjacent / touching) - an `apart` crossing is close_faces'. Pure."""
    local = [r for r in rows if r.get("relation") in ("same_face", "adjacent", "touching") and _member_count(r, None)]
    if not local:
        return None
    at = [r for r in local if _member_count(r, member)] or local
    best = max(at, key=lambda r: (_member_count(r, member), _member_count(r, None)))
    kind, about = cause_kind(best, faces, h_mm)
    aim = list(dict.fromkeys([*about, *(f for r in at for f in (r["a"], r["b"]))]))[:MAX_AIM_FACES]
    if kind in ONE_FACE_KINDS:
        aim = list(about)
    params = op_params(kind, [faces.get(f) or {} for f in aim], diag_mm=diag_mm)
    # fix G: split_turn on EVERY curved face of the local pairs (f3819472cf82: with only the pair's own two faces
    # split, the crossings moved to the next pipe joint), at the finest angle any of them needs
    turning = turn_faces(local, faces)
    if turning:
        params["split_turn"] = {"max_deg": min(float(turn_deg(faces[f]) or TURN_DEG_MAX) for f in turning)}
    if kind == "knife_edge":
        # fix G: cut the wedge where it reaches the fidelity bound in thickness, no farther than half an element
        params["cut_knife"] = {"width_mm": fidelity_mm(diag_mm), "max_reach_mm": round(0.5 * h_mm, 6) or 1.0}
    members = sorted({m for r in local for m, n in (r.get("by_member") or {}).items() if n})
    by_member = {m: sum(int((r.get("by_member") or {}).get(m) or 0) for r in local) for m in members}
    return {
        "kind": kind or "unexplained", "relation": best["relation"], "aim": aim,
        "pair": [best["a"], best["b"]], "edge_angle_deg": best.get("edge_angle_deg"),
        "faces": {str(f): faces.get(f) for f in dict.fromkeys((best["a"], best["b"])) if faces.get(f)},
        "local_pairs": [{k: r.get(k) for k in ("a", "b", "relation", "by_member", "edge_angle_deg", "gap_mm", "point")}
                        for r in sorted(local, key=lambda r: -_member_count(r, None))[:MAX_CAUSE_PAIRS]],
        "relations": {rel: sum(1 for r in local if r["relation"] == rel) for rel in RELATIONS
                      if any(r["relation"] == rel for r in local)},
        "predicted_before": by_member, "watch": sorted({f for r in local for f in (r["a"], r["b"])})[:2 * MAX_AIM_FACES],
        "h_mm": h_mm, "op_params": params, "turn_faces": turning,
    }


def takes(defect: Defect) -> bool:
    """Is `defect` one this class may take? A MESHER failure of a kind it explains that close_faces did not
    confirm (a close_faces defect whose pairs the predictor never saw crossing is the geometry's guess, not a
    measured crossing between faces apart)."""
    if defect.kind != "mesher" or defect.code not in KINDS_EXPLAINED:
        return False
    if defect.cls == "close_faces":
        # only UNCONFIRMED: a measured crossing at a gap stays close_faces' even when the gap is a hair (measured on
        # 3ca940d41ed3, 0.006 mm at h 65 mm: close_faces' defeature on the site won, this class's rows did not)
        return not (defect.cause or {}).get("crossing_pairs")
    return defect.cls in (None, "unknown", "surface_crossing", "checker_failed")


def classify_local(defect: Defect, cause: dict | None) -> Defect:
    """`defect` as this class when `cause` (`local_cause`) found a local crossing; unchanged otherwise. Pure."""
    if cause is None or not takes(defect):
        return defect
    rel, kind = cause["relation"], cause["kind"]
    a, b = cause["pair"]
    where = f"within {a}" if rel == "same_face" else f"{a}-{b} ({rel})"
    counts = ", ".join(f"{m} {n}" for m, n in (cause.get("predicted_before") or {}).items())
    return dataclasses.replace(
        defect, cls=CLASS,
        rule=f"{defect.code}: facets cross {where}, cause {kind} [{counts}]"
             + (f" (was {defect.cls}: {defect.rule})" if defect.cls else ""),
        cause={**cause, "was": defect.cls})


# --- the drivers (pool calls; no OCCT in this module) -------------------------------------------------------------

#: the predictor's share of one call (the pool's kernel call times out at 120 s)
PREDICT_BUDGET_S = 90.0
#: a site naming more faces than this is measured on these many (its first), a whole-part scan on at most this many
MAX_SITE_FACES = 24
MAX_WHOLE_FACES = 1500
_CACHE: dict[tuple, dict | None] = {}
_CACHE_MAX = 256


def _faces_for(defect: Defect, site_faces: list[str]) -> list[str]:
    named = [e for e in defect.entities if str(e)[:1] == "F"]
    return list(dict.fromkeys([*named, *site_faces]))[:MAX_SITE_FACES]


def _call_local(pool: Any, ws: Any, faces: list[int], *, h_mm: float) -> dict | None:
    from meshpipeline.cad.repair_v4.sites import _call

    key = (ws.brep_path, tuple(sorted(faces)))
    if key not in _CACHE:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = _call(pool, "v5_local_cross", {
            "brep_path": ws.brep_path, "faces": faces, "h_mm": h_mm, "budget_s": PREDICT_BUDGET_S,
            "max_faces": MAX_WHOLE_FACES, "tol_mm": fidelity_mm(getattr(ws, "part_diag_mm", None)),
            "fit_tol_mm": fidelity_mm(getattr(ws, "part_diag_mm", None))})
    return _CACHE[key]


def translate(answer: dict | None, reverse: dict) -> tuple[list[dict], dict]:
    """Kernel rows/faces -> stable ids; a pair whose face has no live id is dropped."""
    if not answer:
        return [], {}
    faces = {}
    for k, v in (answer.get("faces") or {}).items():
        sid = reverse.get(("F", int(k)))
        if sid is not None:
            faces[sid] = v
    rows = []
    for p in answer.get("pairs") or []:
        a, b = reverse.get(("F", int(p["a"]))), reverse.get(("F", int(p["b"])))
        if a is None or b is None:
            continue
        rows.append({**p, "a": a, "b": b})
    return rows, faces


def attach_local(pool: Any, ws: Any, defects: list[Defect], site_faces: dict[str, list[str]], *, h_mm: float
                 ) -> list[Defect]:
    """`defects` with every mesher defect this class may take measured (`v5_local_cross` on its site's faces, the
    whole part when it names none) and reclassified where a local crossing explains it. A measurement that failed
    leaves the defect as diagnosed."""
    from meshpipeline.cad.repair_v4.sites import _reverse

    reverse = None
    out: list[Defect] = []
    for d in defects:
        if not takes(d):
            out.append(d)
            continue
        reverse = reverse if reverse is not None else _reverse(ws)
        faces = [f for f in _faces_for(d, site_faces.get(d.id) or []) if f in ws.indices]
        answer = _call_local(pool, ws, [ws.indices[f] for f in faces], h_mm=h_mm)
        rows, facts = translate(answer, reverse)
        cause = local_cause(rows, facts, h_mm=h_mm, member=d.member, diag_mm=getattr(ws, "part_diag_mm", None))
        if cause is not None:
            cause["located_by"] = "site faces" if faces else "whole-part 2D mesh"
        out.append(classify_local(d, cause))
    return out


def predicted_after(pool: Any, ws_before: Any, ws_after: Any, defect: Defect) -> dict[str, int] | None:
    """Local crossings per member on `ws_after` among what the defect's crossing faces became (carried through the
    repair's history), with their first ring. None when nothing could be measured. The class's FAST LOCAL CHECK."""
    from meshpipeline.cad.repair_v4.local_verify import _forward, _new_diffs

    watch = list((defect.cause or {}).get("watch") or (defect.cause or {}).get("aim") or [])
    if not watch:
        return None
    carried = sorted(ws_after.indices[s] for s in _forward(watch, _new_diffs(ws_before, ws_after))
                     if s in ws_after.indices and s in set(ws_after.ids))
    if not carried:
        return {}
    answer = _call_local(pool, ws_after, carried[:4 * MAX_SITE_FACES], h_mm=float((defect.cause or {}).get("h_mm")
                                                                                   or 0.0))
    if answer is None or not answer.get("members"):
        return None
    rows = [p for p in answer.get("pairs") or [] if p.get("relation") in ("same_face", "adjacent", "touching")]
    return {m: sum(int((r.get("by_member") or {}).get(m) or 0) for r in rows) for m in answer["members"]}


def faces_of_site(site: Any, ws: Any) -> list[str]:
    return [e for e in getattr(site, "entities", ()) or () if str(e)[:1] == "F" and e in ws.indices]
