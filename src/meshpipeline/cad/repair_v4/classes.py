# Responsibility: CRS v5's DEFECT CLASSES - which class a diagnosed defect belongs to, with its cause, and the
#                 class -> dedicated-repairs map that replaces v4's per-code rule table (v5 design, 2026-11-24).
# Owns: the class names, `Diagnosis`, `classify`, `diagnose`, `CLASS_REPAIRS`, `table_key`, `v5_table`.
# Boundaries: the loop is `seq.repair_sequentially` (unchanged, handed `diagnose` and the class table); the ops,
#             local verification and the guards are used as they are. Lane D owns `classify` (and the AI classifier
#             for UNKNOWN); lane C owns classes 1-2's rows, lane A classes 3a/3b's rows. The lead merges.
"""Recognise, repair with a dedicated operation, verify cheap-first, keep or revert.

Every defect the loop diagnoses gets a CLASS (what kind of thing is wrong), a LOCATION (already on the `Defect`:
its entities, points and box) and a CAUSE (a measurement: gap, clash depth, angle...). The class selects at most
TWO dedicated repairs; the loop tries them in order and otherwise records the defect unrepaired with a reason.

A code no class claims is UNKNOWN. Until lane D's AI classifier names a class for it, an unknown defect keeps v4's
measured per-code rows (capped at two), so v5 never has fewer repairs for a defect than v4 had.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect

Repair = tuple[str, ...]

#: class 1 (lane C): near-coaxial / near-tangent curved faces a tiny gap apart, whose FACETS cross (shaft in bore)
COARSE_CROSSING = "coarse_facet_crossing"
#: class 2 (lane C): true surface crossings and edges through faces, located by a mesher's reported point
SURFACE_CROSSING = "surface_crossing"
#: class 3a (lane A): bodies that overlap each other
BODY_OVERLAP = "body_overlap"
#: class 3b (lane A): a face or shell that intersects itself (same parts as 3a, one agreed boundary)
SELF_INTERSECTION = "self_intersection"
#: class 4 (later): low-quality elements - slivers, narrow faces, tiny edges
LOW_QUALITY = "low_quality_elements"
#: class 5 (lane D): nothing above claims it; the AI classifier names a class, reviewed weekly into new classes
UNKNOWN = "unknown"

CLASSES: tuple[str, ...] = (COARSE_CROSSING, SURFACE_CROSSING, BODY_OVERLAP, SELF_INTERSECTION, LOW_QUALITY,
                            UNKNOWN)
#: the design's cap: a class's next repair at most once, then the defect is marked unrepaired with its reason
MAX_REPAIRS = 2


@dataclass(frozen=True)
class Diagnosis:
    cls: str
    #: the measurement the class was decided on and the repair is parameterised from (never invented)
    cause: dict[str, Any] = field(default_factory=dict)
    #: one readable clause for the trace: why this class
    why: str = ""
    #: `rule` (this module), `ai` (lane D's classifier), or `none` (UNKNOWN, nothing decided)
    by: str = "rule"


#: THE CODE RULES. One mesher or layer-1 code -> one class, where the code alone decides it. `overlapping_facets`
#: is class 1 by default because its dominant measured cause is the coarse-facet crossing (C23, f9501251fb30);
#: lane D's location measurement is what moves a true crossing of that code to class 2.
_BY_CODE: dict[str, str] = {
    "overlapping_facets": COARSE_CROSSING,
    "edge_through_face": SURFACE_CROSSING,
    "plc_intersection": SURFACE_CROSSING,
    "body_overlap": BODY_OVERLAP,
    "self_intersection": SELF_INTERSECTION,
    "low_quality": LOW_QUALITY,
}

#: the measurement keys a cause is read from (whatever of these the defect really carries)
_CAUSE_KEYS = ("clash", "clash_by_pair", "narrowest_face", "free_loops", "diagonal_mm", "gap_mm", "angle_deg",
               "segments", "nearest_face_mm")


def classify(defect: Defect) -> Diagnosis:
    """The defect's class and cause, by rule. UNKNOWN when no rule claims the code (lane D: the AI then)."""
    m = defect.measurement or {}
    cause = {k: m[k] for k in _CAUSE_KEYS if k in m}
    cls = _BY_CODE.get(defect.code)
    if cls is None:
        return Diagnosis(UNKNOWN, cause, f"no class rule for {defect.code!r}", "none")
    return Diagnosis(cls, cause, f"{defect.code} -> {cls}", "rule")


