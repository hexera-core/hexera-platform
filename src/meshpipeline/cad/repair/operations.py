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


def mapping_from_context(context, targets: tuple[EntityRef, ...], *,
                         shape_before, diagonal_mm: float) -> EntityMapping:
    """Read a ShapeBuild_ReShape's history into a fingerprint mapping.

    `context` may be a ShapeFix operation's own `.Context()` or the one `splice` built; both are
    the same class and both answer the same three questions about a sub-shape: was it recorded,
    what did it become, and is the replacement null (which is the kernel saying it is gone).
    """
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


class FixShapeGlobal:
    """Today's whole-shape `ShapeFix_Shape`, kept as the LAST RESORT.

    WHY IT STAYS. Every other operation in this catalogue is narrower and better, but the
    whole-shape pass is what the branch ships today, and a loop that could not reach it might
    repair less than its predecessor on some real file nobody has run yet. It is proposed only
    when no scoped operation claims anything left, which is what `scope = 99` buys.

    It offers a REAL mapping even though it takes no targets: `ShapeFix_Shape.Context()` is a
    ShapeBuild_ReShape, so the session can still ask what became of the entities a defect named.
    """

    name = "fix_shape_global"
    answers = frozenset({
        DefectCode.invalid_brep, DefectCode.curve_inconsistency,
        DefectCode.degenerate_edge, DefectCode.small_edge, DefectCode.wire_gap,
        DefectCode.duplicate_surface_data,
    })
    scope = 99

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        return ()

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


#: Every operation the loop may run, narrowest first. Task 6 adds the five scoped operations ahead
#: of the fallback; the ORDER here is documentation, not policy - the planner sorts on `scope`.
OPERATIONS: tuple[RepairOp, ...] = (
    FixShapeGlobal(),
)
