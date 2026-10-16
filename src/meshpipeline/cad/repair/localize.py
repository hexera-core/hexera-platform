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
    #: A name for this entity that SURVIVES A REPAIR (cad/repair/identity.py). `entity_index`
    #: above is an index into one shape's map, and ShapeFix renumbers everything it touches - so
    #: after a repair the index cannot answer "did THIS defect go away", only "did the count
    #: fall". Empty when the entity had no measurable extent, which makes the defect untargetable
    #: and is reported as such rather than guessed around.
    fingerprint: str = ""

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
            "fingerprint": self.fingerprint,
        }


def _indexed(shape, kind):
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


#: The unit every position and size in this module is expressed in. OpenCASCADE transfers a STEP
#: into its own system unit on read, so these numbers are millimetres REGARDLESS of what the
#: customer authored in - and a number an operator may type into their own CAD system has to say
#: which unit it is, or an inch-authored part sends them 25.4 times too far.
MEASUREMENT_UNIT = "mm"


def _centroid_and_box(sub) -> dict:
    """Where an entity is, in millimetres.

    NOT the file's own unit. OpenCASCADE normalises to millimetres on transfer, so that is what
    these are, and the payload says so rather than leaving a reader to assume the customer's unit.
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
        "unit": MEASUREMENT_UNIT,
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

    BY BOUNDING BOX, NOT BY CLASSIFICATION, and that is a measured decision rather than a
    shortcut. The obvious call is `BRepClass3d.OuterShell_s`, which answers exactly this question
    - and on a 664-face part from the NIST PMI corpus it did not return within 175 seconds. It
    raycasts, and inspection runs on every mesh-intended job, so a call that can take minutes on
    an ordinary part is not one this path may make.
    A solid's outer shell is the one whose bounding box encloses the rest, which costs a box per
    shell and is right for the shapes this distinction is useful on: a hollow body, a duct, a part
    with a cavity. A shape whose shells genuinely interleave would defeat it, and the answer there
    is `unknown` rather than a guess - which is what an empty set produces.

    Returns an empty set when the question does not apply or cannot be answered cheaply: a solid
    with one shell has no inside to be inside of.
    """
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    faces = _indexed(shape, TopAbs_FACE)
    interior: set[int] = set()
    try:
        solids = TopExp_Explorer(shape, TopAbs_SOLID)
        while solids.More():
            solid = solids.Current()
            solids.Next()

            shells = []
            explorer = TopExp_Explorer(solid, TopAbs_SHELL)
            while explorer.More():
                shell = explorer.Current()
                shells.append((_span_of(shell), shell))
                explorer.Next()
            if len(shells) < 2:
                continue        # nothing enclosed, so nothing is interior

            shells.sort(key=lambda pair: pair[0], reverse=True)
            for _, shell in shells[1:]:
                inner = TopExp_Explorer(shell, TopAbs_FACE)
                while inner.More():
                    index = int(faces.FindIndex(inner.Current()))
                    if index > 0:
                        interior.add(index)
                    inner.Next()
    except Exception as exc:  # noqa: BLE001 - classification is evidence, never load-bearing
        logger.debug("localize: could not classify interior faces (%s)", exc)
        return set()
    return interior


def _span_of(shape) -> float:
    """The diagonal of a sub-shape's bounding box - how big it is, cheaply."""
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


#: Sub-shapes the status walk will visit before it stops asking. A real part can carry thousands
#: of edges and the analyser is consulted per sub-shape, so an unbounded walk is how inspection
#: comes to cost minutes. Past this the walk reports what it has and says it is partial: an
#: incomplete localisation is worth having, a stalled worker is not.
LOCALIZE_STATUS_BUDGET = 1200


