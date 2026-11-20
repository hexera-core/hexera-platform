# Responsibility: Ask gmsh whether a B-rep will mesh and, when it will not, say why - which kind of
#                 failure, which gmsh surface tags, and where - plus every surface's bounding box.
# Boundaries: it meshes in a freshly exec'd interpreter (never a fork: production calls from a
#             threaded process, where a forked child's first gmsh import can crash or hang) and reports. It mutates no shape and repairs
#             nothing. gmsh is imported only inside the child, never at module level, so this module
#             imports (and `classify` runs) on a host with no gmsh.
# Collaborates with: cad/repair/localize.py (its `_centroid_and_box`, `_stamped` and `in_region` place and name the defects
#                    `mesh_defects` turns a failure into, so they read exactly like a scan's).
from __future__ import annotations

import bisect
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import (
    EntityDefect,
    _bounds_of,
    _centroid_and_box,
    _indexed,
    _stamped,
    in_region,
)
from meshpipeline.cad.repair.mesh_clash import Clash, reported_kind

if TYPE_CHECKING:
    from meshpipeline.cad.repair.regions import Region

logger = logging.getLogger(__name__)

OVERLAPPING_FACETS = "overlapping_facets"
EDGE_THROUGH_FACE = "edge_through_face"
OPEN_LOOP = "open_loop"
PERIODIC_SURFACE = "periodic_surface"
NO_VOLUME = "no_volume"
TIMEOUT = "timeout"
#: The probe child died (non-zero exit, a signal) without answering: not a timeout, not a verdict.
PROBE_ERROR = "probe_error"
OTHER = "other"

#: Which step of the child's work raised. A surface read off the log is the one being meshed only
#: when the 2D stage is what failed; once the volume is meshing it is merely the last 2D surface.
STAGE_IMPORT = "import"
STAGE_2D = "2d"
STAGE_3D = "3d"

#: xmin, ymin, zmin, xmax, ymax, zmax
Box = tuple[float, float, float, float, float, float]

#: The probe's mesh size is the part's diagonal over this: coarse enough to be quick, fine enough
#: that a real defect still shows.
_SIZE_DIVISOR = 40.0


@dataclass(frozen=True, slots=True)
class MeshFailure:
    kind: str
    surfaces: tuple[int, ...]
    point: tuple[float, float, float] | None
    message: str
    #: The stage that raised (STAGE_*), or "" when not known.
    stage: str = ""
    #: True when `surfaces` was not in gmsh's error text but taken from the log's last
    #: "Meshing surface N". See `locating_surfaces` for when that may be trusted.
    surfaces_from_log: bool = False


@dataclass(frozen=True, slots=True)
class ProbeResult:
    meshed: bool
    volume_elements: int
    failure: MeshFailure | None
    tag_boxes: dict[int, Box] = field(default_factory=dict)
    #: Surface pairs whose 2D triangles cross or coincide, found on the surface mesh after the
    #: VOLUME stage refused it (`mesh_clash`). Empty when it meshed or failed earlier.
    clashes: tuple[Clash, ...] = ()


#: The clash search's own wall in the probe child, in seconds. Bounded so it can never spend the
#: probe's budget and cost the parent the gmsh failure the child already holds (Greptile #495):
#: past it the search answers with what it found, which may be nothing (an unlocated failure).
CLASH_SEARCH_S = 20.0

#: Volume-stage failures whose cause is the surface mesh crossing itself: worth searching the
#: surface mesh for the clashing triangles. Kinds about the whole part (no volume, a timeout) are not.
_CLASH_KINDS = frozenset({OVERLAPPING_FACETS, EDGE_THROUGH_FACE, OTHER})


def _surface_clashes(gmsh) -> tuple[Clash, ...]:
    """The clashing surface pairs of the 2D mesh gmsh holds now. Never raises."""
    try:
        import numpy as np

        from meshpipeline.cad.repair.mesh_clash import clashing_surfaces, surface_components

        node_tags, xyz, _param = gmsh.model.mesh.getNodes()
        index = {int(t): i for i, t in enumerate(node_tags)}
        coords = np.asarray(xyz, dtype=float).reshape(-1, 3)
        component = surface_components({
            int(t): [int(v) for v in gmsh.model.getAdjacencies(2, t)[0]]
            for _dim, t in gmsh.model.getEntities(2)})
        tris: list = []
        tags: list = []
        groups: list = []
        for _dim, surface in gmsh.model.getEntities(2):
            types, _elements, nodes = gmsh.model.mesh.getElements(2, surface)
            for kind, flat in zip(types, nodes, strict=True):
                if int(kind) != 2:          # linear triangles; the probe meshes order 1
                    continue
                rows = np.array([index[int(n)] for n in flat], dtype=np.int64).reshape(-1, 3)
                tris.append(rows)
                tags.append(np.full(len(rows), int(surface), dtype=np.int64))
                groups.append(np.full(len(rows), component.get(int(surface), -1),
                                      dtype=np.int64))
        if not tris:
            return ()
        return clashing_surfaces(coords, np.vstack(tris), np.concatenate(tags),
                                 groups=np.concatenate(groups),
                                 deadline=time.monotonic() + CLASH_SEARCH_S)
    except Exception as exc:  # noqa: BLE001 - a failed search is "no clash found", not an error
        logger.info("mesh_probe: clash search: %s: %s", type(exc).__name__, exc)
        return ()


