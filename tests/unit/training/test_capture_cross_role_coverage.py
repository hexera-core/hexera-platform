# Responsibility: Verify a later role's corpus leaves every preceding role's coverage complete.
from __future__ import annotations

import pytest
from tests.capture_authority import seed_events
from tests.unit.training._capture_corpus import (
    PRECEDING_ROLES,
    builder_full,
    executor_full,
    intake_full,
    ts,
)

from meshpipeline.capture.events import EventLog

CAPTURE_OWNER = "owner-training-tests"

#: The two late roles whose corpora are appended after intake/builder/executor.
LATE_ROLES = ("classifier", "reviewer")

#: role -> the coverage key the completeness report uses for it.
COVERAGE_KEY = {"intake": "intake", "builder": "builder_attempts", "executor": "executor"}


def _classifier_event(job_id: str) -> dict:
    return {
        "timestamp": ts(25), "job_id": job_id, "event_type": "classifier_run", "attempt": 1,
        "payload": {"section": "MESH", "summary": "quality below floor",
                    "error_source": "executor_fail", "failed_gate": "sicn_floor",
                    "failed_axes": [], "builder_mode": "retry", "deterministic": True},
    }


def _reviewer_event(job_id: str) -> dict:
    return {
        "timestamp": ts(30), "job_id": job_id, "event_type": "reviewer_run", "attempt": 1,
        "payload": {"verdict": "PASS", "reviewer_reasoning_chains": "all patches present",
                    "reviewer_tool_call_histories": [{"name": "take_screenshot"}],
                    "reviewer_full_responses": "PASS", "reviewer_system_snapshots": "You review...",
                    "reviewer_input_texts": "review this", "reviewer_review_dirs": "/review/1"},
    }


LATE_ROLE_EVENT = {"classifier": _classifier_event, "reviewer": _reviewer_event}


def _corpus(job_id: str, late_role: str) -> list[dict]:
    return [intake_full(job_id), builder_full(job_id), executor_full(job_id),
            LATE_ROLE_EVENT[late_role](job_id)]


@pytest.mark.parametrize("preceding", PRECEDING_ROLES)
@pytest.mark.parametrize("late_role", LATE_ROLES)
def test_a_late_role_corpus_leaves_the_preceding_roles_complete(
        capture_authority, late_role, preceding):
    job = f"job-{late_role}-{preceding}"
    seed_events(capture_authority, CAPTURE_OWNER, job, _corpus(job, late_role))

    report = EventLog(job, owner_id=CAPTURE_OWNER).validate_completeness()
    key = COVERAGE_KEY[preceding]
    assert report.coverage[key] == "complete", (
        f"seeding a {late_role} corpus left {preceding} coverage at "
        f"{report.coverage[key]!r} - a late role changed the reader's view of an earlier one")


def test_the_family_covers_every_late_role_against_every_preceding_role():
    assert LATE_ROLES == ("classifier", "reviewer")
    assert PRECEDING_ROLES == ("intake", "builder", "executor")
    assert set(COVERAGE_KEY) == set(PRECEDING_ROLES)
    assert set(LATE_ROLE_EVENT) == set(LATE_ROLES)
    assert len(LATE_ROLES) * len(PRECEDING_ROLES) == 6


@pytest.mark.parametrize("late_role", LATE_ROLES)
def test_the_late_roles_corpus_is_actually_seeded(capture_authority, late_role):
    job = f"job-seeded-{late_role}"
    corpus = _corpus(job, late_role)
    assert len(corpus) == 4, corpus
    assert corpus[-1]["event_type"] == f"{late_role}_run"

    seed_events(capture_authority, CAPTURE_OWNER, job, corpus)
    report = EventLog(job, owner_id=CAPTURE_OWNER).validate_completeness()
    assert late_role in report.coverage, (
        f"the {late_role} corpus never reached the report - the rows above would then be "
        f"asserting preceding-role coverage on a corpus without the late role at all")
    assert report.coverage[late_role] == "complete", report.coverage
