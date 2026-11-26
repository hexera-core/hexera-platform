# Responsibility: Build and deliver the REPAIR VIEWER's payload (CRS v5 beta, Decision 2): the
#                 original and the repaired CAD as triangle surfaces with each triangle's B-rep
#                 face, how far each repaired face moved from the upload, and the problems pinned
#                 where they were found.
# Owns: the payload's shape (`repair-viewer/1`), the per-face tessellation, the per-face deviation
#       field, the pins, and the one artifact row (`repair_viewer`) that holds them.
# Boundaries: EVIDENCE for a view - it never decides a status and never fails a run: every failure
#             is logged and the payload (or the artifact) is simply absent, and the API's repair
#             surface routes answer 404. Runs on the worker that repaired the part, where both
#             files are on disk; the API reads only the stored JSON.
# Collaborates with: pipeline/repair_attempt.py (records the two paths), pipeline/repair_result.py
#                    (the section the pins come from), cad/repair/deviation.py and
#                    cad/repair/occ_distance.py (how far a face moved), api/v1/simulation.py (the
#                    `/repair/surfaces` route).
#
# THE PAYLOAD (version "repair-viewer/1"):
#   {"version",
#    "original": SKIN, "repaired": SKIN | None,
#    "pins": [{"id", "type", "type_text", "status", "text", "point": [x, y, z] mm, "faces": [...]}],
#    "deviation": {"by_face": {"<face>": mm | None}, "max_mm", "allowed_mm", "measured": bool}}
#   SKIN is the geometry check's skin shape (application/geometry_check.skin_payload), so the
#   console draws it with the renderer it draws a check's skin with: points in METRES (the file's
#   millimetres / 1000), `rescaled_by` 1, one patch:
#   {"kind": "skin", "mesh_units": "m", "is_mesh": False, "rescaled_by": 1.0,
#    "faces": n_faces, "triangles": n, "decimated": bool,
#    "patches": [{"name", "type": "", "face_count", "tri_count",
#                 "points_b64": float32 x 3 per point, "polys_b64": uint32 VTK [3, a, b, c] per tri,
#                 "face_b64": uint32 per triangle (1-based B-rep face, the kernel's face order),
#                 "deviation_b64": float32 per POINT, millimetres moved (repaired only; -1 where
#                 not measured)}]}
from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from meshpipeline.pipeline.repair_result import _sub

logger = logging.getLogger(__name__)

VERSION = "repair-viewer/1"
#: The per-job logical identity of the payload's artifact row.
REPAIR_VIEWER_LOGICAL_KEY = "repair_viewer"
#: A viewer draws this many triangles per surface at most; past it every k-th is kept.
MAX_TRIANGLES = 400_000
#: Points sampled per changed face, and the whole deviation field's time budget.
SAMPLES_PER_FACE = 6
DEVIATION_BUDGET_S = 60.0
#: Changed faces measured at most; past it the rest read unmeasured (-1), never a guess.
MAX_MEASURED_FACES = 400


def _b64(array) -> str:
    import numpy as np
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def faces_and_triangles(shape) -> tuple[Any, Any, Any, list]:
    """Per-face tessellation: (points (n,3) float, triangles (m,3) int, face index per triangle
    (1-based, the kernel's indexed face order), the face objects)."""
    import numpy as np
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.occ_box import incremental_mesh
    from meshpipeline.cad.repair.localize import _indexed

    incremental_mesh(shape, None, None, parallel=True)
    indexed = _indexed(shape, TopAbs_FACE)
    faces = [indexed.FindKey(i) for i in range(1, indexed.Extent() + 1)]
    blocks, tris, owner = [], [], []
    base = 0
    for i, face in enumerate(faces, start=1):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(face), loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
        trsf = loc.Transformation()
        pts = np.array([(lambda p: (p.X(), p.Y(), p.Z()))(tri.Node(k).Transformed(trsf))
                        for k in range(1, tri.NbNodes() + 1)], dtype=float)
        t = np.array([tri.Triangle(k).Get() for k in range(1, tri.NbTriangles() + 1)],
                     dtype=np.int64) - 1 + base
        blocks.append(pts)
        tris.append(t)
        owner.append(np.full(len(t), i, dtype=np.uint32))
        base += len(pts)
    xyz = np.vstack(blocks) if blocks else np.zeros((0, 3))
    tri_all = np.vstack(tris) if tris else np.zeros((0, 3), dtype=np.int64)
    face_of = np.concatenate(owner) if owner else np.zeros(0, dtype=np.uint32)
    return xyz, tri_all, face_of, faces


