# Responsibility: Build the observation a model author reads about ONE region - everything it may
#                 see, as bounded JSON.
# Owns: which facts about a region, its defects, its target faces, the attempts already made on it
#       and the caps that bind a package are shown, and how each is clipped to a fixed size.
# Boundaries: read-only. It decides nothing, edits nothing and calls no model. Kernel access is
#             confined to the default face reader, imported lazily, so every other field is
#             computable (and testable) without OCP. It does not import `agents/`: the clipping
#             helpers mirror `agents/repair_review/_wire.py` rather than reach up a layer.
# Collaborates with: cad/repair/regions.py (Region), cad/repair/localize.py (EntityDefect),
#                    cad/repair/session.py (PackageVerdict), cad/repair/caps.py (Measure),
#                    settings/cad_repair.py (the caps), cad/repair/rules_author.py (same caps).
from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable
from typing import Any

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import PackageVerdict

# WHY EVERYTHING IS CLIPPED. The observation is a prompt. A region of hundreds of defects, each
# with a multi-kilobyte gmsh message, must not become an unbounded prompt: the worst defects are
# kept first and the rest are counted, so the model knows what it was not shown.
#
# EVERY OMISSION IS VISIBLE IN THE DATA. This dict is the model's entire view of a region; a model
# that cannot tell "nothing there" from "I was not shown" authors on a distorted world. So each
# clip leaves a count (`defects_omitted`, `faces_omitted`, `faces_skipped`, `attempts_omitted`,
# `measurements_omitted`) and each degraded source a marker (`faces_status`, `defects_scope`).
#
# FACE INDICES ARE 1-BASED, as `localize` records them (a `TopTools_IndexedMapOfShape` index); 0
# means "no index" and is never a face. `read_faces` skips anything below 1 for the same reason.
#
# `agents/repair_review/_wire.py` may later import `clip_text`/`clip_measurements` from here
# (agents -> cad is the allowed direction); until then they are mirrored.

TEXT_MAX = 200
MEASURE_KEYS_MAX = 8
DEFECTS_MAX = 12
FACES_MAX = 20
ATTEMPTS_MAX = 4
REASON_MAX = 400
TARGETS_MAX = 6
FACE_INDICES_MAX = 40
OPS_PER_ATTEMPT_MAX = 6
#: Always shown whole, whatever the clip: the facts that say what went wrong in meshing.
_KEEP_FIRST = ("message", "stage")
#: The hard ceiling. The clips above keep a typical region well under it; the shrink loop in
#: `observation_of` keeps a pathological one under it too.
BYTES_MAX = 15_000
_SEVERITY_RANK = {"fatal": 0, "error": 1, "warning": 2, "info": 3}

logger = logging.getLogger(__name__)

FaceReader = Callable[[object, Iterable[int]], dict[int, dict]]


def clip_text(value: object, limit: int = TEXT_MAX) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def clip_measurements(measurements: dict | None) -> dict:
    """Bounded: `message` and `stage` first, then up to MEASURE_KEYS_MAX keys, long values clipped."""
    source = dict(measurements or {})
    ordered = [k for k in _KEEP_FIRST if k in source] + [k for k in source if k not in _KEEP_FIRST]
    out: dict = {}
    for key in ordered[:MEASURE_KEYS_MAX]:
        value = source[key]
        out[str(key)[:60]] = (value if value is None or isinstance(value, (int, float, bool))
                              else clip_text(value, 120 if key == "message" else 80))
    return out


