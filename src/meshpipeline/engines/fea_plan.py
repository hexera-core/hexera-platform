# Responsibility: Compute the FEA meshing plan from a part's measurements: the element type, the zones the rules ask for (walls, hot spots, capped corners, rounds) with the size each one needs, the base size where no rule asks for finer, the element estimate against the element limit, and - when the rules need more than the limit - the finest zones coarsened until it fits, never past what keeps elements valid, with what that leaves short named.
# Boundaries: pure over plain data (the measurement record, the volume histogram, the limit); no engine, no file, no model. Every number names the rule it comes from (docs/RULES.md PLAN-fea-n).
# Collaborates with: cad/fea_measure.py (the record), engines/fea_size_field.py (the histogram), engines/fea_driver.py (runs it and records it), engines/gmsh/fea_adapter.py (meshes it).
"""THE FEA PLAN (fea_plan.json), engine-neutral: ONE rules-based plan (Areen, 21 Oct).

    {"version": 2,
     "element": {"family": "tetrahedra", "order": 2, "solid_only": true},
     "base_size_m": .., "growth": 1.3, "per_h3": 4.5,
     "zones": [{"id": "W3", "kind": "wall" | "wall_spot" | "hole" | "fillet" | "notch" | "corner" | "round",
                "faces": ["Face_3"], "size_m", "reach_m", "rule": "PLAN-fea-3", "why": "...",
                "spot": "H1", "capped": bool, "thickness_m" | "radius_m"}],
     "histogram": [[size_m, volume_m3], ...],
     "estimate": {"elements": ..}, "limit": {"elements": .., "why": ".."},
     "fits": true, "coarsened": {...} | absent, "resolution_short": [...],
     "levers": [], "cusps": [...], "micro": [...], "words": ["..."], "decisions": [...]}

Lengths are metres (the unit gmsh and the deck work in); the measurement record is millimetres.

THE RULES (docs/RULES.md):
  PLAN-fea-1  quadratic tetrahedra, solid only (version 1)
  PLAN-fea-3  at least 2 elements through every wall
  PLAN-fea-4  hot spots: 10 elements per quarter-turn of a hole, fillet or notch, and 3 through
              the wall beside it
  PLAN-fea-5  sharp inside corners are capped at a quarter of the wall beside them
  PLAN-fea-6  sizes grow by at most 1.3 away from every zone
  PLAN-fea-7  every curved face keeps its shape: 4 elements per quarter-turn, by its own curvature
  PLAN-fea-8  the estimate is checked against the element limit before meshing and never stops the
              run: over it, the finest zones are coarsened until it fits, never past what keeps
              elements valid, and what that leaves short is named (resolution_short)
  PLAN-fea-16 an assembly: every part sized by its own rules (its base a tenth of the part), the
              element limit over the whole assembly; bonded parts share their nodes where they touch,
              contact parts are meshed apart with named contact pairs
  PLAN-fea-10 a CUSP is set aside from the quality floor with straight sides, and named
  PLAN-fea-11 curving never makes an element worse
  PLAN-fea-12 a MICRO DETAIL is set aside and named the same way
  PLAN-fea-14 the base size is the coarsest the part allows; the rules refine where they ask
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from meshpipeline.engines.fea_size_field import Histogram, elements

VERSION = 2
PLAN_FILE = "fea_plan.json"
#: THE STAGED SOLID the FEA path reads, first found (PLAN-fea-15): gmsh's own B-rep file, written
#: beside the STEP when the part is staged - lossless, where gmsh's STEP writer and reader together
#: lose faces on some parts (fea0080 could not be read back; fea0100 left a face with no surface
#: mesh) - else the STEP.
STAGED_SOLIDS = ("geometry.brep", "geometry.step")

#: PLAN-fea-8: the most elements one FEA mesh is built with (Areen, 21 Oct).
ELEMENT_LIMIT = 8_000_000
#: The plan aims this far under its limit: the estimate is an estimate (step 8: built/planned
#: median 0.93, 90th percentile 1.12 on 178 meshes).
LIMIT_AIM = 0.85
#: PLAN-fea-14: the base size is the part's diagonal over this - the coarsest the part allows (a
#: part is never one element); every rule refines below it where it asks to.
MIN_DIVISIONS = 10

#: PLAN-fea-3: elements through every wall (version 1 is solid only).
WALL_ELEMENTS = 2
#: PLAN-fea-4: elements per quarter-turn of a hole, fillet or notch, and through the wall beside it.
HOT_SPOT_PER_QUARTER = 10
STRESS_WALL_ELEMENTS = 3
#: PLAN-fea-12: a CAD detail (edge length, face width) under this fraction of the finest element the
#: plan asks for is a MICRO DETAIL: the mesh cannot follow it, so the elements on it are set aside.
MICRO_KEEP = 0.05
#: PLAN-fea-5: a sharp inside corner is refined to its wall over this, and no further.
CORNER_DIVISIONS = 4
#: PLAN-fea-6: the largest ratio between neighbouring element sizes away from a zone.
GROWTH = 1.3
#: PLAN-fea-7: elements per quarter-turn of a round that is not a hot spot (its shape only).
ROUND_PER_QUARTER = 4
#: Tetrahedra per base size cubed, measured on gmsh's own Delaunay fill (lab ~/fea4_lab: a cube
#: at 10/6/4 mm gave 4.9/4.6/4.2 per h^3, boundary included).
PER_H3 = 4.5
#: PLAN-fea-8's VALIDITY MINIMUMS: coarsening a zone never goes past what keeps its elements valid -
#: this many elements through a wall, and per quarter-turn of a round (step 8's VM lab: six coarsened
#: plans put under one element across a 0.1-0.5 mm wall and failed).
KEEP_THROUGH = 1.0
KEEP_PER_QUARTER = 3

QUARTER = math.pi / 2.0
_MM = 1e-3


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _r(v: float) -> float:
    return float(f"{float(v):.6g}")


def zones(record: Mapping, *, tighten: Mapping[str, float] | None = None) -> list[dict]:
    """Every zone the rules ask for, from the measurement record (millimetres in, metres out).
    `tighten`: zone id -> factor, the ladder's LAD-fea-3 (a zone that fell short, made finer)."""
    tighten = dict(tighten or {})
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
        arc = QUARTER * r / HOT_SPOT_PER_QUARTER
        size = min(arc, t / STRESS_WALL_ELEMENTS) if t else arc
        why = f"{h.get('shape') or h.get('type')} of radius {_mm(r)}: {HOT_SPOT_PER_QUARTER} elements per quarter-turn"
        if t and t / STRESS_WALL_ELEMENTS < arc:
            why = (f"{h.get('shape') or h.get('type')} of radius {_mm(r)} in a {_mm(t)} wall: "
                   f"{STRESS_WALL_ELEMENTS} elements through the wall beside it")
        out.append({"id": f"H{h.get('id', '')[1:]}", "kind": str(h.get("type")), "faces": faces,
                    "size_m": size * _MM, "reach_m": r * _MM, "rule": "PLAN-fea-4",
                    "spot": h.get("id"), "capped": False, "radius_m": r * _MM, "why": why,
                    **({"thickness_m": t * _MM, "wall_elements": STRESS_WALL_ELEMENTS} if t else {})})

    face_rows = record.get("faces") or {}
    for name in sorted(face_rows, key=_face_order):
        row = face_rows[name] or {}
        # PLAN-fea-3, THIN ONLY WHERE IT IS THIN: a face thin only in places keeps the wall the rest
        # of it agrees on, and each thin spot is a zone of its own round where its readings are
        t = _f(row.get("thickness_wall_mm")) or _f(row.get("thickness_min_mm"))
        if t is not None:
            out.append({"id": f"W{_face_order(name)}", "kind": "wall", "faces": [name],
                        "size_m": t / WALL_ELEMENTS * _MM, "reach_m": t * _MM, "rule": "PLAN-fea-3",
                        "thickness_m": t * _MM, "capped": False,
                        "why": f"a {_mm(t)} wall under {name}: {WALL_ELEMENTS} elements through it"})
        for i, sp in enumerate(row.get("thin_spots") or [], start=1):
            ts, rad = _f(sp.get("thickness_mm")), _f(sp.get("radius_mm"))
            if not ts or rad is None:
                continue
            out.append({"id": f"T{_face_order(name)}_{i}", "kind": "wall_spot", "faces": [name],
                        "size_m": ts / WALL_ELEMENTS * _MM, "reach_m": ts * _MM, "rule": "PLAN-fea-3",
                        "thickness_m": ts * _MM, "capped": False, "radius_m": rad * _MM,
                        "at_m": [float(v) * _MM for v in sp.get("at_mm") or [0.0, 0.0, 0.0]],
                        "why": (f"a {_mm(ts)} thin spot of the wall under {name}, {_mm(rad)} across: "
                                f"{WALL_ELEMENTS} elements through it there")})
        r = _f(row.get("radius_mm"))
        if r is not None and name not in spot_faces:
            out.append({"id": f"R{_face_order(name)}", "kind": "round", "faces": [name],
                        "size_m": QUARTER * r / ROUND_PER_QUARTER * _MM, "reach_m": 0.0,
                        "rule": "PLAN-fea-7", "radius_m": r * _MM, "capped": False,
                        "why": f"a round of radius {_mm(r)} on {name}: {ROUND_PER_QUARTER} elements per quarter-turn"})
    # PLAN-fea-16: EVERY PART OF AN ASSEMBLY HAS ITS OWN BASE SIZE - a tenth of the part, as
    # PLAN-fea-14 gives a single part - so a bolt beside a flange is sized as the bolt it is
    for part in record.get("part_bases") or []:
        d = _f(part.get("diag_mm"))
        faces = [str(f) for f in part.get("faces") or []]
        if d is None or not faces:
            continue
        size = d / MIN_DIVISIONS
        named = f" ({part['name']})" if part.get("name") else ""
        out.append({"id": f"P{part.get('n')}", "kind": "part", "faces": faces, "size_m": size * _MM,
                    "reach_m": 0.0, "rule": "PLAN-fea-16", "capped": False, "part": part.get("id"),
                    "why": f"{part.get('id')}{named} is {_mm(d)} across: its base size is a tenth of it"})
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


