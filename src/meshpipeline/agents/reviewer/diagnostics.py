# Responsibility: Extend a review run record with the facts diagnostics need.
# Boundaries: the reviewer's slice of the shared accountability vocabulary.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from meshpipeline.contracts.agent_loop import DiagnosticValue
from meshpipeline.contracts.review_outcome import ReviewVerdict


@dataclass(frozen=True)
class ReviewRunExtension:

    required_axes: tuple[str, ...] = ()
    covered_axes: tuple[str, ...] = ()
    missing_axes: tuple[str, ...] = ()
    duplicate_axes: tuple[str, ...] = ()
    ungrounded_axes: tuple[str, ...] = ()
    invalid_evidence_refs: tuple[str, ...] = ()   # the unusable IDs, not the evidence itself
    usable_evidence_count: int = 0
    unusable_evidence_count: int = 0
    # EXPLICIT evidence delta. approximated "new evidence" with the ledger's LENGTH,
    # which counts an unusable record as progress and a re-cited one as nothing. These are the
    # four distinguishable outcomes; only newly_usable/became_usable are new usable evidence.
    newly_usable_evidence: int = 0
    newly_unusable_evidence: int = 0
    became_usable_evidence: int = 0
    repeated_evidence: int = 0
    submissions: int = 0
    malformed_submissions: int = 0        # payloads eligibility never evaluated
    eligibility_rejections: int = 0
    rejection_reasons: tuple[str, ...] = ()       # eligibility's own machine-readable reasons
    last_submission_changed: bool = False
    new_evidence_since_last_submission: bool = False
    # The SAME type the terminal record uses. Absence is None, never a sentinel string: a
    # review that produced no verdict has none, and the sanitized projection OMITS the field
    # rather than serializing a fake value that a reader could mistake for one.
    accepted_verdict: ReviewVerdict | None = None

    def sanitized(self) -> Mapping[str, DiagnosticValue]:
        out: dict[str, DiagnosticValue] = {
            "required_axes": self.required_axes,
            "covered_axes": self.covered_axes,
            "missing_axes": self.missing_axes,
            "duplicate_axes": self.duplicate_axes,
            "ungrounded_axes": self.ungrounded_axes,
            "invalid_evidence_refs": self.invalid_evidence_refs,
            "usable_evidence_count": self.usable_evidence_count,
            "unusable_evidence_count": self.unusable_evidence_count,
            "newly_usable_evidence": self.newly_usable_evidence,
            "newly_unusable_evidence": self.newly_unusable_evidence,
            "became_usable_evidence": self.became_usable_evidence,
            "repeated_evidence": self.repeated_evidence,
            "submissions": self.submissions,
            "malformed_submissions": self.malformed_submissions,
            "eligibility_rejections": self.eligibility_rejections,
            "rejection_reasons": self.rejection_reasons,
            "last_submission_changed": self.last_submission_changed,
            "new_evidence_since_last_submission": self.new_evidence_since_last_submission,
        }
        if self.accepted_verdict is not None:
            out["accepted_verdict"] = self.accepted_verdict.value
        return out


__all__ = ["ReviewRunExtension"]