def read_faces(shape, indices: Iterable[int]) -> dict[int, dict]:
    """Default face reader: surface type, area, bbox and neighbour count from the kernel."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    out: dict[int, dict] = {}
    for index in sorted(indices):
        if not 1 <= index <= faces.Extent():
            continue
        face = TopoDS.Face_s(faces.FindKey(index))
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        box = Bnd_Box()
        BRepBndLib.Add_s(face, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        edges = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(face, TopAbs_EDGE, edges)
        near: set[int] = set()
        for e in range(1, edges.Extent() + 1):
            edge = edges.FindKey(e)
            if ancestors.Contains(edge):
                for other in ancestors.FindFromKey(edge):
                    n = faces.FindIndex(other)
                    if n and n != index:
                        near.add(int(n))
        out[index] = {"surface_type": str(BRepAdaptor_Surface(face).GetType()).rsplit(".", 1)[-1],
                      "area_mm2": float(props.Mass()),
                      "bbox_min": [xmin, ymin, zmin], "bbox_max": [xmax, ymax, zmax],
                      "neighbours": len(near)}
    return out


def _vec(point) -> list[float] | None:
    return None if point is None else [round(float(v), 6) for v in point]


def _text(value: object, limit: int = TEXT_MAX) -> str | None:
    return None if value is None else clip_text(value, limit)


def _defect_of(defect) -> dict:
    raw = dict(defect.measurements or {})
    return {"code": str(getattr(defect.code, "value", defect.code)),
            "fingerprint": str(defect.fingerprint),
            "severity": str(getattr(defect.severity, "value", defect.severity)),
            "entity": f"{defect.entity_type}:{defect.entity_index}",
            "message": _text(defect.message),
            "measurements": clip_measurements(raw),
            "measurements_omitted": max(0, len(raw) - MEASURE_KEYS_MAX)}


def _attempt_of(verdict: PackageVerdict) -> dict:
    ops = [{"op": v.op, "targets": [clip_text(t, 60) for t in list(v.targets)[:TARGETS_MAX]],
            "targets_omitted": max(0, len(v.targets) - TARGETS_MAX),
            "defect_code": v.defect_code, "status": v.status,
            "reason": _text(v.reason, REASON_MAX)} for v in verdict.verdicts[:OPS_PER_ATTEMPT_MAX]]
    return {"status": verdict.status, "reason": _text(verdict.reason, REASON_MAX),
            "ops": ops, "ops_omitted": max(0, len(verdict.verdicts) - OPS_PER_ATTEMPT_MAX)}


def _caps(original: Measure) -> dict:
    return {"max_area_delta_mm2": float(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO)
            * original.surface_area_mm2,
            "max_volume_delta_mm3": float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)
            * original.volume_mm3,
            "max_ops": int(rcfg.CAD_REPAIR_MAX_OPS),
            "timeout_s": float(rcfg.CAD_REPAIR_TIMEOUT_S)}


def _strict(value):
    """JSON-strict: tuples become lists, NaN and infinities become strings."""
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(k): _strict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict(v) for v in value]
    return value


def _size(obj: dict) -> int:
    return len(json.dumps(obj, allow_nan=False).encode())


def _minimal(out: dict[str, Any], errors: list[dict]) -> dict[str, Any]:
    """The last resort. It still says what it is (`truncated`), where the defects came from, whether
    the faces were readable, and WHICH error defects exist: an empty-but-confident view is the one
    thing this dict must never be."""
    base = {"truncated": "minimal", "region": {"index": out["region"]["index"]},
            "defect_count": out["defect_count"], "defects_scope": out["defects_scope"],
            "faces_status": out["faces_status"], "caps": out["caps"]}
    tiers = ([{"code": d["code"], "fingerprint": d["fingerprint"]} for d in errors],
             sorted({d["code"] for d in errors}))
    for key, listing in zip(("error_defects", "error_codes"), tiers, strict=True):
        candidate = {**base, key: listing, "error_defects_total": len(errors)}
        if _size(candidate) <= BYTES_MAX:
            return candidate
    return base


def _bounded(out: dict[str, Any]) -> dict[str, Any]:
    """Shrink to BYTES_MAX: faces, then oldest attempts, then defects, each leaving its count."""
    # Taken BEFORE shrinking: the shrink may empty `defects`, and the last resort needs these.
    errors = [d for d in out["defects"] if d.get("severity") in ("error", "fatal")]

    def drop_face() -> bool:
        if len(out["faces"]) <= 0:
            return False
        out["faces"].pop()
        out["faces_omitted"] += 1
        return True

    def drop_attempt() -> bool:
        if not out["attempts"]:
            return False
        out["attempts"].pop(0)
        out["attempts_omitted"] += 1
        return True

    def drop_defect() -> bool:
        if not out["defects"]:
            return False
        out["defects"].pop()
        out["defects_omitted"] += 1
        return True

    floors = ((drop_face, 5, "faces"), (drop_attempt, 1, "attempts"), (drop_defect, 3, "defects"),
              (drop_face, 0, "faces"), (drop_attempt, 0, "attempts"), (drop_defect, 0, "defects"))
    for step, floor, key in floors:
        while _size(out) > BYTES_MAX and len(out[key]) > floor and step():
            pass
    if _size(out) > BYTES_MAX:
        # Final resort: the ceiling is a guarantee, so shed the listing of indices and say so.
        out["region"]["face_indices_omitted"] = len(out["region"]["face_indices"])
        out["region"]["face_indices"] = []
    if _size(out) > BYTES_MAX:
        out = _minimal(out, errors)
    return out


def observation_of(region: Region, defects: tuple, attempts: tuple[PackageVerdict, ...],
                   shape, *, original: Measure,
                   face_reader: FaceReader | None = None) -> dict:
    """Everything a model author may see about `region`, JSON-safe and size-bounded."""
    mine = [d for d in defects if d.fingerprint in region.fingerprints]
    scope = "region"
    if not mine:
        # Kept so a region whose fingerprints drifted still shows SOMETHING, but never silently.
        mine, scope = list(defects), "part-wide-fallback"
    # Worst first, stable within a severity so the observation is deterministic.
    mine.sort(key=lambda d: _SEVERITY_RANK.get(str(getattr(d.severity, "value", d.severity)), 9))
    shown = mine[:DEFECTS_MAX]

    # Faces the SHOWN defects point at come first, then the rest by index.
    pointed = [int(d.entity_index) for d in shown if d.entity_type == "face"]
    ordered = list(dict.fromkeys(
        [i for i in pointed if i in region.face_indices] + sorted(region.face_indices)))
    valid = [i for i in ordered if i >= 1]
    wanted = valid[:FACES_MAX]
    skipped = len(ordered) - len(valid)
    status = "ok"
    found: dict = {}
    if wanted:
        try:
            found = (face_reader or read_faces)(shape, wanted)
        except Exception as exc:  # noqa: BLE001 - an unreadable face is marked, not a failure
            logger.warning("observation could not read faces: %s", type(exc).__name__,
                           exc_info=True)
            status = f"unavailable: {type(exc).__name__}"
    faces = [{"index": int(i), "surface_type": _text(found[i].get("surface_type")),
              "area_mm2": found[i].get("area_mm2"), "bbox_min": _vec(found[i].get("bbox_min")),
              "bbox_max": _vec(found[i].get("bbox_max")), "neighbours": found[i].get("neighbours")}
             for i in wanted if i in found]
    skipped += len(wanted) - len(faces) if status == "ok" else 0
    listed = sorted(region.face_indices)
    out: dict[str, Any] = {
        "region": {"index": int(region.index), "located": bool(region.located),
                   "bbox_min": _vec(region.bbox_min), "bbox_max": _vec(region.bbox_max),
                   "face_indices": listed[:FACE_INDICES_MAX],
                   "face_indices_omitted": max(0, len(listed) - FACE_INDICES_MAX)},
        "defect_count": len(mine),
        "defects_scope": scope,
        "defects": [_defect_of(d) for d in shown],
        "defects_omitted": len(mine) - len(shown),
        "face_count_total": len(listed),
        "faces_status": status,
        "faces_skipped": skipped,
        "faces": faces,
        "faces_omitted": max(0, len(valid) - len(wanted)),
        "attempts": [_attempt_of(a) for a in attempts[-ATTEMPTS_MAX:]],
        "attempts_omitted": max(0, len(attempts) - ATTEMPTS_MAX),
        "caps": _caps(original),
        "part": {"diagonal_mm": float(original.diagonal_mm),
                 "surface_area_mm2": float(original.surface_area_mm2),
                 "volume_mm3": float(original.volume_mm3)},
    }
    return _bounded(_strict(out))