_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_OVERLAP = re.compile(r"overlapping facets\)?\s+on surface\s+(\d+)(?:\s+surface\s+(\d+))?", re.I)
_PERIODIC = re.compile(r"periodic surface\s+(\d+)", re.I)
_PLC = re.compile(r"segment and a facet intersect", re.I)
_PLC_POINT = re.compile(rf"at point \(\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*\)")
_OPEN_LOOP = re.compile(r"not to be forming a closed loop", re.I)
_MESHING_SURFACE = re.compile(r"Meshing surface (\d+)")


def classify(error: str, log: Sequence[str], *, stage: str = "") -> MeshFailure:
    """Name the kind, surfaces and point of a gmsh failure. Pure; never raises.

    `stage` is recorded on the failure, and so is whether its surfaces came from the log.
    """
    error = error or ""
    surfaces: tuple[int, ...] = ()
    point: tuple[float, float, float] | None = None
    kind = OTHER

    match = _OVERLAP.search(error)
    if match:
        kind = OVERLAPPING_FACETS
        tags = [int(g) for g in match.groups() if g]
        surfaces = tuple(dict.fromkeys(tags))
    elif (match := _PERIODIC.search(error)):
        kind = PERIODIC_SURFACE
        surfaces = (int(match.group(1)),)
    elif _PLC.search(error):
        kind = EDGE_THROUGH_FACE
        at = _PLC_POINT.search(error)
        if at:
            point = (float(at.group(1)), float(at.group(2)), float(at.group(3)))
    elif _OPEN_LOOP.search(error):
        kind = OPEN_LOOP

    from_log = False
    if not surfaces:
        last = None
        for line in log:
            found = _MESHING_SURFACE.search(line)
            if found:
                last = int(found.group(1))
        if last is not None:
            surfaces = (last,)
            from_log = True
    return MeshFailure(kind=kind, surfaces=surfaces, point=point, message=error.strip(),
                       stage=stage, surfaces_from_log=from_log)


def locating_surfaces(failure: MeshFailure) -> tuple[int, ...]:
    """The gmsh surfaces a failure may be LOCATED on - never a guessed one. Pure.

    Surfaces gmsh named in its own error are trusted. A surface read off the log is trusted only
    when the 2D stage failed, because then it is the surface being meshed; in the volume stage (or
    an unknown one) it is just the last surface meshed in 2D. An edge through a face is located only
    by its point (controller ruling R7), so it never takes a surface from here.
    """
    if failure.kind == EDGE_THROUGH_FACE:
        return ()
    if failure.surfaces_from_log and failure.stage != STAGE_2D:
        return ()
    return failure.surfaces


def _probe_child(path: str, diagonal_mm: float, surfaces: tuple[int, ...] | None,
                 volume: bool) -> tuple[int, MeshFailure | None, dict[int, Box], tuple[Clash, ...]]:
    """Runs in the child. Returns (volume_elements, failure, tag_boxes, clashes)."""
    import gmsh

    boxes: dict[int, Box] = {}
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.logger.start()
        stage = STAGE_IMPORT
        try:
            gmsh.model.occ.importShapes(path)
            gmsh.model.occ.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMax", diagonal_mm / _SIZE_DIVISOR)
            for _dim, tag in gmsh.model.getEntities(2):
                x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, tag)
                boxes[int(tag)] = (float(x0), float(y0), float(z0),
                                   float(x1), float(y1), float(z1))
            if surfaces is not None and not surfaces:
                return 0, None, boxes, ()
            if surfaces is not None:
                keep = set(surfaces)
                gmsh.model.removeEntities(
                    [(2, t) for _d, t in gmsh.model.getEntities(2) if t not in keep])
            stage = STAGE_2D
            gmsh.model.mesh.generate(2)
            if surfaces is not None or not volume:
                return 0, None, boxes, ()
            stage = STAGE_3D
            gmsh.model.mesh.generate(3)
            _types, tags, _nodes = gmsh.model.mesh.getElements(3)
            count = sum(len(t) for t in tags)
            if count == 0:
                return 0, MeshFailure(NO_VOLUME, (), None,
                                      "gmsh produced no volume elements", stage=STAGE_3D), boxes, ()
            return count, None, boxes, ()
        except Exception as exc:  # noqa: BLE001 - a gmsh failure is the answer, not an error
            failure = classify(str(exc), gmsh.logger.get(), stage=stage)
            clashes: tuple[Clash, ...] = ()
            if stage == STAGE_3D and failure.kind in _CLASH_KINDS:
                clashes = _surface_clashes(gmsh)
            return 0, failure, boxes, clashes
    finally:
        try:
            gmsh.finalize()
        except Exception:  # noqa: BLE001
            pass


