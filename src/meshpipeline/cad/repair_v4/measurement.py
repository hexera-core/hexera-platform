# Responsibility: The ONE definition of "was this file measured at all", shared by the search that produces the
#                 answer and the report that reads it back.
# Owns: `no_verdict_failure` (the search's own rule) and `file_unmeasured` (the rule a consumer applies to a row).
# Boundaries: pure functions over plain values; imports nothing from the engine, so the host-side report can import
#             it without loading a kernel, an op module or a model adapter.
"""Measured, or never measured.

A search in which no candidate got a usable verdict and at least one was inconclusive says NOTHING about whether the
file is repairable: the checker, the mesher or the pool failed, not the part. That must never be tallied as a
failure. `search.search` writes `SearchResult.infrastructure_failed` with `no_verdict_failure`, and the evaluation
report decides whether a row counts with `file_unmeasured`; both live here so the two cannot drift apart.

A search that stopped on its WALL (a time stop) without ending clean USED to be unmeasured too: Task 49's smoke
recorded such a stop, 900.9 s and 0 candidates, as a conclusive failure, which would inflate the failing set with
files nobody measured.

THE ANYTIME RULE (design 5.7, owner ruling 2026-11-24). That was right about machine time and wrong about the
verdict. The search is anytime: it reserves the last part of its wall as a VERDICT BUDGET, stops proposing, runs the
FULL panel on its best state and delivers that. So a time stop is unmeasured ONLY when even that final verdict did
not complete - which is exactly what `out_tier != "full"` says. Lane B16's evidence: on bcf201b55323 the AI's row
was discarded for stopping on the wall while its best state passed THREE panel members, against the two of the
search_det row that was kept; the full verdict for it was already in the trace. A row with a full-tier verdict is a
measurement whatever stopped the search, and `deliverable` still refuses to ship anything worse than the input.

Every arm gets the same rule, so det is not advantaged by exhausting its candidates in 50 s while the AI is deleted
for still working at 650 s. Rows written BEFORE the ruling carry no full-tier verdict on a time stop (an
inconclusive row carries no verdict at all), so they keep their old classification and stay comparable.
"""

from __future__ import annotations

from typing import Any

NO_VERDICT = "no_verdict"
#: Search stops decided by the CLOCK. How far a search gets in its wall depends on the machine and its load (Task 49's
#: smoke ran at load 18 on 16 vCPU), not on the file, so a time stop that did not end clean says nothing about whether
#: the file is repairable. Count stops (ops, exhausted, patience) are the same on any machine and stay conclusive.
TIME_STOPS = frozenset({"wall"})


def no_verdict_failure(*, inconclusive_candidates: int, verdict_candidates: int, best_clean: bool) -> bool:
    """The search's rule: some candidate was inconclusive, none got a verdict, and the best state is not clean."""
    return bool(inconclusive_candidates and not verdict_candidates and not best_clean)


def file_unmeasured(*, reason: Any, infrastructure_failed: Any, inconclusive_candidates: Any,
                    verdict_candidates: Any, out_clean: Any, out_tier: Any = None) -> bool:
    """What a row's search fields say about whether the file was measured at all (True = it was NOT).

    `out_tier` is the tier of the verdict being delivered: only the FULL panel can say what a state is
    (`clean_of`), so a time stop counts as measured when, and only when, its best state carries a full verdict -
    the anytime rule above. Passing no `out_tier` keeps the pre-ruling behaviour, which is what every row written
    before it has."""
    return bool(reason == NO_VERDICT or infrastructure_failed
                or (bool(inconclusive_candidates) and verdict_candidates == 0 and not out_clean)
                or (reason in TIME_STOPS and not out_clean and str(out_tier) != "full"))
