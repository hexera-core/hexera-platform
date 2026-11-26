# Responsibility: v5's SURFACE CROSSING detectors (step 9): what kind of crossing a surface_crossing defect is -
#                 DUPLICATE faces, NEAR-COINCIDENT faces, or a TRUE crossing - from one kernel measurement of the
#                 faces at the defect, and the cause that picks the rule seed and aims the class menu.
# Owns: `crossing_kind` (the pure rule per measured pair), `attach_crossing` (pure: the kind, pairs and aim on the
#       defects), `measure_crossings` (the driver diagnose calls), the thresholds.
# Boundaries: the measurement is `crossing_kernel.v5_crossing_facts`; the class itself is `diagnose.classify_site`'s
#             (this only refines a surface_crossing defect's CAUSE). The menus per kind are `loop.CLASS_REPAIRS`.
"""Surface-crossing kinds (spec "Defect classes" 2 / owner's horizontal-coverage list: edge through face, true face-face
crossing, duplicate faces):

- `duplicate_faces`: two faces of the same surface type, the same area (within `DUPLICATE_AREA_REL`) and the same box
  (within the fidelity bound), touching - one face written twice, usually once per body. Rule: remove_duplicate_faces
  (the pair becomes one shared face).
- `true_crossing`: the two faces' section runs through the interior of at least one of them (an edge through a face,
  or two faces passing through each other). Rule: imprint_crossing (split both along the intersection curve).
- `near_coincident`: parallel faces (normal angle <= `PARALLEL_DEG`) within the fidelity bound that are not
  duplicates (a partial overlap of two coplanar faces). Rule: unify_same_domain on the pair, then a fragment.

A defect whose faces show none of these keeps its class and the class's general row (`surface_crossing`)."""

from __future__ import annotations

import dataclasses
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect

CLASS = "surface_crossing"
#: two faces are the same face written twice when their areas agree to this share of the larger
DUPLICATE_AREA_REL = 1e-3
#: parallel surfaces, orientation-free (= conformal_kernel.PARALLEL_DEG)
PARALLEL_DEG = 1.0
#: kinds in the order a defect showing several is repaired by (the most decisive repair first)
KIND_ORDER = ("duplicate_faces", "true_crossing", "near_coincident")
#: at most this many measured pairs ride on a cause (the trace stays readable)
MAX_CAUSE_PAIRS = 6


def tolerance_mm(diag: float | None) -> float:
    """The fidelity bound (= body_kernel.fidelity_bound): what two faces may differ by and still be one."""
    return min(0.1, 1e-3 * float(diag)) if diag else 0.1


def crossing_kind(row: dict, tol: float) -> str | None:
    """The crossing kind of one measured pair (a `v5_crossing_facts` row), or None for a mere touch. Pure."""
    gap = row.get("gap_mm")
    if gap is None or gap > tol:
        return None
    area_a, area_b = float(row.get("area_a_mm2") or 0.0), float(row.get("area_b_mm2") or 0.0)
    if (row.get("surface_a") == row.get("surface_b") and max(area_a, area_b) > 0
            and abs(area_a - area_b) <= DUPLICATE_AREA_REL * max(area_a, area_b)
            and float(row.get("box_dev_mm") if row.get("box_dev_mm") is not None else 1e300) <= tol):
        return "duplicate_faces"
    if (row.get("section_mm") or 0.0) > 0.0:
        return "true_crossing"
    angle = row.get("normal_angle_deg")
    if angle is not None and angle <= PARALLEL_DEG:
        return "near_coincident"
    return None


