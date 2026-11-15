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
from collections.abc import Callable, Iterable
from typing import Any

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import PackageVerdict

# WHY EVERYTHING IS CLIPPED. The observation is a prompt. A region of hundreds of defects, each
# with a multi-kilobyte gmsh message, must not become an unbounded prompt: the worst defects are
# kept first and the rest are counted, so the model knows what it was not shown.

TEXT_MAX = 200
MEASURE_KEYS_MAX = 8
DEFECTS_MAX = 12
FACES_MAX = 20
ATTEMPTS_MAX = 4
OPS_PER_ATTEMPT_MAX = 6
#: Always shown whole, whatever the clip: the facts that say what went wrong in meshing.
_KEEP_FIRST = ("message", "stage")
#: The hard ceiling. The clips above keep a typical region well under it; the shrink loop in
#: `observation_of` keeps a pathological one under it too.
BYTES_MAX = 15_000
_SEVERITY_RANK = {"fatal": 0, "error": 1, "warning": 2, "info": 3}

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
        out[str(key)[:60]] = (value if isinstance(value, (int, float, bool))
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
    for index in sorted(indices)[:FACES_MAX]:
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


def _defect_of(defect) -> dict:
    return {"code": str(getattr(defect.code, "value", defect.code)),
            "fingerprint": str(defect.fingerprint),
            "severity": str(getattr(defect.severity, "value", defect.severity)),
            "entity": f"{defect.entity_type}:{defect.entity_index}",
            "message": clip_text(defect.message),
            "measurements": clip_measurements(defect.measurements)}


def _attempt_of(verdict: PackageVerdict) -> dict:
    ops = [{"op": v.op, "defect_code": v.defect_code, "status": v.status,
            "reason": clip_text(v.reason)} for v in verdict.verdicts[:OPS_PER_ATTEMPT_MAX]]
    return {"status": verdict.status, "reason": clip_text(verdict.reason),
            "ops": ops, "ops_omitted": max(0, len(verdict.verdicts) - OPS_PER_ATTEMPT_MAX)}


def _caps(original: Measure) -> dict:
    return {"max_area_delta_mm2": float(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO)
            * original.surface_area_mm2,
            "max_volume_delta_mm3": float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)
            * original.volume_mm3,
            "max_ops": int(rcfg.CAD_REPAIR_MAX_OPS),
            "timeout_s": float(rcfg.CAD_REPAIR_TIMEOUT_S)}


def observation_of(region: Region, defects: tuple, attempts: tuple[PackageVerdict, ...],
                   shape, *, original: Measure,
                   face_reader: FaceReader | None = None) -> dict:
    """Everything a model author may see about `region`, JSON-safe and size-bounded."""
    mine = [d for d in defects if d.fingerprint in region.fingerprints] or list(defects)
    # Worst first, stable within a severity so the observation is deterministic.
    mine.sort(key=lambda d: _SEVERITY_RANK.get(str(getattr(d.severity, "value", d.severity)), 9))
    shown = mine[:DEFECTS_MAX]
    try:
        found = (face_reader or read_faces)(shape, region.face_indices) if region.face_indices \
            else {}
    except Exception:  # noqa: BLE001 - an unreadable face is absent from the view, not a failure
        found = {}
    faces = [{"index": int(i), "surface_type": str(f.get("surface_type", "")),
              "area_mm2": f.get("area_mm2"), "bbox_min": _vec(f.get("bbox_min")),
              "bbox_max": _vec(f.get("bbox_max")), "neighbours": f.get("neighbours")}
             for i, f in sorted(found.items())[:FACES_MAX]]
    out: dict[str, Any] = {
        "region": {"index": int(region.index), "located": bool(region.located),
                   "bbox_min": _vec(region.bbox_min), "bbox_max": _vec(region.bbox_max),
                   "face_indices": sorted(region.face_indices)[:FACES_MAX * 2]},
        "defect_count": len(mine),
        "defects": [_defect_of(d) for d in shown],
        "defects_omitted": len(mine) - len(shown),
        "faces": faces,
        "attempts": [_attempt_of(a) for a in attempts[-ATTEMPTS_MAX:]],
        "caps": _caps(original),
        "part": {"diagonal_mm": float(original.diagonal_mm),
                 "surface_area_mm2": float(original.surface_area_mm2),
                 "volume_mm3": float(original.volume_mm3)},
    }
    while len(json.dumps(out).encode()) > BYTES_MAX and out["defects"]:
        out["defects"].pop()
        out["defects_omitted"] += 1
    return out
