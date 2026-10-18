# Responsibility: Perform one bounded repair on NAMED entities and report what the kernel did to
#                 them, so a caller can ask whether THIS defect went away.
# Owns: the operation contract, the shared splice that lands a fixed sub-shape back in its parent,
#       and the operation catalogue.
# Boundaries: an operation mutates nothing it was given - it returns a NEW shape. It does not
#             decide whether its own result is acceptable, does not re-inspect, and never chooses
#             which operation runs next.
# Collaborates with: cad/repair/identity.py (the names and the mapping shape), cad/repair/caps.py
#                    (which judges the result, using the area and volume each operation declares),
#                    cad/repair/session.py (which applies one of these and keeps or rolls back).
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.identity import EntityMapping, EntityRef, fingerprint_of, quantum_for

logger = logging.getLogger(__name__)

# WHY AN OPERATION TAKES TARGETS.
#
# The whole-shape repair ran one fixed recipe over the entire part. Triage already produced the 20
# worst located defects, and they were LOGGED AND NEVER USED, because the executor had no
# parameter to accept them. So nothing - rules or model - could say "patch the hole bounded by
# edge 12" or "sew faces 3 and 7 at 0.02 mm"; the only available request was "fix everything".
#
# Every operation here takes entities. That is the whole capability this slice adds, and it is
# what a model later needs in order to be given a choice worth making.
#
# WHY EACH ONE REPORTS ITS OWN MAPPING.
#
# `BRepCheck` renumbers sub-shapes, so after a fix the question "is edge 12's defect gone?" cannot
# be answered from indices. Two of the kernel's APIs answer it directly, and this module reads
# whichever one each operation has:
#
#   ShapeFix_*        -> `.Context()` is a ShapeBuild_ReShape: `IsRecorded`, `Value`, `IsNewShape`
#   Sewing            -> `IsModified`/`Modified`, `IsModifiedSubShape`/`ModifiedSubShape`,
#                        `NbDeletedFaces`/`DeletedFace`
#
# Where an operation has neither, the mapping comes back `basis="geometric"` and the session falls
# back to comparing boxes. The distinction is reported rather than smoothed over: a training
# signal that cannot tell a kernel fact from a geometric inference overstates its confidence.

#: The declaration keys `caps.Accounting` reads out of an operation's evidence. An operation that
#: moves no surface ON PURPOSE - every ShapeFix pass, every sewing - omits them, and silence means
#: zero rather than unknown.
AREA_DELTA = "area_delta_mm2"
VOLUME_DELTA = "volume_delta_mm3"


@dataclass(frozen=True, slots=True)
class OpOutcome:
    """What one operation produced: a new shape, what became of the entities, and its own evidence."""

    #: A NEW shape. The input is never mutated, which is what makes rollback a handle swap.
    shape: object
    mapping: EntityMapping = field(default_factory=EntityMapping.empty)
    #: Fingerprints this operation CREATED - a patch face, an edge where two were merged. The
    #: session needs them to tell "a new defect appeared" from "a defect appeared on geometry this
    #: operation deliberately added".
    added: tuple[str, ...] = ()
    #: What it did, in numbers a reviewer can read, plus its AREA_DELTA/VOLUME_DELTA declarations.
    evidence: dict = field(default_factory=dict)


@runtime_checkable
class RepairOp(Protocol):
    """One bounded repair. Stateless: everything it needs arrives as an argument."""

    #: Stable key. It is the ineffective-pair ledger's key and the training log's, so renaming one
    #: invalidates both.
    name: str
    #: The defects this operation CLAIMS to address. The planner only proposes it for these.
    answers: frozenset[DefectCode]
    #: How much of the shape it touches - lower is narrower. The planner prefers the narrowest
    #: operation that claims a defect, because a narrow fix that fails costs one entity's worth of
    #: rollback rather than the whole part's.
    scope: int

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        """The entities this operation would aim at for one defect, or () if it cannot."""
        ...

    def apply(self, shape, targets: tuple[EntityRef, ...], *, tolerance_mm: float) -> OpOutcome:
        """Perform it. Raises on a kernel failure; the session turns that into a `failed` verdict."""
        ...


# THE SHARED KERNEL HELPERS


