# Responsibility: Ask gmsh whether a B-rep will mesh and, when it will not, say why - which kind of
#                 failure, which gmsh surface tags, and where - plus every surface's bounding box.
# Boundaries: it meshes in a throwaway child process and reports. It mutates no shape and repairs
#             nothing. gmsh is imported only inside the child, never at module level, so this module
#             imports (and `classify` runs) on a host with no gmsh.
# Collaborates with: cad/repair/localize.py (`_bounded` supplies the forked, killable child); later
#                    tasks map `tag_boxes` onto OCC faces and feed failures to the repair loop.
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from meshpipeline.cad.repair.localize import _bounded

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
