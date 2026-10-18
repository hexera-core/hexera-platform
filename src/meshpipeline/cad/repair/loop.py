# Responsibility: Drive the repair - ask the planner for one operation, have the session try it,
#                 and stop when the part is sound or the budget is spent.
# Owns: the budget, the stop reasons, and the record of the whole run.
# Boundaries: it chooses nothing and judges nothing. It reads no file and writes none.
# Collaborates with: cad/repair/plan.py (what to try), cad/repair/session.py (trying it),
#                    settings/cad_repair.py (the budget), cad/repair/conservative.py (its caller).
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.session import OpVerdict, RepairSession

logger = logging.getLogger(__name__)

#: The loop SUCCEEDED: the scan was exhaustive, no error defect remains, and nothing a rolled-back
#: attempt aimed at is still there. Decided in one place (`_terminal`) for every way a run ends.
RESOLVED = "resolved"
#: The planner has nothing left to try - every pair tried, or nothing claims what is left.
NO_CANDIDATE = "no_candidate"
#: `CAD_REPAIR_MAX_OPS` attempts made.
OP_BUDGET = "op_budget"
#: `CAD_REPAIR_TIMEOUT_S` spent on the WHOLE loop.
TIME_BUDGET = "time_budget"
#: `CAD_REPAIR_STALL_LIMIT` consecutive attempts were not kept.
STALLED = "stalled"
#: The defect scan hit its own budget, so the list is partial and no absence can be claimed.
SCAN_INCOMPLETE = "scan_incomplete"

_ERRORS = (DefectSeverity.error, DefectSeverity.fatal)


@dataclass(frozen=True, slots=True)
class LoopBudget:
    """How much a run may spend. Read from settings unless a caller says otherwise."""

    max_ops: int
    stall_limit: int
    timeout_s: float

    @classmethod
    def from_settings(cls) -> LoopBudget:
        return cls(max_ops=int(rcfg.CAD_REPAIR_MAX_OPS),
                   stall_limit=int(rcfg.CAD_REPAIR_STALL_LIMIT),
                   timeout_s=float(rcfg.CAD_REPAIR_TIMEOUT_S))


@dataclass(frozen=True, slots=True)
class RepairRun:
    """What a whole run did, and why it stopped.

    `stopped_because` IS NOT A DETAIL. "No error defect remains" and "I ran out of operations"
    produce the same shape and mean opposite things to a reviewer, to the corpus, and to a model
    being scored. Code downstream must be able to tell them apart, so the reason travels with the
    result rather than being guessed from the defect list.
    """

    shape: object
    verdicts: tuple[OpVerdict, ...]
    refusals: tuple[dict, ...]
    defects: tuple
    stopped_because: str
    complete_scan: bool
    planner_label: str = ""
    original: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.stopped_because == RESOLVED

    def to_dict(self) -> dict:
        return {"stopped_because": self.stopped_because, "resolved": self.resolved,
                "complete_scan": bool(self.complete_scan), "planner": self.planner_label,
                "operations": [v.to_dict() for v in self.verdicts],
                "refusals": [dict(r) for r in self.refusals],
                "defects_remaining": [d.to_dict() for d in self.defects],
                "original": dict(self.original), "final": dict(self.final)}


def _errors_remain(session: RepairSession) -> bool:
    """Is any defect left that the kernel calls an error or worse?"""
    return any(d.severity in _ERRORS for d in session.defects())


def _abandoned(session: RepairSession) -> bool:
    """Is a defect still present at a fingerprint that a rolled-back attempt targeted?

    `resolved` MEANS THE LOOP SUCCEEDED, not merely that no error-severity defect remains. Those
    differ exactly when the loop gave up on a warning (a sliver two attempts failed to remove):
    calling that resolved would be read downstream as "repaired". A defect a narrow operation
    failed on but a wider one later cleared is gone from the final list, so its earlier rollback
    does not count against the run.
    """
    tried = {fp for v in session.log if not v.kept for fp in v.targets}
    return any(d.fingerprint in tried for d in session.defects())


def _terminal(session: RepairSession, fallback: str) -> str:
    """What to call a finished run. ONE place, so no break path can disagree with another.

    AN INCOMPLETE SCAN WINS OVER EVERY OTHER REASON. A short list from a stopped walk cannot
    support any claim of absence, so it does not matter whether we also ran out of operations -
    what we owe the caller is that we could not see. The attempt count in the op log still says
    the budget was spent.
    """
    if not session.complete_scan:
        return SCAN_INCOMPLETE
    if not _errors_remain(session) and not _abandoned(session):
        return RESOLVED
    return fallback


def _unreached(chosen) -> list[dict]:
    """Directives still queued when the run ended: reported, never silently dropped."""
    return [{"refusal": "directive_unplayable", "reason": "not_reached", "directive": d}
            for d in getattr(chosen, "pending", ())]