def box_of(sub) -> dict:
    """The bounding box of a sub-shape, in the payload shape `identity.fingerprint_of` reads.

    Deliberately the same function the localiser uses, so a fingerprint computed here and one
    computed from a defect's `location` are computed from identical numbers. Two implementations
    of "where is this entity" that differ in the last decimal would silently rename entities.
    """
    from meshpipeline.cad.repair.localize import _centroid_and_box

    return _centroid_and_box(sub)


def sub_shape(shape, entity_type: str, entity_index: int):
    """The sub-shape an `EntityRef`'s index names, or None when the index does not resolve.

    NONE IS A REAL ANSWER. A ref carries an index into the shape it was BUILT from; handed a shape
    that an operation has already rewritten, the index may name nothing or something else. The
    caller rebuilds refs after every kept operation for exactly this reason, and a None here is
    the signal that it did not.
    """
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_VERTEX, TopAbs_WIRE

    from meshpipeline.cad.repair.localize import _indexed

    kinds = {"face": TopAbs_FACE, "edge": TopAbs_EDGE, "shell": TopAbs_SHELL,
             "wire": TopAbs_WIRE, "vertex": TopAbs_VERTEX}
    kind = kinds.get(entity_type)
    if kind is None or entity_index <= 0:
        return None
    indexed = _indexed(shape, kind)
    if entity_index > indexed.Extent():
        return None
    return indexed.FindKey(entity_index)


def splice(shape, replacements: list[tuple[object, object]]):
    """Land fixed sub-shapes back in their parent, and hand back the history that did it.

    Returns `(new_shape, context)`.

    THIS IS WHAT MAKES A TARGETED FIX POSSIBLE AT ALL. Without it, fixing one face means either
    rebuilding the whole shape around it or running the fixer over everything - which is the
    whole-shape pass this slice exists to replace. `ShapeBuild_ReShape` is the class for it:
    `Replace(old, new)` records the substitution and `Apply(shape)` rebuilds every ancestor that
    contained the old sub-shape, leaving the rest of the part untouched by identity.

    The same context is then the mapping's source, so the substitution and the record of it cannot
    disagree.
    """
    from OCP.ShapeBuild import ShapeBuild_ReShape

    context = ShapeBuild_ReShape()
    for old, new in replacements:
        context.Replace(old, new)
    return context.Apply(shape), context


def _names_its_own_geometry(ref: EntityRef, sub, *, quantum: float) -> bool:
    """Is this ref's name reproducible from the sub-shape its index resolves to?

    WHY A KERNEL RECORD IS NOT ENOUGH ON ITS OWN. A mapping's successor is computed as
    `fingerprint_of(ref.entity_type, box_of(<what the sub-shape became>))`, so it is only a name
    the NEXT SCAN will recognise if the ref's own name was taken from the same geometry. For a
    free-boundary target it was not: `localize._open_boundary_defects` reports the opening against
    `loop[0]` - one EDGE - while fingerprinting the whole WIRE's bounding box. Those boxes differ
    by construction, so trusting the kernel's record of that edge produced a successor denoting
    nothing, and the verdict then reported `basis: "kernel"` on the strength of it: every
    successful fill claimed the kernel had said the rim moved.

    A mismatch therefore means NO KERNEL EVIDENCE FOR THIS TARGET, not a successor to invent. The
    caller skips the ref entirely - including the `consulted` flag - so a mapping built only from
    such refs stays `geometric`, which is the honest answer: the conclusion came from diffing the
    two scans.

    The `#n` suffix `identity.refs_for` adds for a genuinely shared box names a slot in that table
    rather than a place in space, so the comparison is against the base name.
    """
    expected = ref.fingerprint.split("#", 1)[0]
    try:
        return bool(expected) and fingerprint_of(
            ref.entity_type, box_of(sub), quantum=quantum) == expected
    except Exception as exc:  # noqa: BLE001 - an unmeasurable sub-shape is simply no evidence
        logger.debug("operations: could not re-measure a %s (%s)", ref.entity_type, exc)
        return False


