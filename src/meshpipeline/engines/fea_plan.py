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
  PLAN-fea-4  hot spots: 6 elements per quarter-turn of a hole, fillet or notch, and 3 through
              the wall beside it
  PLAN-fea-5  sharp inside corners are capped at a quarter of the wall beside them
  PLAN-fea-6  sizes grow by at most 1.3 away from every zone
  PLAN-fea-7  every curved face keeps its shape: 4 elements per quarter-turn, by its own curvature
  PLAN-fea-8  the estimate is checked against the element limit before meshing and never stops the
              run: over it, the finest zones are coarsened until it fits, never past what keeps
              elements valid, and what that leaves short is named (resolution_short)
  PLAN-fea-17 an assembly: every part sized by its own rules (its base a tenth of the part), the
              element limit over the whole assembly; bonded parts share their nodes where they touch,
              contact parts are meshed apart with named contact pairs
  PLAN-fea-10 a CUSP is set aside from the quality floor with straight sides, and named
  PLAN-fea-11 curving never makes an element worse
  PLAN-fea-12 a MICRO DETAIL is set aside and named the same way
  PLAN-fea-14 the base size is the coarsest the part allows; the rules refine where they ask
  PLAN-fea-27 an element count the user named: every size (the base and every zone) scaled by ONE
              factor until the estimate is near it, both ways, the hard minimums re-applied after
              (2 through every wall, 4 per quarter-turn of every curved face, the corner cap) and
              never past the largest machine
  PLAN-fea-28 under such a count the base grows to a fifth of the part at most
  PLAN-fea-29 the sizes the user asked for per part hold every size in those parts inside [min, max]
  PLAN-fea-30 only validity stops a user's size or count: 3 per quarter-turn of every curve, 3 across every part
  PLAN-fea-31 the estimate reads the surface too (thin walls at coarse sizes), and a count built off the ask is
              re-aimed once on the built count
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
#: 6 for QUADRATIC tetrahedra (Areen, 24 Oct, option b): a 10-node element's curved sides follow
#: the arc, and the textbook Kt cases hold their tolerance at 6 (lab, 24 Oct - see RULES.md
#: PLAN-fea-4); 10 made a 25 mm shaft coupler 2.8M elements and 63 minutes.
HOT_SPOT_PER_QUARTER = 6
STRESS_WALL_ELEMENTS = 3
#: PLAN-fea-4: how far off the feature's faces the hot-spot size is held before it grows (a share
#: of the radius). The stress peak sits at the root and falls off within about a radius; holding the
#: full density a whole radius deep along the whole feature was most of a small assembly's elements
#: (chrono_assembly: 56% of 1.86M from two R2 fillets), and growth 1.3 beyond it still reaches the
#: base size gradually.
HOT_SPOT_REACH = 0.5
#: PLAN-fea-12: a CAD detail (edge length, face width) under this fraction of the finest element the
#: plan asks for is a MICRO DETAIL: the mesh cannot follow it, so the elements on it are set aside.
MICRO_KEEP = 0.05
#: PLAN-fea-5: a sharp inside corner is refined to its wall over this, and no further.
CORNER_DIVISIONS = 4
#: PLAN-fea-6: the largest ratio between neighbouring element sizes away from a zone.
GROWTH = 1.3
#: PLAN-fea-7: elements per quarter-turn of a round that is not a hot spot (its shape only).
ROUND_PER_QUARTER = 4
#: Tetrahedra per size cubed INSIDE the part, measured on gmsh's own Delaunay fill. Before rulebook 57 it was 4.5 with
#: the boundary included (lab ~/fea4_lab: a cube at 10/6/4 mm gave 4.9/4.6/4.2 per h^3), and plans coarser than the
#: rules' were under-counted (#417: 1.38M planned, 1.73M built). PLAN-fea-31 counts the boundary on its own
#: (fea_size_field.SURFACE_PER_AREA), so the volume's own rate is refitted with it: 32 builds of 8 parts (single parts and
#: assemblies, 1,100 to 250k elements) at the rules' sizes, twice coarser, and past the accuracy minimums (lab
#: ~/feabudget_lab/calib.jsonl, fit.py): 3.5 per h^3 and 2.8 per area / h^2 - built over planned 0.88-1.13 at the rules'
#: sizes and 0.80-0.95 past the minimums on every build over 3,000 elements (it was 0.75-1.15 and 0.71-0.87 with 4.5 and
#: the surface at 1.5; 0.26 rms log error with no surface term, 0.10 now).
PER_H3 = 3.5
#: PLAN-fea-8's VALIDITY MINIMUMS: coarsening a zone never goes past what keeps its elements valid -
#: this many elements through a wall, and per quarter-turn of a round (step 8's VM lab: six coarsened
#: plans put under one element across a 0.1-0.5 mm wall and failed).
KEEP_THROUGH = 1.0
KEEP_PER_QUARTER = 3

#: PLAN-fea-27: THE USER'S ELEMENT COUNT. Every size is scaled until the estimate is within this share of the count, in
#: at most TARGET_STEPS estimates per stage (engines/fea_driver.target_plan): first one factor inside the accuracy
#: minimums, then past them as far as valid elements allow (PLAN-fea-30) - no fixed largest factor stops it. Finer, the
#: factor stays at SCALE_MIN or over.
TARGET_TOLERANCE = 0.05
TARGET_STEPS = 8
SCALE_MIN = 1.0 / 20.0
#: PLAN-fea-28: under the user's count the base size grows to the part's diagonal over this at most - twice
#: PLAN-fea-14's tenth. The base is the size only where no rule asks for finer (walls, curved faces and
#: hot spots keep their own minimums), so this bounds the bulk of a chunky part: measured on the
#: textbook cases (RULES.md PLAN-fea-28).
TARGET_MIN_DIVISIONS = 5
#: PLAN-fea-30: THE VALIDITY LIMITS - what alone stops a size the user asked for (per part, or through an element
#: count past the accuracy minimums): elements the mesher can build valid. Measured (lab ~/feabudget_lab/valid,
#: 5 Nov, gmsh as the FEA adapter runs it, quadratic): flat and bent plates 0.5-2 mm thick built valid at every
#: size tried, up to 16 times the wall - a wall is NOT a validity limit; a 1-2 mm tube failed ("a segment and a
#: facet intersect") at 1.96 and 1.44 elements per quarter-turn of its bore and built at 2.16 and more - so every
#: curved face keeps VALID_PER_QUARTER; and a part keeps VALID_DIVISIONS elements across its diagonal.
VALID_PER_QUARTER = KEEP_PER_QUARTER
VALID_DIVISIONS = 3