def point_faces(n_points: int, tris, face_of):
    """The B-rep face each point belongs to. Each face is tessellated with its own points, so a
    point has exactly one face; a point no triangle uses reads 0."""
    import numpy as np
    out = np.zeros(int(n_points), dtype=np.uint32)
    for k in range(3):
        out[np.asarray(tris)[:, k]] = face_of
    return out


def surface_block(xyz_mm, tris, face_of, *, name: str, per_point: Any = None) -> dict:
    """One SKIN of the payload (see the module header); decimated past MAX_TRIANGLES. Points
    leave in metres, like a check's skin; `per_point` (millimetres moved) rides as is."""
    import numpy as np
    decimated = False
    if len(tris) > MAX_TRIANGLES:
        step = int(math.ceil(len(tris) / MAX_TRIANGLES))
        keep = np.arange(len(tris)) % step == 0
        tris, face_of = tris[keep], face_of[keep]
        decimated = True
    polys = np.empty((len(tris), 4), dtype=np.uint32)
    polys[:, 0] = 3
    polys[:, 1:] = np.asarray(tris, dtype=np.uint32)
    patch: dict[str, Any] = {
        "name": name, "type": "", "face_count": int(len(tris)), "tri_count": int(len(tris)),
        "points_b64": _b64((np.asarray(xyz_mm, dtype=np.float64) / 1000.0).astype(np.float32)
                           .ravel()),
        "polys_b64": _b64(polys.ravel()),
        "face_b64": _b64(np.asarray(face_of, dtype=np.uint32))}
    if per_point is not None:
        patch["deviation_b64"] = _b64(np.asarray(per_point, dtype=np.float32))
    return {"kind": "skin", "mesh_units": "m", "is_mesh": False, "rescaled_by": 1.0,
            "faces": int(face_of.max()) if len(face_of) else 0, "triangles": int(len(tris)),
            "decimated": decimated, "patches": [patch]}


def face_deviation(before_shape, after_shape, after_faces: list, *,
                   budget_s: float = DEVIATION_BUDGET_S) -> tuple[dict[int, float | None], bool]:
    """How far each face of the repaired shape moved from the upload, by 1-based face index.

    A face the upload ALSO HAS (same fingerprint, cad/repair/deviation's naming) did not move: 0.
    A changed or added face is sampled and measured against the upload in one bounded child
    (cad/repair/occ_distance); its value is the furthest sample. Unmeasured (budget, cap, a
    failure) is None, never a guess. Returns (by_face, all_measured)."""
    from meshpipeline.cad.repair import deviation as dev
    from meshpipeline.cad.repair import occ_distance

    changed = dev._select_faces(before_shape, after_shape, ())
    changed_ids = {id(f) for f in changed}
    by_face: dict[int, float | None] = {}
    targets: list[tuple[int, list]] = []
    for index, face in enumerate(after_faces, start=1):
        if not any(face.IsSame(c) for c in changed) and id(face) not in changed_ids:
            by_face[index] = 0.0
            continue
        by_face[index] = None
        if len(targets) < MAX_MEASURED_FACES:
            targets.append((index, dev._face_points(face, SAMPLES_PER_FACE)))
    points = [p for _, pts in targets for p in pts]
    if not points:
        return by_face, all(v is not None for v in by_face.values())
    started = time.monotonic()
    answers = occ_distance.distances([before_shape, *(dev._vertex(p) for p in points)],
                                     [(0, n + 1) for n in range(len(points))],
                                     budget_s=budget_s)
    cursor = 0
    for index, pts in targets:
        mine = answers[cursor:cursor + len(pts)]
        cursor += len(pts)
        if len(mine) == len(pts) and all(a.get("distance_status") == "measured"
                                         and a.get("min_distance_mm") is not None for a in mine):
            by_face[index] = max(float(a["min_distance_mm"]) for a in mine) if mine else 0.0
    logger.info("repair viewer: %d changed face(s) measured in %.1fs", len(targets),
                time.monotonic() - started)
    return by_face, all(v is not None for v in by_face.values())


