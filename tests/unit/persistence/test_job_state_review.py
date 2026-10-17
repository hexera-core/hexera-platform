# Responsibility: Verify a parked run can be re-run and that finished states still cannot be left.
from __future__ import annotations

from meshpipeline.persistence.job_state import TERMINAL_STATES, legal_sources
from meshpipeline.persistence.models import JobStatus


def test_pending_review_to_pending_is_legal_and_terminal_states_still_are_not():
    assert JobStatus.pending_review in legal_sources(JobStatus.pending)
    assert JobStatus.running in legal_sources(JobStatus.pending)
    assert JobStatus.running in legal_sources(JobStatus.pending_review)
    for terminal in TERMINAL_STATES:
        assert terminal not in legal_sources(JobStatus.pending)
        assert terminal not in legal_sources(JobStatus.pending_review)
