# Responsibility: Ask gmsh whether a B-rep will mesh and, when it will not, say why - which kind of
#                 failure, which gmsh surface tags, and where - plus every surface's bounding box.
# Boundaries: it meshes in a throwaway child process and reports. It mutates no shape and repairs
#             nothing. gmsh is imported only inside the child, never at module level, so this module
#             imports (and `classify` runs) on a host with no gmsh.
# Collaborates with: cad/repair/localize.py (`_bounded` supplies the forked, killable child; its
#                    `_centroid_and_box`, `_stamped` and `in_region` place and name the defects
#                    `mesh_defects` turns a failure into, so they read exactly like a scan's).
from __future__ import annotations

import bisect
import logging
import re
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import (
    EntityDefect,
    _bounded,
    _bounds_of,
    _centroid_and_box,
    _indexed,
    _stamped,
    in_region,
)

if TYPE_CHECKING:
    from meshpipeline.cad.repair.regions import Region

logger = logging.getLogger(__name__)

OVERLAPPING_FACETS = "overlapping_facets"
EDGE_THROUGH_FACE = "edge_through_face"
OPEN_LOOP = "open_loop"
PERIODIC_SURFACE = "periodic_surface"
NO_VOLUME = "no_volume"
TIMEOUT = "timeout"
OTHER = "other"

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


@dataclass(frozen=True, slots=True)
class ProbeResult:
    meshed: bool
    volume_elements: int
    failure: MeshFailure | None
    tag_boxes: dict[int, Box] = field(default_factory=dict)


_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_OVERLAP = re.compile(r"overlapping facets\)?\s+on surface\s+(\d+)(?:\s+surface\s+(\d+))?", re.I)
_PERIODIC = re.compile(r"periodic surface\s+(\d+)", re.I)
_PLC = re.compile(r"segment and a facet intersect", re.I)
_PLC_POINT = re.compile(rf"at point \(\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*\)")
_OPEN_LOOP = re.compile(r"not to be forming a closed loop", re.I)
_MESHING_SURFACE = re.compile(r"Meshing surface (\d+)")


def classify(error: str, log: Sequence[str]) -> MeshFailure:
    """Name the kind, surfaces and point of a gmsh failure. Pure; never raises."""
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

    if not surfaces:
        last = None
        for line in log:
            found = _MESHING_SURFACE.search(line)
            if found:
                last = int(found.group(1))
        if last is not None:
            surfaces = (last,)
    return MeshFailure(kind=kind, surfaces=surfaces, point=point, message=error.strip())


def _probe_child(path: str, diagonal_mm: float, surfaces: tuple[int, ...] | None,
                 volume: bool) -> tuple[int, MeshFailure | None, dict[int, Box]]:
    """Runs in the forked child. Returns (volume_elements, failure, tag_boxes); picklable."""
    import gmsh

    boxes: dict[int, Box] = {}
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.logger.start()
        try:
            gmsh.model.occ.importShapes(path)
            gmsh.model.occ.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMax", diagonal_mm / _SIZE_DIVISOR)
            for _dim, tag in gmsh.model.getEntities(2):
                x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, tag)
                boxes[int(tag)] = (float(x0), float(y0), float(z0),
                                   float(x1), float(y1), float(z1))
            if surfaces is not None and not surfaces:
                return 0, None, boxes
            if surfaces is not None:
                keep = set(surfaces)
                gmsh.model.removeEntities(
                    [(2, t) for _d, t in gmsh.model.getEntities(2) if t not in keep])
            gmsh.model.mesh.generate(2)
            if surfaces is not None or not volume:
                return 0, None, boxes
            gmsh.model.mesh.generate(3)
            _types, tags, _nodes = gmsh.model.mesh.getElements(3)
            count = sum(len(t) for t in tags)
            if count == 0:
                return 0, MeshFailure(NO_VOLUME, (), None,
                                      "gmsh produced no volume elements"), boxes
            return count, None, boxes
        except Exception as exc:  # noqa: BLE001 - a gmsh failure is the answer, not an error
            return 0, classify(str(exc), gmsh.logger.get()), boxes
    finally:
        try:
            gmsh.finalize()
        except Exception:  # noqa: BLE001
            pass


