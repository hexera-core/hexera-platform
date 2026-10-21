# Responsibility: Compute the FEA meshing plan from a part's measurements: the element type, the base size for the chosen detail level, the zones (walls, hot spots, capped corners, rounds) with the size each one needs by rule, the element estimate, and whether it fits the level - with the options when it does not.
# Boundaries: pure over plain data (the measurement record, the volume histogram, the level); no engine, no file, no model. Every number names the rule it comes from (docs/RULES.md PLAN-fea-n).
# Collaborates with: cad/fea_measure.py (the record), engines/fea_size_field.py (the histogram), engines/fea_driver.py (runs it and records it), engines/gmsh/fea_adapter.py (meshes it).
"""THE FEA PLAN (fea_plan.json), engine-neutral.

    {"version": 1, "rules_version": "3",
     "detail": {"key": "standard", "label": "Standard", "target": 500000, "ceiling": 625000},
     "element": {"family": "tetrahedra", "order": 2, "solid_only": true},
     "base_size_m": .., "coarsest_base_m": .., "growth": 1.3, "per_h3": 4.5,
     "zones": [{"id": "W3", "kind": "wall" | "hole" | "fillet" | "notch" | "corner" | "round",
                "faces": ["Face_3"], "size_m", "reach_m", "rule": "PLAN-fea-3", "why": "...",
                "spot": "H1", "capped": bool, "thickness_m" | "radius_m"}],
     "histogram": [[size_m, volume_m3], ...],
     "estimate": {"elements": .., "zones_at_coarsest": ..},
     "fits": true, "options": [], "levers": [], "words": ["..."], "decisions": [...]}

Lengths are metres (the unit gmsh and the deck work in); the measurement record is millimetres.

THE RULES (docs/RULES.md):
  PLAN-fea-1  quadratic tetrahedra, solid only (version 1)
  PLAN-fea-2  the detail level sets the element count; the base size is solved for it
  PLAN-fea-3  at least 2 elements through every wall
  PLAN-fea-4  hot spots: 10 elements per quarter-turn of a hole, fillet or notch, and 3 through
              the wall beside it
  PLAN-fea-5  sharp inside corners are capped at a quarter of the wall beside them
  PLAN-fea-6  sizes grow by at most 1.3 away from every zone
  PLAN-fea-7  every other round keeps its shape: 3 elements per quarter-turn
  PLAN-fea-8  the estimate is checked before meshing and never stops the run: over the chosen level,
              the next level that holds every rule is used (and said); when even the largest cannot,
              the finest zones are floored until it fits, and what that leaves short is named
              (resolution_short)
  PLAN-fea-9  one part only (version 1)
  PLAN-fea-10 a CUSP (two CAD edges of a face leaving a vertex together: the material runs to nothing)
              cannot be meshed above the quality floor at any size: the elements touching it get
              straight sides, are set aside from the floor, and are named as a concern
  PLAN-fea-12 a MICRO DETAIL (an edge or face far smaller than the elements there) is set aside and
              named the same way, saying stress right at it is not resolved
  PLAN-fea-11 curving never makes an element worse: one that curving onto the CAD would leave worse
              than straight, and under the floor, keeps straight sides
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from meshpipeline.engines.fea_size_field import Histogram, elements

VERSION = 1
PLAN_FILE = "fea_plan.json"


@dataclass(frozen=True)
class Level:
    key: str          # the run's effective_mesh_fidelity (pipeline/enums.MeshFidelity)
    label: str        # what the user reads (design 3.5)
    target: int       # about this many elements


#: THE DETAIL LEVELS (Areen, design 3.5): Quick ~100k, Standard ~500k (default), Fine ~2M.
LEVELS: dict[str, Level] = {
    "draft": Level("draft", "Quick", 100_000),
    "standard": Level("standard", "Standard", 500_000),
    "max": Level("max", "Fine", 2_000_000),
}
NEXT_LEVEL = {"draft": "standard", "standard": "max", "max": None}
DEFAULT_LEVEL = "standard"
#: The base size is solved so the estimate comes to this share of the level (aimed under it, as the
#: snappy budget is: the estimate is an estimate).
AIM = 0.85
#: "About" a level: a plan up to this much over the level's count is still that level.
OVER = 1.25
#: The base size is never coarser than the part's diagonal over this: a part is never one element.
MIN_DIVISIONS = 10

#: PLAN-fea-3: elements through every wall (version 1 is solid only).
WALL_ELEMENTS = 2
#: PLAN-fea-4: elements per quarter-turn of a hole, fillet or notch, and through the wall beside it.
HOT_SPOT_PER_QUARTER = 10
STRESS_WALL_ELEMENTS = 3
#: The fewest the rule allows (design 6: at least 6 per quarter-turn): the "fewer elements at the
#: named spots" option meshes the hot spots at this, and the stress wall at the plain wall rule.
HOT_SPOT_PER_QUARTER_MIN = 6
#: PLAN-fea-12: a CAD detail (edge length, face width) under this fraction of the finest element the
#: plan asks for is a MICRO DETAIL: the mesh cannot follow it, so the elements on it are set aside.
MICRO_KEEP = 0.05
#: PLAN-fea-5: a sharp inside corner is refined to its wall over this, and no further.
CORNER_DIVISIONS = 4
#: PLAN-fea-6: the largest ratio between neighbouring element sizes away from a zone.
GROWTH = 1.3
#: PLAN-fea-7: elements per quarter-turn of a round that is not a hot spot (its shape only).
ROUND_PER_QUARTER = 3
#: Tetrahedra per base size cubed, measured on gmsh's own Delaunay fill (lab ~/fea4_lab: a cube
#: at 10/6/4 mm gave 4.9/4.6/4.2 per h^3, boundary included; the audit parts are checked against
#: it in tests/unit/engines/test_fea_plan_lab.py's notes).
PER_H3 = 4.5

QUARTER = math.pi / 2.0
_MM = 1e-3


def level_of(fidelity: str | None) -> Level:
    return LEVELS.get(str(fidelity or "").strip().lower(), LEVELS[DEFAULT_LEVEL])


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _r(v: float) -> float:
    return float(f"{float(v):.6g}")


def zones(record: Mapping, *, relaxed: bool = False, tighten: Mapping[str, float] | None = None) -> list[dict]:
    """Every zone the rules ask for, from the measurement record (millimetres in, metres out).
    `relaxed`: the hot spots at the fewest elements the rule allows (the over-budget option).
    `tighten`: zone id -> factor, the ladder's LAD-fea-3 (a zone that fell short, made finer)."""
    tighten = dict(tighten or {})
    per_quarter = HOT_SPOT_PER_QUARTER_MIN if relaxed else HOT_SPOT_PER_QUARTER
    stress_wall = WALL_ELEMENTS if relaxed else STRESS_WALL_ELEMENTS
    out: list[dict] = []
    spot_faces: set[str] = set()
    thinnest = _f((record.get("thickness") or {}).get("min_mm"))

    for h in record.get("hot_spots") or []:
        faces = [str(f) for f in h.get("faces") or []]
        t = _f(h.get("local_thickness_mm"))
        if h.get("type") == "sharp_corner":
            basis = _f(h.get("cap_basis_mm")) or t or thinnest
            if basis is None or len(faces) < 2:
                continue
            size = basis / CORNER_DIVISIONS
            out.append({"id": f"C{h.get('id', '')[1:]}", "kind": "corner", "faces": faces[:2],
                        "size_m": size * _MM, "reach_m": size * _MM, "rule": "PLAN-fea-5",
                        "spot": h.get("id"), "capped": True, "thickness_m": basis * _MM,
                        "why": (f"sharp inside corner ({_deg(h.get('angle_deg'))}): refined to a quarter of "
                                f"the {_mm(basis)} wall beside it, and capped there")})
            continue
        r = _f(h.get("radius_mm"))
        if r is None or not faces:
            continue
        spot_faces.update(faces)
        arc = QUARTER * r / per_quarter
        size = min(arc, t / stress_wall) if t else arc
        why = f"{h.get('shape') or h.get('type')} of radius {_mm(r)}: {per_quarter} elements per quarter-turn"
        if t and t / stress_wall < arc:
            why = (f"{h.get('shape') or h.get('type')} of radius {_mm(r)} in a {_mm(t)} wall: "
                   f"{stress_wall} elements through the wall beside it")
        out.append({"id": f"H{h.get('id', '')[1:]}", "kind": str(h.get("type")), "faces": faces,
                    "size_m": size * _MM, "reach_m": r * _MM, "rule": "PLAN-fea-4",
                    "spot": h.get("id"), "capped": False, "radius_m": r * _MM, "why": why,
                    **({"thickness_m": t * _MM, "wall_elements": stress_wall} if t else {})})

    face_rows = record.get("faces") or {}
    for name in sorted(face_rows, key=_face_order):
        row = face_rows[name] or {}
        t = _f(row.get("thickness_min_mm"))
        if t is not None:
            out.append({"id": f"W{_face_order(name)}", "kind": "wall", "faces": [name],
                        "size_m": t / WALL_ELEMENTS * _MM, "reach_m": t * _MM, "rule": "PLAN-fea-3",
                        "thickness_m": t * _MM, "capped": False,
                        "why": f"a {_mm(t)} wall under {name}: {WALL_ELEMENTS} elements through it"})
        r = _f(row.get("radius_mm"))
        if r is not None and name not in spot_faces:
            out.append({"id": f"R{_face_order(name)}", "kind": "round", "faces": [name],
                        "size_m": QUARTER * r / ROUND_PER_QUARTER * _MM, "reach_m": 0.0,
                        "rule": "PLAN-fea-7", "radius_m": r * _MM, "capped": False,
                        "why": f"a round of radius {_mm(r)} on {name}: {ROUND_PER_QUARTER} elements per quarter-turn"})
    for z in out:
        k = tighten.get(z["id"])
        if k:
            z["size_m"] = z["size_m"] * float(k)
            z["tightened"] = float(k)
        z["size_m"] = _r(z["size_m"])
        z["reach_m"] = _r(z["reach_m"])
    return out


