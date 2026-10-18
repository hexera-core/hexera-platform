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
#: An automatic (LLM or replayed) directive aimed at a defect held for the engineer's review.
RESERVED_FOR_REVIEW = "reserved_for_review"


class DirectedPlanner:
    """Proposes each directive in order, then whatever the fallback planner proposes."""

    label = "directed-v1"

    def __init__(self, directives, fallback=None, ops=OPERATIONS, *,
                 reserved: frozenset[DefectCode] = frozenset()) -> None:
        self._pending = [dict(d) for d in directives]
        self._fallback = fallback if fallback is not None else RulesPlanner(reserved=reserved)
        #: Codes only an ENGINEER's directive may aim at (see RulesPlanner).
        self._reserved = frozenset(reserved)
        self._ops = {op.name: op for op in ops}
        #: Directive + reason for every directive that was skipped. Nothing is dropped silently.
        self.unplayable: list[dict] = []

    @property
    def pending(self) -> tuple[dict, ...]:
        """Directives not yet consumed. A run that stops early leaves some here."""
        return tuple(dict(d) for d in self._pending)

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
        if code in self._reserved and directive.get("source") != "engineer":
            # A person has not answered this one yet; a model's guess is not that answer.
            self._skip(directive, RESERVED_FOR_REVIEW)
            return None

        by_fingerprint = {d.fingerprint: d for d in defects if d.fingerprint}
        refs = []
        raw = directive.get("targets") or ()
        if isinstance(raw, str):
            raw = (raw,)
        for fingerprint in raw:
            defect = by_fingerprint.get(fingerprint)
            if defect is not None:
                refs.append(EntityRef(entity_type=defect.entity_type, fingerprint=fingerprint,
                                      entity_index=int(defect.entity_index),
                                      ambiguous=bool(getattr(defect, "ambiguous", False))))
        if not refs:
            self._skip(directive, TARGET_ABSENT)
            return None

        # `group` marks one hole of a group fill (api/v1/repair_review._engineer_directives): a
        # single target, but from one decision over many, so it is not a person's look at it.
        individual = (directive.get("source") == "engineer" and len(refs) == 1
                      and not directive.get("group"))
        if op.name == FillPlanarHole.name and not individual:
            # A REPLAYED FILL ON A PAIRED RIM IS CORRUPT STATE: the autonomous pass could never
            # have kept one. An ENGINEER's approval may override the planner's refusal - a human
            # looking at the hole is the authority the guard lacks - and the caps still judge it.
            # BUT ONLY FOR ONE RIM: the override is a person's look at THAT hole. A multi-target
            # engineer fill comes from one decision over many (a group redirect) and could name
            # both rims of a missing bore, sealing it - so it is judged by the guard like any other.
            # Read-only: a skipped directive must not touch the sticky history (refuse_filling,
            # paired_rim entries), or it would block the rules planner's later fills.
            rims = paired_rim_fingerprints(shape, defects) if shape is not None else frozenset()
            if history.fill_refused_because or any(r.fingerprint in rims for r in refs):
                self._skip(directive, FILL_REFUSED)
                return None

        return Candidate(op=op, targets=tuple(refs), defect_code=code,
                         defect_fingerprint=str(directive.get("defect_fingerprint")
                                                or refs[0].fingerprint),
                         item_id=str(directive["item_id"]) if directive.get("item_id") else None,
                         source=str(directive.get("source") or "directive"),
                         directive_id=(str(directive["directive_id"])
                                       if directive.get("directive_id") else None),
                         planner_confidence=(float(directive["planner_confidence"])
                                             if isinstance(directive.get("planner_confidence"),
                                                           (int, float)) else None))
