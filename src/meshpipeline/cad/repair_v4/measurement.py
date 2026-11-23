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
"""

from __future__ import annotations

from typing import Any

NO_VERDICT = "no_verdict"


def no_verdict_failure(*, inconclusive_candidates: int, verdict_candidates: int, best_clean: bool) -> bool:
    """The search's rule: some candidate was inconclusive, none got a verdict, and the best state is not clean."""
    return bool(inconclusive_candidates and not verdict_candidates and not best_clean)


def file_unmeasured(*, reason: Any, infrastructure_failed: Any, inconclusive_candidates: Any,
                    verdict_candidates: Any, out_clean: Any) -> bool:
    """What a row's search fields say about whether the file was measured at all (True = it was NOT)."""
    return bool(reason == NO_VERDICT or infrastructure_failed
                or (bool(inconclusive_candidates) and verdict_candidates == 0 and not out_clean))
