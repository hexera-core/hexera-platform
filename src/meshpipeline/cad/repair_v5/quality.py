# Responsibility: v5's low_quality DETECTORS (step 10, lever 4): turn the `v5_quality_facts` measurement into faults
#                 (small edge, small face, sliver, tiny/floating solid) sized against the shipped mesh, put them on
#                 the loop's defects as the cause + aim + op parameters, and make layer 1's SOFT small_face/small_edge
#                 findings defects of their own. Plus the re-measurement the local check uses for those.
# Owns: the thresholds (`E_OF_H` ... - PROVISIONAL), `QualityFault`, `faults_of`, `attach_quality`, `measure`,
#       `still_there`.
# Boundaries: pure except `measure` (one kernel call through the pool). The kernel half is `quality_kernel.py`; the
#             ops are `repair_v4/ops/quality.py` (+ remove_small, refit_cluster, fuse, split_by_proximity); the class
#             menu rows are `loop.CLASS_REPAIRS`; `seq.params_for` reads the parameters this module writes on the cause.
"""low_quality: what is small, measured against the mesh that will be made of it.

THRESHOLDS (Cubit's rule, owner decision 2026-11-24 lever 4): `e` = `E_OF_H` x the shipped element size `h`.
- a SMALL EDGE is shorter than `e`;
- a SMALL FACE has an area under `e^2`;
- a SLIVER (narrow region) is a face whose inertia width - how far apart its two long boundary curves are - is
  under `e`;
- a SMALL SOLID keeps under `TINY_SOLID_FRACTION` of the part's volume; FLOATING when it touches no other solid.
All four constants are PROVISIONAL: named here, to be calibrated on the dev set's low_quality defects.

WHERE A FAULT BECOMES A DEFECT. Clean = valid + meshes, so a small entity nothing fails on is not broken. Two ways in:
1. a `low_quality` defect (a mesher's low_quality/inverted_elements, or a sliver site) whose faces carry a fault:
   the fault is its CAUSE (the most severe one: smallest against its threshold), and its KIND picks the rule seed;
2. layer 1's SOFT small_face/small_edge findings (step 2's gap: the loop built defects from HARD codes only, so the
   class rows for them never fired): each soft entity the detector also measures under `e` is a low_quality
   VALIDITY defect, up to `MAX_SOFT_PER_KIND` per kind, smallest first. Its local check re-measures (`still_there`).

RULE SEEDS by kind (the merge is a real topology edit, the deviation measured by the op):
- sliver -> `merge_sliver` into the neighbour it continues most smoothly into; a sliver meeting every neighbour
  above `FEATURE_ANGLE_DEG` is a FEATURE (a chamfer) and is protected - unless it is the wall of a STEP between two
  parallel faces, lower than `e`: then the wall and the smaller side go (the step collapsed); deviation limit `e`;
  menu: `refit_cluster` on sliver + that neighbour under the same deviation limit, `remove_small`, split;
- small face -> `merge_sliver` (no feature protection: a face small both ways moves the part by less than its size);
- small edge -> `collapse_edge` (merge its two vertices); menu: `remove_small`, split;
- small solid -> `fuse` it into the solid it touches; floating -> `drop_solid` (the loss guard judges it).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.types import Site, StableId

#: e = this x the shipped element size (Cubit: a tenth of the element size). PROVISIONAL
E_OF_H = 0.1
#: a solid under this share of the part's volume is small. PROVISIONAL
TINY_SOLID_FRACTION = 1e-3
#: a sliver whose every neighbour meets it above this dihedral (deg) is a feature, not a sliver. PROVISIONAL
FEATURE_ANGLE_DEG = 30.0
#: the merge's deviation limit, x e. PROVISIONAL
MERGE_DEVIATION_OF_E = 1.0
#: layer 1's soft findings made defects, per kind (small_face, small_edge), smallest first. PROVISIONAL
MAX_SOFT_PER_KIND = 4
#: remove_small / collapse_edge must clear the entity's own measured size (the measurement's rounding)
SIZE_MARGIN = 1.05
#: a low_quality defect aims at most this many faults of its cause's kind at its site (worst first). PROVISIONAL
MAX_AIM = 4
MULTI_KINDS = ("small_edge", "small_face", "sliver")
#: no feature protection (merge_sliver's `feature_deg` for a small face)
NO_PROTECTION_DEG = 180.0

LOW_QUALITY = "low_quality"
#: fault kinds; `kind` on a defect's cause, which picks its class-menu row (`loop.CLASS_REPAIRS["low_quality:<kind>"]`)
KINDS = ("small_edge", "small_face", "sliver", "tiny_solid", "floating_solid")
#: layer 1's soft code -> the fault kinds that confirm it
SOFT_KINDS = {"small_face": ("small_face", "sliver"), "small_edge": ("small_edge",)}
SOFT_CODES = frozenset(SOFT_KINDS)
#: the defect id prefix of a soft-finding defect (no site: the detector located it)
SOFT_ID = "Q@"


def e_of(h_mm: float) -> float:
    return E_OF_H * h_mm


@dataclass(frozen=True)
class QualityFault:
    kind: str
    #: the entity the repair aims at (an edge, a face, a body), stable id
    entity: StableId
    #: how far under its threshold, as measured / threshold (smaller = worse); the cause ranking
    ratio: float
    why: str
    #: the faces it sits on (an edge's two faces, a face itself, a solid's none) - what a located failure meets
    faces: tuple[StableId, ...] = ()
    #: the solid it is part of (faces) or is (solids)
    body: StableId | None = None
    measured: dict = field(default_factory=dict)
    #: what the class's ops take, from this measurement (`seq.params_for` reads them off the cause)
    params: dict = field(default_factory=dict)


def _merge_params(row: dict, reverse: dict, e: float, *, protect: bool) -> dict:
    """merge_sliver / refit_cluster / remove_small parameters for one flagged face."""
    near = [n for n in row.get("neighbours") or [] if ("F", int(n["face"])) in reverse]
    smooth = [n for n in near if n.get("dihedral_deg") is not None and n["dihedral_deg"] <= FEATURE_ANGLE_DEG]
    into = max(smooth, key=lambda n: n["shared_mm"]) if smooth else None
    out: dict[str, Any] = {"max_dev_mm": MERGE_DEVIATION_OF_E * e,
                           "feature_deg": FEATURE_ANGLE_DEG if protect else NO_PROTECTION_DEG,
                           "max_width_mm": SIZE_MARGIN * float(row.get("rs_width_mm") or 0.0),
                           "refit_tol_mm": MERGE_DEVIATION_OF_E * e}
    if into is not None:
        out["neighbour"] = reverse[("F", int(into["face"]))]
        out["neighbour_dihedral_deg"] = into["dihedral_deg"]
    else:
        out["protected"] = [n.get("dihedral_deg") for n in near]
    return out


def faults_of(facts: dict, reverse: dict[tuple[str, int], StableId], *, h_mm: float) -> list[QualityFault]:
    """The low_quality faults in one `v5_quality_facts` answer, as stable ids (`reverse`: (prefix, index) -> id).
    Pure. An entity the reverse index cannot place is skipped (never guessed)."""
    e = float(facts.get("e_mm") or e_of(h_mm))
    out: list[QualityFault] = []

    def face(i: Any) -> StableId | None:
        return reverse.get(("F", int(i)))

    def body(i: Any) -> StableId | None:
        return reverse.get(("B", int(i))) if i else None

    for row in facts.get("small_edges") or []:
        sid = reverse.get(("E", int(row["edge"])))
        if sid is None:
            continue
        length = float(row["length_mm"])
        out.append(QualityFault(
            "small_edge", sid, length / e, f"edge {sid} is {length:.4g} mm long, under e = {e:.4g} mm",
            faces=tuple(f for f in (face(i) for i in row.get("faces") or []) if f),
            measured={"length_mm": length, "e_mm": e, "dihedral_deg": row.get("dihedral_deg")},
            params={"max_length_mm": SIZE_MARGIN * length, "max_width_mm": SIZE_MARGIN * length}))
    for kind, key in (("small_face", "small_faces"), ("sliver", "narrow_faces")):
        for row in facts.get(key) or []:
            sid = face(row["face"])
            if sid is None:
                continue
            area, width = float(row["area_mm2"]), float(row["width_mm"])
            ratio = area / (e * e) if kind == "small_face" else width / e
            why = (f"face {sid} has {area:.4g} mm2, under e^2 = {e * e:.4g} mm2" if kind == "small_face" else
                   f"face {sid} is {width:.4g} mm wide, under e = {e:.4g} mm (a sliver)")
            out.append(QualityFault(kind, sid, ratio, why, faces=(sid,), body=body(row.get("solid")),
                                    measured={"area_mm2": area, "width_mm": width, "e_mm": e,
                                              "neighbours": (row.get("neighbours") or [])[:4]},
                                    params=_merge_params(row, reverse, e, protect=kind == "sliver")))
    total = facts.get("part_volume")
    for row in facts.get("solid_volumes") or []:
        sid = body(row["solid"])
        if sid is None or not total:
            continue
        share = float(row["volume_mm3"]) / float(total)
        if share >= TINY_SOLID_FRACTION:
            continue
        touching = bool(row.get("touching"))
        partner = body(row.get("nearest_solid")) if touching else None
        kind = "tiny_solid" if touching and partner else "floating_solid"
        out.append(QualityFault(
            kind, sid, share / TINY_SOLID_FRACTION,
            f"solid {sid} is {share:.3g} of the part (under {TINY_SOLID_FRACTION:g}), "
            + (f"touching {partner}" if partner else f"floating ({row.get('nearest_mm')} mm from the nearest)"),
            body=sid, measured={"share": share, "nearest_mm": row.get("nearest_mm")},
            params={"max_volume_fraction": min(0.1, SIZE_MARGIN * share), **({"partner": partner} if partner else {})}))
    return sorted(out, key=lambda f: (f.ratio, KINDS.index(f.kind), f.entity))


def _cause(fault: QualityFault) -> dict:
    aim = [fault.entity, fault.params["partner"]] if fault.kind == "tiny_solid" else [fault.entity]
    return {"kind": fault.kind, "aim": aim, "fault": fault.measured, "fault_why": fault.why,
            "faces": list(fault.faces), **fault.params}


def _where(defect: Defect, site: Site | None) -> set[StableId]:
    return set(defect.entities) | set(site.entities if site is not None else ())


def attach_quality(defects: list[Defect], faults: list[QualityFault], sites: dict[str, Site], *,
                   soft: dict[StableId, str], site_of: Any, diagonal_mm: float | None = None) -> list[Defect]:
    """`defects` with the low_quality faults put on them, plus the soft-finding defects. Pure.

    - a low_quality defect whose faces (or bodies) carry a fault gets the WORST one as its cause (kind, aim, the
      ops' parameters); one with none keeps the class's general row;
    - each soft entity (`soft`: stable id -> layer-1 code) the detector confirms under `e`, not already taken by a
      defect, becomes a low_quality validity defect (`SOFT_ID<kind>:<entity>`), `MAX_SOFT_PER_KIND` per code."""
    taken: set[StableId] = set()
    out: list[Defect] = []
    for d in defects:
        if d.cls != LOW_QUALITY:
            out.append(d)
            continue
        where = _where(d, site_of(d, sites))
        hit = [f for f in faults if f.entity in where or set(f.faces) & where or (f.body and f.body in where)]
        if not hit:
            out.append(d)
            continue
        pick = hit[0]
        # a short edge that bounds a flagged face is a symptom of that face (a sliver's two ends are short edges):
        # the face is the cause, and merging it takes its edges with it (measured on the injected step, VM)
        pick = next((f for f in hit if f.kind in ("sliver", "small_face") and f.entity in pick.faces), pick)
        taken.add(pick.entity)
        cause = _cause(pick)
        # the mesher's point rarely names ONE entity: the site's other faults of the same kind ride along, worst
        # first (dev, VM: the single worst face was often not the one the element failed at)
        also = [f for f in hit if f.kind == pick.kind and f is not pick and f.kind in MULTI_KINDS][:MAX_AIM - 1]
        if also:
            cause["aim"] = [pick.entity, *(f.entity for f in also)]
            for key in ("max_length_mm", "max_width_mm"):
                if key in cause:
                    cause[key] = max([cause[key], *(f.params.get(key, 0.0) for f in also)])
            taken.update(f.entity for f in also)
        out.append(dataclasses.replace(d, cause={**d.cause, **cause},
                                       rule=f"{d.rule}; measured {pick.kind}: {pick.why}"
                                            + (f" (+{len(also)} more at the site)" if also else "")))
    per_code: dict[str, int] = {}
    for f in faults:
        code = next((c for c, kinds in SOFT_KINDS.items() if f.kind in kinds and soft.get(f.entity) == c), None)
        if code is None or f.entity in taken or per_code.get(code, 0) >= MAX_SOFT_PER_KIND:
            continue
        per_code[code] = per_code.get(code, 0) + 1
        taken.add(f.entity)
        measurement = {"diagonal_mm": diagonal_mm} if diagonal_mm else {}
        # ONLY the measured entity: when an earlier repair in the round removes it, relocation finds nothing and
        # the loop re-checks it (gone -> resolved by the earlier repair), instead of aiming at a dead id (VM)
        out.append(Defect(id=f"{SOFT_ID}{f.kind}:{f.entity}", kind="validity", code=code, entities=(f.entity,),
                          measurement=measurement, cls=LOW_QUALITY,
                          rule=f"layer-1 soft {code}, measured {f.kind}: {f.why}", cause=_cause(f)))
    return out


#: the detectors' share of one diagnosis
FACTS_BUDGET_S = 30.0


def measure(pool: Any, ws: Any, *, h_mm: float, faces: list[int] | None = None) -> dict | None:
    """One `v5_quality_facts` call on `ws` at e = `E_OF_H` x `h_mm`; None when the kernel gave no answer (a
    measurement that failed degrades the causes, never loses a defect)."""
    from meshpipeline.cad.repair_v4.sites import _call

    return _call(pool, "v5_quality_facts", {"brep_path": ws.brep_path, "e_mm": e_of(h_mm), "faces": faces,
                                            "budget_s": FACTS_BUDGET_S})


def still_there(faults: list[QualityFault], defect: Defect, carried: set[StableId]) -> list[QualityFault]:
    """The faults of the defect's kind left on the entities its own carried forward to (pure). An entity a repair
    removed carries to nothing: gone. A collapsed edge's faces carry on: a small edge left on them is still there."""
    kind = (defect.cause or {}).get("kind")
    same = (kind,) if kind not in ("small_face", "sliver") else ("small_face", "sliver")
    return [f for f in faults if f.kind in same and f.entity in carried]


def h_of(defect: Defect) -> float | None:
    """The element size a defect's faults were measured at (e / E_OF_H), from its cause."""
    e = ((defect.cause or {}).get("fault") or {}).get("e_mm")
    return float(e) / E_OF_H if e else None
