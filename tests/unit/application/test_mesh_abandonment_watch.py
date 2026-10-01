# Responsibility: Verify a run waiting on its remote mesh learns it was cancelled, moved or ended - and only then stops.
# Boundaries: the watch and its binding by the submission authority; the claim table and the drain probe are doubles.
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest

from meshpipeline.application import native_submission
from meshpipeline.application.native_submission import (
    OWNERSHIP_RECHECK_SECONDS,
    ClaimingMeshExecutor,
    abandonment_watch_for,
)
from meshpipeline.contracts import mesh_execution, worker_drain
from meshpipeline.persistence.repositories import native_submission_repository as claims
from meshpipeline.persistence.repositories.native_submission_repository import (
    NATIVE_MESH_SUBMISSION,
    ClaimOutcome,
    ClaimResult,
    Disposition,
    operation_key,
)

OWN = SimpleNamespace(job_id=uuid.uuid4(), execution_generation=3, worker_token="tok")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def ownership(monkeypatch):
    """The job row's answer to "is this still the worker's?", and every time it was asked."""
    state = {"owned": True, "asked": [], "error": None}

    def still_owned(*, job_id, execution_generation, worker_token):
        state["asked"].append((job_id, execution_generation, worker_token))
        if state["error"] is not None:
            raise state["error"]
        return state["owned"]

    monkeypatch.setattr(claims, "still_owned", still_owned)
    monkeypatch.setattr(worker_drain, "_probe", None)
    return state


def test_an_owned_job_keeps_waiting(ownership):
    watch = abandonment_watch_for(OWN)
    assert watch() is None
    assert ownership["asked"] == [(OWN.job_id, 3, "tok")]


def test_a_cancelled_job_stops_the_wait(ownership):
    # the owner's cancel cleared the worker token (lease.evict_owner); the next check says so
    ownership["owned"] = False
    reason = abandonment_watch_for(OWN)()
    assert reason and "cancelled" in reason


def test_a_draining_worker_stops_at_once_without_asking_the_database(ownership, monkeypatch):
    # The hand-back runs the job again elsewhere as a new generation, so this run's result would
    # never be read. The drain mark is in memory; there is nothing to wait for.
    monkeypatch.setattr(worker_drain, "_probe", lambda: True)
    reason = abandonment_watch_for(OWN)()
    assert reason and "shutting down" in reason
    assert ownership["asked"] == []


def test_ownership_is_asked_at_most_once_per_recheck_period(ownership):
    clock = _Clock()
    watch = abandonment_watch_for(OWN, clock=clock)
    assert watch() is None                                   # t=0: asked
    clock.now += 10
    assert watch() is None                                   # t=10: within the period, not asked
    clock.now += OWNERSHIP_RECHECK_SECONDS - 10 - 0.5
    assert watch() is None                                   # just inside it still
    assert len(ownership["asked"]) == 1
    ownership["owned"] = False                               # the cancel lands
    clock.now += 1
    assert watch()                                           # past it: asked, and told
    assert len(ownership["asked"]) == 2


def test_a_check_that_fails_keeps_waiting_and_asks_again_next_period(ownership, caplog):
    # Unknown is never "stop": killing a paid mesh over a database blip would cost the owner a
    # run they still wanted, and the fence refuses the result of a run that truly lost its job.
    caplog.set_level(logging.WARNING, logger=native_submission.__name__)
    clock = _Clock()
    watch = abandonment_watch_for(OWN, clock=clock)
    ownership["error"] = ConnectionError("database restarting")
    assert watch() is None
    assert "still waiting" in caplog.text
    ownership["error"] = None
    ownership["owned"] = False
    clock.now += OWNERSHIP_RECHECK_SECONDS
    assert watch(), "the check after a failed one was never made"


def test_the_contract_read_never_raises_and_is_none_when_unbound(monkeypatch):
    assert mesh_execution.abandonment_reason() is None

    def broken():
        raise RuntimeError("boom")
    with mesh_execution.abandonment_watch(broken):
        assert mesh_execution.abandonment_reason() is None
    with mesh_execution.abandonment_watch(lambda: "stop"):
        assert mesh_execution.abandonment_reason() == "stop"
    assert mesh_execution.abandonment_reason() is None, "the watch outlived its binding"


# the submission authority binds the watch around the provider


@pytest.fixture()
def authority(monkeypatch, tmp_path, ownership):
    from meshpipeline.application import execution_fence

    monkeypatch.setattr(execution_fence, "current_ownership", lambda: OWN)
    settled: dict = {}

    def claim(*, job_id, execution_generation, worker_token, engine, payload_digest,
              semantic_operation=NATIVE_MESH_SUBMISSION):
        return ClaimOutcome(settled.get("claim", ClaimResult.acquired),
                            operation_key(job_id, execution_generation, semantic_operation),
                            Disposition.claimed)

    def mark_indeterminate(**kw):
        settled["indeterminate"] = kw
        return None

    monkeypatch.setattr(claims, "claim", claim)
    monkeypatch.setattr(claims, "mark_indeterminate", mark_indeterminate)
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    (ws / "plan.json").write_text("{}")
    return SimpleNamespace(workspace=ws, settled=settled)


class _WatchingProvider:
    """A provider that asks the contract between polls, exactly as cloud_run_client does."""

    def __init__(self) -> None:
        self.seen: list = []

    def run(self, workspace, *, engine, timeout, operation_key):
        self.seen.append(mesh_execution.abandonment_reason())
        return {"rc": -3, "timed_out": False, "log_tail": "[CLOUD_RUN_ABANDONED] snappy: x"}

    def collect(self, workspace, *, engine, timeout, operation_key):
        self.seen.append(mesh_execution.abandonment_reason())
        return {"rc": 0}


def test_the_provider_waits_under_a_watch_that_sees_the_cancel(authority, ownership):
    ownership["owned"] = False
    provider = _WatchingProvider()
    ClaimingMeshExecutor(provider).run(authority.workspace, engine="snappy", timeout=60)
    assert provider.seen and "cancelled" in provider.seen[0]
    assert mesh_execution.abandonment_reason() is None, "the watch leaked past the provider call"


def test_a_given_up_run_is_never_recorded_as_accepted(authority, ownership, monkeypatch):
    # No provider reference travels back from a given-up run, so the authority cannot record an
    # acceptance - the existing "no reference" rule applies (and the database refuses even that
    # once the token is gone).
    monkeypatch.setattr(claims, "mark_accepted",
                        lambda **kw: pytest.fail("a given-up run was recorded as accepted"))
    ownership["owned"] = False
    ClaimingMeshExecutor(_WatchingProvider()).run(authority.workspace, engine="snappy", timeout=60)
    assert authority.settled["indeterminate"]["failure_class"] == "no_provider_reference"


def test_a_collection_of_an_earlier_run_waits_under_the_watch_too(authority, ownership):
    authority.settled["claim"] = ClaimResult.existing_accepted
    ownership["owned"] = False
    provider = _WatchingProvider()
    ClaimingMeshExecutor(provider).run(authority.workspace, engine="snappy", timeout=60)
    assert provider.seen and "cancelled" in provider.seen[0]