def mapping_from_context(context, targets: tuple[EntityRef, ...], *,
                         shape_before, diagonal_mm: float) -> EntityMapping:
    """Read a ShapeBuild_ReShape's history into a fingerprint mapping.

    `context` may be a ShapeFix operation's own `.Context()` or the one `splice` built; both are
    the same class and both answer the same three questions about a sub-shape: was it recorded,
    what did it become, and is the replacement null (which is the kernel saying it is gone).
    """
    # See `_names_its_own_geometry` for why each ref is checked against the sub-shape first.
    moved: dict[str, str] = {}
    gone: set[str] = set()
    consulted = False
    quantum = quantum_for(diagonal_mm)
    for ref in targets:
        if not ref.targetable:
            continue
        sub = sub_shape(shape_before, ref.entity_type, ref.entity_index)
        if sub is None:
            continue
        if not _names_its_own_geometry(ref, sub, quantum=quantum):
            continue
        try:
            recorded = context.IsRecorded(sub)
            consulted = True                # the kernel ANSWERED, even if the answer is "untouched"
            if not recorded:
                continue                    # untouched; `fate_of` will call it unknown
            value = context.Value(sub)
        except Exception as exc:  # noqa: BLE001 - a history API that declines is not a failure
            logger.debug("operations: no history for a %s (%s)", ref.entity_type, exc)
            continue
        if value is None or (hasattr(value, "IsNull") and value.IsNull()):
            gone.add(ref.fingerprint)
            continue
        new = fingerprint_of(ref.entity_type, box_of(value), quantum=quantum)
        # `refs_for` suffixes a ref whose box another entity shares ("face:abc#0") so the table
        # stays addressable. The suffix names a slot in THAT table, not a place in space, so a box
        # recomputed from geometry can never carry it: compare against the base name, or every
        # recorded-but-unmoved duplicate would be reported as moved to a key that matches nothing.
        # The move is still recorded under the ref's FULL key, which is what the session looks up.
        base = ref.fingerprint.split("#", 1)[0]
        if new and new != base:
            moved[ref.fingerprint] = new
        elif not new:
            # Recorded, replaced by something with no measurable extent. Treat as gone: there is
            # nothing left to find it by, which is the same thing from a defect's point of view.
            gone.add(ref.fingerprint)
    if not consulted:
        # NOTHING WAS READ FROM THE HISTORY, so nothing is a kernel fact. Claiming `kernel` here
        # would tell the session the kernel confirmed a fate nobody asked it about.
        return EntityMapping.empty()
    return EntityMapping(moved=moved, gone=frozenset(gone), basis="kernel")


# THE OPERATIONS


class FixSmallEdges:
    """Merge away the sliver edges a writer left behind, running the fixer on the WHOLE SHAPE.

    `ShapeFix_Wireframe` is the kernel's answer for both small edges and the gaps between edges of
    one wire.

    SCOPING IS NOT AVAILABLE FOR THIS OPERATION, unlike `fix_face_boundary`. The fixer needs the
    surrounding topology to constrain its edits. Handed a compound of just the faces that own the
    sliver, it has nothing to hold the neighbours in place, and closing the gap left by a removed
    edge lets it extrapolate: measured on the sliver fixture, a two-face compound went in at
    diagonal 141.77 mm / max tolerance 1e-7 and came out at diagonal 270.00 mm / max tolerance
    80 mm (405.10 mm with `FixWireGaps` omitted), and neither `LimitTolerance(0.02)` nor
    `SetMaxTolerance(0.02)` bounded it. On the whole shape the same calls leave every fixture
    valid with diagonal and tolerance unchanged - and remove nothing, because the solid's own
    topology forbids it and the conservative profile may not rebuild surfaces.

    So the targets state what we AIMED at, the session's progress rule judges whether we ACHIEVED
    it, and the caps bound the damage. An honest outcome on a solid is often "no progress".

    `ModeDropSmallEdges = False` DELIBERATELY: dropping an edge rather than merging it removes
    geometry, and removing geometry is an operator's decision.

    It also calls `FixWireGaps`, closing gaps between edges of the same wire; it can move
    vertices, so any area it shifts is unaccounted for here and is caught by the cumulative cap.
    """

    name = "fix_small_edges"
    answers = frozenset({DefectCode.small_edge, DefectCode.degenerate_edge})
    scope = 1

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        if getattr(defect, "code", None) not in self.answers:
            return ()
        if defect.entity_type != "edge" or not getattr(defect, "fingerprint", ""):
            return ()
        return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                          entity_index=int(defect.entity_index),
                          ambiguous=bool(getattr(defect, "ambiguous", False))),)

    def apply(self, shape, targets: tuple[EntityRef, ...], *,
              tolerance_mm: float) -> OpOutcome:
        from OCP.ShapeFix import ShapeFix_Wireframe

        from meshpipeline.cad.repair.localize import _diagonal

        if not any(sub_shape(shape, "edge", ref.entity_index) is not None for ref in targets):
            # NOTHING RESOLVED is not a failure. A stale index lands here, and the session rolls
            # this back for making no progress rather than for raising.
            return OpOutcome(shape=shape, evidence={"edges_targeted": len(targets)})

        # THE WHOLE SHAPE, never a compound of the owning faces - see the class docstring for the
        # measured damage that does (diagonal 141.77 -> 270.00 mm, tolerance 1e-7 -> 80 mm).
        fixer = ShapeFix_Wireframe(shape)
        fixer.SetPrecision(tolerance_mm)
        fixer.SetMaxTolerance(tolerance_mm)
        # MERGE, DO NOT DROP. Dropping removes geometry; the conservative profile may not.
        fixer.ModeDropSmallEdges = False
        fixer.FixSmallEdges()
        fixer.FixWireGaps()

        context = fixer.Context()
        result = context.Apply(shape)
        mapping = mapping_from_context(context, targets, shape_before=shape,
                                       diagonal_mm=_diagonal(shape))
        return OpOutcome(shape=result, mapping=mapping,
                         evidence={"edges_targeted": len(targets),
                                   "precision_mm": float(tolerance_mm)})