def keep_size(z: dict, through: float = KEEP_THROUGH) -> float | None:
    """The coarsest size a zone may be given and still keep its elements valid; None for no limit."""
    sizes = []
    if z.get("thickness_m"):
        sizes.append(float(z["thickness_m"]) / through)
    if z["kind"] in ("hole", "fillet", "notch", "round") and z.get("radius_m"):
        sizes.append(QUARTER * float(z["radius_m"]) / KEEP_PER_QUARTER)
    return min(sizes) if sizes else None


def floored(zone_list: list[dict], floor_m: float, through: float = KEEP_THROUGH) -> list[dict]:
    """PLAN-fea-8's coarsening: every zone finer than `floor_m` meshed at `floor_m` instead - but
    never coarser than its validity minimum (keep_size) - the size its rule asked for kept
    (`rule_size_m`) so what it leaves short can be named."""
    out = []
    for z in zone_list:
        size = float(z["size_m"])
        if size < floor_m:
            keep = keep_size(z, through)
            new = float(floor_m) if keep is None else max(size, min(float(floor_m), keep))
            if new > size:
                out.append({**z, "rule_size_m": size, "size_m": new, "short": True,
                            **({"kept_valid": True} if keep is not None and new < floor_m else {})})
                continue
        out.append(dict(z))
    return out