def _face_number(value: object) -> int | None:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def pins(section: Mapping, xyz, tris, face_of) -> list[dict]:
    """The problems pinned where they were found: the engine's own point when it gave one, else
    the centre of the first named face's triangles on the ORIGINAL surface."""
    out: list[dict] = []
    for p in section.get("problems") or ():
        if not isinstance(p, Mapping):
            continue
        location = _sub(p, "location")
        point = None
        for q in location.get("points") or ():
            if isinstance(q, (list, tuple)) and len(q) >= 3:
                point = [float(c) for c in q[:3]]
                break
        if point is None and len(tris):
            for f in location.get("faces") or ():
                n = _face_number(f)
                mask = face_of == n if n is not None else None
                if mask is not None and mask.any():
                    centre = xyz[tris[mask].ravel()].mean(axis=0)
                    point = [float(c) for c in centre]
                    break
        out.append({"id": str(p.get("id") or ""), "type": p.get("type"),
                    "type_text": p.get("type_text"), "status": p.get("status"),
                    "text": p.get("reason_text"), "point": point,
                    "faces": list(location.get("faces") or [])[:20]})
    return out


def build(original: Path, repaired: Path | None, section: Mapping) -> dict:
    """The payload for one run. Raises on an unreadable original - `deliver` catches it."""
    import numpy as np

    from meshpipeline.cad.repair.brep import _read_shape

    before = _read_shape(Path(original))
    xyz0, tri0, face0, _ = faces_and_triangles(before)
    payload: dict[str, Any] = {"version": VERSION,
                               "original": surface_block(xyz0, tri0, face0, name="original"),
                               "repaired": None,
                               "pins": pins(section, xyz0, tri0, face0),
                               "deviation": {"by_face": {}, "max_mm": None,
                                             "allowed_mm": section.get("allowed_change_mm"),
                                             "measured": False}}
    if repaired is None or not Path(repaired).is_file():
        return payload
    after = _read_shape(Path(repaired))
    xyz1, tri1, face1, faces1 = faces_and_triangles(after)
    try:
        by_face, complete = face_deviation(before, after, faces1)
    except Exception as exc:  # noqa: BLE001 - no field is better than a wrong one
        logger.warning("repair viewer: the deviation field was not measured (%s)",
                       type(exc).__name__)
        by_face, complete = {}, False

    def _moved(face: int) -> float:
        value = by_face.get(face)
        return -1.0 if value is None else float(value)

    per_point = np.array([_moved(int(f)) for f in point_faces(len(xyz1), tri1, face1)],
                         dtype=np.float32)
    known = [v for v in by_face.values() if v is not None]
    payload["repaired"] = surface_block(xyz1, tri1, face1, name="repaired", per_point=per_point)
    payload["deviation"] = {"by_face": {str(k): v for k, v in by_face.items()},
                            "max_mm": max(known) if known else None,
                            "allowed_mm": section.get("allowed_change_mm"),
                            "measured": bool(complete and by_face)}
    return payload


def paths_of(state: Mapping) -> tuple[Path | None, Path | None]:
    """(original, repaired) the attempt recorded, each only when it is on disk here."""
    attempt = _sub(state, "repair_attempt")
    viewer = _sub(attempt, "viewer")

    def _on_disk(value: object) -> Path | None:
        try:
            path = Path(str(value or ""))
            return path if str(value or "") and path.is_file() else None
        except Exception:  # noqa: BLE001
            return None

    original = _on_disk(viewer.get("original"))
    if original is None and attempt and not state.get("repair_lineage"):
        # A REPAIR THAT DECLINED records no viewer paths, and the run's geometry is still the
        # upload (nothing was promoted): the view draws that original alone, problems pinned
        try:
            from meshpipeline.pipeline.geometry_state import materialized
            geometry = materialized(state)
            original = _on_disk(getattr(geometry, "path", None)) if geometry is not None else None
        except Exception:  # noqa: BLE001 - no original is no view, never a failure
            original = None
    return original, _on_disk(viewer.get("repaired"))