class FixFaceBoundary:
    """Repair one face's own wires: ordering, pcurves, same-parameter, missing seams.

    `ShapeFix_Face` is a per-face fixer, so this is the narrowest real operation in the catalogue
    after the edge one - and it is the one that answers `curve_inconsistency`, which is the
    commonest attributed status on a translated part.

    `FixSmallAreaWireMode` and `RemoveSmallAreaFaceMode` are LEFT OFF: both remove geometry.
    """

    name = "fix_face_boundary"
    answers = frozenset({DefectCode.curve_inconsistency, DefectCode.wire_gap,
                         DefectCode.duplicate_surface_data})
    scope = 2

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        if getattr(defect, "code", None) not in self.answers:
            return ()
        if defect.entity_type not in ("face", "wire") or not getattr(defect, "fingerprint", ""):
            return ()
        if defect.entity_type == "wire":
            # A wire defect is repaired through its face, and the localiser does not record which
            # face that is. Rather than guess, decline: the planner falls through to
            # `fix_shape_global`, which claims `wire_gap` too and names the same entity, so the
            # defect still gets an attempt from the honest wider tool. (That fall-through was
            # promised here while `FixShapeGlobal.targets_for` returned `()` unconditionally, which
            # meant no attempt at all - see that class's docstring.)
            return ()
        return (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                          entity_index=int(defect.entity_index),
                          ambiguous=bool(getattr(defect, "ambiguous", False))),)

    def apply(self, shape, targets: tuple[EntityRef, ...], *,
              tolerance_mm: float) -> OpOutcome:
        from OCP.ShapeFix import ShapeFix_Face
        from OCP.TopoDS import TopoDS

        from meshpipeline.cad.repair.localize import _diagonal

        replacements: list[tuple[object, object]] = []
        for ref in targets:
            sub = sub_shape(shape, "face", ref.entity_index)
            if sub is None:
                continue
            # Two targets naming one face must repair it once.
            if any(sub.IsSame(done) for done, _ in replacements):
                continue
            fixer = ShapeFix_Face(TopoDS.Face_s(sub))
            fixer.SetPrecision(tolerance_mm)
            fixer.SetMaxTolerance(tolerance_mm)
            # REMOVING A SLIVER WIRE OR FACE IS DEFEATURING. Off, like everywhere else here.
            fixer.FixSmallAreaWireMode = 0
            fixer.RemoveSmallAreaFaceMode = 0
            fixer.Perform()
            replacements.append((sub, fixer.Face()))

        if not replacements:
            return OpOutcome(shape=shape, evidence={"faces_targeted": len(targets),
                                                    "faces_fixed": 0})

        spliced, context = splice(shape, replacements)
        mapping = mapping_from_context(context, targets, shape_before=shape,
                                       diagonal_mm=_diagonal(shape))
        return OpOutcome(shape=spliced, mapping=mapping,
                         evidence={"faces_targeted": len(targets),
                                   "faces_fixed": len(replacements),
                                   "precision_mm": float(tolerance_mm)})


