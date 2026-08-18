# Responsibility: Separate what a review DECIDED from what HAPPENED to the review.
# Boundaries: vocabulary only - a rendering failure and a quality rejection must never collapse into one outcome.
# Collaborates with: agents/reviewer/ and application/final_result.py.
from __future__ import annotations

import enum


class ReviewVerdict(str, enum.Enum):

    passed = "passed"
    failed = "failed"


class ReviewExecution(str, enum.Enum):

    not_reached = "not_reached"                # an earlier gate or the executor ended the run
    completed = "completed"                    # eligibility accepted a submission
    failed_to_complete = "failed_to_complete"  # it ran and produced no eligible verdict


def normalize_verdict(verdict: str) -> str:
    return verdict.strip() if isinstance(verdict, str) and verdict.strip() in ("PASS", "FAIL") \
        else ""