def shortfalls(zone_list: list[dict], n: int = 12) -> list[str]:
    """What the coarsened zones leave short of their rules, the worst first, in plain words
    (resolution_short): elements through a wall, per quarter-turn of a round."""
    rows = []
    for z in zone_list:
        if not z.get("short"):
            continue
        size = float(z["size_m"])
        where = ", ".join(z.get("faces") or []) or str(z.get("id"))
        if z["kind"] in ("wall", "wall_spot") and z.get("thickness_m"):
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


def finish(record: Mapping, zone_list: list[dict], hist: Histogram, *, diag_m: float, limit: int,
           levers: list[dict] | None = None, growth: float = GROWTH) -> dict:
    """The whole plan, from the zones and their histogram (see the module text): the base size is
    the coarsest the part allows (PLAN-fea-14), and the plan fits when its estimate is within
    LIMIT_AIM of `limit` (PLAN-fea-8)."""
    base = diag_m / MIN_DIVISIONS
    est = elements(hist, base, PER_H3)
    fits = hist.complete and est <= LIMIT_AIM * float(limit)
    plan: dict = {
        "version": VERSION,
        "element": {"family": "tetrahedra", "order": 2, "solid_only": True},
        "base_size_m": _r(base), "growth": float(growth), "per_h3": PER_H3,
        "zones": zone_list, "histogram": hist.as_list(), "histogram_complete": hist.complete,
        "estimate": {"elements": int(round(est))},
        "limit": {"elements": int(limit)},
        "fits": bool(fits), "levers": list(levers or []),
        # PLAN-fea-10: the cusps the adapter sets aside (metres, the mesh's unit)
        "cusps": [{"at_m": [_r(float(v) * _MM) for v in c["at_mm"]], "faces": list(c.get("faces") or []),
                   "angle_deg": c.get("angle_deg")} for c in record.get("cusps") or []],
    }
    # PLAN-fea-16: an assembly's parts, its staged file and how they are joined (the driver's)
    if record.get("assembly_plan"):
        plan["assembly"] = dict(record["assembly_plan"])
    # PLAN-fea-12: the micro details far below the finest element this plan asks for
    finest_mm = min([float(z["size_m"]) for z in zone_list] + [float(base)]) / _MM
    plan["micro"] = [{"kind": m.get("kind"), "size_mm": m.get("size_mm"), "faces": list(m.get("faces") or []),
                      "points_m": [[_r(float(v) * _MM) for v in p] for p in m.get("points_mm") or []]}
                     for m in record.get("micro") or []
                     if float(m.get("size_mm") or 0.0) < MICRO_KEEP * finest_mm]
    plan["words"] = words(record, plan)
    plan["decisions"] = decisions(record, plan)
    return plan