def mapping_from_sewing(sewing, targets: tuple[EntityRef, ...], *,
                        shape_before, diagonal_mm: float) -> EntityMapping:
    """Read `BRepBuilderAPI_Sewing`'s own history into a fingerprint mapping.

    Sewing has no ShapeBuild_ReShape, but it answers the same questions through a different set of
    calls: `IsModified`/`Modified` for a sub-shape that was replaced, `IsModifiedSubShape` for one
    that survived inside a modified parent, and the deleted-face list for faces it dropped.
    """
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    moved: dict[str, str] = {}
    gone: set[str] = set()
    quantum = quantum_for(diagonal_mm)

    deleted_boxes: list[dict] = []
    try:
        explorer = TopExp_Explorer(sewing.DeletedFace(), TopAbs_FACE)
        while explorer.More():
            deleted_boxes.append(box_of(explorer.Current()))
            explorer.Next()
    except Exception as exc:  # noqa: BLE001 - no deleted faces is the common case
        logger.debug("operations: sewing reported no deleted faces (%s)", exc)

    deleted = {fingerprint_of("face", box, quantum=quantum) for box in deleted_boxes}

    for ref in targets:
        if not ref.targetable:
            continue
        if ref.fingerprint in deleted:
            gone.add(ref.fingerprint)
            continue
        sub = sub_shape(shape_before, ref.entity_type, ref.entity_index)
        if sub is None:
            continue
        if not _names_its_own_geometry(ref, sub, quantum=quantum):
            # Same reason as in `mapping_from_context`: a ref whose name came from other geometry
            # than the sub-shape being measured cannot be given a successor the next scan knows.
            continue
        try:
            if sewing.IsModified(sub):
                replacement = sewing.Modified(sub)
            elif sewing.IsModifiedSubShape(sub):
                replacement = sewing.ModifiedSubShape(sub)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("operations: sewing has no record for a %s (%s)", ref.entity_type, exc)
            continue
        if replacement is None or (hasattr(replacement, "IsNull") and replacement.IsNull()):
            gone.add(ref.fingerprint)
            continue
        new = fingerprint_of(ref.entity_type, box_of(replacement), quantum=quantum)
        if new and new != ref.fingerprint:
            moved[ref.fingerprint] = new
    return EntityMapping(moved=moved, gone=frozenset(gone), basis="kernel")


def free_wire_containing(shape, edge_index: int):
    """The free-boundary wire that one edge belongs to, or None.

    PER WIRE, NOT PER COMPOUND, for the same reason `localize._open_boundary_defects` is: free-
    bound analysis hands back ONE compound holding every boundary it found, so walking its edges
    directly merges unrelated openings - which is how the two rims of one bore first came back as
    a single hole.
    """
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    target = sub_shape(shape, "edge", edge_index)
    if target is None:
        return None
    try:
        wires = ShapeAnalysis_FreeBounds(shape).GetClosedWires()
        if wires is None:
            return None
        explorer = TopExp_Explorer(wires, TopAbs_WIRE)
        while explorer.More():
            wire = TopoDS.Wire_s(explorer.Current())
            edges = TopExp_Explorer(wire, TopAbs_EDGE)
            while edges.More():
                if edges.Current().IsSame(target):
                    return wire
                edges.Next()
            explorer.Next()
    except Exception as exc:  # noqa: BLE001
        logger.debug("operations: free-bound analysis unavailable (%s)", exc)
    return None


