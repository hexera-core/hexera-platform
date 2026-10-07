# Responsibility: Say WHERE a B-rep is broken - which entity, how big, and whether it is on the
#                 part's outside or inside a cavity.
# Boundaries: it measures and reports. It mutates nothing, repairs nothing, and recommends nothing.
# Collaborates with: cad/repair/brep.py (which reports what this found), cad/repair/triage.py
#                    (which may only recommend an automatic repair for a defect it can point at),
#                    and a repair executor, which needs a target rather than a verdict.
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity

logger = logging.getLogger(__name__)

# WHY THIS EXISTS.
#
# `BRepCheck_Analyzer(shape).IsValid()` answers one bit about a whole part. "Your file is invalid"
# is not something an operator can act on, not something an automatic repair can aim at, and not
# something a model could be given enough context to reason about. This module turns that bit into
# located failure points: this edge loop, 12 mm across, bounding a hole on an interior wall.
#
# WHAT THE KERNEL ACTUALLY GIVES YOU, learned by probing rather than from the documentation:
#
#   - BRepCheck_Analyzer reports IsValid() == False for an open solid while EVERY sub-shape's
#     Result(...).Status() comes back NoError. The invalidity is a property of the assembly, not
#     attributed to any face or edge, so a localiser built only on the status walk finds nothing
#     on the commonest real defect.
#   - ShapeAnalysis_FreeBounds DOES localise it: for a box missing one face it returns the four
#     edges bounding the hole, as a closed wire. That is the single most useful signal here -
#     "there is a hole, these edges bound it, it is this big, it is here".
#
# So both are used, and neither is trusted to find what the other finds.

#: A face smaller than this fraction of the part's total surface area is reported as small. Not a
#: defect by itself - a legitimate chamfer can be tiny - which is why it is a warning that routes
#: to a person rather than something an automatic repair acts on.
SMALL_FACE_AREA_FRACTION = 1e-5

#: An edge shorter than this fraction of the part's diagonal is reported as small. Short edges are
#: what mesh generators choke on, so this is deliberately more sensitive than the face threshold.
SMALL_EDGE_LENGTH_FRACTION = 1e-4

#: A sub-shape whose tolerance exceeds this multiple of the part's median tolerance is an outlier.
TOLERANCE_OUTLIER_MULTIPLE = 100.0

#: BRepCheck statuses this code understands, mapped onto the product's defect vocabulary. A status
#: absent from here is reported as an unmapped invalid_brep rather than guessed at, because a
#: wrong defect code sends an operator - or an automatic repair - at the wrong thing.
_STATUS_TO_CODE: dict[str, tuple[DefectCode, DefectSeverity]] = {
    "NotClosed": (DefectCode.open_shell, DefectSeverity.error),
    "FreeEdge": (DefectCode.open_shell, DefectSeverity.error),
    "EmptyShell": (DefectCode.open_shell, DefectSeverity.error),
    "EmptyWire": (DefectCode.invalid_brep, DefectSeverity.error),
    "RedundantEdge": (DefectCode.duplicate_surface_data, DefectSeverity.warning),
    "RedundantWire": (DefectCode.duplicate_surface_data, DefectSeverity.warning),
    "SelfIntersectingWire": (DefectCode.self_intersection, DefectSeverity.error),
    "IntersectingWires": (DefectCode.self_intersection, DefectSeverity.error),
    "InvalidCurveOnSurface": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "InvalidCurveOnClosedSurface": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "Invalid3DCurve": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "NoCurveOnSurface": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "InvalidPointOnCurve": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "InvalidPointOnCurveOnSurface": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "InvalidPointOnSurface": (DefectCode.curve_inconsistency, DefectSeverity.error),
    "BadOrientation": (DefectCode.bad_orientation, DefectSeverity.error),
    "BadOrientationOfSubshape": (DefectCode.bad_orientation, DefectSeverity.error),
    "InvalidDegeneratedFlag": (DefectCode.degenerate_edge, DefectSeverity.error),
    "InvalidMultiConnexity": (DefectCode.non_manifold_surface, DefectSeverity.error),
    "InvalidImbricationOfShells": (DefectCode.non_manifold_surface, DefectSeverity.error),
    "InvalidImbricationOfWires": (DefectCode.non_manifold_surface, DefectSeverity.error),
    "EnclosedRegion": (DefectCode.non_manifold_surface, DefectSeverity.warning),
    "CheckFail": (DefectCode.invalid_brep, DefectSeverity.error),
}