#: an optional second opinion for UNKNOWN defects (lane D): (defect) -> a class name, or None. Never an op.
AIClassify = Callable[[Defect], "str | None"]


def diagnose(defects: Sequence[Defect], *, ai_classify: AIClassify | None = None,
             classify_fn: Callable[[Defect], Diagnosis] = classify) -> list[Defect]:
    """Each defect with its diagnosis written into its measurement (`class`, `cause`, `class_by`, `class_why`),
    so the loop's table key and the trace line both read it. The `Defect` model stays lane C's one model."""
    out: list[Defect] = []
    for d in defects:
        dx = classify_fn(d)
        if dx.cls == UNKNOWN and ai_classify is not None:
            named = ai_classify(d)
            if named in CLASSES and named != UNKNOWN:
                dx = Diagnosis(named, dx.cause, f"the model classified {d.code!r} as {named}", "ai")
        out.append(dataclasses.replace(d, measurement={**(d.measurement or {}), "class": dx.cls,
                                                       "cause": dx.cause, "class_by": dx.by,
                                                       "class_why": dx.why}))
    return out


def class_of(defect: Defect) -> str:
    return str((defect.measurement or {}).get("class") or UNKNOWN)


#: THE CLASS -> DEDICATED REPAIRS MAP (at most `MAX_REPAIRS` each, in order). Seeded from what is MEASURED today;
#: each lane replaces its rows with its dedicated ops as they land. Nothing here is a ladder: parameters come from
#: the defect's measurement (`seq.params_for`).
CLASS_REPAIRS: dict[str, tuple[Repair, ...]] = {
    # C23: aligned split took f9501251fb30's shaft-in-bore from overlapping_facets to a mesh. The oracle's measured
    # 2-step row stays behind it.
    COARSE_CROSSING: (("split_aligned",), ("split_angle", "run_stage:condition")),
    # placeholder rows from v4's measured winners for these codes, until lane C's imprint/split-along-intersection,
    # near-coincident unify and wedge refit land
    SURFACE_CROSSING: (("defeature",), ("conformalise",)),
    # lane D's oracle: conformalise and fuse tie at 5 files each (fuzzy 0.0); lane A owns fuse-with-reheal,
    # fragment-and-drop-slivers and the cut chosen by volume
    BODY_OVERLAP: (("conformalise",), ("fuse",)),
    # defeature leads on files; plain conformalise is the one body op (what relocate onto a body can still aim);
    # lane A owns the rebuild-from-boundary repair
    SELF_INTERSECTION: (("defeature",), ("conformalise",)),
    # split_by_proximity made every low_quality repair in two waves; reapproximate never applied
    LOW_QUALITY: (("split_by_proximity",), ("remove_small",)),
}


def table_key(defect: Defect) -> str:
    """The key the loop's table is looked up by: the class, or for UNKNOWN the code (v4's legacy rows)."""
    cls = class_of(defect)
    return cls if cls != UNKNOWN else defect.code


def v5_table(*, legacy: dict[str, tuple[Repair, ...]]) -> dict[str, tuple[Repair, ...]]:
    """ONE lookup table for the loop: class rows, plus v4's per-code rows for every code no class claims."""
    claimed = set(_BY_CODE)
    return {**{k: v[:MAX_REPAIRS] for k, v in legacy.items() if k not in claimed},
            **{k: v[:MAX_REPAIRS] for k, v in CLASS_REPAIRS.items()}}