QUARTER = math.pi / 2.0
#: zone kinds in plain words (PLAN-fea-8's coarsening says which it touched and which it kept)
KIND_WORDS = {"round": "rounds", "corner": "capped corners", "wall": "walls", "wall_spot": "thin spots",
              "part": "the parts' own sizes", "hole": "holes", "fillet": "fillets", "notch": "notches"}
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
    own = _part_thinnest(record)
    # PLAN-fea-24 and PLAN-fea-25: the cosmetic fillets (no hot spot of their own) and the round faces whose
    # wall reading is only their own edge break (no wall of their own)
    cosmetic_rows = cosmetic_fillets(record)
    cosmetic = {c["spot"] for c in cosmetic_rows}
    cosmetic_faces = {f for c in cosmetic_rows for f in c["faces"]} - {
        str(f) for h in record.get("hot_spots") or [] if h.get("id") not in cosmetic for f in h.get("faces") or []}
    no_wall = {r["face"]: r for r in round_walls(record)}
    face_rows = record.get("faces") or {}

    for h in record.get("hot_spots") or []:
        faces = [str(f) for f in h.get("faces") or []]
        t = _f(h.get("local_thickness_mm"))
        if h.get("id") in cosmetic:
            # PLAN-fea-24: a cosmetic fillet is meshed at the size round it (its face is a micro round below)
            continue
        if h.get("type") == "sharp_corner":
            # a corner whose faces read no wall of their own: the thinnest wall of ITS OWN PART
            # (PLAN-fea-17: every part sized by its own rules), the whole part's only for a single part
            basis = _f(h.get("cap_basis_mm")) or t or own.get(_part_key(faces[0]) if faces else "", thinnest)
            if no_wall and any(f in no_wall for f in faces):
                # PLAN-fea-25: a corner beside a round whose reading was its own edge break is capped by the wall
                # of its other faces (the round's reading is no wall)
                read = [_face_wall(face_rows.get(f) or {}) for f in faces if f not in no_wall]
                rest = [v for v in read if v is not None]
                if rest:
                    basis = min(rest)
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
                    "size_m": size * _MM, "reach_m": HOT_SPOT_REACH * r * _MM, "rule": "PLAN-fea-4",
                    "spot": h.get("id"), "capped": False, "radius_m": r * _MM, "why": why,
                    **({"thickness_m": t * _MM, "wall_elements": STRESS_WALL_ELEMENTS} if t else {})})

    for name in sorted(face_rows, key=_face_sort):
        row = face_rows[name] or {}
        # PLAN-fea-3, THIN ONLY WHERE IT IS THIN: a face thin only in places keeps the wall the rest
        # of it agrees on, and each thin spot is a zone of its own round where its readings are
        t = _f(row.get("thickness_wall_mm")) or _f(row.get("thickness_min_mm"))
        span = no_wall[name]["span_mm"] if name in no_wall else None
        if span is not None and t is not None and t <= span:
            # PLAN-fea-25: the reading across a micro round's own edge break is no wall (the faces it joins
            # carry the wall rule for any real thin material there)
            t = None
        if t is not None:
            out.append({"id": f"W{_face_tag(name)}", "kind": "wall", "faces": [name],
                        "size_m": t / WALL_ELEMENTS * _MM, "reach_m": t * _MM, "rule": "PLAN-fea-3",
                        "thickness_m": t * _MM, "capped": False,
                        "why": f"a {_mm(t)} wall under {name}: {WALL_ELEMENTS} elements through it"})
        for i, sp in enumerate(row.get("thin_spots") or [], start=1):
            ts, rad = _f(sp.get("thickness_mm")), _f(sp.get("radius_mm"))
            if not ts or rad is None or (span is not None and ts <= span):
                continue
            out.append({"id": f"T{_face_tag(name)}_{i}", "kind": "wall_spot", "faces": [name],
                        "size_m": ts / WALL_ELEMENTS * _MM, "reach_m": ts * _MM, "rule": "PLAN-fea-3",
                        "thickness_m": ts * _MM, "capped": False, "radius_m": rad * _MM,
                        "at_m": [float(v) * _MM for v in sp.get("at_mm") or [0.0, 0.0, 0.0]],
                        "why": (f"a {_mm(ts)} thin spot of the wall under {name}, {_mm(rad)} across: "
                                f"{WALL_ELEMENTS} elements through it there")})
        r = _f(row.get("radius_mm"))
        if r is not None and name in cosmetic_faces:
            # PLAN-fea-24: a cosmetic fillet's face keeps its shape the way a micro round does
            continue
        if r is not None and name not in spot_faces and r < ROUND_MICRO * _part_diag(record, name):
            # PLAN-fea-12: A ROUND KEPT ONLY FOR ITS SHAPE that is smaller than ROUND_MICRO of its part is an edge
            # break the elements' curved sides follow, not a shape that needs elements of its own (a 127 mm
            # coil's 0.06 mm rounds asked for 0.024 mm elements, 31.6M of them); named among the micro details
            continue
        if r is not None and name not in spot_faces:
            out.append({"id": f"R{_face_tag(name)}", "kind": "round", "faces": [name],
                        "size_m": QUARTER * r / ROUND_PER_QUARTER * _MM, "reach_m": 0.0,
                        "rule": "PLAN-fea-7", "radius_m": r * _MM, "capped": False,
                        "why": f"a round of radius {_mm(r)} on {name}: {ROUND_PER_QUARTER} elements per quarter-turn"})
    # PLAN-fea-17: EVERY PART OF AN ASSEMBLY HAS ITS OWN BASE SIZE - a tenth of the part, as
    # PLAN-fea-14 gives a single part - so a bolt beside a flange is sized as the bolt it is
    for part in record.get("part_bases") or []:
        d = _f(part.get("diag_mm"))
        faces = [str(f) for f in part.get("faces") or []]
        if d is None or not faces:
            continue
        size = d / MIN_DIVISIONS
        named = f" ({part['name']})" if part.get("name") else ""
        out.append({"id": f"P{part.get('n')}", "kind": "part", "faces": faces, "size_m": size * _MM,
                    "reach_m": 0.0, "rule": "PLAN-fea-17", "capped": False, "part": part.get("id"),
                    "diag_m": d * _MM,
                    "why": f"{part.get('id')}{named} is {_mm(d)} across: its base size is a tenth of it"})
    for z in out:
        k = tighten.get(z["id"])
        if k:
            z["size_m"] = z["size_m"] * float(k)
            z["tightened"] = float(k)
        z["size_m"] = _r(z["size_m"])
        z["reach_m"] = _r(z["reach_m"])
    return out


#: PLAN-fea-12: a round kept only for its shape (PLAN-fea-7) under this share of its part's diagonal is an
#: edge break, not a shape (cad/fea_measure.MICRO_FRACTION: the CAD details recorded as micro)
ROUND_MICRO = 1e-3
#: PLAN-fea-24: A COSMETIC FILLET is a fillet whose radius is under this share of the wall it sits in (and,
#: as every micro round, under ROUND_MICRO of its part). Resolving one would ask for elements under 1/150 of
#: that wall (6 per quarter-turn: 0.26 r), and its stress peak is confined to about one radius of its root -
#: the defeaturing both Ansys Mechanical (defeature size, "remove fillets below") and Abaqus/CAE (Geometry
#: Edit: remove small fillets) apply before meshing for stiffness and the stress away from the feature.
COSMETIC_WALL_SHARE = 0.05
#: PLAN-fea-25: a micro round's wall reading up to this many radii is its own edge break - the ray across the
#: round meets the neighbouring face within about two radii - not a wall
ROUND_WALL_SPAN = 3.0


def cosmetic_fillets(record: Mapping) -> list[dict]:
    """PLAN-fea-24: the fillet hot spots that are cosmetic - radius under COSMETIC_WALL_SHARE of the wall they
    sit in AND under ROUND_MICRO of their part. Holes and notches are never cosmetic (a hole carries load round
    it; a notch is cut on purpose), nor is a fillet with no wall reading."""
    out = []
    for h in record.get("hot_spots") or []:
        if h.get("type") != "fillet":
            continue
        r, t = _f(h.get("radius_mm")), _f(h.get("local_thickness_mm"))
        faces = [str(f) for f in h.get("faces") or []]
        if r is None or t is None or not faces:
            continue
        if r < COSMETIC_WALL_SHARE * t and r < ROUND_MICRO * _part_diag(record, faces[0]):
            out.append({"spot": h.get("id"), "faces": faces, "radius_mm": r, "wall_mm": t})
    return out


def round_walls(record: Mapping) -> list[dict]:
    """PLAN-fea-25: the micro rounds and cosmetic fillets (faces) whose own wall reading is at most
    ROUND_WALL_SPAN of their radius - the gap across their own edge break, not a wall. Each with the span
    (mm) a reading must be over to count as a wall there."""
    rounds = {m["face"]: float(m["radius_mm"]) for m in micro_rounds(record)}
    for c in cosmetic_fillets(record):
        for f in c["faces"]:
            rounds.setdefault(f, float(c["radius_mm"]))
    out = []
    rows = record.get("faces") or {}
    for name in sorted(rounds, key=_face_sort):
        row = rows.get(name) or {}
        span = ROUND_WALL_SPAN * rounds[name]
        readings = [_f(row.get("thickness_wall_mm")) or _f(row.get("thickness_min_mm"))]
        readings += [_f(sp.get("thickness_mm")) for sp in row.get("thin_spots") or []]
        under = [v for v in readings if v is not None and v <= span]
        if under:
            out.append({"face": name, "radius_mm": rounds[name], "span_mm": span, "reading_mm": min(under)})
    return out


