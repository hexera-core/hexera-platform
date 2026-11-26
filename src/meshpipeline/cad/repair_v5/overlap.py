# Responsibility: the DETECTORS of v5's overlap_self_intersection class (build step 8): a duplicate body inside a
#                 body_overlap cluster, and the faces a self-intersecting solid's interference sits on (which of them
#                 is a self-intersecting FACE). They turn the measurement into the defect's cause KIND and AIM.
# Owns: `KIND_DUPLICATE`, `KIND_SELF_FACE`, `attach_overlap_facts` (pure), `measure_overlap` (one kernel call).
# Boundaries: no OCCT here; the measurement is `overlap_kernel.v5_overlap_facts`. The class menus keyed by these kinds
#             are `loop.CLASS_REPAIRS`; `diagnose.diagnose` calls `measure_overlap` once per diagnosis.
"""What the conformal pass leaves of overlap and self-intersection, made aimable.

- a body_overlap cluster with a pair that carries the DUPLICATE signature (same volume, area, centre, moments, box,
  topology) gets cause kind `duplicate_body` and the pairs in `duplicates`: its row puts `drop_duplicate_body` first;
- a self_intersection defect (layer 1 names only the BODY) gets an AIM: the faces the self-interference analysis
  names, a self-intersecting face (its own boundary crosses itself) first, and cause kind `self_intersecting_face`
  when there is one - its row puts `rebuild_face` first. Without an aim the face ops are unaimable, which is what
  every self_intersection defect was until now (b25999e0f8be: conformalise and fuse, both two-body ops, on one body).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect

CLASS = "overlap_self_intersection"
KIND_DUPLICATE = "duplicate_body"
KIND_SELF_FACE = "self_intersecting_face"
FACTS_TIMEOUT_S = 120.0


def _pair_key(a: str, b: str) -> str:
    return f"{a}|{b}" if a <= b else f"{b}|{a}"


def attach_overlap_facts(defects: list[Defect], facts: dict | None, reverse: dict[tuple[str, int], str]) -> list[Defect]:
    """`defects` with the overlap detectors' answer (`v5_overlap_facts`, kernel indices) on them. Pure.
    `reverse` maps (prefix, kernel index) -> stable id (`sites._reverse`). No answer: the defects unchanged."""
    if not facts:
        return defects
    dups = {_pair_key(*ids) for ids in ([reverse.get(("B", int(i))), reverse.get(("B", int(j)))]  # type: ignore[arg-type]
                                        for i, j in facts.get("duplicates") or []) if all(ids)}
    common: dict[str, float] = {}
    for key, vol in (facts.get("common") or {}).items():
        i, _, j = str(key).partition("|")
        a, b = reverse.get(("B", int(i))), reverse.get(("B", int(j)))
        if a and b:
            common[_pair_key(a, b)] = float(vol)
    by_body: dict[str, dict] = {}
    for k, row in (facts.get("self_int") or {}).items():
        body = reverse.get(("B", int(k)))
        if body:
            by_body[body] = row
    out = []
    for d in defects:
        if d.cls != CLASS:
            out.append(d)
            continue
        cause = dict(d.cause or {})
        if d.code == "body_overlap":
            volumes = {k: v for k, v in common.items() if k in {_pair_key(a, b) for a, b in d.pairs}}
            if volumes:
                cause["pair_common_mm3"] = volumes
            mine = [_pair_key(a, b) for a, b in d.pairs if _pair_key(a, b) in dups]
            if mine:
                cause.update(kind=KIND_DUPLICATE, duplicates=mine)
                out.append(dataclasses.replace(d, cause=cause, rule=f"{d.rule}; {len(mine)} pair(s) are one body "
                                                                     f"written twice (duplicate signature)"))
                continue
            if volumes:
                out.append(dataclasses.replace(d, cause=cause))
                continue
        elif d.code == "self_intersection":
            rows = [by_body[b] for b in d.entities if b in by_body]
            wsi = [reverse.get(("F", int(i))) for r in rows for i in r.get("wire_self_int") or []]
            faces = [reverse.get(("F", int(i))) for r in rows for i in r.get("faces") or []]
            aim = list(dict.fromkeys(f for f in [*wsi, *faces] if f))
            if aim:
                cause.update(aim=aim, interference_faces=[f for f in faces if f],
                             self_intersecting_faces=[f for f in wsi if f])
                if any(wsi):
                    cause["kind"] = KIND_SELF_FACE
                rule = (f"{d.rule}; interference on {len(aim)} face(s)"
                        + (f", {len([f for f in wsi if f])} with a self-crossing boundary" if any(wsi) else ""))
                out.append(dataclasses.replace(d, cause=cause, rule=rule))
                continue
            errors = [r["error"] for r in rows if r.get("error")]
            if errors:
                cause["locate_error"] = errors[0]
                out.append(dataclasses.replace(d, cause=cause))
                continue
        out.append(d)
    return out


def measure_overlap(pool: Any, ws: Any, defects: list[Defect]) -> dict | None:
    """One `v5_overlap_facts` call for the class's defects on `ws` (None when there is nothing to measure or the
    kernel gave no answer - a failed measurement degrades the cause, never loses a defect)."""
    idx = getattr(ws, "indices", None) or {}
    pairs = sorted({(int(idx[a]), int(idx[b])) for d in defects if d.cls == CLASS and d.code == "body_overlap"
                    for a, b in d.pairs if a in idx and b in idx})
    bodies = sorted({int(idx[b]) for d in defects if d.cls == CLASS and d.code == "self_intersection"
                     for b in d.entities if str(b)[:1] == "B" and b in idx})
    if not pairs and not bodies:
        return None
    try:
        got = pool.call("v5_overlap_facts", {"brep_path": ws.brep_path,
                                             "part_diag_mm": getattr(ws, "part_diag_mm", None),
                                             "pairs": [list(p) for p in pairs], "self_int": bodies},
                        timeout_s=FACTS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - an unmeasured part keeps its defects as they were
        return None
    return got.value if getattr(got, "ok", False) and got.value is not None else None
