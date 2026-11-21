# Responsibility: Say WHERE a B-rep is broken - which entity, how big, and whether it is on the
#                 part's outside or inside a cavity.
# Boundaries: it measures and reports. It mutates nothing, repairs nothing, and recommends nothing.
# Collaborates with: cad/repair/brep.py (which reports what this found), cad/repair/triage.py
#                    (which may only recommend an automatic repair for a defect it can point at),
#                    and a repair executor, which needs a target rather than a verdict.
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity

if TYPE_CHECKING:                   # a type, not a dependency: nothing here calls into regions
    from meshpipeline.cad.repair.regions import Region

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
#: defect by itself - a legitimate chamfer can be tiny - which is why it is a warning. Only the
#: consented sweep (`operations.FixSmallFaces`) acts on one; without it a person decides.
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
    #: Another entity shares this one's box, so `fingerprint` cannot tell them apart
    #: (identity.refs_for). Carried onto every operation target, so the session's basis rule can
    #: refuse to call a verdict about it kernel-confirmed.
    ambiguous: bool = False

    def to_dict(self) -> dict:
        out = {
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
        if self.ambiguous:
            out["ambiguous"] = True
        if analyzer_only(self):
            # ITS OWN MARKER, so a reader of `defects_remaining` can tell a finding only the face
            # analyzer made - one that targets the heal and asks a person, but decides no ending
            # and no route - from a status-walk defect. Absent on every other record, which is
            # what keeps a flag-off report byte-for-byte what it was.
            out["analyzer_only"] = True
        return out


def analyzer_only(defect) -> bool:
    """Is this a finding ONLY the face-level analyzer made (no status-walk record of it)?

    THE FLOOR-FIX RULING. Such a finding exists to give the heal a target and a person a question.
    It must not change any other operation's verdict, the run's ending, or the inspection's triage
    route: those are decided on the status walk, exactly as they were before the analyzer existed.
    A face the status walk ALSO reported (a `SelfIntersectingWire` the analyzer's record shadowed
    in `_deduped`) is not analyzer-only - the flag-off world measured it too.
    Reads an EntityDefect or its stored dict, so triage and the loop share one definition.
    """
    if isinstance(defect, dict):
        status = defect.get("kernel_status", "")
        measurements = defect.get("measurements") or {}
    else:
        status = getattr(defect, "kernel_status", "")
        measurements = getattr(defect, "measurements", None) or {}
    return status == _SELF_INTERFERENCE and not measurements.get(_STATUS_WALK_ALSO)


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


#: How far a region's box is grown before an entity is tested against it, as a fraction of the
#: PART's diagonal. The same fraction `regions.decompose` merges on by default, deliberately: a
#: scope tighter than the clustering that produced the region could exclude geometry the region
#: was defined to contain, and then a scoped re-inspection would report a defect gone because it
#: never looked at it.
SCOPE_GROW_FRACTION = 0.01


@dataclass(frozen=True, slots=True)
class _Scope:
    """Which entities a scoped scan is allowed to measure.

    A FACE IS ADMITTED ON ITS INDEX **OR** ON ITS BOX, and the `or` is the whole safety property.
    The region's `face_indices` is a set lookup and so the fast path, but a region built from edge
    defects alone names NO face - and a scope that stopped there could not see a face defect that
    appeared inside the region since the region was built. A package that damaged the very region
    it was proving would then be re-inspected and pass, which is the one failure the region loop
    exists to prevent. So a face the index set does not name gets a second chance on geometry.

    THE COST ASYMMETRY THAT MAKES THAT AFFORDABLE: a face's bounding box is cheap beside the
    per-face work it gates - the surface-area integration, the status walk's `Result(sub)`, the
    crossing analysis. Measuring a box to reject a face still avoids the expensive part, and the
    box test is memoised per face index, so each face is placed at most once per scan. Admitting a
    face you then reject is fine; never looking at it is not.

    AN UNMEASURABLE ENTITY IS NEVER REJECTED. `Bnd_Box` comes back void for a degenerate edge or
    an empty shell, and "outside the region" is not something that can be concluded from an
    absent extent. A scope that cannot place an entity measures it, because the alternative is
    reporting its absence from a region nobody looked in.
    """

    #: Faces the region named, admitted without being placed. A region made only of edge defects
    #: names none, and then every face reaches the box test instead.
    faces: frozenset[int]
    #: The region's box, already grown. None on the UNLOCATED region, which has no position: there
    #: NOTHING is rejected, because nothing can be placed relative to a region that has none, and
    #: a scan scoped to it is simply the whole-shape scan. That region exists so unlocated defects
    #: cannot be silently dropped, and there is no geometric saving to be had on it - an honest
    #: whole-shape answer beats a scope that would guess at what to skip.
    low: tuple[float, float, float] | None
    high: tuple[float, float, float] | None
    #: The box test's answer per face index, for the ONE shape and ONE scan this scope serves
    #: (an index means nothing across two shapes). Several producers ask about the same face.
    placed: dict[int, bool] = field(default_factory=dict, compare=False, repr=False)

    def allows_face(self, index: int, face) -> bool:
        """May a face be measured? Named by the region, unplaceable, or inside its grown box."""
        key = int(index)
        if key in self.faces or self.low is None:
            return True
        answer = self.placed.get(key)
        if answer is None:
            answer = self.allows_box(_bounds_of(face))
            self.placed[key] = answer
        return answer

    def allows(self, sub) -> bool:
        """May a non-face sub-shape be measured? Box-tested, or admitted when unplaceable."""
        if self.low is None:
            return True
        return self.allows_box(_bounds_of(sub))

    def allows_box(self, bounds) -> bool:
        if self.low is None or bounds is None:
            return True
        lo, hi = bounds
        assert self.high is not None
        return all(lo[k] <= self.high[k] and self.low[k] <= hi[k] for k in range(3))


def _scope_for(within: Region, *, diagonal: float) -> _Scope:
    """A `Region` (cad/repair/regions.py) read as a scope, with its box grown."""
    grow = max(0.0, SCOPE_GROW_FRACTION * float(diagonal))
    faces = frozenset(int(i) for i in within.face_indices)
    lo, hi = within.bbox_min, within.bbox_max
    if lo is None or hi is None:
        return _Scope(faces=faces, low=None, high=None)
    return _Scope(faces=faces,
                  low=tuple(float(v) - grow for v in lo),      # type: ignore[arg-type]
                  high=tuple(float(v) + grow for v in hi))     # type: ignore[arg-type]


def scoped_to(defects: Iterable[EntityDefect], within: Region, *,
              diagonal_mm: float) -> tuple[EntityDefect, ...]:
    """The part of an ALREADY-MEASURED defect list that a scan scoped to `within` could report.

    THE ONE RULE, SHARED. A caller that compares a whole-shape list against a scoped scan has to
    filter the whole-shape side to the same scope or the comparison is between two different
    parts (`session.try_op`). Written here rather than there so that the admission rule a scoped
    scan applies to the SHAPE and the rule a caller applies to its own RECORDS cannot drift: both
    go through `_Scope`, and both admit a face on its index or its box and never reject an entity
    they cannot place.

    IT MEASURES NOTHING. The boxes come from each defect's own recorded `location`, so this is
    arithmetic over a report and needs no kernel and no shape. `diagonal_mm` is the part's
    diagonal, which is what grows the region's box, and it is the SCAN'S side of the agreement
    too: the caller hands the same number to `localize_scan(..., scope_diagonal_mm=)`, so one
    grown box really does serve both sides. Hand the two different diagonals and the filter
    admits records the scan would not have reported.

    ERRING TOWARDS ADMITTING is deliberate: this list is the BEFORE side of a progress rule, and
    a record wrongly dropped from it reads as a defect the operation has just introduced.
    """
    scope = _scope_for(within, diagonal=diagonal_mm)
    return tuple(d for d in defects if _admits(scope, within, d))


def in_region(defect: EntityDefect, within: Region, *, diagonal_mm: float) -> bool:
    """`scoped_to`'s rule for ONE record - could a scan scoped to `within` have reported it?"""
    return _admits(_scope_for(within, diagonal=diagonal_mm), within, defect)


def _admits(scope: _Scope, within: Region, defect: EntityDefect) -> bool:
    if defect.fingerprint in within.fingerprints:
        return True
    if defect.entity_type == "face" and int(defect.entity_index) in scope.faces:
        return True
    location = defect.location or {}
    low, high = location.get("bbox_min"), location.get("bbox_max")
    if low is None or high is None:
        # UNPLACEABLE IS NOT OUTSIDE, exactly as `_Scope` has it: "outside the region" is not
        # something that can be concluded from an absent extent.
        return True
    return scope.allows_box((tuple(float(v) for v in low), tuple(float(v) for v in high)))


def _bounds_of(sub) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """An entity's bounding box as two corners, or None when it has no measurable extent."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    try:
        BRepBndLib.Add_s(sub, box)
        if box.IsVoid():
            return None
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    except Exception:  # noqa: BLE001 - an unplaceable entity is in scope, not out of it
        return None
    return (xmin, ymin, zmin), (xmax, ymax, zmax)


def _face_area(face) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    try:
        BRepGProp.SurfaceProperties_s(face, props)
        return float(props.Mass())
    except Exception:  # noqa: BLE001
        return 0.0


def _degenerated(edge) -> bool:
    """Does the kernel call this edge DEGENERATE - a cone or sphere apex seam, zero-length by
    construction? A FLAG READ, not an algorithm: nothing here can hang.

    Fails CLOSED on anything unexpected: an edge we cannot ask about is treated as ordinary, so a
    real short edge is never silently dropped from the inspection.
    """
    try:
        from OCP.BRep import BRep_Tool
        from OCP.TopoDS import TopoDS

        return bool(BRep_Tool.Degenerated_s(TopoDS.Edge_s(edge)))
    except Exception as exc:  # noqa: BLE001 - an unreadable flag is "not degenerate"
        logger.debug("localize: degeneracy unreadable (%s)", exc)
        return False


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


def _open_boundary_defects(shape, *, diagonal: float, interior: set[int],
                           scope: _Scope | None = None) -> list[EntityDefect]:
    """Holes, as the edge loops that bound them.

    THE MOST USEFUL SIGNAL IN THIS MODULE, and the one BRepCheck's status walk does not provide:
    for an open solid the analyser reports `IsValid() == False` and attributes it to nothing, while
    free-bounds analysis hands back the exact edges around the opening.

    `scope` DROPS A WIRE BEFORE ITS EDGES ARE MEASURED, not after: the per-edge length sum is the
    cost here, and a wire outside the scope is placed by one box and then left alone. The
    free-bound analysis itself is whole-shape - it has no scoped form - but it is the cheap part
    (see `_status_defects` for what the expensive part is).
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
                if scope is not None and not scope.allows(wire):
                    continue
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
LOCALIZE_STATUS_BUDGET = 5000


def _status_defects(shape, *, interior: set[int], valid: bool,
                    scope: _Scope | None = None) -> tuple[list[EntityDefect], bool]:
    """Whatever BRepCheck DOES attribute to a sub-shape, mapped onto the product's vocabulary.

    Returns the findings AND whether the walk saw every sub-shape IT WAS ASKED ABOUT. Unscoped
    that is every sub-shape; with a `scope` it is every sub-shape of that region, and nothing is
    claimed about the rest of the part. The second half matters as much as the first: a walk that
    stopped at its budget has a defect list nobody may read as a full diagnosis, and the only way
    a caller can know is for this to say so.

    `scope` IS WHERE THE SAVING IS. This walk is the expensive half of an inspection - 40.5
    seconds on a 664-face NIST part - because `analyzer.Result(sub).Status()` is paid per
    sub-shape. An out-of-scope sub-shape is never asked about and never counted against the
    budget: the budget bounds work done, so counting skips would make a scoped walk declare itself
    partial for work it correctly refused to do.

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
            if scope is not None and not (
                    scope.allows_face(int(indexed.FindIndex(sub)), sub)
                    if kind == TopAbs_FACE else scope.allows(sub)):
                continue
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
                  interior: set[int], scope: _Scope | None = None) -> list[EntityDefect]:
    """Faces and edges too small to mesh well. Warnings: a tiny chamfer is legitimate geometry.

    `diagonal` and `total_area` STAY WHOLE-PART under a scope, because that is what the two
    thresholds mean: a sliver is small relative to the PART, not relative to the region it sits
    in, and re-scaling them per region would make the same face a defect in one scan and sound in
    the next.
    """
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    out: list[EntityDefect] = []
    faces = _indexed(shape, TopAbs_FACE)
    for i in range(1, faces.Extent() + 1):
        face = faces.FindKey(i)
        if scope is not None and not scope.allows_face(i, face):
            continue
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
        if scope is not None and not scope.allows(edge):
            continue
        length = _edge_length(edge)
        if diagonal > 0 and 0.0 <= length < diagonal * SMALL_EDGE_LENGTH_FRACTION:
            if _degenerated(edge):
                # ZERO BY CONSTRUCTION, NOT BY DAMAGE. OpenCASCADE represents a cone's or
                # sphere's apex as an edge of no meaningful length and FLAGS it, and every such
                # edge is under this threshold - 883 of them across 26 wave-9 failing files, all
                # flagged, not one a defect. Reported here they inflated the small_edge count the
                # wave reads, and they reached operations and size hints as TARGETS: an operation
                # aimed at one can only decline, and a hint sized for one asks the mesher to
                # resolve a point. The flag is the fact; the LENGTH cannot tell them apart,
                # because `measurements["length"]` is rounded to nine places and an apex seam
                # therefore reads as a flat 0.0.
                continue
            out.append(EntityDefect(
                code=DefectCode.small_edge, severity=DefectSeverity.warning,
                entity_type="edge", entity_index=i,
                message="This edge is far shorter than the part's scale; it will force tiny cells.",
                measurements={"length": round(length, 9),
                              "length_fraction": round(length / diagonal, 9),
                              # Always False here - a degenerate edge is skipped above - and
                              # recorded so a reader never has to infer it from the length.
                              "degenerated": False},
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
        shadowed = defect
        if _SEVERITY_RANK.get(defect.severity, 0) > _SEVERITY_RANK.get(out[slot].severity, 0):
            # The same defect, reported worse. Replace IN PLACE, so the survivor keeps the
            # position - and the precedence - the first record had.
            shadowed, out[slot] = out[slot], defect
        survivor = out[slot]
        if analyzer_only(survivor) and shadowed.kernel_status != _SELF_INTERFERENCE:
            # THE STATUS WALK SAW THIS FACE TOO. The analyzer's record survives for its partner
            # evidence, but the finding is one the flag-off world also measured, so it keeps its
            # weight in the run's ending and the triage route (see `analyzer_only`).
            out[slot] = replace(survivor, measurements={
                **survivor.measurements, _STATUS_WALK_ALSO: shadowed.kernel_status or "walk"})
    return out


def _stamped(found: tuple[EntityDefect, ...], *,
             diagonal_mm: float) -> tuple[EntityDefect, ...]:
    """Give every located defect a name that survives a repair.

    Done HERE, in one place, after the whole scan: the fingerprint's grid is a fraction of the
    PART's diagonal, so it cannot be computed by each defect producer in isolation without each
    of them re-measuring the shape.

    `diagonal_mm` IS THE RUN'S, NOT NECESSARILY THIS SHAPE'S. `localize_scan` passes its
    `quantum_diagonal_mm` when it was given one, so every scan of one repair run names on the
    upload's grid; it passes the scanned shape's own diagonal only for a caller with no run to
    belong to. See `localize_scan` for what the difference bought.
    """
    from dataclasses import replace

    from meshpipeline.cad.repair.identity import refs_for

    refs = refs_for([(d.entity_type, d.entity_index, d.location) for d in found],
                    diagonal_mm=diagonal_mm)
    return tuple(replace(defect, fingerprint=ref.fingerprint, ambiguous=ref.ambiguous)
                 for defect, ref in zip(found, refs, strict=True))


#: The share of a run's REMAINING time the face-level self-intersection scan may spend, and its
#: ceiling. A quarter, because the scan is evidence for one heal among several and the loop still
#: needs time to try operations and re-inspect; capped, because a two-minute repair budget must
#: not hand half a minute to one analysis on every re-scan.
FACE_SCAN_FRACTION = 0.25
FACE_SCAN_CAP_S = 30.0

#: The kernel status a face-level finding carries. Not a BRepCheck status - BRepCheck never tests
#: whether two faces cross - so it is named for what the analyzer found.
_SELF_INTERFERENCE = "SelfInterference"
#: Set on an analyzer record that shadowed a status-walk record of the same face in `_deduped`:
#: the walk's kernel status, kept so the finding is not mistaken for one only the analyzer made.
_STATUS_WALK_ALSO = "status_walk_status"


def face_scan_budget_s(remaining_s: float) -> float:
    """How long the face-level scan may run, given what is left of the caller's budget."""
    return max(0.0, min(FACE_SCAN_FRACTION * remaining_s, FACE_SCAN_CAP_S))


def _bounded(fn, *args, budget_s: float):
    """`fn(*args)`'s result, or None when it ran past `budget_s` or failed in any way.

    IN A FRESH INTERPRETER, NEVER A FORK, and NOT A DAEMON THREAD - both measured decisions.
    OCP's bindings HOLD THE GIL through a kernel call: with `BOPAlgo_ArgumentAnalyzer.Perform`
    running in a daemon thread, the caller's `join(0.2)` returned only when the 3-second analysis
    finished, so an abandoned thread would be waited for anyway. Nor can the kernel be asked to
    stop: OCP exposes `Message_ProgressIndicator` without a constructor. And a FORK is unsafe here:
    the repair worker is threaded (Celery, plus model-call threads), and a forked child inherits
    their locks - the allocator's, which OCC takes at once - held by threads that do not exist in
    it, so it can deadlock and the evidence is silently lost. `bounded_child` starts a new
    `python -m` in its own session, hands it the shapes as BREP, and SIGKILLs the group at the wall.

    `fn` MUST BE ON `bounded_child.KERNELS`, the allow-list the child dispatches by key; anything
    else is refused (None) without starting a child. Shape arguments cross as one BREP, so shared
    sub-shapes keep their identity and the face map its order; every other argument and the answer
    cross as JSON (bytes as base64; a tuple comes back a list).

    NEVER RAISES, NEVER BLOCKS PAST BUDGET. Every failure - expiry, an exception in `fn`, a child
    that died, an argument JSON cannot carry, a spawn that failed - is logged at info and returned
    as None, because the caller treats this as optional evidence (see `face_self_intersections`).
    """
    if not budget_s or budget_s <= 0:
        return None
    try:
        from meshpipeline.cad.repair import bounded_child

        return bounded_child.run(fn, args, float(budget_s))
    except Exception as exc:  # noqa: BLE001 - optional evidence; a failure here is silence
        logger.info("localize: bounded analysis gave no answer (%s: %s)", type(exc).__name__, exc)
        return None


def _kernel_face_pairs(shape) -> list[tuple[int, int]]:
    """Pairs of faces that cross each other, as 1-based indices into the shape's face map.

    `BOPAlgo_ArgumentAnalyzer` WITH ONLY ITS SELF-INTERFERENCE MODE ON, chosen over
    `BRepAlgoAPI_Check`. The latter wraps the same analyzer but first runs a full
    `BRepCheck_Analyzer` validity pass and the small-edge and rebuild-face checks - the validity
    answer `localize_scan` already has, and defects `_status_defects` and `_size_defects` already
    report - so it would pay twice for what is known to learn the one thing that is not. The
    analyzer runs `BOPAlgo_CheckerSI`, which intersects every pair of sub-shapes that do not
    legitimately share topology; that is the only kernel test of whether two faces cross, and
    BRepCheck has no equivalent.

    FACE-TYPED PAIRS ONLY. The checker also reports vertex, edge and solid participants (on two
    overlapping boxes, roughly half its records name a solid or an edge); a heal aims at faces,
    and an edge or solid record carries no face index to aim with.
    Serial, never `RunParallel`: this runs in a bounded child (`_bounded`) that is killed at its
    wall, and a thread pool there would only compete with the worker for cores.
    """
    from OCP.BOPAlgo import BOPAlgo_ArgumentAnalyzer, BOPAlgo_CheckStatus
    from OCP.TopAbs import TopAbs_FACE

    analyzer = BOPAlgo_ArgumentAnalyzer()
    analyzer.SetShape1(shape)
    analyzer.ArgumentTypeMode = False
    analyzer.SelfInterMode = True
    analyzer.SmallEdgeMode = False
    analyzer.RebuildFaceMode = False
    analyzer.TangentMode = False
    analyzer.MergeVertexMode = False
    analyzer.MergeEdgeMode = False
    analyzer.ContinuityMode = False
    analyzer.CurveOnSurfaceMode = False
    analyzer.StopOnFirstFaulty = False
    analyzer.SetRunParallel(False)
    analyzer.Perform()

    faces = _indexed(shape, TopAbs_FACE)
    pairs: set[tuple[int, int]] = set()
    for result in analyzer.GetCheckResult():
        if result.GetCheckStatus() != BOPAlgo_CheckStatus.BOPAlgo_SelfIntersect:
            continue
        faulty = [*result.GetFaultyShapes1(), *result.GetFaultyShapes2()]
        if len(faulty) != 2 or any(s.ShapeType() != TopAbs_FACE for s in faulty):
            continue
        a, b = (int(faces.FindIndex(s)) for s in faulty)
        if a > 0 and b > 0 and a != b:
            pairs.add((min(a, b), max(a, b)))
    return sorted(pairs)


def _components(pairs: list[tuple[int, int]]) -> dict[int, list[int]]:
    """Each face mapped to the sorted rest of its connected component in the crossing graph.

    A COMPONENT, NOT JUST THE DIRECT PARTNERS: three faces that cross pairwise in a chain are one
    tangle, and a heal that resolves one crossing changes the others, so every face is told about
    the whole knot it sits in.
    """
    parent: dict[int, int] = {}

    def root(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for face in parent:
        groups.setdefault(root(face), []).append(face)
    return {face: sorted(set(groups[root(face)]) - {face}) for face in sorted(parent)}


def face_self_intersections(shape, *, budget_s: float,
                            interior: set[int] | None = None) -> list[EntityDefect]:
    """Faces that cross other faces of the same part, one error per face, within `budget_s`.

    RUN ONLY WHEN THE SELF-INTERSECTION HEAL IS ENABLED (`CAD_REPAIR_HEAL_SELF_INTERSECTIONS`):
    the analysis is the costliest thing a scan can do, and a finding nothing can act on is not
    worth seconds of every inspection. Callers express that by passing a zero budget.

    A TIMEOUT IS SILENCE, NOT INCOMPLETENESS. `localize_scan`'s completeness flag means "the status
    walk saw every sub-shape", and a loop reads False as "no absence may be claimed" - a stop
    reason that would end every run on a large part whose crossing analysis is merely slow. What
    this finds is extra evidence for one heal; without it the scan is exactly as complete as it
    was before the heal existed.
    """
    pairs = _bounded(_kernel_face_pairs, shape, budget_s=budget_s)
    if not pairs:
        return []
    from OCP.TopAbs import TopAbs_FACE

    inside = interior or set()
    faces = _indexed(shape, TopAbs_FACE)
    out: list[EntityDefect] = []
    for index, partners in _components([(int(a), int(b)) for a, b in pairs]).items():
        if index < 1 or index > faces.Extent():
            continue
        out.append(EntityDefect(
            code=DefectCode.self_intersection, severity=DefectSeverity.error,
            entity_type="face", entity_index=index,
            message=f"This face crosses {len(partners)} other face(s) of the part.",
            region=(REGION_INTERIOR if index in inside
                    else REGION_EXTERIOR if inside else REGION_UNKNOWN),
            measurements={"crossing_face_indices": list(partners)},
            location=_centroid_and_box(faces.FindKey(index)),
            kernel_status=_SELF_INTERFERENCE))
    return out


def _linked(found: tuple[EntityDefect, ...]) -> tuple[EntityDefect, ...]:
    """Name each crossing face's partners by fingerprint, once every fingerprint exists.

    AFTER THE STAMP, because a partner's fingerprint does not exist before it: an index is only
    good for this shape, and a heal that renumbers faces must still be able to say which crossing
    it aimed at. A partner with no surviving face record is left out rather than guessed.
    """
    from dataclasses import replace

    names = {d.entity_index: d.fingerprint for d in found
             if d.entity_type == "face" and d.code is DefectCode.self_intersection
             and d.fingerprint}
    out = []
    for defect in found:
        partners = defect.measurements.get("crossing_face_indices")
        if defect.entity_type != "face" or partners is None:
            out.append(defect)
            continue
        out.append(replace(defect, measurements={
            **defect.measurements,
            "crossing_faces": [names[i] for i in partners if i in names]}))
    return tuple(out)


def localize_scan(shape, *, valid: bool | None = None, face_scan_s: float = 0.0,
                  within: Region | None = None,
                  scope_diagonal_mm: float | None = None,
                  quantum_diagonal_mm: float | None = None,
                  ) -> tuple[tuple[EntityDefect, ...], bool]:
    """Every located failure point this module can find, and whether the scan was exhaustive.

    Order is by entity type then index, NOT by severity: a caller that wants the worst first can
    sort, and a stable order means two inspections of the same bytes produce the same report -
    which is what makes a report comparable before and after a repair.

    THE SECOND RETURN VALUE IS NOT A DETAIL. The status walk is bounded (see
    `LOCALIZE_STATUS_BUDGET`), so on a very large invalid part the list can be short because the
    walk stopped, not because the part is nearly sound. Reporting that as a complete diagnosis is
    how a repairable defect gets missed, so completeness travels with the findings.

    `face_scan_s` IS THE FACE-LEVEL SELF-INTERSECTION BUDGET, and zero - the default - means the
    analyzer is never called, so a caller without the heal enabled gets byte-for-byte the scan it
    always got. A face scan that times out adds nothing and leaves completeness the status walk's
    alone (see `face_self_intersections`).

    `within` SCOPES THE SCAN TO ONE REGION (`regions.Region`), and `None` - the default - is the
    whole-shape scan this has always been, entity for entity and in the same order. This is the
    primitive that makes an iterative repair affordable: after an edit inside one region, every
    other region is otherwise re-proved for nothing. What a scope changes:

      - WHICH ENTITIES ARE MEASURED. A face is admitted by the region's `face_indices` OR by its
        box meeting the region's grown box; every other entity by its box alone (see `_Scope`).
        The filter runs before the measurement, so the saving is work avoided rather than results
        discarded - and because a face is admitted on either test, a defect that appeared inside
        the region after the region was built is still found.
      - WHAT COMPLETENESS MEANS. The flag becomes "exhaustive WITHIN THIS REGION". A scoped scan
        never says anything about the part outside its scope - not that it is sound, and not that
        it was checked.

    WHAT A SCOPE DOES NOT CHANGE: the part-wide quantities. The diagonal that scales a
    fingerprint's grid, the total area a sliver is judged against and the interior-face
    classification are all measured on the whole shape, because a name or a threshold that moved
    with the scope would make two scans of one shape incomparable - which is the one thing this
    function may never do.

    `quantum_diagonal_mm` IS THE RUN'S NAMING GRID, AND IT IS NOT AN OPTIMISATION. Left `None`,
    each scan names its entities on a grid scaled to the diagonal of the shape IT JUST SCANNED,
    which is how a repair that moved the part's extent came to rename every entity on the part -
    including entities nobody had touched - so the progress rule could not find the defect it had
    just named and read that as the defect being gone. A FALSE KEEP; measured on
    0059_00592781_58be5d61afe56e0fcdda08e3_step_000, where four zero-length edges sat at boxes
    identical to the micrometre either side of an operation that removed nothing, and all four
    were renamed (docs/superpowers/specs/2026-11-19-target-remains-diagnosis.md).
    `identity.quantum_for` now snaps the diagonal up to a binary octave, which absorbs any extent
    change short of a factor of two - but a part whose diagonal straddles a power of two still
    moves the grid once, and the deeper point stands whatever the snap absorbs: AN ENTITY'S NAME
    MUST BE A FACT ABOUT THE ENTITY, not about what else happened to the part. So a run hands the
    UPLOAD's diagonal here, the same number it hands `scope_diagonal_mm`, and the grid becomes a
    property of the run rather than of the scan.
    `None` KEEPS THE SHAPE'S OWN DIAGONAL, which is right - and is all that is available - for a
    caller with no run to belong to: `brep.inspect_brep_file` scans one shape once and has no
    upload diagonal to offer. A caller that cannot supply it must not be handed a guess.

    `scope_diagonal_mm` IS THE DIAGONAL THAT GROWS THE REGION'S BOX, and it exists so that the
    two sides of a scope cannot disagree. A caller comparing its own records against a scoped
    scan filters them with `scoped_to(..., diagonal_mm=...)`, and the session passes the UPLOAD'S
    diagonal there (it never changes, so a region means the same thing all run). Grown here by
    the CURRENT shape's diagonal instead, a repair that shrank the part would leave the filter
    admitting records this scan would not have reported - which inflates the `previous` side of
    `try_op`'s progress rule and excuses a warning the operation just introduced. `None` keeps
    the shape's own diagonal, which is right for the whole-shape scan, where there is no filter
    to agree with.

    TWO LIMITS, STATED RATHER THAN HIDDEN. The face-level crossing analyzer has no scoped form
    (`BOPAlgo_ArgumentAnalyzer` takes a shape), so under a scope its findings are filtered after
    the fact and an in-scope face's partner list names only partners that are also in scope. And
    a scope that cuts between two entities sharing one box (`identity.refs_for` marks those
    `ambiguous`) can leave the survivor with a different collision suffix than the global scan
    gave it.
    """
    from OCP.TopAbs import TopAbs_FACE

    diagonal = _diagonal(shape)
    # THE SCOPE'S DIAGONAL IS THE CALLER'S WHEN IT HAS ONE, so the box this scan grows is the box
    # the caller's `scoped_to` filter grows. The part-wide `diagonal` below is untouched by it.
    scope = (_scope_for(within, diagonal=diagonal if scope_diagonal_mm is None
                        else float(scope_diagonal_mm))
             if within is not None else None)
    faces = _indexed(shape, TopAbs_FACE)
    # THE AREA SWEEP IS SKIPPED ONLY WHEN THE SCOPE ADMITS NO FACE AT ALL, and that is not a
    # shortcut: the total is read only to judge an in-scope face against it (a sliver is small
    # relative to the PART, so the total stays whole-part whenever it is needed), and with no
    # in-scope face there is nothing to judge. The pre-pass that decides it places faces with
    # their boxes - cheap, short-circuiting, and memoised into the scope, so the walks below
    # re-use the answers rather than paying for them again.
    total_area = (sum(_face_area(faces.FindKey(i)) for i in range(1, faces.Extent() + 1))
                  if scope is None or any(scope.allows_face(i, faces.FindKey(i))
                                          for i in range(1, faces.Extent() + 1))
                  else 0.0)
    interior = _interior_faces(shape)

    if valid is None:
        from OCP.BRepCheck import BRepCheck_Analyzer
        valid = bool(BRepCheck_Analyzer(shape, True).IsValid())

    found: list[EntityDefect] = []
    # FREE BOUNDS FIRST, AND `_deduped` DEPENDS ON IT: the free-bound record of an opening is the
    # only one carrying `boundary_edges`, which is the only `open_shell` record
    # `FillPlanarHole.targets_for` will aim at. At equal severity the earlier record survives
    # deduplication, so putting the status walk first would silently disable planar filling.
    found.extend(_open_boundary_defects(shape, diagonal=diagonal, interior=interior, scope=scope))
    # FACE CROSSINGS BEFORE THE STATUS WALK on a valid body, for the same reason: the analyzer's
    # record carries `crossing_face_indices`, so at equal severity it must survive over the walk's
    # record. An invalid body first needs the cheaper status-directed repair; pair analysis there
    # is both expensive and premature, and will run on the repaired body if it becomes valid.
    ran_face_scan = face_scan_s > 0 and valid
    if ran_face_scan:
        crossings = face_self_intersections(shape, budget_s=face_scan_s, interior=interior)
        found.extend(crossings if scope is None else [
            d for d in crossings
            if 1 <= d.entity_index <= faces.Extent()
            and scope.allows_face(d.entity_index, faces.FindKey(d.entity_index))])
    attributed, complete = _status_defects(shape, interior=interior, valid=valid, scope=scope)
    found.extend(attributed)
    found.extend(_size_defects(shape, diagonal=diagonal, total_area=total_area,
                               interior=interior, scope=scope))
    found.sort(key=lambda d: (d.entity_type, d.entity_index, d.code.value))
    # DEDUPED BEFORE THE STAMP, and after the sort so the survivor of a pair is chosen in the same
    # stable order the report is read in. A duplicate record of one entity makes `refs_for` believe
    # two entities share a box, and the `#n` suffix that follows is not stable across a repair -
    # see `_deduped`.
    found = _deduped(found)
    # STAMPED AFTER THE SORT, so the collision suffix a shared box gets is assigned in the same
    # stable order the report is read in - two scans of identical bytes must produce identical
    # names, or nothing can be compared across a repair.
    # NAMED ON THE RUN'S GRID WHEN THE CALLER HAS ONE, on this shape's own when it has not. Every
    # OTHER use of `diagonal` above - the sliver thresholds, the free-bound tolerance, the grown
    # scope box - is untouched by it: this parameter buys a stable NAME, not a stable threshold.
    stamped = _stamped(tuple(found), diagonal_mm=(
        diagonal if quantum_diagonal_mm is None else float(quantum_diagonal_mm)))
    return (_linked(stamped) if ran_face_scan else stamped), complete


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