#: PLAN-fea-26: a plan whose base size is more than this many times its finest zone is said to be slow to mesh
#: before it meshes. Calibrated on the 240 corpus parts planned on main beside their corpus run (docs/RULES.md
#: PLAN-fea-26): under 100 to 1 the median part meshed at 0.5 ms an element; 100-300, 2.2 ms; over 300, 15-25 ms
#: (and 3 of 3 failed)
SLOW_RATIO = 300.0


def slow_ratio(zone_list: list[dict], base_m: float) -> dict | None:
    """PLAN-fea-26: the plan's size spread (base over finest zone) when it is over SLOW_RATIO, else None."""
    sized = [z for z in zone_list if _f(z.get("size_m"))]
    if not sized or not base_m:
        return None
    fin = min(sized, key=lambda z: float(z["size_m"]))
    ratio = float(base_m) / float(fin["size_m"])
    if ratio <= SLOW_RATIO:
        return None
    return {"ratio": _r(ratio), "base_mm": _r(base_m * 1e3), "finest_mm": _r(float(fin["size_m"]) * 1e3),
            "zone": str(fin["id"])}


def _face_wall(row: Mapping) -> float | None:
    return _f(row.get("thickness_wall_mm")) or _f(row.get("thickness_min_mm"))


def _part_diag(record: Mapping, face: str) -> float:
    """The diagonal (mm) of the part `face` belongs to: its assembly part's, else the part's own."""
    key = _part_key(face)
    for b in record.get("part_bases") or []:
        if b.get("id") == key and _f(b.get("diag_mm")):
            return float(b["diag_mm"])
    return float(_f(record.get("diag_mm")) or 0.0)


def _part_key(face: str) -> str:
    """Part_k of an assembly's Part_k_Face_j; "" for a single part's Face_j."""
    head, sep, _tail = str(face).rpartition("_Face_")
    return head if sep else ""


def _part_thinnest(record: Mapping) -> dict[str, float]:
    """An assembly's thinnest measured wall, part by part (Part_k -> mm); empty for a single part."""
    if not record.get("part_bases"):
        return {}
    out: dict[str, float] = {}
    for name, row in (record.get("faces") or {}).items():
        t = _f((row or {}).get("thickness_min_mm"))
        key = _part_key(name)
        if t is not None and key:
            out[key] = min(out.get(key, t), t)
    return out


