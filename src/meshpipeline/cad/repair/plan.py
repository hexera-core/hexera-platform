# Responsibility: Choose the next repair to try, and say why a defect was not aimed at.
# Owns: the ordering rules, the ineffective-pair ledger, and the refusal to patch either rim of a
#       missing bore wall.
# Boundaries: it proposes. It never applies anything, never measures a result, and never decides
#             when the run is over.
# Collaborates with: cad/repair/operations.py (the catalogue it chooses from), cad/repair/loop.py
#                    (which asks it), cad/repair/session.py (which carries out its choice).
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.operations import OPERATIONS, FillPlanarHole, RepairOp

logger = logging.getLogger(__name__)

# WHY THIS IS A SEPARATE MODULE WITH A PROTOCOL.
#
# This is the seat a model takes in slice 8. Nearly everything about safety - the caps, the
# progress rule, the rollback - lives in the session, so a planner choosing badly wastes a budget
# rather than damaging a part. What a planner chooses is which of FIVE bounded operations
# (`operations.OPERATIONS`) to point at which located defect, and nothing more. Keeping that behind
# one small interface is what makes "a model picks, the rules pick, compare them on the same
# corpus" an experiment rather than a rewrite.
#
# THE ONE EXCEPTION, AND IT IS NOT A SMALL ONE. For a SEALED THROUGH-HOLE - both rims of a missing
# bore wall patched flat - the refusal below is the ONLY defence in the system. Nothing downstream
# can see that case: `sanity_refusal` passes (faces rise, no solid is lost), the volume comparison
# is skipped because a shape with a free boundary has `solids == 0`, the patch area is declared by
# the fill so `_accounted` passes, and the progress rule correctly sees the rim's `open_shell`
# gone. Every check agrees the part was repaired, and the customer has a plate with no bolt hole.
# That ships only because `CAD_REPAIR_FILL_PLANAR_HOLES` DEFAULTS OFF, which makes the ban
# unreachable in a default deployment. Before filling is enabled by default, or before a model
# takes this seat, the ban has to move BEHIND the planner interface - a planner-independent check
# the session or the fill enforces - because a planner that can be replaced is not a defence.
# Do not read the paragraph above as "the session makes a planner harmless"; for this one
# behaviour it does not.
#
# The rules below are deliberately plain. They are the BASELINE a model has to beat.

