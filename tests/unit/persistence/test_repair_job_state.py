# Responsibility: Verify the repair-job transition table cannot overwrite a settled job and names who each state waits on.
from __future__ import annotations

import pytest

from meshpipeline.persistence.models import RepairJobStatus as S
from meshpipeline.persistence.repair_job_state import (
    AWAITING_HUMAN,
    BLOCKED,
    IN_FLIGHT,
    LEGAL_SOURCES,
    TERMINAL_STATES,
    awaits_human,
    is_blocked,
    is_legal,
    is_terminal,
    legal_sources,
)

#: The two deliberate reopenings - the only transitions allowed to leave a settled job.
_REOPENINGS = {
    (S.delivered, S.delivery_disputed),
    (S.repair_delivered, S.delivery_disputed),
}


_BUCKETS = {"terminal": TERMINAL_STATES, "awaiting_human": AWAITING_HUMAN,
            "in_flight": IN_FLIGHT, "blocked": BLOCKED}


def test_every_state_is_classified_exactly_once():
    # a state in no bucket is one no queue, metric or alert knows what to do with; one in two
    # buckets is a contradiction - a job cannot be both settled and in progress
    classified = set().union(*_BUCKETS.values())
    assert set(S) - classified == set(), f"unclassified: {set(S) - classified}"
    for a, first in _BUCKETS.items():
        for b, second in _BUCKETS.items():
            if a < b:
                assert not (first & second), f"{a} and {b} overlap: {first & second}"


def test_a_settled_job_is_only_left_by_a_dispute():
    for target, sources in LEGAL_SOURCES.items():
        for src in sources & TERMINAL_STATES:
            assert (src, target) in _REOPENINGS, (
                f"{src.value} -> {target.value} would overwrite a settled job")


def test_a_delivered_job_is_never_silently_redelivered():
    assert not is_legal(S.delivered, S.delivered)
    assert not is_legal(S.delivered, S.meshing)
    assert not is_legal(S.repair_delivered, S.delivered)
    # but the customer's right of reply exists, and a dispute can be settled either way
    assert is_legal(S.delivered, S.delivery_disputed)
    assert is_legal(S.delivery_disputed, S.delivered)
    assert is_legal(S.delivery_disputed, S.repair_delivered)


def test_a_customer_who_finally_sends_a_file_starts_the_job_again():
    # not "resume": the old inspection measured bytes we are no longer working on
    assert is_legal(S.waiting_customer, S.received)
    assert is_legal(S.customer_blocked, S.received)
    assert is_legal(S.received, S.inspecting)


def test_every_state_is_reachable_from_received():
    reachable = {S.received}
    while True:
        grown = {t for t, srcs in LEGAL_SOURCES.items() if srcs & reachable}
        if grown <= reachable:
            break
        reachable |= grown
    assert set(S) - reachable == set(), f"unreachable states: {set(S) - reachable}"


def test_every_state_except_the_first_has_a_way_in():
    for state in S:
        if state is S.received:
            continue        # intake creates the row in it
        assert legal_sources(state), f"{state.value} can never be entered"


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_report_themselves_as_settled(state):
    assert is_terminal(state) and is_terminal(state.value)
    assert not awaits_human(state)


def test_a_cancel_reaches_anything_unsettled_and_nothing_settled():
    sources = legal_sources(S.cancelled)
    assert not (sources & TERMINAL_STATES), "a cancel must not undo a delivered job"
    # every unsettled state except a live dispute, which is an argument about a delivery that
    # already happened - the owner's cancel is not the way to end one
    assert sources == (AWAITING_HUMAN | IN_FLIGHT | BLOCKED) - {S.delivery_disputed}


def test_a_blocked_job_is_stopped_but_still_resumable():
    for state in BLOCKED:
        assert is_blocked(state) and is_blocked(state.value)
        assert not is_terminal(state), f"{state.value} must stay resumable"
        assert legal_sources(state), f"{state.value} can never be entered"
    # the two ways out: a different file from the customer, or a service failure given up on
    assert is_legal(S.customer_blocked, S.received)
    assert is_legal(S.service_failed, S.dead_lettered)
    assert is_blocked("not-a-status") is False


def test_a_mesh_may_be_attempted_without_any_repair():
    # the service's commonest happy path: the file was fine, so nothing is mutated
    assert is_legal(S.awaiting_strategy, S.meshing)
    assert is_legal(S.repair_review, S.meshing)


def test_an_unknown_status_is_neither_settled_nor_waiting():
    for junk in (None, "", "not-a-status", 7):
        assert is_terminal(junk) is False
        assert awaits_human(junk) is False


def test_waiting_on_a_person_is_not_a_stall():
    # the clock that matters for these is somebody else's; an SLA that counts them as our time
    # reports work as late that nobody could have done
    for state in (S.waiting_customer, S.awaiting_strategy, S.repair_review, S.mesh_review,
                  S.manual_cleanup, S.escalated):
        assert awaits_human(state)
    for state in (S.inspecting, S.repairing, S.meshing, S.retrying):
        assert not awaits_human(state)