def attach_crossing(defect: Defect, rows: list[dict], tol: float) -> Defect:
    """`defect` with its crossing kind, the measured pairs, the duplicate pairs (across bodies: what
    remove_duplicate_faces can merge) and the AIM (the faces of the pairs of that kind). Rows carry stable ids
    (`a`/`b` faces, `body_a`/`body_b`). Unchanged when no row is a crossing. Pure."""
    kinds = {k: [r for r in rows if crossing_kind(r, tol) == k] for k in KIND_ORDER}
    kind = next((k for k in KIND_ORDER if kinds[k]), None)
    if kind is None:
        return defect
    picked = kinds[kind]
    aim = list(dict.fromkeys(f for r in picked for f in (r["a"], r["b"])))
    dups = [[r["a"], r["b"]] for r in kinds["duplicate_faces"]
            if r.get("body_a") and r.get("body_b") and r["body_a"] != r["body_b"]]
    first = picked[0]
    cause = {**(defect.cause or {}), "kind": kind, "aim": aim, "tol_mm": tol,
             "crossing": [{k: r.get(k) for k in ("a", "b", "body_a", "body_b", "surface_a", "surface_b", "gap_mm",
                                                 "normal_angle_deg", "section_mm", "box_dev_mm")}
                          for r in picked[:MAX_CAUSE_PAIRS]],
             "duplicate_pairs": dups[:MAX_CAUSE_PAIRS]}
    if first.get("body_a") and first.get("body_b") and first["body_a"] != first["body_b"]:
        cause.update(body_a=first["body_a"], body_b=first["body_b"])
    what = {"duplicate_faces": "duplicate of", "true_crossing": "crosses", "near_coincident": "lies on"}[kind]
    rule = (f"{defect.rule}; measured {kind}: {first['a']} {what} {first['b']}"
            + (f" ({len(picked)} pairs)" if len(picked) > 1 else ""))
    return dataclasses.replace(defect, cause=cause, rule=rule)


def _faces_of(defect: Defect) -> list[str]:
    cause = defect.cause or {}
    named = [e for e in defect.entities if str(e)[:1] == "F"]
    contacts = [f for p in cause.get("pairs") or [] if isinstance(p, dict) for f in (p.get("a"), p.get("b"))]
    return list(dict.fromkeys(f for f in (*named, *contacts) if isinstance(f, str) and f[:1] == "F"))


def measure_crossings(pool: Any, ws: Any, defects: list[Defect]) -> list[Defect]:
    """`defects` with every surface_crossing defect's crossing measured (`v5_crossing_facts`, one call per distinct
    face set) and attached. A failed measurement leaves the defect as diagnosed."""
    from meshpipeline.cad.repair_v4.sites import _call, _reverse

    reverse = _reverse(ws)
    tol = tolerance_mm(getattr(ws, "part_diag_mm", None))
    answers: dict[tuple[str, ...], list[dict]] = {}
    out: list[Defect] = []
    for d in defects:
        faces = [f for f in _faces_of(d) if f in ws.indices] if d.cls == CLASS else []
        if not faces:
            out.append(d)
            continue
        key = tuple(sorted(faces))
        if key not in answers:
            got = _call(pool, "v5_crossing_facts", {"brep_path": ws.brep_path, "tol_mm": tol,
                                                    "part_diag_mm": getattr(ws, "part_diag_mm", None),
                                                    "faces": [ws.indices[f] for f in faces]})
            answers[key] = translate((got or {}).get("pairs") or [], reverse)
        out.append(attach_crossing(d, answers[key], tol))
    return out


def translate(raw: list[dict], reverse: dict) -> list[dict]:
    """Kernel rows -> stable ids (`a`/`b` faces, `body_a`/`body_b`); a row whose face has no live id is dropped."""
    out = []
    for p in raw:
        a, b = reverse.get(("F", int(p["a"]))), reverse.get(("F", int(p["b"])))
        if a is None or b is None:
            continue
        out.append({**p, "a": a, "b": b,
                    "body_a": reverse.get(("B", int(p["solid_a"]))) if p.get("solid_a") else None,
                    "body_b": reverse.get(("B", int(p["solid_b"]))) if p.get("solid_b") else None})
    return out