#: Worse first. The loop exists to clear errors; warnings are worth attending to only once the
#: errors are gone, because a sliver edge beside an open shell is not the part's problem.
_SEVERITY_RANK = {DefectSeverity.fatal: 0, DefectSeverity.error: 1,
                  DefectSeverity.warning: 2, DefectSeverity.info: 3}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One proposal: this operation, aimed at these entities, for this defect."""

    op: RepairOp
    targets: tuple[EntityRef, ...]
    defect_code: DefectCode
    defect_fingerprint: str
    #: Set only by DirectedPlanner: the directive this proposal plays. An autonomous proposal has none.
    item_id: str | None = None
    source: str = ""
    #: The LLM planner's stamp (`llm:<n>`) and its confidence; empty for every other proposal.
    directive_id: str | None = None
    planner_confidence: float | None = None

    def to_dict(self) -> dict:
        return {"op": self.op.name, "defect": self.defect_fingerprint,
                "defect_code": self.defect_code.value,
                "targets": [t.fingerprint for t in self.targets]}


@dataclass
class RunHistory:
    """What this run has already learned. The planner's only memory.

    `ineffective` is THE PER-OPERATION LEARNING SIGNAL: a `(defect, operation)` pair the session
    rolled back. Recorded per pair rather than per operation, because an operation that could not
    fix one edge may be exactly right for another, and banning it outright would waste the rest of
    the catalogue. In slice 8 this is precisely what the model is shown on its next turn.
    """

    ineffective: set[tuple[str, str]] = field(default_factory=set)
    refusals: list[dict] = field(default_factory=list)
    #: Run-level policy, NOT a trial outcome: why planar filling is off for the rest of this run
    #: (empty when it is not). Kept apart from `ineffective` because that set means "tried and
    #: rolled back", and a never-attempted policy ban read as a failure would mislead a model.
    fill_refused_because: str = ""

    def refuse_filling(self, reason: str) -> None:
        """Turn planar filling off for the rest of the run. Set once; the first reason stands."""
        if not self.fill_refused_because:
            self.fill_refused_because = reason

    def mark_ineffective(self, defect_fingerprint: str, op_name: str) -> None:
        self.ineffective.add((defect_fingerprint, op_name))

    def was_ineffective(self, defect_fingerprint: str, op_name: str) -> bool:
        return (defect_fingerprint, op_name) in self.ineffective

    def refuse(self, defect_fingerprint: str, reason: str, detail: str = "") -> None:
        """Record that a defect was not aimed at, ONCE. The loop asks repeatedly, and a list that
        grew on every tick would bury the report it exists to produce."""
        key = (defect_fingerprint, reason)
        if any((r["defect"], r["reason"]) == key for r in self.refusals):
            return
        record = {"defect": defect_fingerprint, "reason": reason}
        if detail:
            record["detail"] = detail
        self.refusals.append(record)


class Planner(Protocol):
    """Chooses the next repair to try. A model implements this in slice 8."""

    def propose(self, defects, history: RunHistory, *, shape) -> Candidate | None:
        ...


class RimsUnknown(frozenset):
    """The pairing question COULD NOT BE ANSWERED: every candidate rim, with `unknown` set.

    FAIL CLOSED. An empty answer would read as "no bore here" and leave the fill free to seal a
    through-hole the check never looked at. Holding every candidate rim instead means every caller
    that already refuses a paired rim (the rules planner, a directive's fill, the review minting)
    refuses these too, with no special case of its own; `unknown` lets the planner say why.
    """

    unknown = True


def paired_rim_fingerprints(shape, defects) -> frozenset[str]:
    """Fingerprints of the open-boundary defects that are THE TWO ENDS OF A MISSING TUBE.

    MOVED HERE OUT OF THE FILL EXECUTOR, and the move is the point. A through-hole whose wall is
    missing leaves two rims: planar, parallel, offset along their own normal. Each looks exactly
    like a small fillable hole, and patching both SEALS the hole - handing back a plate with no
    bolt hole, which is a ruined part rather than a repaired one. Demonstrated on a 100x100x10
    plate with an 8 mm bore and with an 8 mm hex: two loops, both far inside the size cap.

    As a silent skip inside the fill, nothing downstream could report it. As a PLANNING decision,
    refusing to propose it is recorded and reaches the report an operator reads.

    An empty set is the common and correct answer: an ordinary single opening has nothing to pair
    with, and the guard must not cost the case filling exists for. It never raises. When the
    question cannot be answered - the kernel or the discriminator fails - it FAILS CLOSED: a
    `RimsUnknown` holding every candidate rim, so no fill is proposed on a guess.

    THIS IS LOAD-BEARING. The targeted fill operation has no rim check of its own, so the planner's
    refusal is the only thing between the loop and a sealed through-hole. That is why the planner
    makes the refusal durable (see RulesPlanner.propose) rather than trusting this to be recomputed.
    """
    candidates = [d.fingerprint for d in defects
                  if d.code is DefectCode.open_shell and d.fingerprint]
    try:
        from meshpipeline.cad.repair.conservative import _paired_rims
        from meshpipeline.cad.repair.operations import free_wire_containing

        loops: list = []
        owners: list[str] = []
        for defect in defects:
            if defect.code is not DefectCode.open_shell or not defect.fingerprint:
                continue
            if not (defect.measurements or {}).get("boundary_edges"):
                continue
            wire = free_wire_containing(shape, int(defect.entity_index))
            if wire is None:
                continue
            loops.append(wire)
            owners.append(defect.fingerprint)

        if len(loops) < 2:
            return frozenset()
        paired = _paired_rims(loops, shape=shape)
        return frozenset(owners[i] for i in paired if i < len(owners))
    except Exception as exc:  # noqa: BLE001 - an unanswerable question must not take the planner down
        logger.warning("plan: the rim discriminator failed (%s: %s); refusing every fill this "
                       "pass rather than guessing there is no through-hole", type(exc).__name__, exc)
        return RimsUnknown(candidates)


def held_fingerprints(defects, reserved) -> frozenset[str]:
    """Fingerprints of the entities carrying a defect held for review (a code in `reserved`).

    THE ONE DEFINITION OF "AIMED AT A HELD ENTITY", shared by the rules planner and the directed
    planner so the two cannot disagree about what a hold covers: an entity is held when ANY defect
    on it is held, whatever code the operation aimed at it claims.
    """
    if not reserved:
        return frozenset()
    return frozenset(d.fingerprint for d in defects if d.fingerprint and d.code in reserved)


class RulesPlanner:
    """Fixed rules: worst defect first, narrowest operation first, whole-shape last.

    Labelled `rules-v1` in what it reports, so a later comparison can say which planner produced a
    run - the point of slice 8 being a swap behind this interface rather than a rewrite.
    """

    label = "rules-v1"

    def __init__(self, ops: tuple[RepairOp, ...] = OPERATIONS, *,
                 reserved: frozenset[DefectCode] = frozenset()) -> None:
        self._ops = tuple(sorted(ops, key=lambda op: op.scope))
        #: Codes a PERSON decides (per-defect review: cad/repair/triage.REVIEW_RESERVED_CODES).
        #: Never proposed here, whatever claims them - only an engineer's directive aims at one.
        self._reserved = frozenset(reserved)

    def propose(self, defects, history: RunHistory, *, shape) -> Candidate | None:
        rims = paired_rim_fingerprints(shape, defects) if shape is not None else frozenset()

        # REFUSE AT DETECTION, AND REFUSE FILLING FOR THE REST OF THE RUN.
        # Recorded now, not when the fill happens to be reached, so a run stopped early still
        # reports it. And the refusal is RUN-LEVEL, not per rim: `rims` is recomputed from the
        # current defects, so once a kept operation closes one rim (or shifts the other's bounding
        # box, changing its fingerprint) the survivor has nothing to pair with and a per-rim ban
        # would miss it - and the targeted fill has no rim check of its own, so that fill would seal
        # the customer's through-hole.
        # DELIBERATELY CONSERVATIVE: a part carrying both a missing bore wall and a legitimately
        # fillable small hole gets the legitimate hole declined too. One missed repair an operator
        # picks up, against a plate handed back with no bolt hole. Do not "optimise" this to
        # per-rim.
        if getattr(rims, "unknown", False):
            # FAIL CLOSED: the pairing check could not run, so no loop is known not to be a rim.
            for rim in sorted(rims):
                history.refuse(
                    rim, "paired_rim",
                    "the through-hole check could not run on this shape, so this loop may be one "
                    "end of a missing tube wall and is not filled")
            history.refuse_filling("the through-hole (paired rim) check could not run")
        else:
            for rim in sorted(rims):
                history.refuse(
                    rim, "paired_rim",
                    "this loop pairs with another on a parallel plane - a missing tube wall, "
                    "and patching both would seal a through-hole")
                history.refuse_filling("a missing tube wall was detected: " + rim)

        ordered = sorted(
            defects,
            key=lambda d: (_SEVERITY_RANK.get(d.severity, 9), d.entity_type, d.entity_index,
                           d.code.value))

        held = held_fingerprints(defects, self._reserved)
        for defect in ordered:
            if defect.code in self._reserved:
                # HELD FOR THE ENGINEER: never the subject of a proposal here, whatever claims it.
                continue
            if not defect.fingerprint:
                # FOUND AND NOT AIMABLE. The case a model most needs to see: an unlocated defect
                # cannot be verified against, so nothing may be tried on it.
                history.refuse(f"{defect.entity_type}:{defect.entity_index}", "untargetable",
                               f"a {defect.code.value} on a {defect.entity_type} with no "
                               "measurable extent")
                continue

            claimed = False
            for op in self._ops:
                if defect.code not in op.answers:
                    continue
                claimed = True
                if history.was_ineffective(defect.fingerprint, op.name):
                    continue
                # AGAINST THE OPERATION'S OWN NAME, never a literal: a rename that updated
                # operations.py and the contract tests but missed a string here would leave this
                # guard dead with every test green - and the guard is the only thing between the
                # loop and a sealed through-hole.
                if op.name == FillPlanarHole.name and history.fill_refused_because:
                    continue
                targets = op.targets_for(defect)
                if not targets:
                    continue
                if any(t.fingerprint in held for t in targets):
                    # AIMED AT A HELD ENTITY, by an operation answering some OTHER code on it: the
                    # same refusal DirectedPlanner gives a model's directive. Filtering by code
                    # alone let a small_edge fix change the very face the engineer is being asked
                    # about. The defect is not refused outright - a later operation whose targets
                    # avoid the held entity may still claim it.
                    continue
                return Candidate(op=op, targets=targets, defect_code=defect.code,
                                 defect_fingerprint=defect.fingerprint)
            if not claimed:
                history.refuse(defect.fingerprint, "no_operation_claims_it",
                               f"{defect.code.value} needs a judgement no bounded operation makes")
        return None