async def deliver(session_factory, *, job_id: str, state: Mapping, section: Mapping,
                  owner_id: str, delivery_attempt: int = 0, execution_generation: int = 0,
                  fence_commit=None) -> str:
    """Build the payload off the event loop and store it as the job's `repair_viewer` artifact.
    Returns the delivery's outcome name, or "" when there was nothing to draw or it failed.
    NEVER RAISES but for a lost claim (the caller's fence)."""
    import asyncio
    import tempfile

    from meshpipeline.contracts.execution_guard import StaleWorkerFenced

    original, repaired = paths_of(state)
    if original is None:
        return ""
    try:
        with tempfile.TemporaryDirectory(prefix="repair_viewer_") as tmp:
            path = Path(tmp) / "repair_viewer.json"
            spec = Path(tmp) / "section.json"
            spec.write_text(json.dumps(dict(section or {}), default=str))
            # IN A FRESH INTERPRETER, never in this worker: OCC holds the GIL through a kernel
            # call and can crash on a pathological part, and a view must never cost the run
            # its delivery. The child's whole process group is killed past its wall.
            built = await asyncio.to_thread(_build_in_child, original, repaired, spec, path,
                                            CHILD_WALL_S)
            if not built:
                logger.warning("repair viewer: not built for job %s", job_id)
                return ""
            from meshpipeline.application.repair_report_delivery import _deliver_file
            from meshpipeline.artifact_keys import repair_viewer_key
            from meshpipeline.persistence.models import ArtifactType
            return await _deliver_file(
                session_factory, job_id=job_id, local_path=path,
                key_for=lambda digest: repair_viewer_key(str(job_id), digest),
                logical_key=REPAIR_VIEWER_LOGICAL_KEY, artifact_type=ArtifactType.repair_viewer,
                content_type="application/json", what="repair viewer", owner_id=owner_id,
                delivery_attempt=delivery_attempt, execution_generation=execution_generation,
                fence_commit=fence_commit)
    except StaleWorkerFenced:
        raise
    except Exception as exc:  # noqa: BLE001 - a view never costs a run
        logger.warning("repair viewer: not stored for job %s (%s)", job_id, type(exc).__name__)
        return ""


#: The child's whole wall: the deviation budget plus reading and tessellating two parts.
CHILD_WALL_S = DEVIATION_BUDGET_S + 120.0


def _build_in_child(original: Path, repaired: Path | None, spec: Path, out: Path,
                    wall_s: float) -> bool:
    """Run `build` in a fresh interpreter (`python -m meshpipeline.application.repair_viewer`),
    in its own session so that a run past `wall_s` is killed with everything it started (the
    deviation's own distance child included). True when the payload was written. Never raises."""
    import os
    import signal
    import subprocess
    import sys

    cmd = [sys.executable, "-m", "meshpipeline.application.repair_viewer", str(original),
           str(repaired or ""), str(spec), str(out)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair viewer: the child could not start (%s)", type(exc).__name__)
        return False
    try:
        _, err = proc.communicate(timeout=wall_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        logger.warning("repair viewer: the child ran past %.0fs and was killed", wall_s)
        return False
    if proc.returncode != 0:
        logger.warning("repair viewer: the child ended %s (%s)", proc.returncode,
                       (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:])
        return False
    return out.is_file()


def main(argv: list[str]) -> int:
    """The child's entry: `<original> <repaired or ""> <section.json> <out.json>`."""
    original, repaired, spec, out = argv
    section = json.loads(Path(spec).read_text())
    payload = build(Path(original), Path(repaired) if repaired else None, section)
    Path(out).write_text(json.dumps(payload, separators=(",", ":")))
    return 0


__all__ = ["CHILD_WALL_S", "REPAIR_VIEWER_LOGICAL_KEY", "VERSION", "build", "deliver",
           "face_deviation", "faces_and_triangles", "main", "paths_of", "pins", "point_faces",
           "surface_block"]


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