class SewFaces:
    """Join coincident face boundaries into a shell, for a body with NO SOLID TO LOSE.

    `BRepBuilderAPI_Sewing` RETURNS A SHELL. Handed a shape that already contains a solid it hands
    back the sewn shell and the solid is gone - a real-kernel test caught exactly that, a sound box
    in with one solid and out with none. So this operation declines such a body itself rather than
    letting the caps refuse the result: the refusal is the same, and declining early costs no
    re-inspection.

    The judgement is PER BODY, as `conservative._sew_loose_bodies` established: a STEP assembly is
    a compound of independent bodies, and a loose body beside a solid must be repairable without
    costing the solid.
    """

    name = "sew_faces"
    answers = frozenset({DefectCode.open_shell})
    scope = 4

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        if getattr(defect, "code", None) not in self.answers:
            return ()
        if not getattr(defect, "fingerprint", ""):
            return ()
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=int(defect.entity_index),
                          ambiguous=bool(getattr(defect, "ambiguous", False))),)

    def apply(self, shape, targets: tuple[EntityRef, ...], *,
              tolerance_mm: float) -> OpOutcome:
        from OCP.BRep import BRep_Builder
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
        from OCP.TopAbs import TopAbs_COMPOUND
        from OCP.TopoDS import TopoDS_Compound, TopoDS_Iterator

        from meshpipeline.cad.repair.caps import measure
        from meshpipeline.cad.repair.conservative import _as_solid_if_closed
        from meshpipeline.cad.repair.localize import _diagonal

        diagonal = _diagonal(shape)

        def sew_one(body):
            sewing = BRepBuilderAPI_Sewing(tolerance_mm)
            # NON-MANIFOLD MODE OFF: joining three faces along one edge produces a shape no mesher
            # will accept, and calling that a repair moves the failure downstream.
            sewing.SetNonManifoldMode(False)
            sewing.Add(body)
            sewing.Perform()
            sewn = sewing.SewedShape()
            if sewn is None or (hasattr(sewn, "IsNull") and sewn.IsNull()):
                return None, None
            return _as_solid_if_closed(sewn), sewing

        if measure(shape).solids == 0:
            sewn, sewing = sew_one(shape)
            if sewn is None:
                return OpOutcome(shape=shape, evidence={"declined": "sewing produced nothing"})
            mapping = mapping_from_sewing(sewing, targets, shape_before=shape,
                                          diagonal_mm=diagonal)
            return OpOutcome(shape=sewn, mapping=mapping,
                             evidence={"bodies_sewn": 1, "tolerance_mm": float(tolerance_mm)})

        if shape.ShapeType() != TopAbs_COMPOUND:
            return OpOutcome(shape=shape,
                             evidence={"declined": "the targeted body already has a solid to lose"})

        children = []
        iterator = TopoDS_Iterator(shape)
        while iterator.More():
            children.append(iterator.Value())
            iterator.Next()
        if not children:
            return OpOutcome(shape=shape, evidence={"declined": "the compound is empty"})

        builder = BRep_Builder()
        rebuilt = TopoDS_Compound()
        builder.MakeCompound(rebuilt)
        sewn_count = 0
        last_sewing = None
        for child in children:
            if measure(child).solids > 0:
                builder.Add(rebuilt, child)
                continue
            joined, sewing = sew_one(child)
            if joined is None:
                builder.Add(rebuilt, child)
                continue
            builder.Add(rebuilt, joined)
            sewn_count += 1
            last_sewing = sewing

        if not sewn_count:
            # UNCHANGED MEANS UNCHANGED. Returning the original rather than a re-wrapped copy, so
            # an operation that did nothing cannot look like one that rebuilt the assembly.
            return OpOutcome(shape=shape,
                             evidence={"declined": "the targeted body already has a solid to lose"})

        mapping = (mapping_from_sewing(last_sewing, targets, shape_before=shape,
                                       diagonal_mm=diagonal)
                   if last_sewing is not None else EntityMapping.empty())
        return OpOutcome(shape=rebuilt, mapping=mapping,
                         evidence={"bodies_sewn": sewn_count,
                                   "tolerance_mm": float(tolerance_mm)})