def words(record: Mapping, plan: Mapping) -> list[str]:
    """The plan in plain words (design 3.8: what was refined and why, where corners were capped)."""
    zs = plan["zones"]
    est = plan["estimate"]["elements"]
    limit = int((plan.get("limit") or {}).get("elements") or ELEMENT_LIMIT)
    out = [f"The mesh the rules recommend: the base element size is {plan['base_size_m'] * 1e3:.3g} mm (a tenth "
           "of the part), and every rule below makes it finer where it asks to.",
           "Quadratic tetrahedra (10-node), a solid mesh; every CAD face is a named group."]
    out.extend(assembly_words(plan.get("assembly") or {}))
    walls = [z for z in zs if z["kind"] == "wall" and z["size_m"] < plan["base_size_m"]]
    if walls:
        thin = min(walls, key=lambda z: z["thickness_m"])
        out.append(f"Walls: at least {WALL_ELEMENTS} elements through every wall. {len(walls)} face(s) sit on "
                   f"walls thin enough to need it; the thinnest, {thin['thickness_m'] * 1e3:.3g} mm under "
                   f"{thin['faces'][0]}, gets {thin['size_m'] * 1e3:.3g} mm elements.")
    spots_thin = [z for z in zs if z["kind"] == "wall_spot" and z["size_m"] < plan["base_size_m"]]
    if spots_thin:
        t = min(spots_thin, key=lambda z: z["thickness_m"])
        out.append(f"{len(spots_thin)} spot(s) where a wall is thin only in places get their own elements there, "
                   f"not across the whole face; the thinnest, {t['thickness_m'] * 1e3:.3g} mm on {t['faces'][0]}, gets "
                   f"{t['size_m'] * 1e3:.3g} mm elements.")
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
        base = plan["base_size_m"]
        if finest["size_m"] < base:
            refined = sum(1 for z in spots if z["size_m"] < base)
            out.append(f"Hot spots: {said}. At least {HOT_SPOT_PER_QUARTER} elements per quarter-turn of each round; "
                       f"{refined} refined below the base size, the finest, {finest['spot']}, to "
                       f"{finest['size_m'] * 1e3:.3g} mm elements.")
        else:
            out.append(f"Hot spots: {said}. At least {HOT_SPOT_PER_QUARTER} elements per quarter-turn of each round; "
                       "the base size is already that fine at every one of them.")
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
                   "touching them get straight sides and are set aside, and the delivery names each place.")
    micro = plan.get("micro") or []
    if micro:
        smallest = min(float(m.get("size_mm") or 0.0) for m in micro)
        out.append(f"Micro details: {len(micro)} CAD edge(s) or face(s) far smaller than the elements there "
                   f"(the smallest {smallest:.3g} mm). The mesh cannot follow them: the elements on them get "
                   "straight sides and are set aside, and the delivery names each one. Stress right at them is "
                   "not resolved - on a gear, that includes the contact stress at the tooth tips.")
    out.append("Curved elements: an element that curving onto the CAD would leave worse than straight keeps "
               "straight sides.")
    out.append(f"Sizes grow by at most {plan.get('growth', GROWTH)} from one element to the next away from "
               "these spots.")
    coarse = plan.get("coarsened") or {}
    if coarse:
        out.append(f"The rules need {coarse['needed_words']}, more than the {limit:,}-element limit allows: the "
                   f"finest zones were made coarser - to {float(coarse['zone_floor_m']) * 1e3:.3g} mm - until it fits, "
                   "never past 1 element through any wall or 3 per quarter-turn of any round. What that leaves "
                   "short: " + "; ".join(plan.get("resolution_short") or []) + ".")
        if coarse.get("at_minimums"):
            out.append("Even with every zone at those minimums the part needs more than the limit: meshed so "
                       "anyway, as the least resolution that keeps every element valid.")
        if coarse.get("over_memory"):
            out.append("That least valid mesh may be more than the mesh job's memory holds; it is meshed anyway, "
                       "and if the memory runs out the result says so.")
    out.append(f"Estimated before meshing: about {est:,} elements (the limit is {limit:,}).")
    for lever in plan.get("levers") or []:
        out.append(str(lever.get("words") or ""))
    return [w for w in out if w]


