# Responsibility: Verify which jobs the reaper selects - the lease rule that frees a job a dead worker left behind
#                 within the hour, and the ceiling that needs no lease at all.
# Boundaries: it reads the predicate the reaper queries with; the database is not involved.
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.application.maintenance import cleanup
from meshpipeline.persistence.models import JobStatus

NOW = datetime(2026, 9, 28, 6, 30, tzinfo=UTC)

# THE DEFECT THIS PINS. Job 4243b45c on shared dev, 2026-09-28: its worker instance was replaced
# by the autoscaler at 01:29 UTC and its heartbeat stopped there, yet it stayed `running` for the
# next five hours because the only rule was "started more than four hours ago". A running job
# holds back its organisation's base credits, so every new job that tenant submitted was refused
# with "your remaining credits are held by the run already in progress".


def _compiled(monkeypatch, *, lease: int = 900, hours: int = 4):
    monkeypatch.setattr(rtcfg, "WORKER_LEASE_SECONDS", lease)
    monkeypatch.setattr(polcfg, "STALLED_JOB_TIMEOUT_HOURS", hours)
    compiled = cleanup.stalled_jobs_clause(NOW).compile()
    return str(compiled), dict(compiled.params)


def test_a_running_job_whose_lease_has_been_expired_for_a_whole_lease_is_selected(monkeypatch):
    # 15 min lease + 15 min grace: about thirty minutes after the last heartbeat, not four hours.
    sql, params = _compiled(monkeypatch)
    assert "lease_expires_at <" in sql
    assert NOW - timedelta(seconds=900) in params.values()


def test_the_grace_follows_the_lease_setting_rather_than_a_number_of_its_own(monkeypatch):
    _, params = _compiled(monkeypatch, lease=120)
    assert NOW - timedelta(seconds=120) in params.values()
    assert NOW - timedelta(seconds=900) not in params.values()


def test_only_a_running_job_is_judged_by_its_lease(monkeypatch):
    # A pending job has no lease yet and a terminal one is done; the lease says nothing about them.
    sql, params = _compiled(monkeypatch)
    m = re.search(r"(\w+)\.status = :(\w+) AND \1\.lease_expires_at IS NOT NULL", sql)
    assert m, sql
    assert params[m.group(2)] == JobStatus.running


def test_a_job_that_never_held_a_lease_is_left_to_the_ceiling(monkeypatch):
    # A NULL lease is a row that reached `running` without a claim, not a lapsed heartbeat.
    sql, _ = _compiled(monkeypatch)
    assert "lease_expires_at IS NOT NULL" in sql


def test_the_ceiling_still_stands_for_running_and_for_never_started_jobs(monkeypatch):
    sql, params = _compiled(monkeypatch, hours=4)
    assert "started_at <" in sql and "created_at <" in sql
    assert NOW - timedelta(hours=4) in params.values()


def test_a_live_job_is_never_reaped_for_its_age(monkeypatch):
    # The ceiling is four hours and the pipeline deadline is six. An age rule applied to a leased
    # job would fail every live run between its fourth and sixth hour, and the worker's real result
    # would then be refused by its own terminal compare-and-set. So the age rule reaches only a
    # running row that never held a lease; a leased one is judged by the lease alone.
    import meshpipeline.settings.runtime as rt
    assert polcfg.STALLED_JOB_TIMEOUT_HOURS * 3600 < rt.PIPELINE_TOTAL_TIMEOUT_SECONDS, (
        "the premise moved: the ceiling now exceeds the pipeline deadline")
    sql, _ = _compiled(monkeypatch)
    assert re.search(
        r"(\w+)\.status = :\w+ AND \1\.lease_expires_at IS NULL AND \1\.started_at IS NOT NULL "
        r"AND \1\.started_at <", sql), sql