def _status_defects(shape, *, interior: set[int],
                    valid: bool) -> tuple[list[EntityDefect], bool]:
    """Whatever BRepCheck DOES attribute to a sub-shape, mapped onto the product's vocabulary.

    Returns the findings AND whether the walk saw every sub-shape. The second half matters as much
    as the first: a walk that stopped at its budget has a defect list nobody may read as a full
    diagnosis, and the only way a caller can know is for this to say so.

    SKIPPED ENTIRELY FOR A VALID SHAPE, which is most of them, and that is not an optimisation
    with a trade-off - it is the definition. BRepCheck attributes a status to a sub-shape only
    where it has something to report, so a shape the analyser calls valid has nothing here by
    construction. Measured on a 664-face NIST part: the walk cost 40.5 seconds and found zero
    defects, while the free-boundary analysis that found the real openings cost nothing.

    FACES AND SHELLS FIRST. The walk visits them before wires and edges, because that is the order
    in which findings are worth having and the budget may run out: a part's edges outnumber its
    faces several times over, and they are where the analyser attributes least.
    """
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer

    out: list[EntityDefect] = []
    if valid:
        return out, True

    visited = 0
    truncated = False
    analyzer = BRepCheck_Analyzer(shape, True)
    for kind, label in ((TopAbs_FACE, "face"), (TopAbs_SHELL, "shell"),
                        (TopAbs_WIRE, "wire"), (TopAbs_EDGE, "edge")):
        if visited >= LOCALIZE_STATUS_BUDGET:
            truncated = True
            break
        indexed = _indexed(shape, kind)
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            sub = explorer.Current()
            explorer.Next()
            visited += 1
            if visited > LOCALIZE_STATUS_BUDGET:
                logger.info("localize: status walk stopped at %d sub-shapes - the report is "
                            "partial", LOCALIZE_STATUS_BUDGET)
                truncated = True
                break
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
    return out, not truncated


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


#: Worst-highest, for choosing which of two records of one defect survives.
_SEVERITY_RANK = {DefectSeverity.info: 0, DefectSeverity.warning: 1,
                  DefectSeverity.error: 2, DefectSeverity.fatal: 3}


def _deduped(found: list[EntityDefect]) -> list[EntityDefect]:
    """One defect per `(entity_type, entity_index, code)` - the MOST SEVERE, in the first's place.

    THE SAME ENTITY REALLY IS REPORTED TWICE. `_status_defects` emits one defect per kernel
    STATUS, and several statuses map to one product code (`NotClosed`, `FreeEdge` and `EmptyShell`
    are all `open_shell`); `TopExp_Explorer` also enumerates a shared sub-shape once per owning
    parent, so one edge can be visited from two faces. The duplicate records carry no information
    the first does not.

    WHY LEAVING THEM IN WAS A CORRECTNESS BUG RATHER THAN NOISE. `identity.refs_for` saw two
    entries with an identical box, concluded two entities shared it, and keyed them `edge:abc#0`
    and `edge:abc#1`. After a repair cleared one status the survivor was stamped `edge:abc` with no
    suffix, so `session._progress_refusal` looked for a name that no longer existed, found neither
    it nor a mapped successor, and KEPT an operation whose targeted defect was still there -
    `loop._abandoned` missed it identically, so the run could report `resolved`.

    Two DIFFERENT codes on one entity stay two defects: an edge can genuinely be both a sliver and
    a broken curve, and the progress rule is asked about one code at a time.

    A WARNING MUST NEVER SHADOW AN ERROR, which is why the survivor is the most severe rather than
    the first. `non_manifold_surface` is the one code in `_STATUS_TO_CODE` carrying both: a shell's
    `EnclosedRegion` is a warning while its `InvalidMultiConnexity` and
    `InvalidImbricationOfShells` are errors, and all three are shell statuses, so one shell can
    report both. Keeping whichever the kernel listed first would drop the error record, and
    `status_for` would then call the run `repaired` over an error recorded as a warning.

    THE POSITION IS THE FIRST RECORD'S, AND THAT IS LOAD-BEARING, not neatness. At equal severity
    the earlier record wins, and `localize_scan` extends the free-bound findings BEFORE the status
    walk - so the rich `open_shell` record (the one carrying `boundary_edges`) survives a
    status-walk record of the same opening. `FillPlanarHole.targets_for` accepts only a defect with
    `boundary_edges`, so reordering those `extend` calls, or making a later duplicate win, would
    silently disable planar filling.
    """
    best: dict[tuple[str, int, str], int] = {}
    out: list[EntityDefect] = []
    for defect in found:
        key = (defect.entity_type, int(defect.entity_index), defect.code.value)
        slot = best.get(key)
        if slot is None:
            best[key] = len(out)
            out.append(defect)
            continue
        if _SEVERITY_RANK.get(defect.severity, 0) > _SEVERITY_RANK.get(out[slot].severity, 0):
            # The same defect, reported worse. Replace IN PLACE, so the survivor keeps the
            # position - and the precedence - the first record had.
            out[slot] = defect
    return out