#: Where on the part an entity sits. `interior` means it belongs to a shell that is not the solid's
#: outer shell - a cavity wall, a duct lining, the inside of a hollow body. The distinction matters
#: because a hole in an outside face and a hole in a cavity wall are different conversations with a
#: customer, and because an interior defect is the one a person is least likely to spot in a viewer.
REGION_EXTERIOR = "exterior"
REGION_INTERIOR = "interior"
REGION_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EntityDefect:
    """One located failure point: what is wrong, which entity, where, and how big."""

    code: DefectCode
    severity: DefectSeverity
    entity_type: str
    #: 1-based index into the shape's indexed map of that entity type. STABLE for a given shape,
    #: which is what lets a repair aim at this entity and a later report say whether it is gone.
    entity_index: int
    message: str
    region: str = REGION_UNKNOWN
    measurements: dict = field(default_factory=dict)
    location: dict = field(default_factory=dict)
    #: The kernel's own status name, kept verbatim when there was one. A mapped code is this
    #: module's reading of the kernel; the raw name is the kernel's, and a support question months
    #: later wants both.
    kernel_status: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "entity": f"{self.entity_type}:{self.entity_index}",
            "entity_type": self.entity_type,
            "entity_index": int(self.entity_index),
            "message": self.message,
            "region": self.region,
            "measurements": dict(self.measurements),
            "location": dict(self.location),
            "kernel_status": self.kernel_status,
        }