def _face_order(name: str) -> int:
    try:
        return int(str(name).rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _face_tag(name: str) -> str:
    """A face zone's id tail: "9" for a single part's Face_9, "2.9" for an assembly's Part_2_Face_9 - every
    part numbers its faces from 1, so the part is in the id (FEA hunt, 27 Oct: 44 of 46 assembly plans gave two
    zones one id, and a lever or an AI choice naming "W9" took both parts' walls)."""
    part = _part_key(name)
    j = _face_order(name)
    return f"{part.rsplit('_', 1)[-1]}.{j}" if part else str(j)


def _face_sort(name: str) -> tuple[int, int]:
    """Faces part by part, then by number (a single part's faces are all part 0)."""
    part = _part_key(name)
    try:
        k = int(part.rsplit("_", 1)[-1]) if part else 0
    except ValueError:
        k = 0
    return k, _face_order(name)


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


def floored(zone_list: list[dict], floor_m: float, through: float = KEEP_THROUGH,
            kinds: tuple[str, ...] | None = None) -> list[dict]:
    """PLAN-fea-8's coarsening: every zone finer than `floor_m` meshed at `floor_m` instead - but
    never coarser than its validity minimum (keep_size) - the size its rule asked for kept
    (`rule_size_m`) so what it leaves short can be named. `kinds`: only zones of these kinds."""
    out = []
    for z in zone_list:
        size = float(z["size_m"])
        if size < floor_m and (kinds is None or z["kind"] in kinds):
            keep = keep_size(z, through)
            new = float(floor_m) if keep is None else max(size, min(float(floor_m), keep))
            if new > size:
                out.append({**z, "rule_size_m": size, "size_m": new, "short": True,
                            **({"kept_valid": True} if keep is not None and new < floor_m else {})})
                continue
        out.append(dict(z))
    return out


def base_size(diag_m: float, scale: float = 1.0, beyond: float = 1.0) -> float:
    """The base size: PLAN-fea-14's tenth of the part, times PLAN-fea-27's factor, never coarser than
    PLAN-fea-28's fifth of the part; past the accuracy minimums (`beyond` > 1, PLAN-fea-30) times that too,
    never coarser than VALID_DIVISIONS across the part."""
    base = float(diag_m) / MIN_DIVISIONS
    if scale == 1.0 and beyond == 1.0:
        return base
    base = min(base * float(scale), float(diag_m) / TARGET_MIN_DIVISIONS)
    if beyond > 1.0:
        base = max(base, min(base * float(beyond), float(diag_m) / VALID_DIVISIONS))
    return base


def valid_cap(z: Mapping) -> tuple[float, str] | None:
    """PLAN-fea-30: the coarsest a zone may be and still build valid, and why in plain words; None for no limit
    (a wall is none: plates built valid at 16 times their wall)."""
    kind = z.get("kind")
    r = _f(z.get("radius_m"))
    if kind in ("hole", "fillet", "notch", "round") and r:
        return QUARTER * r / VALID_PER_QUARTER, "curve"
    if kind == "part" and _f(z.get("diag_m")):
        return float(z["diag_m"]) / VALID_DIVISIONS, "part"
    return None


def _accuracy(z: Mapping, row: dict, new: float) -> None:
    """What a zone made coarser than its rule's size gives, for the mesh check (so a zone coarser by the user's
    choice is not "short" and tightened again, LAD-fea-3) and for the words: per quarter-turn and through the wall
    of a hot spot (`below_accuracy` past PLAN-fea-4's 6 and 3), through a wall (`crossed` past PLAN-fea-3's 2),
    per quarter-turn of a round (`crossed` past PLAN-fea-7's 4)."""
    size = float(z["size_m"])
    if new <= size:
        return
    r, t = _f(z.get("radius_m")), _f(z.get("thickness_m"))
    kind = z.get("kind")
    if kind in ("hole", "fillet", "notch"):
        if r:
            got = QUARTER * r / new
            row["per_quarter"] = round(min(float(z.get("per_quarter") or HOT_SPOT_PER_QUARTER), got), 3)
            if got < HOT_SPOT_PER_QUARTER * (1 - 1e-6):
                row["below_accuracy"] = True
            if got < ROUND_PER_QUARTER * (1 - 1e-6):
                row["crossed"] = "PLAN-fea-7"
        if t:
            got_t = t / new
            row["wall_elements"] = round(min(float(z.get("wall_elements") or STRESS_WALL_ELEMENTS), got_t), 3)
            if got_t < STRESS_WALL_ELEMENTS * (1 - 1e-6):
                row["below_accuracy"] = True
            if got_t < WALL_ELEMENTS * (1 - 1e-6):
                row["crossed"] = row.get("crossed") or "PLAN-fea-3"
    elif kind in ("wall", "wall_spot") and t:
        got_t = t / new
        if z.get("wall_elements") or got_t < WALL_ELEMENTS * (1 - 1e-6):
            row["wall_elements"] = round(min(float(z.get("wall_elements") or WALL_ELEMENTS), got_t), 3)
        if got_t < WALL_ELEMENTS * (1 - 1e-6):
            row["crossed"] = "PLAN-fea-3"
    elif kind == "round" and r and QUARTER * r / new < ROUND_PER_QUARTER * (1 - 1e-6):
        row["crossed"] = "PLAN-fea-7"
    elif kind == "corner" and t and new > t / WALL_ELEMENTS * (1 + 1e-6):
        row["crossed"] = "PLAN-fea-5"


def scale_cap(z: Mapping) -> tuple[float, str] | None:
    """PLAN-fea-27: the coarsest a zone may be scaled to and keep the hard minimums, and the rule that sets it:
    2 elements through its wall (PLAN-fea-3; a sharp corner's wall too), 4 per quarter-turn of a curved
    face (PLAN-fea-7; a hole, fillet or notch is one), a part's own base at a fifth of the part
    (PLAN-fea-28). None for no limit."""
    kind = z.get("kind")
    caps: list[tuple[float, str]] = []
    t, r = _f(z.get("thickness_m")), _f(z.get("radius_m"))
    if kind in ("wall", "wall_spot", "corner", "hole", "fillet", "notch") and t:
        caps.append((t / WALL_ELEMENTS, "PLAN-fea-3"))
    if kind in ("hole", "fillet", "notch", "round") and r:
        caps.append((QUARTER * r / ROUND_PER_QUARTER, "PLAN-fea-7"))
    if kind == "part":
        d = _f(z.get("diag_m")) or float(z["size_m"]) * MIN_DIVISIONS
        caps.append((d / TARGET_MIN_DIVISIONS, "PLAN-fea-28"))
    return min(caps) if caps else None


def scaled(zone_list: list[dict], scale: float, beyond: float = 1.0) -> list[dict]:
    """PLAN-fea-27: every zone's size times `scale`, the hard minimums re-applied: coarser never past
    scale_cap (and never finer than the zone already was); finer, a capped sharp corner keeps its cap
    (PLAN-fea-5: refining it buys nothing). Each zone keeps the size the rules gave it (`unscaled_m`);
    one a minimum held is marked with that rule (`held`); a hot spot made coarser than PLAN-fea-4's
    accuracy size is marked `below_accuracy`, and the counts the mesh is checked against become what its
    scaled size gives (`per_quarter`, `wall_elements`), so a zone coarser by the user's choice is not
    "short" and tightened again (LAD-fea-3)."""
    s, u = float(scale), float(beyond)
    if s == 1.0 and u == 1.0:
        return [dict(z) for z in zone_list]
    out = []
    for z in zone_list:
        size = float(z["size_m"])
        if z.get("user"):
            # PLAN-fea-29: a part the user sized keeps the sizes they asked for; the count moves the rest
            out.append(dict(z))
            continue
        want = size * s
        new, held = want, ""
        if s > 1.0:
            cap = scale_cap(z)
            if cap is not None and cap[0] < want:
                new, held = max(size, cap[0]), cap[1]
        elif z.get("kind") == "corner" and z.get("capped"):
            new, held = size, "PLAN-fea-5"
        row = {**z, "unscaled_m": size}
        if u > 1.0:
            # PLAN-fea-30: PAST THE ACCURACY MINIMUMS, as far as valid elements allow
            vc = valid_cap(z)
            far = new * u if vc is None else max(new, min(new * u, vc[0]))
            if vc is not None and vc[0] < new * u:
                row["valid_held"] = vc[1]
            if far > new:
                row.pop("held", None)
                held = ""
                new = far
        row["size_m"] = _r(new)
        if held:
            row["held"] = held
        _accuracy(z, row, new)
        out.append(row)
    return out


def user_sized(zone_list: list[dict], sizes: Mapping[str, Mapping]) -> list[dict]:
    """PLAN-fea-29: THE SIZES THE USER ASKED FOR, part by part (part id -> {"min_m", "max_m"}; a single part is
    Part_1). In such a part every size - its own base (the middle of the range) and every zone in it: walls,
    curves, hot spots, corners, the AI's refinements - is held inside [min, max]: a rule asking finer than min
    stops at min, unless the elements there would not build valid (PLAN-fea-30: a curve keeps 3 per
    quarter-turn), and then it is held at that limit and named (`valid_held`). What that leaves short of a rule
    is marked for the words (`crossed`, `below_accuracy`); the mesh check is held to what the size gives, so the
    ladder does not tighten it back."""
    if not sizes:
        return [dict(z) for z in zone_list]
    # NOTE-6 / PLAN-fea-32: AN AREA INSIDE A PART THE USER SIZED (a feature id with its faces) wins over its part on
    # its faces: every zone on them is held inside the area's range, and the area gets a zone of its own
    areas = {k: v for k, v in sizes.items() if v.get("faces")}
    area_of = {str(f): k for k, v in areas.items() for f in v["faces"]}
    out = []
    for z in zone_list:
        on = [area_of[str(f)] for f in z.get("faces") or [] if str(f) in area_of] if z.get("kind") != "part" else []
        key = on[0] if on else (str(z.get("part") or "") if z.get("kind") == "part"
                                else (_part_key((z.get("faces") or [""])[0]) or "Part_1"))
        want = sizes.get(key)
        if not want:
            out.append(dict(z))
            continue
        lo, hi = float(want["min_m"]), float(want["max_m"])
        mid = 0.5 * (lo + hi)
        size = float(z["size_m"])
        row = {**z, "user": key, "user_min_m": lo, "user_max_m": hi, "rule_size_m": size}
        if z.get("kind") == "part":
            new = mid
        else:
            new = min(max(size, lo), mid)
            vc = valid_cap(z)
            if vc is not None and vc[0] < new:
                new = max(size, vc[0])
                row["valid_held"] = vc[1]
        row["size_m"] = _r(new)
        _accuracy(z, row, new)
        out.append(row)
    for k, v in areas.items():
        lo, hi = float(v["min_m"]), float(v["max_m"])
        mid = 0.5 * (lo + hi)
        out.append({"id": f"U{str(k).rsplit('_', 1)[-1]}", "kind": "area", "faces": [str(f) for f in v["faces"]],
                    "size_m": _r(mid), "reach_m": _r(AREA_REACH * mid), "user": k, "user_min_m": lo,
                    "user_max_m": hi, "rule_size_m": _r(mid), "rule": "PLAN-fea-32"})
    return out


#: PLAN-fea-32: how far an area's own size holds off its faces, in that size (then the sizes grow by PLAN-fea-6)
AREA_REACH = 3.0


def user_base(sizes: Mapping[str, Mapping]) -> float | None:
    """PLAN-fea-29: the base the user's sizes ask for - the middle of the coarsest range (the base is the
    ceiling; each part's own base holds inside it). An area inside a part (NOTE-6) sets no base."""
    mids = [0.5 * (float(v["min_m"]) + float(v["max_m"])) for v in (sizes or {}).values() if not v.get("faces")]
    return max(mids) if mids else None


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
           levers: list[dict] | None = None, growth: float = GROWTH, scale: float = 1.0, beyond: float = 1.0,
           base_m: float | None = None) -> dict:
    """The whole plan, from the zones and their histogram (see the module text): the base size is
    the coarsest the part allows (PLAN-fea-14; times PLAN-fea-27's `scale` under the user's element
    count, the zones scaled already), and the plan fits when its estimate is within LIMIT_AIM of
    `limit` (PLAN-fea-8)."""
    # PLAN-fea-29: the base the user's sizes set (a single part's middle; an assembly's coarsest part), when larger
    base = base_size(diag_m, scale, beyond)
    if base_m:
        base = float(base_m) if not record.get("part_bases") else max(base, float(base_m))
    est = elements(hist, base, PER_H3)
    # PLAN-fea-8: a count that ran out of cubes after counting further (engines/fea_driver._histogram) is a
    # floor; under the limit it fits - a plan short of its machine steps up when it runs (RUN-3)
    fits = (hist.complete or getattr(hist, "stopped", "") == "cubes") and est <= LIMIT_AIM * float(limit)
    plan: dict = {
        "version": VERSION,
        "element": {"family": "tetrahedra", "order": 2, "solid_only": True},
        "base_size_m": _r(base), "growth": float(growth), "per_h3": PER_H3,
        "zones": zone_list, "histogram": hist.as_list(), "histogram_complete": hist.complete,
        "estimate": {"elements": int(round(est)), **({} if hist.complete else {"at_least": True})},
        "limit": {"elements": int(limit)},
        "fits": bool(fits), "levers": list(levers or []),
        **({"scale": float(scale)} if scale != 1.0 else {}),
        **({"beyond": float(beyond)} if beyond != 1.0 else {}),
        # PLAN-fea-31: the thin walls' surface rows the estimate reads at coarse sizes
        **({"histogram_surface": hist.surface_list()} if getattr(hist, "surface", None) else {}),
        # PLAN-fea-10: the cusps the adapter sets aside (metres, the mesh's unit)
        "cusps": [{"at_m": [_r(float(v) * _MM) for v in c["at_mm"]], "faces": list(c.get("faces") or []),
                   "angle_deg": c.get("angle_deg")} for c in record.get("cusps") or []],
    }
    # PLAN-fea-17: an assembly's parts, its staged file and how they are joined (the driver's)
    if record.get("assembly_plan"):
        plan["assembly"] = dict(record["assembly_plan"])
    # PLAN-fea-12: the micro details far below the finest element this plan asks for
    finest_mm = min([float(z["size_m"]) for z in zone_list] + [float(base)]) / _MM
    plan["micro"] = [{"kind": m.get("kind"), "size_mm": m.get("size_mm"), "faces": list(m.get("faces") or []),
                      "points_m": [[_r(float(v) * _MM) for v in p] for p in m.get("points_mm") or []]}
                     for m in record.get("micro") or []
                     if float(m.get("size_mm") or 0.0) < MICRO_KEEP * finest_mm]
    rounds = micro_rounds(record)
    if rounds:
        plan["micro_rounds"] = rounds          # PLAN-fea-12 (absent when there is none: the plan is as before)
    cosmetic = cosmetic_fillets(record)
    if cosmetic:
        plan["cosmetic"] = cosmetic            # PLAN-fea-24 (absent when there is none)
    edge_breaks = round_walls(record)
    if edge_breaks:
        plan["round_walls"] = edge_breaks      # PLAN-fea-25 (absent when there is none)
    slow = slow_ratio(zone_list, float(base))
    if slow:
        plan["slow"] = slow                    # PLAN-fea-26 (absent when the sizes are not that far apart)
    plan["words"] = words(record, plan)
    plan["decisions"] = decisions(record, plan)
    return plan