def _child_main(argv: Sequence[str]) -> int:
    """`python -m meshpipeline.cad.repair.mesh_probe PATH DIAGONAL SURFACES VOLUME`: one JSON line."""
    path, diagonal, surfaces, volume = argv
    wanted = json.loads(surfaces)
    count, failure, boxes, clashes = _probe_child(
        path, float(diagonal), None if wanted is None else tuple(wanted), volume == "1")
    sys.stdout.write("\n" + json.dumps({
        "volume_elements": count,
        "failure": None if failure is None else {
            "kind": failure.kind, "surfaces": list(failure.surfaces),
            "point": None if failure.point is None else list(failure.point),
            "message": failure.message, "stage": failure.stage,
            "surfaces_from_log": failure.surfaces_from_log},
        "tag_boxes": {str(k): list(v) for k, v in boxes.items()},
        "clashes": [{"surfaces": list(c.surfaces), "hits": c.hits, "point": list(c.point),
                     "kind": c.kind} for c in clashes]}) + "\n")
    sys.stdout.flush()
    return 0


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _run_child(path: str, diagonal_mm: float, surfaces: tuple[int, ...] | None, volume: bool,
               budget_s: float) -> dict[str, Any] | None | Literal["error"]:
    """The child's JSON answer; None when it ran out of budget; "error" when it died unanswering."""
    cmd = [sys.executable, "-W", "ignore", "-m", "meshpipeline.cad.repair.mesh_probe", path,
           repr(float(diagonal_mm)), json.dumps(None if surfaces is None else list(surfaces)),
           "1" if volume else "0"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    try:
        out, _ = proc.communicate(timeout=max(budget_s, 0.0))
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        return "error"
    try:
        answer = json.loads(lines[-1])
    except ValueError:
        return "error"
    return answer if isinstance(answer, dict) else "error"


def probe_file(path: Path, *, diagonal_mm: float, surfaces: Sequence[int] | None = None,
               volume: bool = True, budget_s: float = 120.0) -> ProbeResult:
    """Mesh `path` in a bounded fresh interpreter and report. NEVER raises, never blocks past
    `budget_s` (plus a few seconds to reap the child).

    `surfaces=None` meshes everything; an empty sequence meshes nothing and only fills `tag_boxes`.
    A child that ran out of budget is a TIMEOUT failure; one that died without answering is a
    PROBE_ERROR failure.
    """
    wanted = None if surfaces is None else tuple(int(s) for s in surfaces)
    try:
        answer = _run_child(str(path), float(diagonal_mm), wanted, bool(volume), budget_s)
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        logger.info("mesh_probe: %s: %s", type(exc).__name__, exc)
        answer = "error"
    if answer is None:
        return ProbeResult(False, 0, MeshFailure(
            TIMEOUT, (), None, f"the mesh probe ran past its {budget_s:g} s budget"), {})
    if isinstance(answer, str):
        return ProbeResult(False, 0, MeshFailure(
            PROBE_ERROR, (), None, "the mesh probe process died without an answer"), {})
    try:
        raw = answer["failure"]
        failure = None if raw is None else MeshFailure(
            kind=str(raw["kind"]), surfaces=tuple(int(x) for x in raw["surfaces"]),
            point=None if raw["point"] is None else (
                float(raw["point"][0]), float(raw["point"][1]), float(raw["point"][2])),
            message=str(raw["message"]), stage=str(raw["stage"]),
            surfaces_from_log=bool(raw["surfaces_from_log"]))
        boxes = {int(k): (float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4]),
                          float(v[5])) for k, v in answer["tag_boxes"].items()}
        count = int(answer["volume_elements"])
        clashes = tuple(Clash(surfaces=(int(c["surfaces"][0]), int(c["surfaces"][1])),
                              hits=int(c["hits"]),
                              point=(float(c["point"][0]), float(c["point"][1]),
                                     float(c["point"][2])),
                              kind=str(c["kind"])) for c in answer.get("clashes") or ())
    except (KeyError, TypeError, ValueError, IndexError):
        return ProbeResult(False, 0, MeshFailure(
            PROBE_ERROR, (), None, "the mesh probe answered in a form that could not be read"), {})
    if failure is not None:
        return ProbeResult(False, 0, failure, boxes, clashes)
    return ProbeResult(True, count, None, boxes)