class FillPlanarHole:
    """Patch ONE flat closed free boundary, and say exactly how much surface that added.

    THE ONLY OPERATION HERE THAT ADDS SURFACE THE CUSTOMER NEVER DREW, which is why it is the
    narrowest-targeted of all of them - one loop, named by the first edge of its boundary - and
    why it declares its own area so the cumulative cap can account for it rather than allow it.

    PLANAR ONLY: across a flat loop exactly one surface can go there, so the patch is determined
    rather than invented. `BRepBuilderAPI_MakeFace(wire, OnlyPlane=True)` declines a non-planar
    loop, which is the behaviour wanted - left alone, reported, routed to a person.

    THE PAIRED-RIM CASE IS NOT CHECKED HERE. Two rims of a missing tube wall are each a perfectly
    fillable flat loop, and patching both seals a drilled through-hole. That is a decision about
    which loop to aim at, so it belongs to the planner (`plan.py`), which refuses to PROPOSE the
    pair and records the refusal. It used to be a silent skip inside this function, where nothing
    downstream could see it.

    THE PAIRED-RIM GUARD IS THE ONLY DEFENCE, and the deploy's `cad_repair` tick turns this
    operation on. No cap, sanity check or progress rule can see a sealed through-hole:
    `sanity_refusal` passes (faces rise, solids do not fall), the volume comparison is skipped
    because a shape with a free boundary has `solids == 0`, the area is declared by this operation
    so `_accounted` passes, and the progress rule correctly sees the rim's `open_shell` gone - the
    part is sealed and every check agrees it was repaired. So every path that proposes a fill
    consults `plan.paired_rim_fingerprints`: RulesPlanner refuses filling for the run once a pair
    is seen, and DirectedPlanner (where a model's directives play) applies the same guard to every
    fill except one engineer's single-rim approval. The guard fails closed when it cannot run or
    cannot resolve a candidate rim. A new planner that proposes fills without it would bypass the
    only defence.
    """

    name = "fill_planar_hole"
    answers = frozenset({DefectCode.open_shell})
    scope = 5

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        if getattr(defect, "code", None) not in self.answers:
            return ()
        if defect.entity_type != "edge" or not getattr(defect, "fingerprint", ""):
            return ()
        # ONLY A LOCATED LOOP. The free-bound finding carries `boundary_edges`; an `open_shell`
        # the status walk attributed to a face or shell carries no wire to make a face from.
        if not (defect.measurements or {}).get("boundary_edges"):
            return ()
        return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                          entity_index=int(defect.entity_index),
                          ambiguous=bool(getattr(defect, "ambiguous", False))),)

    def apply(self, shape, targets: tuple[EntityRef, ...], *,
              tolerance_mm: float) -> OpOutcome:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Sewing

        import meshpipeline.settings.cad_repair as rcfg
        from meshpipeline.cad.repair.conservative import _as_solid_if_closed, _span
        from meshpipeline.cad.repair.localize import _diagonal, _face_area

        if not rcfg.CAD_REPAIR_FILL_PLANAR_HOLES:
            return OpOutcome(shape=shape,
                             evidence={"declined": "filling_disabled (CAD_REPAIR_FILL_PLANAR_HOLES)"})
        if not targets:
            return OpOutcome(shape=shape, evidence={"declined": "no loop was named"})

        wire = free_wire_containing(shape, targets[0].entity_index)
        if wire is None:
            return OpOutcome(shape=shape,
                             evidence={"declined": "the named edge bounds no closed free loop"})

        diagonal = _diagonal(shape)
        span = _span(wire)
        ratio = (span / diagonal) if diagonal > 0 else 0.0
        if diagonal > 0 and ratio > rcfg.CAD_REPAIR_MAX_HOLE_SPAN_RATIO:
            # NOT A HOLE, A MISSING WALL. A flat lid over one is a different part.
            return OpOutcome(shape=shape,
                             evidence={"declined": f"too_large (span ratio {ratio:.4f})",
                                       "span_mm": round(span, 6)})

        maker = BRepBuilderAPI_MakeFace(wire, True)
        if not maker.IsDone():
            return OpOutcome(shape=shape,
                             evidence={"declined": "not_planar", "span_mm": round(span, 6)})
        patch = maker.Face()
        patch_area = _face_area(patch)

        sewing = BRepBuilderAPI_Sewing(tolerance_mm)
        sewing.SetNonManifoldMode(False)
        sewing.Add(shape)
        sewing.Add(patch)
        sewing.Perform()
        sewn = sewing.SewedShape()
        if sewn is None or (hasattr(sewn, "IsNull") and sewn.IsNull()):
            return OpOutcome(shape=shape, evidence={"declined": "sewing_failed"})

        promoted = _as_solid_if_closed(sewn)
        quantum = quantum_for(diagonal)
        patch_fingerprint = fingerprint_of("face", box_of(patch), quantum=quantum)
        mapping = mapping_from_sewing(sewing, targets, shape_before=shape,
                                      diagonal_mm=diagonal)
        return OpOutcome(
            shape=promoted, mapping=mapping,
            added=(patch_fingerprint,) if patch_fingerprint else (),
            # DECLARED, so the cumulative cap compares against `original + this` instead of
            # allowing 75% of unexplained area. Volume is NOT declared: whether the shell closed
            # into a solid is the kernel's answer, and `caps.cumulative_refusal` records a volume
            # that appeared from nothing rather than comparing it.
            evidence={AREA_DELTA: round(float(patch_area), 6),
                      "patch_is_planar": True,
                      "span_mm": round(span, 6), "span_ratio": round(ratio, 6),
                      "tolerance_mm": float(tolerance_mm)})