def assembly_words(asm: Mapping) -> list[str]:
    """PLAN-fea-16 in plain words: the parts, how they are joined, and the notes (an empty list for
    a single part)."""
    if not asm:
        return []
    parts = list(asm.get("parts") or [])
    named = [f"{p['id']} ({p['name']})" if p.get("name") else str(p["id"]) for p in parts]
    shown = ", ".join(named[:8]) + (f" and {len(named) - 8} more" if len(named) > 8 else "")
    out = [f"An assembly of {len(parts)} parts: {shown}. Each part is its own element set (give each its "
           "material in your solver), each is sized by its own rules, and the element limit covers the whole "
           "assembly. Its faces are named Part_k_Face_j."]
    n = int(asm.get("pairs") or 0)
    mode = asm.get("mode")
    if mode == "bonded":
        out.append(f"Bonded (welded or glued): one mesh, and the parts share their nodes where they touch "
                   f"({n} joint(s)); each joint's faces are named Bond_n_Part_k.")
    elif mode == "tie":
        out.append(f"Bonded, tied: each part is meshed on its own and the {n} touching pair(s) are tied in the "
                   "files (bonded, but their nodes are not shared).")
    else:
        out.append(f"Contact (the parts can slide or separate): each part is meshed on its own, and the {n} "
                   "touching pair(s) are written as named contact pairs, the smaller surface the slave.")
    out.extend(str(x) for x in asm.get("notes") or [] if x)
    return out


