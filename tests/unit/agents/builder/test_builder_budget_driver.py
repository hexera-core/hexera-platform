# Responsibility: Verify a deterministic engine driver's time is the pipeline's remaining time, never the model loop's aggregate budget (PLAN-fea-13: no guessed time limit).
from __future__ import annotations

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.agents.builder import budget


def test_the_driver_may_use_the_pipelines_time_past_the_builders_budget(monkeypatch):
    """fw_case, dev-areen 24 Oct: the builder budget (7200 s there) ended the FEA mesher after 108 min while
    it was still making progress; the job itself had hours left."""
    monkeypatch.setattr(bcfg, "BUILDER_TOTAL_TIMEOUT_SECONDS", 7200)
    now = 1_000_000.0
    b = budget.settle(carried_epoch=0.0, pipeline_deadline_epoch=now + 21_000, now=now)
    assert b.remaining_s == 7200                   # the model loop keeps its own budget
    assert b.driver_remaining_s == 21_000          # the driver gets what the job has left


def test_the_driver_never_gets_more_than_the_pipeline_has(monkeypatch):
    monkeypatch.setattr(bcfg, "BUILDER_TOTAL_TIMEOUT_SECONDS", 7200)
    now = 1_000_000.0
    b = budget.settle(carried_epoch=now + 7200, pipeline_deadline_epoch=now + 1_800, now=now)
    assert b.remaining_s == 1_800 and b.driver_remaining_s == 1_800


def test_without_a_pipeline_deadline_the_driver_keeps_the_builders_budget(monkeypatch):
    monkeypatch.setattr(bcfg, "BUILDER_TOTAL_TIMEOUT_SECONDS", 7200)
    b = budget.settle(carried_epoch=0.0, pipeline_deadline_epoch=None, now=1_000_000.0)
    assert b.driver_remaining_s == b.remaining_s == 7200
