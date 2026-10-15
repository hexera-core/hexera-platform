# Responsibility: Declare which repair-job transitions are legal, and who a state is waiting on.
# Boundaries: a transition table, so a status change is checkable rather than assumed.
# Collaborates with: persistence/models.py (the enum) and the repair job repository (which applies it).
from __future__ import annotations

from meshpipeline.persistence.models import RepairJobStatus as S

# WHY THIS IS NOT A LINE. A service job is not a pipeline run: it waits on a customer for a file
# it never received, it goes to a person when the geometry needs judgement rather than an
# algorithm, and it can be reopened after it was delivered. A linear flow hides all three inside
# free-text notes, which is exactly how a queue stops being manageable. Every state below exists
# because something real is waiting, and the table says what may follow it.

#: SETTLED. Nothing further happens on its own, and no ordinary transition may leave one: a
#: delivered job is not re-delivered and an abandoned one is not quietly resumed. The two ways out
#: are a dispute (`delivered` -> `delivery_disputed`, the customer's right of reply) and a
#: reopened blocker (`customer_blocked` -> `received`, when the customer finally sends the file).
TERMINAL_STATES: frozenset[S] = frozenset({
    S.delivered, S.repair_delivered, S.cancelled, S.dead_lettered, S.expired})

#: WAITING ON SOMEONE OUTSIDE THE SYSTEM. These do not age into a failure and they do not count
#: against throughput: the clock that matters for them is the customer's or the operator's, not
#: ours. Reporting them as "in progress" is how a queue comes to be full of work nobody is doing.
AWAITING_HUMAN: frozenset[S] = frozenset({
    S.waiting_customer, S.awaiting_strategy, S.repair_review, S.mesh_review,
    S.manual_cleanup, S.escalated, S.delivery_disputed})

#: OURS TO MOVE. A job sitting in one of these with no recent attempt is a stall worth alerting on.
IN_FLIGHT: frozenset[S] = frozenset({
    S.received, S.inspecting, S.repairing, S.meshing, S.retrying})

#: STOPPED, BUT NOT SETTLED. Nothing is in flight and nobody is working the queue item: it needs a
#: decision before anything else can happen, and until then it is neither progress nor an outcome.
#: They are kept apart from the settled states because both can still be resumed - a blocked
#: customer can send a different file, and a service failure can be retried once it is fixed - and
#: apart from AWAITING_HUMAN because no named person owes us an action yet. Counting either as
#: in-flight would show a queue busier than it is; counting them as delivered would be a lie.
BLOCKED: frozenset[S] = frozenset({S.customer_blocked, S.service_failed})

# target -> the states a transition to it is legal FROM. Terminal states appear as a source only
# for the two deliberate reopenings named above, so nothing else can overwrite a settled job.
LEGAL_SOURCES: dict[S, frozenset[S]] = {
    S.inspecting:        frozenset({S.received, S.retrying}),
    # inspection finished and said what it saw; an operator picks the route
    S.awaiting_strategy: frozenset({S.inspecting, S.repair_review, S.mesh_review, S.escalated}),
    S.repairing:         frozenset({S.awaiting_strategy, S.retrying}),
    S.repair_review:     frozenset({S.repairing, S.manual_cleanup}),
    # a mesh may be attempted on the ORIGINAL geometry (straight from the strategy choice, no
    # repair at all) or on approved repaired geometry
    S.meshing:           frozenset({S.awaiting_strategy, S.repair_review, S.retrying}),
    S.mesh_review:       frozenset({S.meshing}),
    S.manual_cleanup:    frozenset({S.awaiting_strategy, S.repair_review, S.mesh_review,
                                    S.escalated}),
    # a retry is a decision, not a state anything falls into: only a review or an escalation
    # chooses one, and the attempt budget is spent by leaving this state, not by entering it
    S.retrying:          frozenset({S.repair_review, S.mesh_review, S.escalated}),
    S.escalated:         frozenset({S.awaiting_strategy, S.repairing, S.repair_review,
                                    S.meshing, S.mesh_review, S.manual_cleanup, S.retrying,
                                    S.service_failed}),
    # the customer owes us something: a file, a unit, an intent. Reachable from any point at which
    # we discover that, including after a review has looked at what they sent.
    S.waiting_customer:  frozenset({S.received, S.inspecting, S.awaiting_strategy,
                                    S.repair_review, S.mesh_review, S.manual_cleanup,
                                    S.escalated}),
    # they sent it: the job starts again on the new file rather than resuming a stale inspection
    S.received:          frozenset({S.waiting_customer, S.customer_blocked}),
    S.customer_blocked:  frozenset({S.waiting_customer, S.awaiting_strategy, S.escalated}),
    S.service_failed:    frozenset({S.inspecting, S.repairing, S.meshing, S.retrying,
                                    S.escalated}),
    S.delivered:         frozenset({S.mesh_review, S.delivery_disputed}),
    # repaired geometry handed over with no mesh - a real outcome of this service, and never
    # reported as a mesh success
    S.repair_delivered:  frozenset({S.repair_review, S.delivery_disputed}),
    S.delivery_disputed: frozenset({S.delivered, S.repair_delivered}),
    S.dead_lettered:     frozenset({S.service_failed, S.escalated}),
    S.expired:           frozenset({S.waiting_customer, S.customer_blocked}),
    # the owner's cancel: anything not already settled
    S.cancelled:         frozenset({S.received, S.inspecting, S.awaiting_strategy, S.repairing,
                                    S.repair_review, S.meshing, S.mesh_review, S.manual_cleanup,
                                    S.retrying, S.escalated, S.waiting_customer,
                                    S.customer_blocked, S.service_failed}),
}


def legal_sources(target: S) -> frozenset[S]:
    return LEGAL_SOURCES.get(target, frozenset())


def is_legal(current: S, target: S) -> bool:
    return current in legal_sources(target)


def is_terminal(status: object) -> bool:
    try:
        return S(status) in TERMINAL_STATES
    except ValueError:
        return False


def awaits_human(status: object) -> bool:
    """True while the job is waiting on a person - ours or the customer's.

    Read by the queue: these are not stalls, and an SLA clock that counts them as our time
    reports work as late that nobody could have done.
    """
    try:
        return S(status) in AWAITING_HUMAN
    except ValueError:
        return False


def is_blocked(status: object) -> bool:
    """True while the job is stopped awaiting a decision - the customer's, or ours to fix."""
    try:
        return S(status) in BLOCKED
    except ValueError:
        return False
