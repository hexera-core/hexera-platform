# Responsibility: Verify the pipeline deadline is absolute from creation and caps every child budget beneath it.
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.application import pipeline_budget as pb


def test_setting_is_finite_and_positive():
    assert isinstance(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS, int)
    assert rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS > 0


def test_deadline_is_absolute_from_created_at():
    created = datetime(2030, 1, 1, tzinfo=UTC)
    dl = pb.deadline_epoch_from_created_at(created)
    assert dl == created.timestamp() + rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS


def test_deadline_does_not_move_with_repeated_computation():
    created = datetime.now(UTC) - timedelta(hours=1)
    assert pb.deadline_epoch_from_created_at(created) == pb.deadline_epoch_from_created_at(created)


def test_exhausted_when_created_far_in_the_past():
    old = datetime.now(UTC) - timedelta(seconds=rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS + 100)
    assert pb.is_exhausted(pb.deadline_epoch_from_created_at(old))


def test_not_exhausted_for_a_fresh_job():
    assert not pb.is_exhausted(pb.deadline_epoch_from_created_at(datetime.now(UTC)))


def test_remaining_seconds():
    dl = time.time() + 100
    assert 95 < pb.remaining_seconds(dl) <= 100


# child-budget composition
def test_child_budget_capped_by_pipeline_remaining():
    now = 1000.0
    dl = 1050.0                                   # 50s of pipeline budget remain
    assert pb.cap_child_budget(3600, dl, now=now) == 50.0     # child wants 3600, gets 50
    assert pb.cap_child_budget(20, dl, now=now) == 20.0       # child wants 20, keeps 20


def test_child_budget_zero_when_pipeline_exhausted():
    assert pb.cap_child_budget(3600, 1000.0, now=2000.0) == 0.0


def test_child_budget_uncapped_without_a_pipeline_deadline():
    assert pb.cap_child_budget(3600, 0.0) == 3600.0
    assert pb.cap_child_budget(3600, None) == 3600.0


def test_child_deadline_epoch_takes_the_earlier():
    assert pb.cap_child_deadline_epoch(2000.0, 1500.0) == 1500.0   # pipeline binds
    assert pb.cap_child_deadline_epoch(1200.0, 1500.0) == 1200.0   # child binds
    assert pb.cap_child_deadline_epoch(2000.0, None) == 2000.0     # no pipeline cap


# the state field is registered
def test_pipeline_deadline_epoch_is_a_registered_state_field():
    from meshpipeline.contracts.pipeline_state import PipelineState
    from meshpipeline.pipeline import data_contract as dc
    assert "pipeline_deadline_epoch" in PipelineState.__annotations__
    assert "pipeline_deadline_epoch" in dc.STATE_FIELDS