def decisions(record: Mapping, plan: Mapping) -> list[dict]:
    """The why-record entries the plan makes (contracts/decision_record.cite shape), one per rule
    that decided something on this part."""
    zs = plan["zones"]
    limit = int((plan.get("limit") or {}).get("elements") or ELEMENT_LIMIT)
    out = [
        {"stage": "fea_plan", "decided": "element", "value": "quadratic tetrahedra, solid", "rule": "PLAN-fea-1",
         "inputs": {"solid_only": True}, "words": "Version 1 meshes the solid with 10-node tetrahedra."},
        {"stage": "fea_plan", "decided": "base size", "value": plan["base_size_m"], "rule": "PLAN-fea-14",
         "inputs": {"min_divisions": MIN_DIVISIONS, "per_h3": PER_H3},
         "words": f"Base size {plan['base_size_m'] * 1e3:.3g} mm: a tenth of the part, the rules refine below it."},
    ]
    for rule, kinds, what in (("PLAN-fea-3", ("wall", "wall_spot"), "walls"), ("PLAN-fea-4", ("hole", "fillet", "notch"), "hot spots"),
                              ("PLAN-fea-5", ("corner",), "capped corners"), ("PLAN-fea-7", ("round",), "rounds")):
        sel = [z for z in zs if z["kind"] in kinds]
        if sel:
            out.append({"stage": "fea_plan", "decided": what, "value": len(sel), "rule": rule,
                        "inputs": {"finest_m": min(z["size_m"] for z in sel)},
                        "words": f"{len(sel)} {what} zone(s), the finest {min(z['size_m'] for z in sel) * 1e3:.3g} mm."})
    asm = plan.get("assembly") or {}
    if asm:
        out.append({"stage": "fea_plan", "decided": "assembly", "value": asm.get("mode"), "rule": "PLAN-fea-16",
                    "inputs": {"parts": len(asm.get("parts") or []), "connection": asm.get("connection"),
                               "pairs": asm.get("pairs")},
                    "words": " ".join(assembly_words(asm)[:2])})
    cusps = plan.get("cusps") or []
    if cusps:
        out.append({"stage": "fea_plan", "decided": "cusps set aside", "value": len(cusps), "rule": "PLAN-fea-10",
                    "inputs": {"places_m": [c["at_m"] for c in cusps]},
                    "words": f"{len(cusps)} cusp(s): the elements touching them are set aside."})
    micro = plan.get("micro") or []
    if micro:
        out.append({"stage": "fea_plan", "decided": "micro details set aside", "value": len(micro),
                    "rule": "PLAN-fea-12",
                    "inputs": {"smallest_mm": min(float(m.get("size_mm") or 0.0) for m in micro), "keep": MICRO_KEEP},
                    "words": f"{len(micro)} micro detail(s): the elements on them are set aside."})
    out.append({"stage": "fea_plan", "decided": "curving", "value": "never worse than straight", "rule": "PLAN-fea-11",
                "inputs": {}, "words": "An element curving would leave worse than straight keeps straight sides."})
    out.append({"stage": "fea_plan", "decided": "growth", "value": plan["growth"], "rule": "PLAN-fea-6", "inputs": {},
                "words": f"Element sizes grow by at most {plan.get('growth', GROWTH)} away from every zone."})
    coarse = plan.get("coarsened") or {}
    out.append({"stage": "fea_plan", "decided": "limit", "rule": "PLAN-fea-8",
                "value": "coarsened to fit" if coarse else "fits",
                "inputs": {"estimate": plan["estimate"]["elements"], "limit": limit, "aim": LIMIT_AIM,
                           "needed": coarse.get("needed"), "zone_floor_m": coarse.get("zone_floor_m"),
                           "at_minimums": coarse.get("at_minimums")},
                "words": (f"About {plan['estimate']['elements']:,} elements against the {limit:,} limit: "
                          + ("the finest zones coarsened to fit, short where named." if coarse else "meshed as planned."))})
    memory = (plan.get("limit") or {}).get("memory")
    if memory:
        out.append({"stage": "fea_plan", "decided": "memory limit", "rule": "PLAN-fea-16",
                    "value": int(memory.get("memory_elements") or 0),
                    "inputs": {**memory, "over_memory": bool(coarse.get("over_memory"))},
                    "words": (f"The mesh job's {memory.get('memory_gib'):g} GiB holds about "
                              f"{int(memory.get('memory_elements') or 0):,} elements "
                              f"({memory.get('bytes_per_element'):g} bytes each, {memory.get('margin'):g} times to spare)"
                              + ("; even the least valid mesh is over it, and it is meshed anyway."
                                 if coarse.get("over_memory") else "."))})
    return out


__all__ = ["CORNER_DIVISIONS", "assembly_words", "ELEMENT_LIMIT", "GROWTH", "HOT_SPOT_PER_QUARTER", "KEEP_PER_QUARTER",
           "KEEP_THROUGH", "LIMIT_AIM", "MICRO_KEEP", "MIN_DIVISIONS", "PER_H3", "PLAN_FILE", "ROUND_PER_QUARTER",
           "STRESS_WALL_ELEMENTS", "VERSION", "WALL_ELEMENTS", "decisions", "finish", "floored", "keep_size",
           "shortfalls", "words", "zones"]