def _indexed(shape, kind):
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def _centroid_and_box(sub) -> dict:
    """Where an entity is, in the file's own coordinates.

    Deliberately NOT normalised to metres: this is read beside the CAD in whatever unit the
    customer authored, and silently rescaling a coordinate an operator is about to type into their
    own CAD system would be worse than leaving it alone. The unit travels with the report.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    try:
        BRepBndLib.Add_s(sub, box)
        if box.IsVoid():
            return {}
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    except Exception:  # noqa: BLE001 - a degenerate entity may have no measurable extent
        return {}
    return {
        "centroid": [round((xmin + xmax) / 2, 6), round((ymin + ymax) / 2, 6),
                     round((zmin + zmax) / 2, 6)],
        "bbox_min": [round(xmin, 6), round(ymin, 6), round(zmin, 6)],
        "bbox_max": [round(xmax, 6), round(ymax, 6), round(zmax, 6)],
    }


def _face_area(face) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    try:
        BRepGProp.SurfaceProperties_s(face, props)
        return float(props.Mass())
    except Exception:  # noqa: BLE001
        return 0.0


def _edge_length(edge) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    try:
        BRepGProp.LinearProperties_s(edge, props)
        return float(props.Mass())
    except Exception:  # noqa: BLE001
        return 0.0


def _interior_faces(shape) -> set[int]:
    """Indices of faces that are NOT on a solid's outer shell - cavity walls and the like.

    Returns an empty set when the question does not apply or cannot be answered: a loose shell has
    no outer shell to be outside of, and a shape the classifier refuses is reported as `unknown`
    rather than guessed as exterior. Claiming a defect is on the outside when it is in a cavity
    would send an operator looking in the wrong place.
    """
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    faces = _indexed(shape, TopAbs_FACE)
    interior: set[int] = set()
    try:
        from OCP.BRepClass3d import BRepClass3d

        solids = TopExp_Explorer(shape, TopAbs_SOLID)
        while solids.More():
            solid = solids.Current()
            outer = BRepClass3d.OuterShell_s(solid)
            outer_faces = _indexed(outer, TopAbs_FACE) if outer is not None else None
            inner = TopExp_Explorer(solid, TopAbs_FACE)
            while inner.More():
                face = inner.Current()
                if outer_faces is None or not outer_faces.Contains(face):
                    idx = faces.FindIndex(face)
                    if idx > 0:
                        interior.add(int(idx))
                inner.Next()
            solids.Next()
    except Exception as exc:  # noqa: BLE001 - classification is evidence, never load-bearing
        logger.debug("localize: could not classify interior faces (%s)", exc)
        return set()
    return interior


def _edge_region(shape, edge_indices: list[int], interior: set[int]) -> str:
    """Whether an opening sits on the part's outside or inside a cavity.

    Decided by the FACES THE EDGES BELONG TO: an edge is only interior if a face it bounds is. With
    no interior faces to speak of the answer is `unknown` rather than `exterior`, because a loose
    shell has no inside and claiming one would be an invention.
    """
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    if not interior:
        return REGION_UNKNOWN
    try:
        faces = _indexed(shape, TopAbs_FACE)
        edges = _indexed(shape, TopAbs_EDGE)
        ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
        for index in edge_indices:
            if index < 1 or index > edges.Extent():
                continue
            edge = edges.FindKey(index)
            if not ancestors.Contains(edge):
                continue
            for face in ancestors.FindFromKey(edge):
                if int(faces.FindIndex(face)) in interior:
                    return REGION_INTERIOR
        return REGION_EXTERIOR
    except Exception as exc:  # noqa: BLE001 - a region is evidence, never load-bearing
        logger.debug("localize: could not place an opening (%s)", exc)
        return REGION_UNKNOWN


def _open_boundary_defects(shape, *, diagonal: float, interior: set[int]) -> list[EntityDefect]:
    """Holes, as the edge loops that bound them.

    THE MOST USEFUL SIGNAL IN THIS MODULE, and the one BRepCheck's status walk does not provide:
    for an open solid the analyser reports `IsValid() == False` and attributes it to nothing, while
    free-bounds analysis hands back the exact edges around the opening.
    """
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer

    out: list[EntityDefect] = []
    try:
        from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds

        analysis = ShapeAnalysis_FreeBounds(shape)
        all_edges = _indexed(shape, TopAbs_EDGE)
        for wires, closed in ((analysis.GetClosedWires(), True),
                              (analysis.GetOpenWires(), False)):
            if wires is None:
                continue
            # PER WIRE, NOT PER COMPOUND. Free-bound analysis hands back ONE compound holding
            # every boundary it found, so walking its edges directly merges unrelated openings
            # into a single defect - which is how the two rims of one bore first came back as one
            # hole, disagreeing with the discriminator that correctly saw two.
            wire_explorer = TopExp_Explorer(wires, TopAbs_WIRE)
            while wire_explorer.More():
                wire = wire_explorer.Current()
                wire_explorer.Next()
                loop: list[int] = []
                length = 0.0
                edges = TopExp_Explorer(wire, TopAbs_EDGE)
                while edges.More():
                    edge = edges.Current()
                    index = int(all_edges.FindIndex(edge))
                    if index > 0:
                        loop.append(index)
                    length += _edge_length(edge)
                    edges.Next()
                if not loop:
                    continue
                geometry = _centroid_and_box(wire)
                # Reported against the FIRST edge of the loop, with the whole loop listed in the
                # measurements: a hole is a property of the loop, but a defect has to name one
                # entity so that a later report can say whether this one is gone.
                out.append(EntityDefect(
                    code=DefectCode.open_shell,
                    severity=DefectSeverity.error,
                    entity_type="edge", entity_index=loop[0],
                    message=(f"A {'closed' if closed else 'broken'} free boundary of "
                             f"{len(loop)} edge(s) bounds an opening in the surface."),
                    region=_edge_region(shape, loop, interior),
                    measurements={"boundary_edges": loop, "boundary_length": round(length, 6),
                                  "closed_loop": closed,
                                  "relative_size": (round(length / diagonal, 6)
                                                    if diagonal > 0 else None)},
                    location=geometry,
                    kernel_status="FreeBounds"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("localize: free-bound analysis unavailable (%s)", exc)
    return out


def _status_defects(shape, *, interior: set[int]) -> list[EntityDefect]:
    """Whatever BRepCheck DOES attribute to a sub-shape, mapped onto the product's vocabulary."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer

    out: list[EntityDefect] = []
    analyzer = BRepCheck_Analyzer(shape, True)
    for kind, label in ((TopAbs_FACE, "face"), (TopAbs_WIRE, "wire"),
                        (TopAbs_EDGE, "edge"), (TopAbs_SHELL, "shell")):
        indexed = _indexed(shape, kind)
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            sub = explorer.Current()
            explorer.Next()
            try:
                statuses = [str(s).rsplit("BRepCheck_", 1)[-1]
                            for s in analyzer.Result(sub).Status()]
            except Exception as exc:  # noqa: BLE001, S112
                # NOT A DEFECT. BRepCheck has no result for most sub-shapes of a valid-enough
                # shape, and asking raises rather than returning empty. Debug-logged because a
                # storm of these would mean the analyser itself is unhappy with the shape.
                logger.debug("localize: no check result for a %s (%s)", label, exc)
                continue
            for status in statuses:
                if status == "NoError":
                    continue
                code, severity = _STATUS_TO_CODE.get(
                    status, (DefectCode.invalid_brep, DefectSeverity.error))
                index = int(indexed.FindIndex(sub))
                out.append(EntityDefect(
                    code=code, severity=severity, entity_type=label,
                    entity_index=index if index > 0 else 0,
                    message=f"OpenCASCADE reports {status} on this {label}.",
                    region=(REGION_INTERIOR if label == "face" and index in interior
                            else REGION_EXTERIOR if label == "face" and interior
                            else REGION_UNKNOWN),
                    location=_centroid_and_box(sub),
                    kernel_status=status))
    return out