def run(shape, *, planner=None, budget: LoopBudget | None = None, tolerance_mm: float,
        session: RepairSession | None = None, clock=time.monotonic) -> RepairRun:
    """Repair `shape` by trying one operation at a time, and report everything that happened.

    `session` and `clock` are injectable so the driver's budget arithmetic can be tested without
    a kernel and without waiting.

    WHY THE PLANNER DECIDES WHETHER THERE IS WORK, rather than a severity test on the defect list.
    An earlier version stopped as `resolved` the moment no ERROR-severity defect remained - which
    made `fix_small_edges` unreachable on the very fixture that proves it works, because a sliver
    edge is reported as a WARNING. Yet a sliver is exactly what breaks a mesher: it forces cells
    orders of magnitude smaller than the part. So the loop runs while the planner still has
    something it is willing to try, and severity decides only what the STOP IS CALLED when the
    planner runs dry: `resolved` if nothing at error severity is left, `no_candidate` if something
    is. A sound part still produces zero verdicts, because the planner proposes nothing for it.
    """
    chosen = planner if planner is not None else RulesPlanner()
    limits = budget if budget is not None else LoopBudget.from_settings()
    # THE CLOCK STARTS BEFORE THE SESSION EXISTS, so the face scan's slice of the budget is a
    # slice of what this run actually has left - including the first scan - and not of a fresh
    # allowance every time the session re-inspects.
    started = clock()

    def remaining() -> float:
        return limits.timeout_s - (clock() - started)

    work = session if session is not None else RepairSession(shape, remaining_s=remaining)
    if work.remaining_s is None:
        work.remaining_s = remaining
    history = RunHistory()

    original = work.original.to_dict()

    # THE FIRST SCAN HAPPENS HERE, and its completeness is read AFTER it. `complete_scan` starts
    # out True, so asking before any scan has run reports a truncated walk as exhaustive.
    work.defects()
    if not work.complete_scan:
        # A SHORT LIST FROM A STOPPED WALK IS NOT AN ABSENCE OF DEFECTS. Reporting it as resolved
        # is how a repairable defect gets delivered as a clean bill of health.
        history.refusals.extend(_unreached(chosen))
        return RepairRun(shape=work.shape, verdicts=(), refusals=tuple(history.refusals),
                         defects=work.defects(), stopped_because=SCAN_INCOMPLETE,
                         complete_scan=False, planner_label=getattr(chosen, "label", ""),
                         original=original, final=original)

    consecutive_misses = 0
    while True:
        if len(work.log) >= limits.max_ops:
            stopped = _terminal(work, OP_BUDGET)
            break
        if clock() - started >= limits.timeout_s:
            # THE WHOLE LOOP, not one operation. Applied per operation, a six-operation run could
            # spend six times a job's allowance before admission has judged the original at all.
            stopped = _terminal(work, TIME_BUDGET)
            break

        candidate = chosen.propose(work.defects(), history, shape=work.shape)
        if candidate is None:
            stopped = _terminal(work, NO_CANDIDATE)
            break

        verdict = work.try_op(candidate.op, candidate.targets, candidate.defect_code,
                              tolerance_mm=tolerance_mm)
        if candidate.source and work.log and work.log[-1] is verdict:
            # THE DIRECTIVE THAT PROPOSED IT travels with the verdict, so what became of an
            # engineer's decision is read off the log and never inferred from op names.
            verdict = replace(verdict, item_id=candidate.item_id, source=candidate.source,
                              directive_id=candidate.directive_id,
                              planner_confidence=candidate.planner_confidence)
            work.log[-1] = verdict
        if verdict.kept:
            consecutive_misses = 0
            if not work.complete_scan:
                # The re-scan after a kept operation was cut short, so the progress rule passed on
                # a partial list. Nothing further can be judged against it.
                stopped = SCAN_INCOMPLETE
                break
            continue

        # THE FEEDBACK. Without this the planner proposes the same failing pair until the budget
        # runs out, and nothing in the run has learned anything. In slice 8 this ledger is exactly
        # what the model is shown on its next turn.
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)
        consecutive_misses += 1
        if consecutive_misses >= limits.stall_limit:
            stopped = _terminal(work, STALLED)
            break

    # DIRECTIVES THAT COULD NOT BE PLAYED reach the report; none is dropped silently.
    for skipped in getattr(chosen, "unplayable", ()):
        history.refusals.append({"refusal": "directive_unplayable", **skipped})
    history.refusals.extend(_unreached(chosen))

    logger.info("repair loop: stopped=%s attempts=%d kept=%d planner=%s",
                stopped, len(work.log), sum(1 for v in work.log if v.kept),
                getattr(chosen, "label", ""))
    return RepairRun(shape=work.shape, verdicts=tuple(work.log),
                     refusals=tuple(history.refusals), defects=work.defects(),
                     stopped_because=stopped, complete_scan=work.complete_scan,
                     planner_label=getattr(chosen, "label", ""),
                     original=original, final=work.current().to_dict())