#: One defect code per kind the probe names, one-to-one.
CODE_FOR_KIND: dict[str, DefectCode] = {
    OVERLAPPING_FACETS: DefectCode.mesh_overlapping_facets,
    EDGE_THROUGH_FACE: DefectCode.mesh_edge_through_face,
    OPEN_LOOP: DefectCode.mesh_open_loop,
    PERIODIC_SURFACE: DefectCode.mesh_periodic_surface,
    NO_VOLUME: DefectCode.mesh_no_volume,
    TIMEOUT: DefectCode.mesh_timeout,
    PROBE_ERROR: DefectCode.mesh_probe_error,
    OTHER: DefectCode.mesh_other,
}

#: Every mesh code, and the two that mean the mesher was never heard from rather than that it
#: refused (a probe skipped, unwritable, out of time, or dead). Only the rest are evidence.
MESH_CODES = frozenset(CODE_FOR_KIND.values())
NO_ANSWER_CODES = frozenset({DefectCode.mesh_timeout, DefectCode.mesh_probe_error})

#: Kinds that are about the whole part, never one face: no surface gmsh names for them is a cause.
_WHOLE_PART_KINDS = frozenset({NO_VOLUME, TIMEOUT, PROBE_ERROR})

#: How far a gmsh surface's box may sit from an OCC face's and still be that face, as a fraction
#: of the part's diagonal. gmsh reads the very BREP the face came from and boxes it with the same
#: kernel, so on real parts the two agree to ~1e-13 mm; this is room for float noise, not for a
#: neighbouring face.
TAG_MATCH_FRACTION = 1e-6


def faces_for_tags(face_boxes: dict[int, Box], tag_boxes: dict[int, Box], *,
                   tol: float) -> dict[int, int]:
    """Which OCC face (1-based index) each gmsh surface tag is. Pure.

    A tag maps when every coordinate of its box is within `tol` of exactly ONE face's box. Two
    faces sharing a box leave the tag unmapped: a defect aimed at the wrong one of a pair is worse
    than one reported unlocated.
    """
    order = sorted(face_boxes.items(), key=lambda item: item[1][0])
    starts = [box[0] for _index, box in order]
    out: dict[int, int] = {}
    for tag, box in tag_boxes.items():
        lo = bisect.bisect_left(starts, box[0] - tol)
        hi = bisect.bisect_right(starts, box[0] + tol)
        hits = [index for index, face in order[lo:hi]
                if all(abs(a - b) <= tol for a, b in zip(box, face, strict=True))]
        if len(hits) == 1:
            out[int(tag)] = int(hits[0])
    return out


def _face_boxes(faces) -> dict[int, Box]:
    out: dict[int, Box] = {}
    for index in range(1, faces.Extent() + 1):
        bounds = _bounds_of(faces.FindKey(index))
        if bounds is not None:
            out[index] = (*bounds[0], *bounds[1])
    return out


def _face_defect(code: DefectCode, faces, index: int, failure: MeshFailure) -> EntityDefect:
    measurements: dict = {"mesh_kind": failure.kind, "gmsh_surfaces": list(failure.surfaces),
                          "stage": failure.stage}
    if failure.point is not None:
        measurements["point"] = list(failure.point)
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                        entity_index=int(index), message=failure.message,
                        measurements=measurements,
                        location=_centroid_and_box(faces.FindKey(index)))


def _unlocated(failure: MeshFailure) -> EntityDefect:
    """The failure as a defect on no face: index 0, no location, and so no fingerprint."""
    return EntityDefect(code=CODE_FOR_KIND.get(failure.kind, DefectCode.mesh_other),
                        severity=DefectSeverity.error, entity_type="shape", entity_index=0,
                        message=failure.message,
                        measurements={"mesh_kind": failure.kind,
                                      "gmsh_surfaces": list(failure.surfaces),
                                      "stage": failure.stage})


#: Mesh codes only a VOLUME mesh can report, whatever stage was recorded beside them.
VOLUME_CODES = frozenset({DefectCode.mesh_edge_through_face, DefectCode.mesh_no_volume})


def volume_stage(defect) -> bool:
    """Is this a mesh finding only a WHOLE-SHAPE probe can see (controller ruling R11a)?

    A scoped probe surface-meshes its region and never builds the volume, so a failure of the
    volume stage - an edge through a face, no volume, or any failure whose recorded stage was 3D -
    is invisible to it. Its absence from a scoped probe is therefore no evidence it went, and the
    session carries such a finding through scoped scans until a whole-shape probe judges it.
    """
    code = getattr(defect, "code", None)
    if code in VOLUME_CODES:
        return True
    if not getattr(code, "value", str(code)).startswith("mesh_"):
        return False
    return (getattr(defect, "measurements", None) or {}).get("stage") == STAGE_3D