def _size_defects(shape, *, diagonal: float, total_area: float,
                  interior: set[int]) -> list[EntityDefect]:
    """Faces and edges too small to mesh well. Warnings: a tiny chamfer is legitimate geometry."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    out: list[EntityDefect] = []
    faces = _indexed(shape, TopAbs_FACE)
    for i in range(1, faces.Extent() + 1):
        face = faces.FindKey(i)
        area = _face_area(face)
        if total_area > 0 and 0.0 <= area < total_area * SMALL_FACE_AREA_FRACTION:
            out.append(EntityDefect(
                code=DefectCode.small_face, severity=DefectSeverity.warning,
                entity_type="face", entity_index=i,
                message="This face is a sliver relative to the part; it may not mesh cleanly.",
                region=REGION_INTERIOR if i in interior else (
                    REGION_EXTERIOR if interior else REGION_UNKNOWN),
                measurements={"area": round(area, 9),
                              "area_fraction": round(area / total_area, 9)},
                location=_centroid_and_box(face)))

    edges = _indexed(shape, TopAbs_EDGE)
    for i in range(1, edges.Extent() + 1):
        edge = edges.FindKey(i)
        length = _edge_length(edge)
        if diagonal > 0 and 0.0 <= length < diagonal * SMALL_EDGE_LENGTH_FRACTION:
            out.append(EntityDefect(
                code=DefectCode.small_edge, severity=DefectSeverity.warning,
                entity_type="edge", entity_index=i,
                message="This edge is far shorter than the part's scale; it will force tiny cells.",
                measurements={"length": round(length, 9),
                              "length_fraction": round(length / diagonal, 9)},
                location=_centroid_and_box(edge)))
    return out


def localize(shape) -> tuple[EntityDefect, ...]:
    """Every located failure point this module can find, in a stable order.

    Order is by entity type then index, NOT by severity: a caller that wants the worst first can
    sort, and a stable order means two inspections of the same bytes produce the same report -
    which is what makes a report comparable before and after a repair.
    """
    from OCP.TopAbs import TopAbs_FACE

    diagonal = _diagonal(shape)
    faces = _indexed(shape, TopAbs_FACE)
    total_area = sum(_face_area(faces.FindKey(i)) for i in range(1, faces.Extent() + 1))
    interior = _interior_faces(shape)

    found: list[EntityDefect] = []
    found.extend(_open_boundary_defects(shape, diagonal=diagonal, interior=interior))
    found.extend(_status_defects(shape, interior=interior))
    found.extend(_size_defects(shape, diagonal=diagonal, total_area=total_area,
                               interior=interior))
    found.sort(key=lambda d: (d.entity_type, d.entity_index, d.code.value))
    return tuple(found)


def _diagonal(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    try:
        BRepBndLib.Add_s(shape, box)
        if box.IsVoid():
            return 0.0
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    except Exception:  # noqa: BLE001
        return 0.0
    return float(((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5)


def summarise(found: tuple[EntityDefect, ...]) -> dict:
    """Counts per defect code and per region - the shape of a part's problem, in one line.

    The region split is the answer to "is this a surface problem or is something wrong inside":
    defects in a cavity are the ones nobody spots by looking at the part in a viewer.
    """
    by_code: dict[str, int] = {}
    by_region: dict[str, int] = {}
    for defect in found:
        by_code[defect.code.value] = by_code.get(defect.code.value, 0) + 1
        by_region[defect.region] = by_region.get(defect.region, 0) + 1
    return {"total": len(found), "by_code": by_code, "by_region": by_region}
