# Responsibility: Declare which job-status transitions are legal.
# Boundaries: a transition table, so a status change is checkable rather than assumed.
from __future__ import annotations

from enum import Enum

from meshpipeline.persistence.models import JobStatus

TERMINAL_STATES: frozenset[JobStatus] = frozenset({JobStatus.succeeded, JobStatus.failed})

# target -> the current states from which a transition to it is LEGAL. Terminal states never
# appear as a source, so an ordinary transition can never overwrite a succeeded/failed result.
LEGAL_SOURCES: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.running:        frozenset({JobStatus.pending}),                       # worker start
    JobStatus.pending:        frozenset({JobStatus.running}),                       # redelivery reset of a stale running
    JobStatus.pending_review: frozenset({JobStatus.running}),                       # reserved (unused by the current pipeline)
    JobStatus.queued:         frozenset({JobStatus.pending}),                       # reserved (unused by the current pipeline)
    JobStatus.succeeded:      frozenset({JobStatus.running, JobStatus.pending_review}),
    JobStatus.failed:         frozenset({JobStatus.pending, JobStatus.queued,
                                         JobStatus.running, JobStatus.pending_review}),
}


class TransitionResult(str, Enum):
    applied = "applied"                       # this caller performed the transition
    already_at_target = "already_at_target"   # the job is already in the target state (idempotent)
    rejected_current_state = "rejected_current_state"  # current state is not a legal source (stale)
    not_found = "not_found"                   # no such job


def legal_sources(target: JobStatus) -> frozenset[JobStatus]:
    return LEGAL_SOURCES.get(target, frozenset())
