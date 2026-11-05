# Responsibility: Classify measured quality into the outcome vocabulary.
# Boundaries: a narrow mapping, kept separate so both the executor and the reviewer read one rule.
from __future__ import annotations

from meshpipeline.contracts.review_outcome import normalize_verdict
from meshpipeline.pipeline.enums import QualityOutcome, Verdict

__all__ = ["normalize_verdict", "classify_quality"]


def classify_quality(
    *,
    reviewer_verdict: str = "",
    api_failure: str = "",
    solvability_failed: bool = False,
    executor_ever_succeeded: bool = False,
) -> str:
    if api_failure:
        return QualityOutcome.API_FAILURE
    if solvability_failed:
        return QualityOutcome.UNSOLVABLE
    if not executor_ever_succeeded:
        return QualityOutcome.PIPELINE_FAILURE
    if normalize_verdict(reviewer_verdict) == Verdict.PASS:
        return QualityOutcome.SUCCEEDED
    return QualityOutcome.REVIEWER_REJECTION