def micro_rounds(record: Mapping) -> list[dict]:
    """PLAN-fea-12: the rounds kept only for their shape that are under ROUND_MICRO of their part - no zone of
    their own (zones() leaves them out), named."""
    # the faces of the holes, fillets and notches (a cosmetic fillet's too: PLAN-fea-24 names those itself);
    # a sharp corner's faces may be micro rounds, as zones() reads them
    spot = {str(f) for h in record.get("hot_spots") or []
            if h.get("type") != "sharp_corner" for f in h.get("faces") or []}
    out = []
    for name, row in sorted((record.get("faces") or {}).items(), key=lambda kv: _face_order(kv[0])):
        r = _f((row or {}).get("radius_mm"))
        if r is not None and name not in spot and r < ROUND_MICRO * _part_diag(record, name):
            out.append({"face": name, "radius_mm": r})
    return out


def words(record: Mapping, plan: Mapping) -> list[str]:
    """The plan in plain words (design 3.8: what was refined and why, where corners were capped)."""
    zs = plan["zones"]
    est = plan["estimate"]["elements"]
    limit = int((plan.get("limit") or {}).get("elements") or ELEMENT_LIMIT)
    scale = float(plan.get("scale") or 1.0)
    if float(plan.get("beyond") or 1.0) > 1.0:
        first = (f"The mesh sized to your element count: the base element size is {plan['base_size_m'] * 1e3:.3g} mm, "
                 "and every size was made coarser to reach the count (what that costs is said below).")
    elif scale != 1.0:
        first = (f"The mesh sized to your element count: the base element size is {plan['base_size_m'] * 1e3:.3g} mm "
                 f"(a tenth of the part, times {scale:.3g}), and every rule below makes it finer where it asks to, "
                 "scaled by the same factor.")
    else:
        first = (f"The mesh the rules recommend: the base element size is {plan['base_size_m'] * 1e3:.3g} mm (a tenth "
                 "of the part), and every rule below makes it finer where it asks to.")
    out = [first, "Quadratic tetrahedra (10-node), a solid mesh; every CAD face is a named group."]
    out.extend(target_words(plan))
    out.extend(user_words(plan))
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
    lying = [list(c.get("faces") or []) for c in record.get("coincident") or []]
    if lying:
        named = "; ".join(" and ".join(f) for f in lying[:6]) + (f" and {len(lying) - 6} more" if len(lying) > 6 else "")
        out.append(f"Faces lying on each other: {len(lying)} pair(s) of faces of one part lie on top of each other "
                   f"with no material between them ({named}) - a skin of no thickness in the CAD. They set no wall "
                   "size: the wall there is read past them.")
    small = plan.get("micro_rounds") or []
    if small:
        out.append(f"{len(small)} round face(s) far smaller than their part (the smallest of radius "
                   f"{min(float(m['radius_mm']) for m in small):.3g} mm) are edge breaks the elements' curved sides "
                   "follow; they get no elements of their own.")
    cosmetic = plan.get("cosmetic") or []
    if cosmetic:
        c = min(cosmetic, key=lambda x: x["radius_mm"])
        out.append(f"Cosmetic fillets: {len(cosmetic)} fillet(s) far smaller than the wall they sit in (the smallest, "
                   f"radius {c['radius_mm']:.3g} mm on {c['faces'][0]} in a {c['wall_mm']:.3g} mm wall) are meshed at "
                   "the size around them, not refined: they do not change the part's stiffness or the stress away "
                   "from them. The stress right at them is not resolved - it will read lower than the true peak. If "
                   "a peak there matters (fatigue, a crack starting there), ask for those fillets to be refined.")
    edge_breaks = plan.get("round_walls") or []
    if edge_breaks:
        e = min(edge_breaks, key=lambda x: x["reading_mm"])
        out.append(f"{len(edge_breaks)} small round face(s) read a 'wall' only across their own edge break (the "
                   f"thinnest, {e['reading_mm']:.3g} mm on {e['face']}, of radius {e['radius_mm']:.3g} mm). That is "
                   "no wall, so it sets no size; the faces beside each round still keep at least "
                   f"{WALL_ELEMENTS} elements through any real wall there.")
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
        floors = coarse.get("floors_m") or {}
        if floors:
            done = "; ".join(f"{KIND_WORDS.get(k, k)} to {float(v) * 1e3:.3g} mm" for k, v in floors.items())
            kept = [KIND_WORDS.get(k, k) for k in coarse.get("kept") or []]
            out.append(f"The rules need {coarse['needed_words']}, more than the {limit:,}-element limit allows: the "
                       f"low-stress zones were made coarser first, each only as far as it had to be ({done}), never "
                       "past 1 element through any wall or 3 per quarter-turn of any round"
                       + (f"; {', '.join(kept)} kept the sizes their rules ask for" if kept else "")
                       + ". What that leaves short: " + ("; ".join(plan.get("resolution_short") or []) or "nothing")
                       + ".")
        else:
            out.append(f"The rules need {coarse['needed_words']}, more than the {limit:,}-element limit allows: the "
                       f"finest zones were made coarser - to {float(coarse['zone_floor_m']) * 1e3:.3g} mm - until it "
                       "fits, never past 1 element through any wall or 3 per quarter-turn of any round. What that "
                       "leaves short: " + "; ".join(plan.get("resolution_short") or []) + ".")
        if coarse.get("at_minimums"):
            out.append("Even with every zone at those minimums the part needs more than the limit: meshed so "
                       "anyway, as the least resolution that keeps every element valid.")
        if coarse.get("over_memory"):
            out.append("That least valid mesh may be more than the mesh job's memory holds; it is meshed anyway, "
                       "and if the memory runs out the result says so.")
    out.append(f"Estimated before meshing: about {est:,} elements (the limit is {limit:,}).")
    slow = plan.get("slow") or {}
    if slow:
        out.append(f"Slow to mesh: element sizes here run from {slow['base_mm']:.3g} mm down to {slow['finest_mm']:.3g} mm "
                   f"({slow['zone']}, {slow['ratio']:,.0f} to 1). Sizes that far apart make the surface stage slow on "
                   "every face the small zones touch, so this run may take a long time.")
    for lever in plan.get("levers") or []:
        out.append(str(lever.get("words") or ""))
    return [w for w in out if w]


