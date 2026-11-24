# Responsibility: CRS v5's ONE repair path (v5 design, 2026-11-24): diagnose every defect into a class, apply the
#                 class's dedicated repairs one defect at a time with local verification, one full panel per round,
#                 and - if the part is still not clean and time remains - v4's AI search from the best state.
# Owns: `repair_v5`, `V5Outcome`, the wall split between the class loop and the fallback.
# Boundaries: the loop is `seq.repair_sequentially`, the classes and their repairs are `classes`, the fallback is
#             whatever search the caller hands in (the eval hands `search.search` with the search_ai generators).
#             The delivery gate and the catastrophic-loss guard stay with the caller, exactly as for every arm.
"""Recognise, repair with a dedicated operation, verify cheap-first, keep or revert - then v4's search if needed.

The fallback is what makes v5 never worse than v4's best: it starts from the class loop's best state, so whatever
the loop kept is the search's floor, and a loop that kept nothing hands the search the same start v4 had (with
less wall). The wall is SPLIT, not shared first-come: the class loop may use at most `SEQ_SHARE` of it, so the
fallback always gets real time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meshpipeline.cad.repair_v4 import classes as C
from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.workspace import Workspace

#: the share of the file's wall the class loop may use; the rest is the fallback's
SEQ_SHARE = 0.5
#: a fallback with less than this left is not started (a search that cannot finish one expansion measures nothing)
MIN_FALLBACK_S = 60.0

#: (start state, its full verdict, wall seconds left) -> a `search.SearchResult`-shaped result
Fallback = Callable[[Workspace, Any, float], Any]


@dataclass
class V5Outcome:
    seq: Q.SeqOutcome
    #: the fallback's result, or None when it did not run
    fallback: Any
    #: why the fallback did or did not run, for the row
    fallback_why: str
    best: Workspace
    verdict: Any

    @property
    def clean(self) -> bool:
        return bool(getattr(self.verdict, "clean", False))

    def lines(self) -> list[str]:
        """The readable trace: defect -> cause -> repair -> result, every round, then the fallback."""
        return [*self.seq.lines(), f"fallback: {self.fallback_why}"]


def repair_v5(
    pool: Any,
    start: Workspace,
    original: str,
    *,
    judge: Any,
    sites_of: Callable[[Any, Workspace, Any], list],
    wall_s: float,
    fallback: Fallback | None = None,
    ai_pick: Q.AIPick | None = None,
    ai_classify: C.AIClassify | None = None,
    start_verdict: Any = None,
    run_stage: Any = None,
    clock: Callable[[], float] = time.monotonic,
    trace: Any = None,
    seq_share: float = SEQ_SHARE,
    **seq_kwargs: Any,
) -> V5Outcome:
    t0 = clock()
    table = C.v5_table(legacy=Q.RULE_TABLE)
    outcome = Q.repair_sequentially(
        pool, start, original, judge=judge, sites_of=sites_of, ai_pick=ai_pick,
        run_stage=run_stage if run_stage is not None else Q.stage_runner(), table=table,
        start_verdict=start_verdict, wall_s=wall_s * seq_share, clock=clock, trace=trace,
        diagnose=lambda ds: C.diagnose(ds, ai_classify=ai_classify), key_of=C.table_key,
        max_alternatives=C.MAX_REPAIRS, **seq_kwargs)
    best, verdict = outcome.best, outcome.verdict
    left = wall_s - (clock() - t0)
    if bool(getattr(verdict, "clean", False)):
        why, result = "not run: the class repairs made the part clean", None
    elif fallback is None:
        why, result = "not run: no fallback search configured", None
    elif left < MIN_FALLBACK_S:
        why, result = f"not run: {left:.0f} s of wall left (< {MIN_FALLBACK_S:g} s)", None
    else:
        result = fallback(best, verdict, left)
        improved = bool(getattr(result, "changed", False))
        why = (f"search_ai from the {'class-repaired' if best is not start else 'start'} state with {left:.0f} s: "
               f"{'clean' if getattr(result, 'clean', False) else 'not clean'}, "
               f"{'improved on it' if improved else 'no better'} ({getattr(result, 'reason', '?')})")
        if improved:
            best, verdict = result.best, result.best_verdict
    if trace is not None:
        trace.record("v5_fallback", line=why)
    return V5Outcome(seq=outcome, fallback=result, fallback_why=why, best=best, verdict=verdict)