def _stamped(found: tuple[EntityDefect, ...], *,
             diagonal_mm: float) -> tuple[EntityDefect, ...]:
    """Give every located defect a name that survives a repair.

    Done HERE, in one place, after the whole scan: the fingerprint's grid is a fraction of the
    PART's diagonal, so it cannot be computed by each defect producer in isolation without each
    of them re-measuring the shape.
    """
    from dataclasses import replace

    from meshpipeline.cad.repair.identity import refs_for

    refs = refs_for([(d.entity_type, d.entity_index, d.location) for d in found],
                    diagonal_mm=diagonal_mm)
    return tuple(replace(defect, fingerprint=ref.fingerprint)
                 for defect, ref in zip(found, refs, strict=True))


def localize_scan(shape, *, valid: bool | None = None) -> tuple[tuple[EntityDefect, ...], bool]:
    """Every located failure point this module can find, and whether the scan was exhaustive.

    Order is by entity type then index, NOT by severity: a caller that wants the worst first can
    sort, and a stable order means two inspections of the same bytes produce the same report -
    which is what makes a report comparable before and after a repair.

    THE SECOND RETURN VALUE IS NOT A DETAIL. The status walk is bounded (see
    `LOCALIZE_STATUS_BUDGET`), so on a very large invalid part the list can be short because the
    walk stopped, not because the part is nearly sound. Reporting that as a complete diagnosis is
    how a repairable defect gets missed, so completeness travels with the findings.
    """
    from OCP.TopAbs import TopAbs_FACE

    diagonal = _diagonal(shape)
    faces = _indexed(shape, TopAbs_FACE)
    total_area = sum(_face_area(faces.FindKey(i)) for i in range(1, faces.Extent() + 1))
    interior = _interior_faces(shape)

    if valid is None:
        from OCP.BRepCheck import BRepCheck_Analyzer
        valid = bool(BRepCheck_Analyzer(shape, True).IsValid())

    found: list[EntityDefect] = []
    # FREE BOUNDS FIRST, AND `_deduped` DEPENDS ON IT: the free-bound record of an opening is the
    # only one carrying `boundary_edges`, which is the only `open_shell` record
    # `FillPlanarHole.targets_for` will aim at. At equal severity the earlier record survives
    # deduplication, so putting the status walk first would silently disable planar filling.
    found.extend(_open_boundary_defects(shape, diagonal=diagonal, interior=interior))
    attributed, complete = _status_defects(shape, interior=interior, valid=valid)
    found.extend(attributed)
    found.extend(_size_defects(shape, diagonal=diagonal, total_area=total_area,
                               interior=interior))
    found.sort(key=lambda d: (d.entity_type, d.entity_index, d.code.value))
    # DEDUPED BEFORE THE STAMP, and after the sort so the survivor of a pair is chosen in the same
    # stable order the report is read in. A duplicate record of one entity makes `refs_for` believe
    # two entities share a box, and the `#n` suffix that follows is not stable across a repair -
    # see `_deduped`.
    found = _deduped(found)
    # STAMPED AFTER THE SORT, so the collision suffix a shared box gets is assigned in the same
    # stable order the report is read in - two scans of identical bytes must produce identical
    # names, or nothing can be compared across a repair.
    return _stamped(tuple(found), diagonal_mm=diagonal), complete


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
