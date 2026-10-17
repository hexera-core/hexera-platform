# Responsibility: Say where a review camera should look for one located defect - a centre, the
#                 direction to view it from, and how much of the part to frame.
# Boundaries: it measures and reports, and it NEVER raises. A defect whose entity cannot be
#             resolved still gets a camera (the part's centre), because a review item with no
#             camera is a worse outcome than one that frames the whole part.
# Collaborates with: cad/repair/operations.py (sub_shape, box_of), cad/repair/localize.py
#                    (EntityDefect), and the pipeline step that mints review items.
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshpipeline.cad.repair.localize import EntityDefect

logger = logging.getLogger(__name__)

#: A part with nothing measurable is framed at this fraction of its diagonal.
_FALLBACK_EXTENT_FRACTION = 1.0 / 20.0
_DEFAULT_NORMAL = [0.0, 0.0, 1.0]


def camera_for(shape, defect: EntityDefect) -> dict:
    """`{"centroid_mm": [x,y,z], "normal": [x,y,z] (unit), "extent_mm": float}` for one defect."""
    try:
        return _camera(shape, defect)
    except Exception:  # noqa: BLE001 - the contract is that this never raises
        logger.warning("spotlight fell back to the part centre", exc_info=True)
        try:
            return _whole_part(shape)
        except Exception:  # noqa: BLE001
            return {"centroid_mm": [0.0, 0.0, 0.0], "normal": list(_DEFAULT_NORMAL),
                    "extent_mm": 1.0}


def _part_bounds(shape) -> tuple[list[float], float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    centre = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]
    return centre, math.dist((x0, y0, z0), (x1, y1, z1))


def _whole_part(shape) -> dict:
    centre, diagonal = _part_bounds(shape)
    return {"centroid_mm": centre, "normal": list(_DEFAULT_NORMAL),
            "extent_mm": _extent(0.0, diagonal)}


def _extent(span: float, diagonal: float) -> float:
    if math.isfinite(span) and span > 0.0:
        return span
    fallback = diagonal * _FALLBACK_EXTENT_FRACTION
    return fallback if math.isfinite(fallback) and fallback > 0.0 else 1.0


def _unit(v) -> list[float] | None:
    length = math.sqrt(sum(c * c for c in v))
    if not math.isfinite(length) or length < 1e-12:
        return None
    return [c / length for c in v]


def _camera(shape, defect: EntityDefect) -> dict:
    from meshpipeline.cad.repair.operations import box_of, sub_shape

    centre, diagonal = _part_bounds(shape)
    sub = sub_shape(shape, defect.entity_type, defect.entity_index)
    box = box_of(sub) if sub is not None else {}
    if not box:
        return _whole_part(shape)

    centroid = [float(c) for c in box["centroid"]]
    span = math.dist(box["bbox_min"], box["bbox_max"])
    normal = _face_normal(shape, defect.entity_type, sub, centroid)
    if normal is None:
        normal = _unit([c - m for c, m in zip(centroid, centre, strict=True)])
    return {"centroid_mm": centroid, "normal": normal or list(_DEFAULT_NORMAL),
            "extent_mm": _extent(span, diagonal)}


def _face_normal(shape, entity_type: str, sub, near: list[float]) -> list[float] | None:
    """The outward normal of the entity's own face, or of the first face that carries it."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    if entity_type == "face":
        return _normal_of_face(TopoDS.Face_s(sub), near)

    parent = sub
    if entity_type == "shell":
        explorer = TopExp_Explorer(sub, TopAbs_FACE)
        return _normal_of_face(TopoDS.Face_s(explorer.Current()), near) if explorer.More() else None
    if entity_type == "wire":
        explorer = TopExp_Explorer(sub, TopAbs_EDGE)
        if not explorer.More():
            return None
        parent = explorer.Current()
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, parent.ShapeType(), TopAbs_FACE, ancestors)
    if not ancestors.Contains(parent):
        return None
    for face in ancestors.FindFromKey(parent):
        normal = _normal_of_face(TopoDS.Face_s(face), near)
        if normal is not None:
            return normal
    return None


def _normal_of_face(face, near: list[float]) -> list[float] | None:
    """The face's outward normal at the point of the face closest to `near`.

    Sampling at the entity matters on a curved face: a cylinder's normal turns with the angle, so
    the parameter-space midpoint would give an arbitrary radial direction. If the projection
    fails, the midpoint is used.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_REVERSED

    surface = BRepAdaptor_Surface(face, True)
    u = (surface.FirstUParameter() + surface.LastUParameter()) / 2
    v = (surface.FirstVParameter() + surface.LastVParameter()) / 2
    try:
        projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*near), BRep_Tool.Surface_s(face))
        if projector.NbPoints() > 0:
            u, v = projector.LowerDistanceParameters()
    except Exception:  # noqa: BLE001 - keep the midpoint
        pass
    props = BRepLProp_SLProps(surface, u, v, 1, 1e-9)
    if not props.IsNormalDefined():
        return None
    d = props.Normal()
    vec = [d.X(), d.Y(), d.Z()]
    if face.Orientation() == TopAbs_REVERSED:
        vec = [-c for c in vec]
    return _unit(vec)
