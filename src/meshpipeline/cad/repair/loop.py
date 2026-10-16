# Responsibility: Drive the repair - ask the planner for one operation, have the session try it,
#                 and stop when the part is sound or the budget is spent.
# Owns: the budget, the stop reasons, and the record of the whole run.
# Boundaries: it chooses nothing and judges nothing. It reads no file and writes none.
# Collaborates with: cad/repair/plan.py (what to try), cad/repair/session.py (trying it),
#                    settings/cad_repair.py (the budget), cad/repair/conservative.py (its caller).
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.session import OpVerdict, RepairSession

logger = logging.getLogger(__name__)

#: Every error defect is gone and the scan that says so was exhaustive.
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
    work = session if session is not None else RepairSession(shape)
    history = RunHistory()
    started = clock()

    original = work.original.to_dict()

    # THE FIRST SCAN HAPPENS HERE, and its completeness is read AFTER it. `complete_scan` starts
    # out True, so asking before any scan has run reports a truncated walk as exhaustive.
    work.defects()
    if not work.complete_scan:
        # A SHORT LIST FROM A STOPPED WALK IS NOT AN ABSENCE OF DEFECTS. Reporting it as resolved
        # is how a repairable defect gets delivered as a clean bill of health.
        return RepairRun(shape=work.shape, verdicts=(), refusals=tuple(history.refusals),
                         defects=work.defects(), stopped_because=SCAN_INCOMPLETE,
                         complete_scan=False, planner_label=getattr(chosen, "label", ""),
                         original=original, final=original)

    consecutive_misses = 0
    stopped = NO_CANDIDATE
    while True:
        if len(work.log) >= limits.max_ops:
            stopped = OP_BUDGET
            break
        if clock() - started >= limits.timeout_s:
            # THE WHOLE LOOP, not one operation. Applied per operation, a six-operation run could
            # spend six times a job's allowance before admission has judged the original at all.
            stopped = TIME_BUDGET
            break

        candidate = chosen.propose(work.defects(), history, shape=work.shape)
        if candidate is None:
            stopped = RESOLVED if not _errors_remain(work) else NO_CANDIDATE
            break

        verdict = work.try_op(candidate.op, candidate.targets, candidate.defect_code,
                              tolerance_mm=tolerance_mm)
        if verdict.kept:
            consecutive_misses = 0
            continue

        # THE FEEDBACK. Without this the planner proposes the same failing pair until the budget
        # runs out, and nothing in the run has learned anything. In slice 8 this ledger is exactly
        # what the model is shown on its next turn.
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)
        consecutive_misses += 1
        if consecutive_misses >= limits.stall_limit:
            stopped = STALLED
            break

    logger.info("repair loop: stopped=%s attempts=%d kept=%d planner=%s",
                stopped, len(work.log), sum(1 for v in work.log if v.kept),
                getattr(chosen, "label", ""))
    return RepairRun(shape=work.shape, verdicts=tuple(work.log),
                     refusals=tuple(history.refusals), defects=work.defects(),
                     stopped_because=stopped, complete_scan=work.complete_scan,
                     planner_label=getattr(chosen, "label", ""),
                     original=original, final=work.current().to_dict())