#: the zone kinds a held minimum is said for, in plain words, with what it keeps
_HELD_WORDS = {"PLAN-fea-3": f"{WALL_ELEMENTS} elements through their wall",
               "PLAN-fea-7": f"{ROUND_PER_QUARTER} elements per quarter-turn of their curve",
               "PLAN-fea-5": "their cap at a quarter of the wall beside them",
               "PLAN-fea-28": "their base at a fifth of their part"}


def _n(v) -> str:
    """An element count in plain words: 1.04M, 640k, 9,500."""
    x = float(v)
    if x >= 1e9:
        return f"{x / 1e9:.3g} billion"
    if x >= 1e6:
        return f"{x / 1e6:.3g}M"
    if x >= 1e4:
        return f"{x / 1e3:.3g}k"
    return f"{int(round(x)):,}"


def held_words(z: Mapping) -> str:
    """The minimum that held one zone, where it is: 'the 4 mm wall under Face_7'."""
    where = (z.get("faces") or [z.get("id")])[0]
    if z.get("held") == "PLAN-fea-3" and z.get("thickness_m"):
        return f"{WALL_ELEMENTS} elements through the {float(z['thickness_m']) * 1e3:.3g} mm wall under {where}"
    if z.get("held") == "PLAN-fea-7" and z.get("radius_m"):
        return (f"{ROUND_PER_QUARTER} elements per quarter-turn of the radius "
                f"{float(z['radius_m']) * 1e3:.3g} mm curve of {where}")
    return f"{_HELD_WORDS.get(str(z.get('held')), 'its minimum')} ({where})"


def held_zones(zone_list: list[dict], base_m: float) -> list[dict]:
    """The zones a minimum held under PLAN-fea-27 that still size the mesh (finer than the base)."""
    return [z for z in zone_list if z.get("held") and float(z["size_m"]) < float(base_m)]


def binding(zone_list: list[dict], base_m: float = math.inf) -> dict | None:
    """The held zone a lower count would break first: the thinnest held wall, else the smallest held
    curve, else any held zone (PLAN-fea-27's words)."""
    held = [z for z in held_zones(zone_list, base_m) if z.get("held") != "PLAN-fea-5"]
    walls = [z for z in held if z["held"] == "PLAN-fea-3" and z.get("thickness_m")]
    if walls:
        return min(walls, key=lambda z: float(z["thickness_m"]))
    curves = [z for z in held if z["held"] == "PLAN-fea-7" and z.get("radius_m")]
    if curves:
        return min(curves, key=lambda z: float(z["radius_m"]))
    return held[0] if held else None


#: AI-fea-4: the AI step that reads the notes gave no answer (and no saved one): a count in them is not applied
UNREAD_WORDS = ("If your notes asked for an element count, it was not applied this run: the AI step that reads "
                "your notes did not answer, so the mesh is the rules' own plan.")
UNREAD_REMARK = ("any element count you asked for was not applied this run: the AI step that reads your notes did "
                 "not answer, so the mesh is the rules' own plan.")


def asked_words(t: Mapping) -> str:
    """The count the user named, as they asked it: "about 1M", "between 700k and 1.5M", "at most 300k"."""
    if t.get("mode") == "range" and t.get("low") is not None:
        return f"between {_n(t['low'])} and {_n(t['high'])}"
    said = {"at_most": "at most", "at_least": "at least"}.get(str(t.get("mode")), "about")
    return f"{said} {_n(t['asked'])}"


def _thinnest(zs: list[dict], kinds: tuple[str, ...]) -> dict | None:
    sel = [z for z in zs if z.get("kind") in kinds and _f(z.get("thickness_m"))]
    return min(sel, key=lambda z: float(z["thickness_m"])) if sel else None


def crossed_words(zs: list[dict], *, why: str) -> list[str]:
    """The accuracy minimums a choice of the user's crossed, where, and what it costs, in plain words (no rule ids):
    walls under 2 elements through, curves under 4 per quarter-turn, hot spots under the stress size, corners
    coarser than their cap - and the curves a validity limit held. `why`: what crossed them ("your count", "your
    sizes")."""
    out: list[str] = []
    walls = [z for z in zs if z.get("crossed") == "PLAN-fea-3" and z.get("kind") in ("wall", "wall_spot")]
    if walls:
        w = _thinnest(walls, ("wall", "wall_spot")) or walls[0]
        got = float(w["thickness_m"]) / float(w["size_m"])
        out.append(f"Because of {why}, {len(walls)} wall(s) get fewer than {WALL_ELEMENTS} elements through them; the "
                   f"thinnest, {float(w['thickness_m']) * 1e3:.3g} mm under {(w.get('faces') or [w['id']])[0]}, gets about "
                   f"{got:.2g} across, so bending stress in those walls is less accurate.")
    curves = [z for z in zs if z.get("crossed") == "PLAN-fea-7"]
    if curves:
        c = min(curves, key=lambda z: QUARTER * float(z.get("radius_m") or 0.0) / float(z["size_m"]))
        got = QUARTER * float(c.get("radius_m") or 0.0) / float(c["size_m"])
        out.append(f"Because of {why}, {len(curves)} curved face(s) get fewer than {ROUND_PER_QUARTER} elements per "
                   f"quarter-turn (the coarsest about {got:.2g}, on {(c.get('faces') or [c['id']])[0]}), so their shape "
                   "is followed less closely and the stress on them is less accurate.")
    low = [z for z in zs if z.get("below_accuracy")]
    if low:
        worst = min(low, key=lambda z: float(z.get("per_quarter") or HOT_SPOT_PER_QUARTER))
        names = ", ".join(str(z.get("spot") or z["id"]) for z in low[:6]) + (f" and {len(low) - 6} more" if len(low) > 6 else "")
        out.append(f"{len(low)} hot spot(s) ({names}) are coarser than the size their peak stress needs "
                   f"({HOT_SPOT_PER_QUARTER} per quarter-turn and {STRESS_WALL_ELEMENTS} through the wall beside it; the "
                   f"coarsest has about {float(worst.get('per_quarter') or HOT_SPOT_PER_QUARTER):.2g} per quarter-turn): "
                   "the peak stress there is less accurate.")
    corners = [z for z in zs if z.get("crossed") == "PLAN-fea-5"]
    if corners:
        out.append(f"{len(corners)} sharp inside corner(s) are coarser than a quarter of the wall beside them.")
    held = [z for z in zs if z.get("valid_held") == "curve" and (z.get("user") or z.get("crossed")
                                                                   or float(z["size_m"]) < float(z.get("user_min_m") or 0))]
    if held:
        c = min(held, key=lambda z: float(z.get("radius_m") or 0.0))
        out.append(f"{len(held)} small curve(s) were kept finer than asked - at {VALID_PER_QUARTER} elements per "
                   f"quarter-turn - because coarser elements there cannot be built valid (the smallest, radius "
                   f"{float(c.get('radius_m') or 0.0) * 1e3:.3g} mm on {(c.get('faces') or [c['id']])[0]}, gets "
                   f"{float(c['size_m']) * 1e3:.3g} mm elements).")
    return out