def _face_order(name: str) -> int:
    try:
        return int(str(name).rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _mm(v_mm: float) -> str:
    return f"{float(v_mm):.3g} mm"


def _deg(v) -> str:
    x = _f(v)
    return f"{x:.0f} degrees" if x is not None else "sharp"


def base_size(hist: Histogram, *, target: float, coarsest: float, per_h3: float = PER_H3) -> float:
    """PLAN-fea-2: the base size whose estimate is AIM of the target - the coarsest allowed when
    even that is over it. Bisection on the log of the size: the same inputs give the same size."""
    want = AIM * float(target)
    if elements(hist, coarsest, per_h3) >= want:
        return coarsest
    lo, hi = math.log(coarsest) - math.log(1e6), math.log(coarsest)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if elements(hist, math.exp(mid), per_h3) > want:
            lo = mid
        else:
            hi = mid
    return math.exp(hi)


def finish(record: Mapping, zone_list: list[dict], hist: Histogram, *, level: Level, diag_m: float,
           base_factor: float = 1.0, relaxed_hist: Histogram | None = None,
           levers: list[dict] | None = None, relaxed: bool = False, growth: float = GROWTH) -> dict:
    """The whole plan, from the zones and their histogram (see the module text). `base_factor`
    is the ladder's LAD-fea-4 (under 1: the base size is divided by it, so coarser; the rules
    kept). `relaxed_hist` is the
    histogram of the relaxed zones, asked for only when the plan does not fit."""
    coarsest = diag_m / MIN_DIVISIONS
    base = min(coarsest, base_size(hist, target=level.target, coarsest=coarsest) / float(base_factor))
    est = elements(hist, base, PER_H3)
    at_coarsest = elements(hist, coarsest, PER_H3)
    ceiling = int(level.target * OVER)
    fits = hist.complete and est <= ceiling
    plan: dict = {
        "version": VERSION,
        "detail": {"key": level.key, "label": level.label, "target": level.target, "ceiling": ceiling},
        "element": {"family": "tetrahedra", "order": 2, "solid_only": True},
        "base_size_m": _r(base), "coarsest_base_m": _r(coarsest), "growth": float(growth), "per_h3": PER_H3,
        "zones": zone_list, "histogram": hist.as_list(), "histogram_complete": hist.complete,
        "estimate": {"elements": int(round(est)), "zones_at_coarsest": int(round(at_coarsest))},
        "fits": bool(fits), "relaxed": bool(relaxed), "options": [], "levers": list(levers or []),
        # PLAN-fea-10: the cusps the adapter sets aside (metres, the mesh's unit)
        "cusps": [{"at_m": [_r(float(v) * _MM) for v in c["at_mm"]], "faces": list(c.get("faces") or []),
                   "angle_deg": c.get("angle_deg")} for c in record.get("cusps") or []],
    }
    # PLAN-fea-12: the micro details far below the finest element this plan asks for
    finest_mm = min([float(z["size_m"]) for z in zone_list] + [float(base)]) / _MM
    plan["micro"] = [{"kind": m.get("kind"), "size_mm": m.get("size_mm"), "faces": list(m.get("faces") or []),
                      "points_m": [[_r(float(v) * _MM) for v in p] for p in m.get("points_mm") or []]}
                     for m in record.get("micro") or []
                     if float(m.get("size_mm") or 0.0) < MICRO_KEEP * finest_mm]
    if not fits:
        plan["options"] = options(hist, relaxed_hist, level=level, coarsest=coarsest, zone_list=zone_list)
    plan["words"] = words(record, plan)
    plan["decisions"] = decisions(record, plan)
    return plan


def _fits_level(hist: Histogram | None, level: Level, coarsest: float) -> int | None:
    """The estimate at `level` when the zones fit it, else None."""
    if hist is None or not hist.complete:
        return None
    base = base_size(hist, target=level.target, coarsest=coarsest)
    est = elements(hist, base, PER_H3)
    return int(round(est)) if est <= level.target * OVER else None


def options(hist: Histogram, relaxed_hist: Histogram | None, *, level: Level, coarsest: float,
            zone_list: list[dict]) -> list[dict]:
    """PLAN-fea-8's outs, in order: the first level up that holds every rule, and fewer elements
    at the named spots (at this level, or else at the first level up that holds it)."""
    out: list[dict] = []
    nxt = NEXT_LEVEL.get(level.key)
    while nxt:
        n = _fits_level(hist, LEVELS[nxt], coarsest)
        if n is not None:
            out.append({"option": "level_up", "level": nxt, "label": LEVELS[nxt].label, "elements": n,
                        "words": f"Use the {LEVELS[nxt].label} level: about {n:,} elements, every rule kept."})
            break
        nxt = NEXT_LEVEL.get(nxt)
    key: str | None = level.key
    while key:
        n = _fits_level(relaxed_hist, LEVELS[key], coarsest)
        if n is not None:
            spots = costly_spots(zone_list)
            where = (f"Stay at {level.label}" if key == level.key else f"Use the {LEVELS[key].label} level")
            out.append({"option": "fewer_at_spots", "level": key, "label": LEVELS[key].label, "elements": n,
                        "spots": spots,
                        "words": (f"{where} and accept fewer elements at the named spots "
                                  f"({HOT_SPOT_PER_QUARTER_MIN} per quarter-turn instead of "
                                  f"{HOT_SPOT_PER_QUARTER}, {WALL_ELEMENTS} through the wall beside them"
                                  + (f": {', '.join(spots)}" if spots else "") + f"): about {n:,} elements.")})
            break
        key = NEXT_LEVEL.get(key)
    return out


#: The largest level a run is meshed at (PLAN-fea-8: the best possible within it when no level holds
#: every rule).
LARGEST = "max"


def floored(zone_list: list[dict], floor_m: float) -> list[dict]:
    """PLAN-fea-8's best possible: every zone finer than `floor_m` meshed at `floor_m` instead, the
    size its rule asked for kept (`rule_size_m`) so what it leaves short can be named."""
    out = []
    for z in zone_list:
        if float(z["size_m"]) < floor_m:
            out.append({**z, "rule_size_m": z["size_m"], "size_m": float(floor_m), "short": True})
        else:
            out.append(dict(z))
    return out


def shortfalls(zone_list: list[dict], n: int = 12) -> list[str]:
    """What the floored zones leave short of their rules, the worst first, in plain words
    (resolution_short): elements through a wall, per quarter-turn of a round."""
    rows = []
    for z in zone_list:
        if not z.get("short"):
            continue
        size = float(z["size_m"])
        where = ", ".join(z.get("faces") or []) or str(z.get("id"))
        if z["kind"] == "wall" and z.get("thickness_m"):
            got = float(z["thickness_m"]) / size
            rows.append((got / WALL_ELEMENTS, f"{where}: about {got:.2g} elements through the "
                         f"{float(z['thickness_m']) * 1e3:.3g} mm wall instead of {WALL_ELEMENTS}"))
        elif z["kind"] in ("hole", "fillet", "notch") and z.get("radius_m"):
            got = QUARTER * float(z["radius_m"]) / size
            rows.append((got / HOT_SPOT_PER_QUARTER, f"{z.get('spot') or z['id']} ({z['kind']}, radius "
                         f"{float(z['radius_m']) * 1e3:.3g} mm, {where}): about {got:.2g} elements per quarter-turn "
                         f"instead of {HOT_SPOT_PER_QUARTER}"))
        else:
            ratio = float(z.get("rule_size_m") or size) / size
            rows.append((ratio, f"{z['id']} ({z['kind']}, {where}): elements {1 / ratio:.2g} times the size its "
                         "rule asks for"))
    rows.sort(key=lambda r: r[0])
    words = [w for _r, w in rows[:n]]
    if len(rows) > n:
        words.append(f"and {len(rows) - n} more zone(s) coarser than their rule")
    return words


def costly_spots(zone_list: list[dict], n: int = 3) -> list[str]:
    """The hot spots that cost the most (the finest first), named for the user."""
    spots = sorted((z for z in zone_list if z["kind"] in ("hole", "fillet", "notch")),
                   key=lambda z: (z["size_m"], z["id"]))
    return [f"{z['spot']} ({z['kind']}, radius {z['radius_m'] * 1e3:.3g} mm)" for z in spots[:n]]


def words(record: Mapping, plan: Mapping) -> list[str]:
    """The plan in plain words (design 3.8: what was refined and why, where corners were capped)."""
    d = plan["detail"]
    zs = plan["zones"]
    est = plan["estimate"]["elements"]
    out = [f"Detail level {d['label']}: about {d['target']:,} elements. The base element size is "
           f"{plan['base_size_m'] * 1e3:.3g} mm, set so the part comes to about that count.",
           "Quadratic tetrahedra (10-node), a solid mesh; every CAD face is a named group."]
    walls = [z for z in zs if z["kind"] == "wall" and z["size_m"] < plan["base_size_m"]]
    if walls:
        thin = min(walls, key=lambda z: z["thickness_m"])
        out.append(f"Walls: at least {WALL_ELEMENTS} elements through every wall. {len(walls)} face(s) sit on "
                   f"walls thin enough to need it; the thinnest, {thin['thickness_m'] * 1e3:.3g} mm under "
                   f"{thin['faces'][0]}, gets {thin['size_m'] * 1e3:.3g} mm elements.")
    else:
        out.append(f"Walls: at least {WALL_ELEMENTS} elements through every wall; the base size already gives "
                   "that everywhere.")
    spots = [z for z in zs if z["kind"] in ("hole", "fillet", "notch")]
    if spots:
        by: dict[str, int] = {}
        for z in spots:
            by[z["kind"]] = by.get(z["kind"], 0) + 1
        said = ", ".join(f"{n} {k}{'' if n == 1 else ('es' if k == 'notch' else 's')}" for k, n in by.items())
        finest = min(spots, key=lambda z: z["size_m"])
        per = HOT_SPOT_PER_QUARTER_MIN if plan.get("relaxed") else HOT_SPOT_PER_QUARTER
        base = plan["base_size_m"]
        if finest["size_m"] < base:
            refined = sum(1 for z in spots if z["size_m"] < base)
            out.append(f"Hot spots: {said}. At least {per} elements per quarter-turn of each round; {refined} "
                       f"refined below the base size, the finest, {finest['spot']}, to "
                       f"{finest['size_m'] * 1e3:.3g} mm elements.")
        else:
            out.append(f"Hot spots: {said}. At least {per} elements per quarter-turn of each round; the base "
                       "size is already that fine at every one of them.")
    corners = [z for z in zs if z["kind"] == "corner" and z["size_m"] < plan["base_size_m"]]
    if corners:
        out.append(f"Sharp inside corners: {len(corners)} capped at a quarter of the wall beside them (the "
                   "finest at {:.3g} mm). Their stress never settles under refinement, so refining further "
                   "would only add elements.".format(min(z["size_m"] for z in corners) * 1e3))
    rounds = [z for z in zs if z["kind"] == "round" and z["size_m"] < plan["base_size_m"]]
    if rounds:
        out.append(f"{len(rounds)} other round face(s) keep their shape: {ROUND_PER_QUARTER} elements per quarter-turn.")
    cusps = plan.get("cusps") or []
    if cusps:
        out.append(f"Cusps: {len(cusps)} place(s) where two CAD edges of a face meet at almost no angle, so the "
                   "material runs to nothing there. No element there can be well shaped at any size: the ones "
                   "touching them get straight sides and are set aside from the quality floor, and the delivery "
                   "names each place. Every other element must meet the floor.")
    micro = plan.get("micro") or []
    if micro:
        smallest = min(float(m.get("size_mm") or 0.0) for m in micro)
        out.append(f"Micro details: {len(micro)} CAD edge(s) or face(s) far smaller than the elements there "
                   f"(the smallest {smallest:.3g} mm). The mesh cannot follow them: the elements on them get "
                   "straight sides and are set aside from the quality floor, and the delivery names each one. "
                   "Stress right at them is not resolved - on a gear, that includes the contact stress at the "
                   "tooth tips.")
    out.append("Curved elements: an element that curving onto the CAD would leave worse than straight keeps "
               "straight sides.")
    out.append(f"Sizes grow by at most {plan.get('growth', GROWTH)} from one element to the next away from "
               "these spots.")
    auto = plan.get("auto_level") or {}
    best = plan.get("best_possible") or {}
    if auto:
        out.append(f"The rules need {auto['needed_words']}, more than the {auto['asked_label']} level asked "
                   f"for: meshed at the {d['label']} level instead, every rule kept.")
    if best:
        out.append(f"The rules need {best['needed_words']}, more than even the {d['label']} level holds: "
                   f"meshed at the {d['label']} level as finely as it allows. The finest zones get elements of "
                   f"{float(best['zone_floor_m']) * 1e3:.3g} mm instead of what their rule asks, and what that leaves "
                   "short is named: " + "; ".join(plan.get("resolution_short") or []) + ".")
    if plan["fits"]:
        out.append(f"Estimated before meshing: about {est:,} elements, within the {d['label']} level.")
    else:
        why = (f"about {est:,} elements" if plan.get("histogram_complete", True)
               else "more elements than any level holds")
        out.append(f"Stopped before meshing: the rules need {why}, over the {d['label']} level "
                   f"(about {d['target']:,}).")
        for o in plan.get("options") or []:
            out.append("Option: " + o["words"])
        if not plan.get("options"):
            out.append("No level fits every rule. Simplify the part where it is finest (the smallest "
                       "holes, fillets and corners), or mesh a smaller piece of it.")
    for lever in plan.get("levers") or []:
        out.append(str(lever.get("words") or ""))
    return [w for w in out if w]


def decisions(record: Mapping, plan: Mapping) -> list[dict]:
    """The why-record entries the plan makes (contracts/decision_record.cite shape), one per rule
    that decided something on this part."""
    d = plan["detail"]
    zs = plan["zones"]
    out = [
        {"stage": "fea_plan", "decided": "element", "value": "quadratic tetrahedra, solid", "rule": "PLAN-fea-1",
         "inputs": {"solid_only": True}, "words": "Version 1 meshes the solid with 10-node tetrahedra."},
        {"stage": "fea_plan", "decided": "base size", "value": plan["base_size_m"], "rule": "PLAN-fea-2",
         "inputs": {"level": d["key"], "target": d["target"], "aim": AIM, "coarsest_m": plan["coarsest_base_m"],
                    "per_h3": PER_H3},
         "words": f"Base size {plan['base_size_m'] * 1e3:.3g} mm for the {d['label']} level."},
    ]
    for rule, kinds, what in (("PLAN-fea-3", ("wall",), "walls"), ("PLAN-fea-4", ("hole", "fillet", "notch"), "hot spots"),
                              ("PLAN-fea-5", ("corner",), "capped corners"), ("PLAN-fea-7", ("round",), "rounds")):
        sel = [z for z in zs if z["kind"] in kinds]
        if sel:
            out.append({"stage": "fea_plan", "decided": what, "value": len(sel), "rule": rule,
                        "inputs": {"finest_m": min(z["size_m"] for z in sel)},
                        "words": f"{len(sel)} {what} zone(s), the finest {min(z['size_m'] for z in sel) * 1e3:.3g} mm."})
    cusps = plan.get("cusps") or []
    if cusps:
        out.append({"stage": "fea_plan", "decided": "cusps set aside", "value": len(cusps), "rule": "PLAN-fea-10",
                    "inputs": {"places_m": [c["at_m"] for c in cusps]},
                    "words": f"{len(cusps)} cusp(s): the elements touching them are set aside from the quality floor."})
    micro = plan.get("micro") or []
    if micro:
        out.append({"stage": "fea_plan", "decided": "micro details set aside", "value": len(micro),
                    "rule": "PLAN-fea-12",
                    "inputs": {"smallest_mm": min(float(m.get("size_mm") or 0.0) for m in micro), "keep": MICRO_KEEP},
                    "words": f"{len(micro)} micro detail(s): the elements on them are set aside from the quality floor."})
    out.append({"stage": "fea_plan", "decided": "curving", "value": "never worse than straight", "rule": "PLAN-fea-11",
                "inputs": {}, "words": "An element curving would leave worse than straight keeps straight sides."})
    out.append({"stage": "fea_plan", "decided": "growth", "value": plan["growth"], "rule": "PLAN-fea-6", "inputs": {},
                "words": f"Element sizes grow by at most {plan.get('growth', GROWTH)} away from every zone."})
    if plan.get("auto_level") or plan.get("best_possible"):
        a = plan.get("auto_level") or plan.get("best_possible") or {}
        out.append({"stage": "fea_plan", "decided": "level", "rule": "PLAN-fea-8",
                    "value": "best possible" if plan.get("best_possible") else d["key"],
                    "inputs": {"asked": a.get("asked"), "needed": a.get("needed"), "complete": a.get("complete"),
                               "zone_floor_m": (plan.get("best_possible") or {}).get("zone_floor_m")},
                    "words": (f"Asked for {a.get('asked_label')}; meshed at {d['label']}"
                              + (" as finely as it allows, short where named." if plan.get("best_possible")
                                 else ", every rule kept."))})
    out.append({"stage": "fea_plan", "decided": "budget", "value": "fits" if plan["fits"] else "over",
                "rule": "PLAN-fea-8",
                "inputs": {"estimate": plan["estimate"]["elements"], "ceiling": d["ceiling"],
                           "complete": plan.get("histogram_complete", True)},
                "alternatives": [o["option"] for o in plan.get("options") or []],
                "words": (f"About {plan['estimate']['elements']:,} elements against the {d['label']} level's "
                          f"{d['ceiling']:,}: " + ("meshed." if plan["fits"] else "stopped before meshing."))})
    return out


def one_part(record: Mapping) -> str:
    """PLAN-fea-9: '' for one body, else what the user is told (design 3.6)."""
    try:
        n = int(record.get("bodies"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if n > 1:
        return (f"This file has {n} separate bodies. Version 1 meshes one part at a time: export each part "
                "on its own and upload it, or wait for assembly support.")
    return ""


__all__ = ["AIM", "CORNER_DIVISIONS", "DEFAULT_LEVEL", "GROWTH", "HOT_SPOT_PER_QUARTER",
           "HOT_SPOT_PER_QUARTER_MIN", "LEVELS", "Level", "MIN_DIVISIONS", "NEXT_LEVEL", "OVER", "PER_H3",
           "LARGEST", "PLAN_FILE", "ROUND_PER_QUARTER", "floored", "shortfalls", "STRESS_WALL_ELEMENTS", "VERSION", "WALL_ELEMENTS",
           "base_size", "costly_spots", "decisions", "finish", "level_of", "one_part", "options", "words",
           "zones"]