def _box_distance(point: tuple[float, float, float], box: Box) -> float:
    """How far a point is from a box: 0 inside it. A lower bound on the distance to what it holds."""
    gaps = (max(box[k] - point[k], 0.0, point[k] - box[k + 3]) for k in range(3))
    return sum(g * g for g in gaps) ** 0.5


def _nearest_faces(faces, point: tuple[float, float, float], *, tol: float,
                   deadline: float) -> list[int] | None:
    """The face(s) nearest a point - every face within `tol` of the least distance - or None when
    the deadline passed first.

    BOX-BOUNDED, AND STILL EXACT. A face's box distance is a lower bound on its true distance, so
    faces are measured in order of box distance and the search stops at the first whose box is
    already further than the best exact distance (+ tol): nothing after it can be nearer. Taking
    only the faces with the least BOX distance would not be exact - the point may sit inside the
    box of a large curved face that is itself far away. A face with no box is measured first.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    if time.monotonic() >= deadline:
        return None
    order: list[tuple[float, int]] = []
    for index in range(1, faces.Extent() + 1):
        bounds = _bounds_of(faces.FindKey(index))
        order.append((0.0 if bounds is None else _box_distance(point, (*bounds[0], *bounds[1])),
                      index))
    order.sort()
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    distances: dict[int, float] = {}
    for bound, index in order:
        if distances and bound > min(distances.values()) + tol:
            break
        if time.monotonic() >= deadline:
            return None
        try:
            extrema = BRepExtrema_DistShapeShape(vertex, faces.FindKey(index))
            if extrema.IsDone():
                distances[index] = float(extrema.Value())
        except Exception as exc:  # noqa: BLE001 - a face the kernel cannot measure is not nearest
            logger.debug("mesh_probe: face %d unmeasurable from %s: %s", index, point, exc)
    if not distances:
        return []
    least = min(distances.values())
    return sorted(i for i, d in distances.items() if d <= least + tol)


#: How many bbox-meeting solid pairs the contact search measures before it picks the nearest. A
#: part with more candidate pairs than this is measured on its first CAP only: bounded, not exact.
CONTACT_PAIR_CAP = 8

#: `measurements["located_by"]` on a defect placed by `_contact_support_faces`: the failure named no
#: point and no surface, so WE inferred the faces from the geometry. Never gmsh's own word.
LOCATED_BY_CONTACT = "contact-pair"

#: `measurements["located_by"]` on a defect placed by the surface-mesh clash search: gmsh's own
#: triangles of these faces cross (or coincide) - measured on the mesh it refused, not inferred.
LOCATED_BY_CLASH = "surface-mesh-clash"


@dataclass(frozen=True, slots=True)
class Contact:
    """What the contact search found: the support faces (indices in the shape's face map), the
    solid pair they belong to, and how much of the candidate field was searched."""
    faces: tuple[int, ...]
    #: "solids:I+J", I and J being indices in the shape's solid map.
    pair: str
    pairs_total: int
    pairs_considered: int


def _shared_pairs(shape, solids) -> set[tuple[int, int]]:
    """Solid-index pairs that ALREADY own a common face (a conformal interface).

    Reimplements the few lines of FragmentBodies' `already_shared` / third-body notion (faces to
    owning solids, compared with IsSame) rather than importing operations.
    """
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SOLID, ancestors)
    out: set[tuple[int, int]] = set()
    for index in range(1, ancestors.Extent() + 1):
        owners = sorted({int(solids.FindIndex(o)) for o in ancestors.FindFromIndex(index)} - {0})
        out.update((a, b) for n, a in enumerate(owners) for b in owners[n + 1:])
    return out


def _contact_support_faces(shape, faces, *, tol: float, deadline: float) -> Contact | None:
    """The contact of the nearest pair of touching solids that share no face yet, or None when
    there is no such pair or the deadline passed first. SEAM: tests replace it.

    A part gmsh cannot tetrahedralise with an edge through a face and no coordinates is, necessarily,
    carrying a body pair with no conformal interface. So: solid pairs whose boxes meet form the
    candidates; pairs that ALREADY share a face are dropped (a healthy interface would score
    distance 0 and win); the rest are ranked by box-overlap volume, biggest contact first, then
    index, and the first CONTACT_PAIR_CAP are measured with `BRepExtrema_DistShapeShape`. The
    least distance wins (ties by the same ranking) and its support faces are the answer. A support
    the kernel reports IN a face is that face. When it reports only vertices or edges (touching
    planar faces usually do), the faces nearest the centroid of the minimal-distance points are
    used instead, on each body. Every kernel failure is "no pair": this never raises.
    """
    try:
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID

        from meshpipeline.cad.repair.localize import _indexed

        solids = _indexed(shape, TopAbs_SOLID)
        if solids.Extent() < 2:
            return None
        boxes = {i: b for i in range(1, solids.Extent() + 1)
                 if (b := _bounds_of(solids.FindKey(i))) is not None}
        shared = _shared_pairs(shape, solids)

        def overlap(i: int, j: int) -> float:
            # Padded by tol so two boxes that merely touch still rank by their contact area.
            volume = 1.0
            for k in range(3):
                volume *= max(min(boxes[i][1][k], boxes[j][1][k])
                              - max(boxes[i][0][k], boxes[j][0][k]), tol)
            return volume

        pairs = [(i, j) for i in boxes for j in boxes
                 if i < j and (i, j) not in shared
                 and all(boxes[i][0][k] <= boxes[j][1][k] + tol
                         and boxes[j][0][k] <= boxes[i][1][k] + tol for k in range(3))]
        pairs.sort(key=lambda ij: (-overlap(*ij), ij))
        best = None
        best_key = None
        for rank, (i, j) in enumerate(pairs[:CONTACT_PAIR_CAP]):
            # One DistShapeShape call is uninterruptible, so a slow pair can overshoot the
            # deadline by its own duration; the check is only BETWEEN pairs.
            if time.monotonic() >= deadline:
                return None
            try:
                extrema = BRepExtrema_DistShapeShape(solids.FindKey(i), solids.FindKey(j))
                if not extrema.IsDone() or extrema.NbSolution() < 1:
                    continue
                key = (round(float(extrema.Value()), 9), rank)
            except Exception as exc:  # noqa: BLE001 - an unmeasurable pair is merely not nearest
                logger.debug("mesh_probe: pair %d/%d unmeasurable: %s", i, j, exc)
                continue
            if best_key is None or key < best_key:
                best, best_key = (i, j, extrema), key
        if best is None:
            return None
        i, j, extrema = best
        distance = float(extrema.Value())
        found: set[int] = set()
        points: list[tuple[float, float, float]] = []
        for n in range(1, extrema.NbSolution() + 1):
            for support, kind in ((extrema.SupportOnShape1(n), extrema.SupportTypeShape1(n)),
                                  (extrema.SupportOnShape2(n), extrema.SupportTypeShape2(n))):
                if kind.name.endswith("IsInFace") and support.ShapeType() == TopAbs_FACE:
                    index = faces.FindIndex(support)
                    if index:
                        found.add(int(index))
            gap = extrema.PointOnShape1(n).Distance(extrema.PointOnShape2(n))
            if gap <= distance + tol:
                p1 = extrema.PointOnShape1(n)
                points.append((p1.X(), p1.Y(), p1.Z()))

        def contact() -> Contact | None:
            if not found:
                return None
            return Contact(faces=tuple(sorted(found)), pair=f"solids:{i}+{j}",
                           pairs_total=len(pairs), pairs_considered=min(len(pairs),
                                                                         CONTACT_PAIR_CAP))

        if found or not points:
            return contact()
        # FALLBACK CENTRE: the average of body i's minimal-distance points. For a contact that is
        # not convex (an L-shaped patch) that average can lie OFF the contact, and the nearest
        # faces to it are then a guess - `located_by` already says this is an inference.
        count = len(points)
        centre = (sum(p[0] for p in points) / count, sum(p[1] for p in points) / count,
                  sum(p[2] for p in points) / count)
        for index in (i, j):
            body_faces = _indexed(solids.FindKey(index), TopAbs_FACE)
            near = _nearest_faces(body_faces, centre, tol=tol, deadline=deadline)
            if near is None:
                return None
            for local in near:
                found.add(int(faces.FindIndex(body_faces.FindKey(local))))
        found.discard(0)
        return contact()
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        logger.info("mesh_probe: contact search: %s: %s", type(exc).__name__, exc)
        return None


def mesh_defects(shape, *, diagonal_mm: float, budget_s: float,
                 within: Region | None = None) -> tuple[EntityDefect, ...]:
    """Why gmsh will not mesh `shape`, as defects on the faces it named. NEVER raises.

    Empty when the part meshes. Each defect is stamped exactly as `localize` stamps a scan, so a
    defect on a face carries the fingerprint the scan gives that face. A failure that names no face
    this can identify - every NO_VOLUME or TIMEOUT, any failure whose only surface is a guess
    (`locating_surfaces`), and one the budget ran out before locating - is ONE unlocated defect
    (index 0, empty location, empty fingerprint): never aimed at a guessed face.

    `within` scopes the probe to the gmsh surfaces whose face `localize.in_region` admits, the one
    region rule the scan uses. A scoped probe meshes surfaces only, so failures that first appear
    in the volume mesh (overlapping facets, an edge through a face, no volume) are not seen there.
    A located defect the region does not admit is not reported by a scoped call at all.
    """
    try:
        return _stamped(_mesh_defects(shape, diagonal_mm=float(diagonal_mm),
                                      budget_s=float(budget_s), within=within),
                        diagonal_mm=diagonal_mm)
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        logger.info("mesh_defects: %s: %s", type(exc).__name__, exc)
        # Unlocated, so it needs no stamp: its fingerprint is "" either way.
        return (_unlocated(MeshFailure(
            TIMEOUT, (), None, f"the mesh probe could not run: {type(exc).__name__}")),)


def _mesh_defects(shape, *, diagonal_mm: float, budget_s: float,
                  within: Region | None) -> tuple[EntityDefect, ...]:
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE

    deadline = time.monotonic() + budget_s
    tol = diagonal_mm * TAG_MATCH_FRACTION
    faces = _indexed(shape, TopAbs_FACE)
    with tempfile.TemporaryDirectory(prefix="mesh-probe-") as tmp:
        path = Path(tmp) / "shape.brep"
        if not BRepTools.Write_s(shape, str(path)):
            return (_unlocated(MeshFailure(
                TIMEOUT, (), None, "the mesh probe could not write the shape to a BREP")),)

        surfaces: list[int] | None = None
        # The tag-to-face map, cached for this BREP: a scoped call reads it off the boxes-only
        # pass and reuses it below rather than probing for boxes a second time.
        mapping: dict[int, int] | None = None
        if within is not None:
            boxes_only = probe_file(path, diagonal_mm=diagonal_mm, surfaces=(), volume=False,
                                    budget_s=budget_s)
            if boxes_only.failure is not None:
                return (_unlocated(boxes_only.failure),)
            mapping = faces_for_tags(_face_boxes(faces), boxes_only.tag_boxes, tol=tol)
            surfaces = sorted(
                tag for tag, box in boxes_only.tag_boxes.items()
                if _tag_in_region(tag, box, mapping, faces, within, diagonal_mm=diagonal_mm))
            if not surfaces:
                return ()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (_unlocated(MeshFailure(
                TIMEOUT, (), None, f"the mesh probe ran past its {budget_s:g} s budget")),)
        result = probe_file(path, diagonal_mm=diagonal_mm, surfaces=surfaces,
                            budget_s=remaining)

    failure = result.failure
    if failure is None:
        return ()
    if failure.kind in _WHOLE_PART_KINDS or time.monotonic() >= deadline:
        # The budget is spent: report the failure, but do no more kernel work to place it.
        return (_unlocated(failure),)
    code = CODE_FOR_KIND.get(failure.kind, DefectCode.mesh_other)
    located: list[int] | None = []
    if failure.point is not None:
        located = _nearest_faces(faces, failure.point, tol=tol, deadline=deadline)
    elif trusted := locating_surfaces(failure):
        if mapping is None:
            mapping = faces_for_tags(_face_boxes(faces), result.tag_boxes, tol=tol)
        located = list(dict.fromkeys(mapping[t] for t in trusted if t in mapping))
    by_clash = False
    if not located and result.clashes:
        # gmsh named no point and no surface that maps to one face (its PLC error names nothing
        # at all in 4.15): the surface pairs whose triangles actually cross are the place.
        if mapping is None:
            mapping = faces_for_tags(_face_boxes(faces), result.tag_boxes, tol=tol)
        located = list(dict.fromkeys(mapping[t] for c in result.clashes for t in c.surfaces
                                     if t in mapping))
        by_clash = bool(located)
    by_contact = False
    contact: Contact | None = None
    if (not located and failure.kind == EDGE_THROUGH_FACE and failure.point is None
            and not locating_surfaces(failure)):
        # gmsh named no point and no surface: infer the place from the bodies' contact, and say so.
        contact = _contact_support_faces(shape, faces, tol=tol, deadline=deadline)
        located = list(contact.faces) if contact is not None else []
        by_contact = bool(located)
    if not located:
        return (_unlocated(failure),)
    defects = [_face_defect(code, faces, index, failure) for index in sorted(located)]
    if by_clash:
        # EACH FACE CARRIES ITS OWN PAIR'S EVIDENCE (Greptile #495): the leading clash - most hits
        # - that names this face, never the run's first pair, so a face whose pair really meets is
        # not read as another pair's sizing gap. One gap per pair, measured once.
        tags_of = {face: tag for tag, face in (mapping or {}).items()}
        owns = {}
        for defect in defects:
            tag = tags_of.get(int(defect.entity_index))
            owns[defect.entity_index] = next((c for c in result.clashes if tag in c.surfaces),
                                             result.clashes[0])
        gaps, evidence = _clash_gaps(faces, mapping or {}, list(owns.values()),
                                     budget_s=min(CLASH_EVIDENCE_S, deadline - time.monotonic()))
        for defect in defects:
            own = owns[defect.entity_index]
            if evidence:
                defect.measurements["clash_evidence"] = evidence
            defect.measurements["located_by"] = LOCATED_BY_CLASH
            defect.measurements["clash_pairs"] = [list(c.surfaces) for c in result.clashes]
            defect.measurements["clash_pair"] = list(own.surfaces)
            defect.measurements["clash_gap_mm"] = gaps.get(own.surfaces)
            # THE KIND AGREES WITH THE GAP: triangles that crossed between faces the kernel
            # measures apart are a GAP, and the triangle test's own word is kept beside it.
            defect.measurements["clash_kind"] = reported_kind(own.kind, gaps.get(own.surfaces),
                                                              diagonal_mm)
            defect.measurements["clash_mesh_kind"] = own.kind
            defect.measurements["clash_point"] = list(own.point)
    if by_contact and contact is not None:
        for defect in defects:
            defect.measurements["located_by"] = LOCATED_BY_CONTACT
            defect.measurements["pair"] = contact.pair
            defect.measurements["pairs_total"] = contact.pairs_total
            defect.measurements["pairs_considered"] = contact.pairs_considered
    if within is not None:
        # A scoped call answers for its region only. Turning an outside face into an UNLOCATED
        # defect would make it read as in-region (unplaceable is admitted), so it is dropped.
        defects = [d for d in defects if in_region(d, within, diagonal_mm=diagonal_mm)]
    return tuple(defects)


#: The wall, in seconds, for measuring every located clash pair's B-rep gap. MEASURED, wave 4:
#: one BRepExtrema_DistShapeShape between two BSpline faces of abc/0054_00540003 ran 150+ s in the
#: parent - uninterruptible kernel code that no deadline check between calls could stop - and the
#: file took 53 min with one core pinned. So the gaps are measured in ONE bounded child
#: (`localize._bounded`: forked, SIGKILLed at the wall) and are evidence, never worth the run.
CLASH_EVIDENCE_S = 10.0


def _clash_gaps(faces, mapping: dict[int, int], clashes: list[Clash], *,
                budget_s: float) -> tuple[dict[tuple[int, int], float | None], str]:
    """Every distinct pair's gap, measured in one bounded child: (gaps by pair, "") or, when the
    child ran out of `budget_s` or died, ({}, "budget_spent") - every gap then reads None
    (unmeasured), never a guess. NEVER RAISES, NEVER BLOCKS PAST `budget_s` (plus the reap)."""
    from meshpipeline.cad.repair.localize import _bounded

    pairs = list(dict.fromkeys(c.surfaces for c in clashes))
    by_pair = {c.surfaces: c for c in clashes}

    def measure() -> dict[tuple[int, int], float | None]:
        # In the child: no deadline needed, the parent's SIGKILL is the deadline.
        return {pair: _clash_gap(faces, mapping, by_pair[pair], deadline=float("inf"))
                for pair in pairs}

    found = _bounded(measure, budget_s=max(0.0, float(budget_s)))
    if found is None:
        return {}, "budget_spent"
    return found, ""


def _clash_gap(faces, mapping: dict[int, int], clash: Clash, *,
               deadline: float) -> float | None:
    """The B-rep distance between the two faces of the leading clash, or None when either is
    unmapped, the kernel cannot say, or the budget is spent. 0 means the faces really meet; more
    than 0 means the faces are apart and only their triangles crossed - a gap narrower than the
    surface mesh's chord, which is the mesher's resolution rather than the part's shape."""
    a, b = (mapping.get(t) for t in clash.surfaces)
    if not a or not b or time.monotonic() >= deadline:
        return None
    try:
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape

        extrema = BRepExtrema_DistShapeShape(faces.FindKey(a), faces.FindKey(b))
        return float(extrema.Value()) if extrema.IsDone() else None
    except Exception as exc:  # noqa: BLE001 - an unmeasured gap is reported as unknown
        logger.debug("mesh_probe: clash gap %d/%d: %s", a, b, exc)
        return None


def _tag_in_region(tag: int, box: Box, mapping: dict[int, int], faces, within: Region, *,
                   diagonal_mm: float) -> bool:
    """Would the scan's region rule admit this gmsh surface? Judged on its OCC face when mapped,
    on gmsh's own box otherwise - an unidentified surface is not thereby outside the region."""
    index = mapping.get(int(tag))
    if index is not None:
        location = _centroid_and_box(faces.FindKey(index))
    else:
        index = 0
        location = {"bbox_min": list(box[:3]), "bbox_max": list(box[3:])}
    provisional = EntityDefect(code=DefectCode.mesh_other, severity=DefectSeverity.error,
                               entity_type="face", entity_index=index, message="",
                               location=location)
    return in_region(provisional, within, diagonal_mm=diagonal_mm)


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