def user_words(plan: Mapping) -> list[str]:
    """PLAN-fea-29 in plain words: each part the user sized, what it got, and what that leaves short."""
    sizes = plan.get("user_sizes") or {}
    if not sizes:
        return []
    zs = list(plan.get("zones") or [])
    out: list[str] = []
    by: dict[tuple[float, float], list[str]] = {}
    for pid, v in sizes.items():
        by.setdefault((float(v["min_m"]), float(v["max_m"])), []).append(pid)
    for (lo, hi), ids in sorted(by.items()):
        own = [z for z in zs if z.get("user") in ids]
        finest = min([float(z["size_m"]) for z in own if z.get("kind") != "part"] or [0.5 * (lo + hi)])
        rng = f"{lo * 1e3:.3g} mm" if abs(hi - lo) < 1e-12 else f"{lo * 1e3:.3g}-{hi * 1e3:.3g} mm"
        said = [str(sizes[i].get("name") or i) for i in ids[:8]]
        line = (f"You asked for {rng} elements on {', '.join(said)}{' and more' if len(ids) > 8 else ''}: "
                f"they are meshed at {0.5 * (lo + hi) * 1e3:.3g} mm, and nothing in them finer than "
                f"{max(finest, 0.0) * 1e3:.3g} mm")
        line += (" - finer than you asked only where coarser elements cannot be built valid." if finest < lo * (1 - 1e-6)
                 else ".")
        out.append(line)
    user_zones = [z for z in zs if z.get("user")]
    out.extend(crossed_words(user_zones, why="the sizes you asked for"))
    out.append("Where a part you sized meets another, the sizes blend over a short distance, so a few elements "
               "there are finer.")
    return out


def target_words(plan: Mapping) -> list[str]:
    """PLAN-fea-27/30 in plain words: what the user asked for, what was planned, how (inside the accuracy minimums,
    past them, or at the validity limits), and where the stress is now less accurate."""
    t = plan.get("target") or {}
    if not t and plan.get("target_unread"):
        return [UNREAD_WORDS]
    if not t:
        m = plan.get("target_missed") or {}
        if m:
            return [f"You asked for about {_n(m['asked'])} elements; even with every size at its smallest the plan is "
                    "more than the largest machine allowed holds, so it is made coarser as any plan over its limit is "
                    "(below)."]
        return []
    said = asked_words(t)
    src = " (read from your notes by the AI)" if t.get("source") == "ai" else ""
    out: list[str] = []
    est = int((plan.get("estimate") or {}).get("elements") or 0)
    rules = int(t.get("rules_elements") or 0)
    s = float(t.get("scale") or 1.0)
    u = float(t.get("beyond") or 1.0)
    if t.get("kept_rules"):
        return [f"You asked for {said} elements{src}; the rules' own plan, about {_n(rules)}, already meets that, so "
                "it is meshed as the rules recommend."]
    if t.get("machine"):
        out.append(f"You asked for {said} elements{src}: more than the largest machine allowed holds, so the plan "
                   f"aims at {_n(t['aim'])}, the most it fits.")
    if t.get("sizes_first"):
        out.append("Your sizes per part come first; the element count only moved the parts you did not size.")
    inside = " - inside your range." if t.get("mode") == "range" and t.get("hit") else "."
    if abs(s - 1.0) < 1e-9 and u <= 1.0:
        out.append(f"You asked for {said} elements{src}; the rules' own plan is about {_n(rules)}, close enough, so "
                   "every size stays as the rules give it.")
    elif s < 1.0:
        out.append(f"You asked for {said} elements{src}; the rules alone plan about {_n(rules)}. Every size was made "
                   f"{1 / s:.3g} times finer: planned about {_n(est)}{inside}")
    elif u <= 1.0:
        out.append(f"You asked for {said} elements{src}; the rules alone plan about {_n(rules)}. Every size was made "
                   f"{s:.3g} times coarser, keeping {WALL_ELEMENTS} elements through every wall and {ROUND_PER_QUARTER} "
                   f"per quarter-turn of every curve: planned about {_n(est)}{inside}")
    else:
        out.append(f"You asked for {said} elements{src}; the rules alone plan about {_n(rules)}. To get near your "
                   f"count every size was made coarser, past the sizes accuracy asks for (below): planned about "
                   f"{_n(est)}{inside}")
    built = int(t.get("built") or 0)
    if built:
        out.append(count_line(t, est, built)[:1].upper() + count_line(t, est, built)[1:])
    if t.get("lowest") and not (built and built <= float(t["asked"]) * 1.1):
        out.append(f"The lowest this part can go is about {_n(built or est)} elements: every element is already as large as it "
                   f"can be and still build valid - every curve keeps {VALID_PER_QUARTER} elements per quarter-turn and "
                   f"every part {VALID_DIVISIONS} elements across.")
    elif (not t.get("hit") and not t.get("machine") and not t.get("lowest")
          and not (built and abs(built / float(t["asked"]) - 1.0) <= 0.1)):
        out.append(f"That is as close to your count as the search got (it aims within "
                   f"{int(TARGET_TOLERANCE * 100)}%).")
    zs = list(plan.get("zones") or [])
    base = float(plan.get("base_size_m") or 0.0)
    spots = [z for z in zs if z.get("kind") in ("hole", "fillet", "notch")]
    if spots and base and u <= 1.0:
        f = min(spots, key=lambda z: float(z["size_m"]))
        ratio = base / float(f["size_m"])
        if ratio > 1.05:
            out.append(f"Fine areas stay finer than the rest: the finest hot spot ({f.get('spot') or f['id']}) is "
                       f"{ratio:.3g} times finer than the base size.")
    if t.get("base_capped"):
        out.append(f"The base size stops at a fifth of the part ({base * 1e3:.3g} mm), so the part keeps its shape.")
    out.extend(crossed_words([z for z in zs if not z.get("user")], why="the element count you asked for"))
    return out


def count_line(t: Mapping, planned: int, built: int | None = None) -> str:
    """PLAN-fea-27's one honest line: asked, planned, built - and whether the count was reached."""
    said = asked_words(t)
    if t.get("kept_rules"):
        line = f"you asked for {said} elements; the rules' own plan already meets it: planned about {_n(planned)}"
    else:
        line = f"you asked for {said} elements; planned about {_n(planned)}"
    if built is not None:
        line += f", built {built:,}"
        got = float(built)
    else:
        got = float(planned)
    lo = float(t.get("low") or 0) if t.get("mode") == "range" else float(t["asked"]) / 1.1
    hi = float(t.get("high") or 0) if t.get("mode") == "range" else float(t["asked"]) * 1.1
    if t.get("mode") == "at_most":
        reached = got <= float(t["asked"]) * 1.1
    elif t.get("mode") == "at_least":
        reached = got >= float(t["asked"]) / 1.1
    else:
        reached = lo <= got <= hi
    if t.get("kept_rules") or reached or (t.get("lowest") and got <= float(t["asked"]) * 1.1):
        return line + "."
    if t.get("lowest"):
        return line + (" - not reached: that is the lowest this part can go with every element still valid.")
    if t.get("machine"):
        return line + " - not reached: that is the most the largest machine holds."
    return line + f" - not reached: {'more' if got > float(t['asked']) else 'fewer'} than asked."


def target_remark(plan: Mapping, built: int | None = None) -> str:
    """PLAN-fea-27's one line for the result ("From your notes: ..."), '' without a count."""
    t = plan.get("target") or {}
    m = plan.get("target_missed") or {}
    if plan.get("target_unread") and not t:
        return UNREAD_REMARK
    if m and not t:
        return (f"you asked for about {_n(m['asked'])} elements; even at every minimum the plan was over the largest "
                "machine, so it was made coarser as any plan over its limit is"
                + (f"; built {built:,}." if built is not None else "."))
    if not t:
        return ""
    est = int((plan.get("estimate") or {}).get("elements") or 0)
    out = count_line(t, est, built)
    if t.get("sizes_first"):
        out += " Your sizes per part came first; the count moved only the parts you did not size."
    return out


