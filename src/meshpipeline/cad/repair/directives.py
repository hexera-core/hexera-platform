# Responsibility: Play an engineer's (or a replayed) list of repair directives before the rules planner.
# Owns: resolving a directive's operation name and target fingerprints against the CURRENT defects,
#       and recording each directive it could not play.
# Boundaries: it proposes. A directive is a planner directive, NOT a cap waiver - the session's
#             caps, rollback and progress rule judge every candidate exactly as they judge the
#             rules planner's. It touches no operation and no cap.
# Collaborates with: cad/repair/plan.py (Candidate, RulesPlanner, the rim guard),
#                    cad/repair/loop.py (surfaces `unplayable` in the run's refusals).
from __future__ import annotations

from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.operations import OPERATIONS, FillPlanarHole
from meshpipeline.cad.repair.plan import Candidate, RulesPlanner, RunHistory, paired_rim_fingerprints

TARGET_ABSENT = "target_absent"
UNKNOWN_OP = "unknown_op"
MALFORMED = "malformed"
FILL_REFUSED = "fill_refused"


class DirectedPlanner:
    """Proposes each directive in order, then whatever the fallback planner proposes."""

    label = "directed-v1"

    def __init__(self, directives, fallback=None, ops=OPERATIONS) -> None:
        self._pending = [dict(d) for d in directives]
        self._fallback = fallback if fallback is not None else RulesPlanner()
        self._ops = {op.name: op for op in ops}
        #: Directive + reason for every directive that was skipped. Nothing is dropped silently.
        self.unplayable: list[dict] = []

    def _skip(self, directive: dict, reason: str) -> None:
        self.unplayable.append({"directive": directive, "reason": reason})

    def propose(self, defects, history: RunHistory, *, shape) -> Candidate | None:
        while self._pending:
            directive = self._pending.pop(0)
            candidate = self._resolve(directive, defects, history, shape)
            if candidate is not None:
                return candidate
        return self._fallback.propose(defects, history, shape=shape)

    def _resolve(self, directive: dict, defects, history, shape) -> Candidate | None:
        op = self._ops.get(str(directive.get("op", "")))
        if op is None:
            self._skip(directive, UNKNOWN_OP)
            return None
        try:
            code = DefectCode(directive.get("defect_code"))
        except ValueError:
            self._skip(directive, MALFORMED)
            return None

        by_fingerprint = {d.fingerprint: d for d in defects if d.fingerprint}
        refs = []
        for fingerprint in directive.get("targets") or ():
            defect = by_fingerprint.get(fingerprint)
            if defect is not None:
                refs.append(EntityRef(entity_type=defect.entity_type, fingerprint=fingerprint,
                                      entity_index=int(defect.entity_index)))
        if not refs:
            self._skip(directive, TARGET_ABSENT)
            return None

        if op.name == FillPlanarHole.name:
            # THE SEALED-THROUGH-HOLE GUARD LIVES IN THE PLANNER, so a directive may not walk
            # around it: an engineer approving one rim must not seal the bore.
            rims = paired_rim_fingerprints(shape, defects) if shape is not None else frozenset()
            for rim in sorted(rims):
                history.refuse(rim, "paired_rim",
                               "this loop pairs with another on a parallel plane - a missing "
                               "tube wall, and patching both would seal a through-hole")
                history.refuse_filling("a missing tube wall was detected: " + rim)
            if history.fill_refused_because:
                self._skip(directive, FILL_REFUSED)
                return None

        return Candidate(op=op, targets=tuple(refs), defect_code=code,
                         defect_fingerprint=str(directive.get("defect_fingerprint")
                                                or refs[0].fingerprint))