def probe_file(path: Path, *, diagonal_mm: float, surfaces: Sequence[int] | None = None,
               volume: bool = True, budget_s: float = 120.0) -> ProbeResult:
    """Mesh `path` in a bounded child and report. NEVER raises, never blocks past `budget_s`.

    `surfaces=None` meshes everything; an empty sequence meshes nothing and only fills `tag_boxes`.
    A child that crashed or ran out of budget is a TIMEOUT failure.
    """
    wanted = None if surfaces is None else tuple(int(s) for s in surfaces)
    try:
        answer = _bounded(_probe_child, str(path), float(diagonal_mm), wanted, bool(volume),
                          budget_s=budget_s)
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        logger.info("mesh_probe: %s: %s", type(exc).__name__, exc)
        answer = None
    if answer is None:
        return ProbeResult(False, 0, MeshFailure(
            TIMEOUT, (), None,
            f"the mesh probe crashed or ran past its {budget_s:g} s budget"), {})
    count, failure, boxes = answer
    if failure is not None:
        return ProbeResult(False, 0, failure, boxes)
    return ProbeResult(True, count, None, boxes)


#: One defect code per kind the probe names, one-to-one.
CODE_FOR_KIND: dict[str, DefectCode] = {
    OVERLAPPING_FACETS: DefectCode.mesh_overlapping_facets,
    EDGE_THROUGH_FACE: DefectCode.mesh_edge_through_face,
    OPEN_LOOP: DefectCode.mesh_open_loop,
    PERIODIC_SURFACE: DefectCode.mesh_periodic_surface,
    NO_VOLUME: DefectCode.mesh_no_volume,
    TIMEOUT: DefectCode.mesh_timeout,
    OTHER: DefectCode.mesh_other,
}

#: Kinds that are about the whole part, never one face: no surface gmsh names for them is a cause.
_WHOLE_PART_KINDS = frozenset({NO_VOLUME, TIMEOUT})

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
    measurements: dict = {"mesh_kind": failure.kind, "gmsh_surfaces": list(failure.surfaces)}
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
                                      "gmsh_surfaces": list(failure.surfaces)})


def _nearest_faces(faces, point: tuple[float, float, float], *, tol: float) -> list[int]:
    """The face(s) nearest a point: every face within `tol` of the least distance."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    distances: dict[int, float] = {}
    for index in range(1, faces.Extent() + 1):
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


def mesh_defects(shape, *, diagonal_mm: float, budget_s: float,
                 within: Region | None = None) -> tuple[EntityDefect, ...]:
    """Why gmsh will not mesh `shape`, as defects on the faces it named. NEVER raises.

    Empty when the part meshes. Each defect is stamped exactly as `localize` stamps a scan, so a
    defect on a face carries the fingerprint the scan gives that face. A failure that names no face
    this can identify - and every NO_VOLUME or TIMEOUT - is ONE unlocated defect (index 0, empty
    location, empty fingerprint): never aimed at a guessed face.

    `within` scopes the probe to the gmsh surfaces whose face `localize.in_region` admits, the one
    region rule the scan uses. A scoped probe meshes surfaces only, so failures that first appear
    in the volume mesh (overlapping facets, an edge through a face, no volume) are not seen there.
    """
    try:
        found = _mesh_defects(shape, diagonal_mm=float(diagonal_mm), budget_s=float(budget_s),
                              within=within)
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        logger.info("mesh_defects: %s: %s", type(exc).__name__, exc)
        found = (_unlocated(MeshFailure(
            TIMEOUT, (), None, f"the mesh probe could not run: {type(exc).__name__}")),)
    return _stamped(found, diagonal_mm=diagonal_mm)


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
    if failure.kind in _WHOLE_PART_KINDS:
        return (_unlocated(failure),)
    code = CODE_FOR_KIND.get(failure.kind, DefectCode.mesh_other)
    located: list[int] = []
    if failure.point is not None:
        located = _nearest_faces(faces, failure.point, tol=tol)
    elif failure.kind != EDGE_THROUGH_FACE:
        # An edge through a face is found in the VOLUME mesh, so the surface `classify` took from
        # the log is only the last one meshed in 2D - a guess. Without a point it stays unlocated.
        if mapping is None:
            mapping = faces_for_tags(_face_boxes(faces), result.tag_boxes, tol=tol)
        located = list(dict.fromkeys(mapping[t] for t in failure.surfaces if t in mapping))
    defects = [_face_defect(code, faces, index, failure) for index in sorted(located)]
    if within is not None:
        defects = [d for d in defects if in_region(d, within, diagonal_mm=diagonal_mm)]
    return tuple(defects) if defects else (_unlocated(failure),)


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
