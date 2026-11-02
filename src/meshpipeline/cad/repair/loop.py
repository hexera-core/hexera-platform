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

# The ONE list of sources whose directive may answer a held defect, shared with RepairPlanner so
# the planner's permission and the session's exemption cannot drift apart.
from meshpipeline.cad.repair.directives import HELD_ANSWERING_SOURCES, RepairPlanner
from meshpipeline.cad.repair.localize import analyzer_only
from meshpipeline.cad.repair.plan import RunHistory
from meshpipeline.cad.repair.session import HEAL_OP, OpVerdict, RepairSession

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


def _judged(session: RepairSession) -> tuple:
    """The defects the ENDING is decided on: everything but the face analyzer's own findings.

    NON-DEMOTION (floor-fix ruling 2). A crossing only the analyzer measured exists to target the
    heal and to ask a person; one no operation removed must not turn a run the flag-off world
    calls `resolved` into `no_candidate` or `stalled`. It stays in `defects_remaining`, marked
    `analyzer_only`. A face the status walk also reported is not analyzer-only and still counts.
    """
    return tuple(d for d in session.defects() if not analyzer_only(d))


def _errors_remain(session: RepairSession) -> bool:
    """Is any defect left that the kernel calls an error or worse?"""
    return any(d.severity in _ERRORS for d in _judged(session))


def _abandoned(session: RepairSession) -> bool:
    """Is a defect still present at a fingerprint that a rolled-back attempt targeted?

    `resolved` MEANS THE LOOP SUCCEEDED, not merely that no error-severity defect remains. Those
    differ exactly when the loop gave up on a warning (a sliver two attempts failed to remove):
    calling that resolved would be read downstream as "repaired". A defect a narrow operation
    failed on but a wider one later cleared is gone from the final list, so its earlier rollback
    does not count against the run.
    """
    # A ROLLED-BACK HEAL ABANDONS NOTHING the flag-off run would have tried: it aims at the
    # analyzer's crossing and its partner faces, and a partner's unrelated warning must not turn
    # into "abandoned" because the heal could not re-trim the tangle (floor-fix ruling 2). A
    # crossing the status walk also reported is an error and still blocks `resolved` above.
    tried = {fp for v in session.log if not v.kept and v.op != HEAL_OP for fp in v.targets}
    return any(d.fingerprint in tried for d in _judged(session))


def _analyzer_heal(candidate, session: RepairSession) -> bool:
    """Is this the one optional operation an error-led run may still attempt?"""
    return candidate.op.name == HEAL_OP and any(
        analyzer_only(d) and d.fingerprint == candidate.defect_fingerprint
        for d in session.defects()
    )


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
        session: RepairSession | None = None, clock=time.monotonic,
        reserved: frozenset = frozenset()) -> RepairRun:
    """Repair `shape` by trying one operation at a time, and report everything that happened.

    `session` and `clock` are injectable so the driver's budget arithmetic can be tested without
    a kernel and without waiting.

    WHY THE PLANNER DECIDES WHETHER THERE IS WORK, rather than a blanket severity test on the
    defect list. A warning-only run must still reach operations such as `fix_small_edges`; a sound
    part must produce zero verdicts. An error-led run is narrower: once its errors are cleared, its
    pre-existing warnings remain report evidence instead of expanding the mutation scope after the
    required repair already succeeded.
    """
    chosen = planner if planner is not None else RepairPlanner(())
    limits = budget if budget is not None else LoopBudget.from_settings()
    # THE CLOCK STARTS BEFORE THE SESSION EXISTS, so the face scan's slice of the budget is a
    # slice of what this run actually has left - including the first scan - and not of a fresh
    # allowance every time the session re-inspects.
    started = clock()

    def remaining() -> float:
        return limits.timeout_s - (clock() - started)

    # `reserved` reaches the SESSION as well as the planner: the planner will not aim at a held
    # entity, and the session will not let a whole-shape sweep remove one it never aimed at.
    work = session if session is not None else RepairSession(
        shape, remaining_s=remaining, reserved=reserved)
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

    entered_with_errors = _errors_remain(work)
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
        if (entered_with_errors and not _errors_remain(work) and not _abandoned(work)
                and not _analyzer_heal(candidate, work)):
            stopped = RESOLVED
            break

        # WHAT THIS PROPOSAL IS ALLOWED TO ANSWER. A directive from the engineer being asked (or a
        # replay of an operation this upload already kept) may aim at a held defect - that is
        # RepairPlanner's HELD_ANSWERING_SOURCES - so the session must not then roll the result
        # back for touching the very entity the answer was about. The exemption is only what this
        # directive aims at, never every hold on the part.
        answered_held = (
            frozenset({*(t.fingerprint for t in candidate.targets),
                       candidate.defect_fingerprint})
            if candidate.source in HELD_ANSWERING_SOURCES else frozenset())
        verdict = work.try_op(candidate.op, candidate.targets, candidate.defect_code,
                              tolerance_mm=tolerance_mm, answered_held=answered_held)
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

        if verdict.evidence.get("declined"):
            # A DECLINE IS NOT A FAILED ATTEMPT: the operation judged the aim and ran nothing, so
            # it says nothing about whether progress is possible - and it must not spend the stall
            # budget, which exists to stop ATTEMPTED work being rolled back over and over. On
            # hero.stp two multi_body declines of the heal ended the run `stalled` while 56 open
            # shells a sew would have fixed were never proposed. Recorded against the AIM
            # (operation + target set) so the planner cannot re-propose it from a sibling finding
            # on the same tangle; a run of nothing-but-declines still terminates, because every
            # decline bans its aim (the planner runs dry, `no_candidate`) and the log it joins
            # still counts against `max_ops`.
            history.mark_declined(candidate.op.name,
                                  (t.fingerprint for t in candidate.targets))
            continue

        # THE FEEDBACK. Without this the same failing pair is proposed until the budget runs out,
        # and nothing in the run has learned anything. Consumed only within this run: model
        # guidance is prepared ahead of the loop and never sees this ledger fill.
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