def assembly_words(asm: Mapping) -> list[str]:
    """PLAN-fea-17 in plain words: the parts, how they are joined, and the notes (an empty list for
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
    raw = asm.get("joint_counts")
    counts: Mapping = raw if isinstance(raw, Mapping) else {}
    kinds = [t for t in ("bonded", "contact", "off") if counts.get(t)]
    if len(kinds) > 1 or counts.get("edited"):
        # PLAN-fea-19: the pairs are not all joined one way - each as its own choice says
        out.append(f"Each pair as chosen: {counts.get('bonded', 0)} bonded, {counts.get('contact', 0)} "
                   f"contact, {counts.get('off', 0)} off" + (f", {counts['edited']} of them set on the stage."
                                                              if counts.get("edited") else "."))
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
        out.append({"stage": "fea_plan", "decided": "assembly", "value": asm.get("mode"), "rule": "PLAN-fea-17",
                    "inputs": {"parts": len(asm.get("parts") or []), "connection": asm.get("connection"),
                               "pairs": asm.get("pairs")},
                    "words": " ".join(assembly_words(asm)[:2])})
    lying = [list(c.get("faces") or []) for c in record.get("coincident") or []]
    if lying:
        out.append({"stage": "fea_plan", "decided": "faces lying on each other read through", "value": len(lying),
                    "rule": "PLAN-fea-3", "inputs": {"pairs": lying},
                    "words": f"{len(lying)} pair(s) of faces lying on each other set no wall size."})
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
    cosmetic = plan.get("cosmetic") or []
    if cosmetic:
        out.append({"stage": "fea_plan", "decided": "cosmetic fillets", "value": len(cosmetic), "rule": "PLAN-fea-24",
                    "inputs": {"spots": [c["spot"] for c in cosmetic], "wall_share": COSMETIC_WALL_SHARE,
                               "part_share": ROUND_MICRO},
                    "words": (f"{len(cosmetic)} cosmetic fillet(s) meshed at the size around them; the stress right at "
                              "them is not resolved.")})
    edge_breaks = plan.get("round_walls") or []
    if edge_breaks:
        out.append({"stage": "fea_plan", "decided": "edge breaks read as no wall", "value": len(edge_breaks),
                    "rule": "PLAN-fea-25", "inputs": {"faces": [e["face"] for e in edge_breaks], "span": ROUND_WALL_SPAN},
                    "words": f"{len(edge_breaks)} small round(s) read a wall only across their own edge break: no wall."})
    slow = plan.get("slow") or {}
    if slow:
        out.append({"stage": "fea_plan", "decided": "slow to mesh", "value": slow["ratio"], "rule": "PLAN-fea-26",
                    "inputs": {**slow, "warn_ratio": SLOW_RATIO},
                    "words": f"Sizes {slow['ratio']:,.0f} to 1 apart: the run may be slow."})
    out.append({"stage": "fea_plan", "decided": "curving", "value": "never worse than straight", "rule": "PLAN-fea-11",
                "inputs": {}, "words": "An element curving would leave worse than straight keeps straight sides."})
    out.append({"stage": "fea_plan", "decided": "growth", "value": plan["growth"], "rule": "PLAN-fea-6", "inputs": {},
                "words": f"Element sizes grow by at most {plan.get('growth', GROWTH)} away from every zone."})
    coarse = plan.get("coarsened") or {}
    out.append({"stage": "fea_plan", "decided": "limit", "rule": "PLAN-fea-8",
                "value": "coarsened to fit" if coarse else "fits",
                "inputs": {"estimate": plan["estimate"]["elements"], "limit": limit, "aim": LIMIT_AIM,
                           "needed": coarse.get("needed"), "zone_floor_m": coarse.get("zone_floor_m"),
                           "at_minimums": coarse.get("at_minimums"),
                           **({"floors_m": coarse["floors_m"], "kept": coarse.get("kept")}
                              if coarse.get("floors_m") else {})},
                "words": (f"About {plan['estimate']['elements']:,} elements against the {limit:,} limit: "
                          + (("the low-stress zones coarsened first to fit, short where named"
                              + (f"; {', '.join(KIND_WORDS.get(k, k) for k in coarse.get('kept') or [])} kept"
                                 if coarse.get("kept") else "") + ".") if coarse.get("floors_m") else
                             "the finest zones coarsened to fit, short where named." if coarse else
                             "meshed as planned."))})
    t = plan.get("target") or {}
    if t:
        held = held_zones(list(zs), float(plan["base_size_m"]))
        out.append({"stage": "fea_plan", "decided": "element count", "rule": "PLAN-fea-27",
                    "value": float(t.get("scale") or 1.0),
                    "inputs": {k: t.get(k) for k in ("asked", "mode", "source", "quote", "aim", "machine",
                                                     "rules_elements", "steps", "beyond", "lowest", "crossed", "sizes_first", "kept_rules",
                                                     "low", "high", "hit") if t.get(k) is not None}
                    | {"estimate": plan["estimate"]["elements"], "held": len(held),
                       "below_accuracy": [z.get("spot") or z["id"] for z in zs if z.get("below_accuracy")]},
                    "words": " ".join(target_words(plan)[:2])})
        if t.get("base_capped"):
            out.append({"stage": "fea_plan", "decided": "base size under the element count", "rule": "PLAN-fea-28",
                        "value": plan["base_size_m"], "inputs": {"divisions": TARGET_MIN_DIVISIONS},
                        "words": "The base size stops at a fifth of the part."})
    sizes = plan.get("user_sizes") or {}
    if sizes:
        uz = [z for z in zs if z.get("user")]
        out.append({"stage": "fea_plan", "decided": "sizes per part", "rule": "PLAN-fea-29", "value": len(sizes),
                    "inputs": {"parts": {k: {"min_m": v.get("min_m"), "max_m": v.get("max_m")} for k, v in sizes.items()},
                               "crossed": sorted({str(z["crossed"]) for z in uz if z.get("crossed")}),
                               "valid_held": len([z for z in uz if z.get("valid_held")])},
                    "words": " ".join(user_words(plan)[:2])})
    held_valid = [z for z in zs if z.get("valid_held")]
    if held_valid or float(t.get("beyond") or 1.0) > 1.0:
        out.append({"stage": "fea_plan", "decided": "validity limits", "rule": "PLAN-fea-30", "value": len(held_valid),
                    "inputs": {"per_quarter": VALID_PER_QUARTER, "divisions": VALID_DIVISIONS,
                               "lowest": bool(t.get("lowest")), "beyond": t.get("beyond")},
                    "words": (f"Sizes the user asked for stop only where elements would not build valid: {VALID_PER_QUARTER} "
                              f"per quarter-turn of every curve, {VALID_DIVISIONS} across every part.")})
    if plan.get("histogram_surface"):
        out.append({"stage": "fea_plan", "decided": "thin walls counted", "rule": "PLAN-fea-31",
                    "value": plan["estimate"]["elements"], "inputs": {"rows": len(plan["histogram_surface"])},
                    "words": "The estimate counts the elements a thin wall holds when the elements are larger than it."})
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


__all__ = ["CORNER_DIVISIONS", "assembly_words", "ELEMENT_LIMIT", "GROWTH", "HOT_SPOT_PER_QUARTER", "HOT_SPOT_REACH", "KEEP_PER_QUARTER",
           "KEEP_THROUGH", "LIMIT_AIM", "MICRO_KEEP", "MIN_DIVISIONS", "PER_H3", "PLAN_FILE", "ROUND_PER_QUARTER",
           "SCALE_MIN", "VALID_DIVISIONS", "VALID_PER_QUARTER", "STRESS_WALL_ELEMENTS", "TARGET_MIN_DIVISIONS", "TARGET_STEPS",
           "TARGET_TOLERANCE", "UNREAD_REMARK", "UNREAD_WORDS", "VERSION", "WALL_ELEMENTS", "asked_words", "base_size", "binding", "count_line", "crossed_words", "decisions", "finish", "floored",
           "held_words", "held_zones", "keep_size", "scale_cap", "scaled", "shortfalls", "target_remark", "target_words", "user_base", "user_sized", "user_words", "valid_cap", "words",
           "zones"]