class FixShapeGlobal:
    """Today's whole-shape `ShapeFix_Shape`, kept as the LAST RESORT.

    WHY IT STAYS. Every other operation in this catalogue is narrower and better, but the
    whole-shape pass is what the branch ships today, and a loop that could not reach it might
    repair less than its predecessor on some real file nobody has run yet. It is proposed only
    when no scoped operation claims anything left, which is what `scope = 99` buys.

    It offers a REAL mapping even though it runs whole-shape: `ShapeFix_Shape.Context()` is a
    ShapeBuild_ReShape, so the session can still ask what became of the entities a defect named.

    IT STILL NAMES WHAT IT AIMED AT, for the same reason `fix_small_edges` does. Returning no
    targets made this operation UNREACHABLE: the planner skips a candidate with an empty target
    tuple, and the session's `_carries` check over an empty tuple is False, so it was blocked
    twice over - and every defect only this operation claims (`invalid_brep`, and any
    `curve_inconsistency` / `duplicate_surface_data` / `wire_gap` / `degenerate_edge` the status
    walk attributed to an edge, wire or shell) got zero attempts. Those are the files the
    whole-shape pass used to repair, which is exactly what this operation exists to preserve.
    """

    name = "fix_shape_global"
    answers = frozenset({
        DefectCode.invalid_brep, DefectCode.curve_inconsistency,
        DefectCode.degenerate_edge, DefectCode.small_edge, DefectCode.wire_gap,
        DefectCode.duplicate_surface_data,
    })
    scope = 99

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        # ANY ENTITY TYPE, because the fixer runs over the whole shape regardless; the ref records
        # what was aimed at so the progress rule has a name to judge against.
        if getattr(defect, "code", None) not in self.answers:
            return ()
        if not getattr(defect, "fingerprint", ""):
            return ()
        return (EntityRef(entity_type=str(defect.entity_type), fingerprint=defect.fingerprint,
                          entity_index=int(defect.entity_index),
                          ambiguous=bool(getattr(defect, "ambiguous", False))),)

    def apply(self, shape, targets: tuple[EntityRef, ...], *,
              tolerance_mm: float) -> OpOutcome:
        from OCP.ShapeFix import ShapeFix_Shape
        from OCP.Standard import Standard_Failure

        from meshpipeline.cad.repair.localize import _diagonal

        fixer = ShapeFix_Shape(shape)
        fixer.SetPrecision(tolerance_mm)
        fixer.SetMaxTolerance(tolerance_mm)
        # TOPOLOGY MAY BE REPAIRED, GEOMETRY MAY NOT BE REWRITTEN. OpenCASCADE can rebuild
        # surfaces and curves to force a fit; that changes what the customer drew rather than how
        # it was written down, so the conservative profile leaves it off.
        fixer.FixSolidMode = 1
        fixer.FixFreeShellMode = 1
        fixer.FixFreeFaceMode = 1
        fixer.FixSameParameterMode = 1
        fixer.FixVertexPositionMode = 0
        fixer.Perform()
        fixed = fixer.Shape()

        mapping = EntityMapping.empty()
        try:
            mapping = mapping_from_context(fixer.Context(), targets, shape_before=shape,
                                           diagonal_mm=_diagonal(shape))
        except (RuntimeError, Standard_Failure) as exc:   # the kernel declining; our own TypeError/AttributeError surface
            logger.warning("fix_shape_global: no usable context (%s: %s)", type(exc).__name__, exc)
        # DECLARES NOTHING. A ShapeFix pass repairs how the geometry was written down, not what it
        # is, so any area or volume it moves is unaccounted for by design - and the cumulative cap
        # is then the thing that catches it.
        return OpOutcome(shape=fixed, mapping=mapping,
                         evidence={"precision_mm": float(tolerance_mm),
                                   "modes": "solid,free_shell,free_face,same_parameter"})


#: Every operation the loop may run, narrowest first. The ORDER here is documentation - the
#: planner sorts on `scope`, so adding one in the wrong place changes nothing but a reader's
#: expectations.
OPERATIONS: tuple[RepairOp, ...] = (
    FixSmallEdges(),
    FixFaceBoundary(),
    SewFaces(),
    FillPlanarHole(),
    FixShapeGlobal(),
)
