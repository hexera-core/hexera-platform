# Responsibility: the DECISION half of v5's invalid_brep_tolerance detectors: which measured facts
#                 (`brep_facts_kernel.v5_brep_facts`) are faults, of which kind, on which entities; and the owner's
#                 units rule (REPORT a suspected mismatch; never rescale - owner decision A, 2026-11-25).
# Owns: the provisional thresholds, `Fault`, `faults_of`, `units_verdict`, `step_length_unit`, `KIND_REPAIRS`.
# Boundaries: pure - no OCCT, no pool. Diagnosis (`diagnose.py`) calls the kernel and hands the facts here; the
#             loop's verify re-measures through the same two functions, so "fault gone" means what "fault" meant.
"""invalid_brep_tolerance faults, by KIND (the kind picks the rule seed):

  large_tolerance   an edge/vertex/face tolerance above layer 1's line (1e-3 x diagonal)     -> tighten_tolerances
  unbounded         an open bounding box or a diagonal beyond `units.UNBOUNDED_DIAGONAL_MM`   -> trim_unbounded
  pole              a degenerated edge that is not collapsed, a collapsed iso with no
                    degenerated edge, a non-degenerated edge shorter than its tolerance       -> fix_poles
  bad_spline        a B-spline face/edge of too high a degree, too many knots, or whose
                    parameter speed varies more than `MAX_SPEED_RATIO` along an iso-line      -> to_canonical
  missing_edge      a face wire whose consecutive edges end at different vertices (a 3D gap):
                    the boundary edge is missing                                             -> rebuild_boundary_edge
  bad_seam          a seam whose 3D curve is off its surface's iso (`boundary.SEAM_DEV_MM`)  -> rebuild_seam
  (units            a suspected header/geometry unit mismatch is REPORTED only (`units_verdict`), never a fault or a
                    repair: owner decision A, 2026-11-25 - injected wrong-unit parts mesh clean, and a wrong
                    rescale silently corrupts the part's real size)

THRESHOLDS ARE PROVISIONAL (named, one place), from OCCT's own conventions rather than corpus calibration:
`MAX_SPLINE_DEGREE` = 9 is ShapeCustom_BSplineRestriction's / `reapproximate`'s ceiling (the degree v4 already
re-approximates to); `MAX_SPLINE_KNOTS` = 200 is far above what an exporter writes for a smooth face (CAD kernels
rarely exceed a few dozen spans) and below the dense-sample fits that slow gmsh's 2D stage; `MAX_SPEED_RATIO` = 30
is 4x above the worst B-spline FACE measured on the step-5 sample (7.8 on dev 0053_00538816; the 12 clean-split parts
carry no spline faces at all) and under the 81 of the injected `bad_spline` face on which netgen timed out twice.
(An edge curve on the occt test file trj7 measures 307; it is also degree 11, so it is flagged either way.)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.units import UNBOUNDED_DIAGONAL_MM, canonical_unit

MAX_SPLINE_DEGREE = 9
MAX_SPLINE_KNOTS = 200
MAX_SPEED_RATIO = 30.0
#: layer 1's own line (`valid_kernel.LARGE_TOLERANCE_FRACTION`), not imported: valid_kernel is a kernel module
LARGE_TOLERANCE_FRACTION = 1e-3

# --- units (owner, 2026-11-25, option A: REPORT a suspected mismatch, never rescale) ---------------------------------
#: a mechanical part's diagonal is plausible inside this range (mm). Below 0.5 mm or above 100 m it is not: the
#: corpus p95 is 22 m (`units.UNBOUNDED_DIAGONAL_MM`'s note), and nothing in dev_v2 is under a millimetre.
PLAUSIBLE_DIAG_MM = (0.5, 1e5)
#: the band a corrected part must land in to count as "made plausible" by a factor (a typical part, not an edge case)
TYPICAL_DIAG_MM = (5.0, 5e3)
#: a tolerance this large a fraction of the diagonal is absurd for any unit (the part is mostly tolerance)
ABSURD_TOLERANCE_RATIO = 1e-2
#: header unit -> {factor: why}. A factor is what the geometry is MULTIPLIED by; each names the mistake it undoes, so
#: a factor applies only under the header that mistake leaves behind.
UNIT_MISTAKES: dict[str, dict[float, str]] = {
    "mm": {1000.0: "metre values under a millimetre header", 25.4: "inch values under a millimetre header"},
    "m": {0.001: "millimetre values under a metre header"},
    "inch": {1 / 25.4: "millimetre values under an inch header"},
    "cm": {0.1: "millimetre values under a centimetre header"},
}

#: fault kind -> its class menu row, rule seed first (the loop's `CLASS_REPAIRS["invalid_brep_tolerance:<kind>"]`)
KIND_REPAIRS: dict[str, tuple[tuple[str, ...], ...]] = {
    "brep_invalid": (("tighten_tolerances",), ("rebuild_edge_as_intersection",), ("fix_poles",)),
    "large_tolerance": (("tighten_tolerances",), ("rebuild_edge_as_intersection",)),
    "unbounded": (("trim_unbounded",), ("tighten_tolerances",)),
    "pole": (("fix_poles",), ("rebuild_boundary_edge",), ("reapproximate",)),
    "bad_spline": (("to_canonical",), ("reapproximate",)),
    # round 2: a wire missing an edge (what a degenerated-flagged real edge leaves after the STEP round trip): the
    # edge rebuilt and shared by both faces; ShapeFix_Face as the fallback
    "missing_edge": (("rebuild_boundary_edge",), ("fix_poles",), ("tighten_tolerances",)),
    # round 2: a seam off its surface: the seam rebuilt from the surface's iso; the closed face split as the fallback
    "bad_seam": (("rebuild_seam",), ("split_closed",), ("tighten_tolerances",)),
}
FAULT_KINDS = tuple(KIND_REPAIRS)
#: the kinds a whole-part fact decides (no located entity needed)
WHOLE_PART_KINDS = frozenset({"unbounded"})


@dataclass(frozen=True)
class Fault:
    kind: str
    #: stable ids (faces/edges) the fault is on; () for a whole-part fault
    entities: tuple[str, ...]
    #: what was measured, for the cause and the trace
    measured: dict = field(default_factory=dict)
    why: str = ""


def spline_faults(facts: dict) -> list[tuple[str, int, dict, str]]:
    """(entity kind 'F'/'E', kernel index, the row, why) for every spline fact over a threshold."""
    out = []
    for r in facts.get("splines") or []:
        why = _spline_why(max(r.get("udeg") or 0, r.get("vdeg") or 0),
                          max(r.get("uknots") or 0, r.get("vknots") or 0),
                          max(r.get("speed_ratio_u") or 0.0, r.get("speed_ratio_v") or 0.0))
        if why:
            out.append(("F", int(r["face"]), r, why))
    for r in facts.get("curve_splines") or []:
        why = _spline_why(r.get("deg") or 0, r.get("knots") or 0, r.get("speed_ratio") or 0.0)
        if why:
            out.append(("E", int(r["edge"]), r, why))
    return out


def _spline_why(degree: int, knots: int, speed: float) -> str:
    bits = []
    if degree > MAX_SPLINE_DEGREE:
        bits.append(f"degree {degree} > {MAX_SPLINE_DEGREE}")
    if knots > MAX_SPLINE_KNOTS:
        bits.append(f"{knots} knots > {MAX_SPLINE_KNOTS}")
    if speed > MAX_SPEED_RATIO:
        bits.append(f"parameter speed ratio {speed:.3g} > {MAX_SPEED_RATIO:g}")
    return "; ".join(bits)


def faults_of(facts: dict, reverse: dict[tuple[str, int], str], *, header_unit: str | None = None) -> list[Fault]:
    """Every fault in `facts` (a `v5_brep_facts` answer on a workspace's BREP), its entities translated through
    `reverse` ((prefix, kernel index) -> stable id, `sites._reverse`). An index `reverse` cannot place is dropped
    from the entities (never guessed); a fault left with no entity is kept only when it is whole-part."""
    def ids(pairs: list[tuple[str, int]]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s for s in (reverse.get(p) for p in pairs) if s is not None))

    out: list[Fault] = []
    diag = facts.get("diag_mm")
    if facts.get("open_box") or diag is None or not math.isfinite(diag) or diag > UNBOUNDED_DIAGONAL_MM:
        out.append(Fault("unbounded", (), {"diag_mm": diag, "open_box": bool(facts.get("open_box"))},
                         "the bounding box is open or beyond the unbounded line"))
        return out          # nothing else measured on an infinite part means anything
    tol = facts.get("tolerance") or {}
    over = [("E", int(i)) for i, _t in tol.get("edges") or []] + [("F", int(i)) for i, _t in tol.get("faces") or []]
    if over:
        ents = ids(over)
        if ents:
            out.append(Fault("large_tolerance", ents,
                             {"max_tolerance_mm": facts.get("max_tolerance"), "threshold_mm": facts.get(
                                 "tol_threshold_mm"), "edges_over": tol.get("edges_over"),
                              "faces_over": tol.get("faces_over")},
                             f"tolerance {facts.get('max_tolerance', 0):.3g} mm over layer 1's "
                             f"{facts.get('tol_threshold_mm') or 0:.3g} mm"))
    missing = [p for p in facts.get("poles") or [] if p.get("why") == "missing_edge"]
    poles = [p for p in facts.get("poles") or [] if p.get("why") != "missing_edge"]
    if missing:
        ents = ids([("F", int(p["face"])) for p in missing])
        if ents:
            out.append(Fault("missing_edge", ents, {"gaps": missing[:8]},
                             f"{len(missing)} wire gap(s) up to {max(p['gap_mm'] for p in missing):.3g} mm: a "
                             f"boundary edge is missing"))
    if poles:
        ents = ids([("F", int(p["face"])) for p in poles])
        if ents:
            out.append(Fault("pole", ents, {"poles": poles[:8]},
                             ", ".join(sorted({str(p["why"]) for p in poles}))))
    seams = facts.get("bad_seams") or []
    if seams:
        ents = ids([("F", int(s["face"])) for s in seams])
        if ents:
            out.append(Fault("bad_seam", ents, {"seams": seams[:8]},
                             f"seam 3D curve {max(s['dev_mm'] for s in seams):.3g} mm off its surface"))
    splines = spline_faults(facts)
    if splines:
        faces = ids([("F", i) for k, i, _r, _w in splines if k == "F"])
        # an edge curve's fault is repaired through its faces' carriers only when a face is bad too; a bad edge
        # alone is aimed by its edge id (reapproximate/to_canonical refuse it, and the match log says so)
        ents = faces or ids([("E", i) for k, i, _r, _w in splines if k == "E"])
        if ents:
            out.append(Fault("bad_spline", ents, {"splines": [r for _k, _i, r, _w in splines][:8]},
                             splines[0][3]))
    # units: REPORTED by the caller (`units_verdict`), never a fault - nothing repairs a unit mismatch (owner, A)
    return out


def closed_faces_of(facts: dict | None, reverse: dict[tuple[str, int], str]) -> list[tuple[str, str | None]]:
    """(face id, its body id or None) for every face carrying a seam (`v5_brep_facts`' `closed_faces`), translated
    through `reverse`; a face `reverse` cannot place is dropped. Round 2: what a periodic_surface failure that names
    only bodies is aimed at."""
    out = []
    for row in (facts or {}).get("closed_faces") or []:
        face = reverse.get(("F", int(row["face"])))
        if face is not None:
            out.append((face, reverse.get(("B", int(row["solid"]))) if row.get("solid") else None))
    return out


def units_verdict(header_unit: str | None, diag_mm: float | None, max_tolerance_mm: float | None) -> dict:
    """The owner's units rule. `verdict` is `consistent` (nothing to say), `report_only` (something looks off but the
    header and the geometry do not CLEARLY disagree) or `mismatch` (they clearly disagree; `factor` names the
    mistake that would explain it). ALL THREE ARE REPORTS: nothing rescales (owner decision A, 2026-11-25).

    CLEAR DISAGREEMENT = the part's size is implausible (outside `PLAUSIBLE_DIAG_MM`; an absurd tolerance fraction only
    corroborates it, being unit-invariant), AND exactly one of the factors the HEADER's own mistakes allow (`UNIT_MISTAKES`) brings the
    diagonal into `TYPICAL_DIAG_MM`. Two factors that both fit, or none, is not clear: report only."""
    unit = canonical_unit(header_unit) if header_unit else None
    base: dict[str, Any] = {"header_unit": unit, "diag_mm": diag_mm, "max_tolerance_mm": max_tolerance_mm,
                            "factor": None}
    if diag_mm is None or not math.isfinite(diag_mm) or diag_mm <= 0:
        return {**base, "verdict": "report_only", "why": "no finite extent to judge the units by"}
    lo, hi = PLAUSIBLE_DIAG_MM
    implausible = not lo <= diag_mm <= hi
    ratio = (max_tolerance_mm or 0.0) / diag_mm
    absurd = ratio > ABSURD_TOLERANCE_RATIO
    base.update(size_plausible=not implausible, tolerance_ratio=ratio)
    if not implausible:
        # A TOLERANCE RATIO IS UNIT-INVARIANT (both numbers carry the same unit), so on its own it says nothing about
        # the units: it corroborates an implausible size, never replaces one. Measured on jig: seam_variant's 10.8 mm
        # seam tolerance on a 126 mm part made the first version of this rule rescale x25.4.
        if absurd:
            return {**base, "verdict": "report_only",
                    "why": f"max tolerance is {ratio:.3g} of the diagonal, but a {diag_mm:.4g} mm part is plausible "
                           f"in {unit or '?'}: a tolerance fault, not a units one"}
        return {**base, "verdict": "consistent", "why": f"a {diag_mm:.4g} mm part is plausible in {unit or '?'}"}
    signs = [f"diagonal {diag_mm:.4g} mm outside {lo:g}-{hi:g} mm"] if implausible else []
    if absurd:
        signs.append(f"max tolerance is {ratio:.3g} of the diagonal")
    if unit is None:
        return {**base, "verdict": "report_only", "why": f"{'; '.join(signs)}, and the header names no length unit"}
    t_lo, t_hi = TYPICAL_DIAG_MM
    fits = {f: why for f, why in UNIT_MISTAKES.get(unit, {}).items() if t_lo <= diag_mm * f <= t_hi}
    base["candidates"] = {f"{f:.6g}": why for f, why in fits.items()}
    if len(fits) != 1:
        return {**base, "verdict": "report_only",
                "why": f"{'; '.join(signs)}, but {'no' if not fits else 'more than one'} {unit}-header mistake "
                       f"makes it a typical part: not a clear mismatch"}
    (factor, mistake), = fits.items()
    return {**base, "verdict": "mismatch", "factor": factor,
            "why": f"{'; '.join(signs)}; x{factor:.6g} ({mistake}) makes it {diag_mm * factor:.4g} mm"}


_SI = re.compile(r"SI_UNIT\s*\(\s*(?:\.(\w+)\.|\$)\s*,\s*\.METRE\.\s*\)", re.I)
_CONV = re.compile(r"CONVERSION_BASED_UNIT\s*\(\s*'([^']+)'", re.I)
_SI_PREFIX = {"MILLI": "mm", "CENTI": "cm", "MICRO": "um", "KILO": "km", "DECI": "dm"}


def step_length_unit(text: str) -> str | None:
    """The length unit a STEP file's units context declares, canonical (`units.canonical_unit`), or None. A
    conversion-based length unit (INCH, FOOT) wins over the SI METRE it is defined from."""
    conv = [canonical_unit(m.group(1)) for m in _CONV.finditer(text)]
    conv = [u for u in conv if u in ("inch", "ft", "mil", "mi", "uin")]
    if conv:
        return conv[0]
    m = _SI.search(text)
    if m is None:
        return None
    return _SI_PREFIX.get(m.group(1).upper()) if m.group(1) else "m"
