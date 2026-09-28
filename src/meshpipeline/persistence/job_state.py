# Responsibility: Declare which job-status transitions are legal.
# Boundaries: a transition table, so a status change is checkable rather than assumed.
from __future__ import annotations

from enum import Enum

from meshpipeline.persistence.models import JobStatus

# `cancelled` is terminal too. The owner's cancel authority (application/job_cancel.py) writes it
# once, and from then on no worker's late succeeded/failed can overwrite it - the CAS below refuses.
TERMINAL_STATES: frozenset[JobStatus] = frozenset({JobStatus.succeeded, JobStatus.failed,
                                                   JobStatus.cancelled})

#: IN FLIGHT: a run in one of these still occupies the conversation it came from, counts against
#: the owner's quota and can still change status. Everything else has settled - succeeded, failed,
#: cancelled by its owner, or parked for review - and the conversation that approved it may move
#: on to another run.
#: (queued and pending_review are not assigned by the current pipeline; they are placed here on
#: what they mean, so a stray status can never pin a conversation or a quota forever.)
ACTIVE_STATES: frozenset[JobStatus] = frozenset({JobStatus.pending, JobStatus.running,
                                                  JobStatus.queued})


def is_active(status: object) -> bool:
    """True while `status` - a JobStatus, or its value as a string - names a run still in flight.

    Anything that is not a job status at all (None, an empty string, a value the enum does not
    know) reads as NOT active: a run whose status cannot be established is not one to wait on.
    """
    try:
        return JobStatus(status) in ACTIVE_STATES
    except ValueError:
        return False

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
    # the owner's cancel: anything not yet finished. Terminal sources are excluded like everywhere
    # else, so a cancel can never undo a delivered mesh or a recorded failure.
    JobStatus.cancelled:      frozenset({JobStatus.pending, JobStatus.queued,
                                         JobStatus.running, JobStatus.pending_review}),
}


class TransitionResult(str, Enum):
    applied = "applied"                       # this caller performed the transition
    already_at_target = "already_at_target"   # the job is already in the target state (idempotent)
    rejected_current_state = "rejected_current_state"  # current state is not a legal source (stale)
    not_found = "not_found"                   # no such job


def legal_sources(target: JobStatus) -> frozenset[JobStatus]:
    return LEGAL_SOURCES.get(target, frozenset())
