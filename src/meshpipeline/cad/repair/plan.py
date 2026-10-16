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
from meshpipeline.cad.repair.operations import OPERATIONS, RepairOp

logger = logging.getLogger(__name__)

# WHY THIS IS A SEPARATE MODULE WITH A PROTOCOL.
#
# This is the seat a model takes in slice 8. Everything about safety - the caps, the progress rule,
# the rollback - lives in the session, so a planner cannot do anything a planner could not do.
# What a planner chooses is which of six bounded operations to point at which located defect, and
# nothing more. Keeping that behind one small interface is what makes "a model picks, the rules
# pick, compare them on the same corpus" an experiment rather than a rewrite.
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


def paired_rim_fingerprints(shape, defects) -> frozenset[str]:
    """Fingerprints of the open-boundary defects that are THE TWO ENDS OF A MISSING TUBE.

    MOVED HERE OUT OF THE FILL EXECUTOR, and the move is the point. A drilled through-hole whose
    bore wall is missing leaves two rims: planar, parallel, offset along their own normal. Each
    looks exactly like a small fillable hole, and patching both SEALS the hole - handing back a
    plate with no bolt hole, which is a ruined part rather than a repaired one. Demonstrated on a
    100x100x10 plate with an 8 mm bore: two loops, both far inside the size cap.

    As a silent skip inside the fill, nothing downstream could report it. As a PLANNING decision,
    refusing to propose it is recorded and reaches the report an operator reads.

    An empty set is the common and correct answer: an ordinary single opening has nothing to pair
    with, and the guard must not cost the case filling exists for.
    """
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
    try:
        paired = _paired_rims(loops)
    except Exception as exc:  # noqa: BLE001 - the discriminator is a guard, never load-bearing
        logger.debug("plan: the rim discriminator did not run (%s)", exc)
        return frozenset()
    return frozenset(owners[i] for i in paired if i < len(owners))


class RulesPlanner:
    """Fixed rules: worst defect first, narrowest operation first, whole-shape last.

    Labelled `rules-v1` in what it reports, so a later comparison can say which planner produced a
    run - the point of slice 8 being a swap behind this interface rather than a rewrite.
    """

    label = "rules-v1"

    def __init__(self, ops: tuple[RepairOp, ...] = OPERATIONS) -> None:
        self._ops = tuple(sorted(ops, key=lambda op: op.scope))

    def propose(self, defects, history: RunHistory, *, shape) -> Candidate | None:
        rims = paired_rim_fingerprints(shape, defects) if shape is not None else frozenset()

        ordered = sorted(
            defects,
            key=lambda d: (_SEVERITY_RANK.get(d.severity, 9), d.entity_type, d.entity_index,
                           d.code.value))

        for defect in ordered:
            if not defect.fingerprint:
                # FOUND AND NOT AIMABLE. The case a model most needs to see: an unlocated defect
                # cannot be verified against, so nothing may be tried on it.
                history.refuse("", "untargetable",
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
                if op.name == "fill_planar_hole" and defect.fingerprint in rims:
                    history.refuse(
                        defect.fingerprint, "paired_rim",
                        "this loop pairs with another on a parallel plane - a missing tube wall, "
                        "and patching both would seal a through-hole")
                    continue
                targets = op.targets_for(defect)
                if not targets:
                    continue
                return Candidate(op=op, targets=targets, defect_code=defect.code,
                                 defect_fingerprint=defect.fingerprint)
            if not claimed:
                history.refuse(defect.fingerprint, "no_operation_claims_it",
                               f"{defect.code.value} needs a judgement no bounded operation makes")
        return None
