# Responsibility: Verify a measured budget overshoot is corrected by arithmetic, decisively, once.
from __future__ import annotations

import json

from meshpipeline.engines.snappy.drivers import _prev_attempt_overshoot
from meshpipeline.engines.snappy.planner import overshoot_corrected_budget


def test_the_failed_trajectory_replayed_converges_on_the_second_attempt():
    # The live experiment, verbatim: ceiling forced to 2M, the model asked for 1.8M and snappy
    # produced 4,318,852. The model then guessed - 2.46M, 2.29M, 2.12M - and the fifth attempt
    # died with a defect-free mesh 6% over. The measured inflation was 2.4x, so the correction is
    # division, not courage.
    corrected = overshoot_corrected_budget(1_800_000, 4_318_852, ceiling=2_000_000)
    assert corrected is not None
    inflation = 4_318_852 / 1_800_000
    predicted_actual = corrected * inflation
    assert predicted_actual < 2_000_000, (corrected, predicted_actual)
    # and not absurdly conservative either - it should land in the 70-100% band of the ceiling
    assert predicted_actual > 0.6 * 2_000_000, (corrected, predicted_actual)


def test_no_measured_overshoot_means_no_correction():
    # first attempts, results already inside the ceiling, or garbage numbers: the model's own
    # budget stands - this rule only ever acts on a measurement
    assert overshoot_corrected_budget(1_800_000, 1_500_000, ceiling=2_000_000) is None
    assert overshoot_corrected_budget(None, 4_000_000, ceiling=2_000_000) is None
    assert overshoot_corrected_budget(1_800_000, None, ceiling=2_000_000) is None
    assert overshoot_corrected_budget("junk", 4_000_000, ceiling=2_000_000) is None
    assert overshoot_corrected_budget(0, 4_000_000, ceiling=2_000_000) is None
    assert overshoot_corrected_budget(-5, 4_000_000, ceiling=2_000_000) is None


def test_the_correction_is_bounded_below_and_above():
    # a catastrophic overshoot cannot drive the budget to nothing, and a mild one cannot exceed
    # the ceiling it exists to respect
    assert overshoot_corrected_budget(100_000, 90_000_000, ceiling=2_000_000) == 200_000
    big = overshoot_corrected_budget(1_999_999, 2_000_001, ceiling=2_000_000)
    assert big is not None and big <= 2_000_000


def test_the_retry_reads_what_its_predecessor_measured(tmp_path):
    gen = tmp_path / "generation_1"
    prev = gen / "attempt_1"
    prev.mkdir(parents=True)
    (prev / ".last_plan.json").write_text(json.dumps({"max_cells": 1_800_000}))
    (prev / "mesh_manifest.json").write_text(json.dumps({"cell_count": 4_318_852}))
    cur = gen / "attempt_2"
    cur.mkdir()

    assert _prev_attempt_overshoot(cur) == (1_800_000.0, 4_318_852.0)
    # a first attempt has no predecessor, and a workspace that names no attempt corrects nothing
    assert _prev_attempt_overshoot(prev) is None
    other = tmp_path / "workspace"
    other.mkdir()
    assert _prev_attempt_overshoot(other) is None
    # missing files on the predecessor: silently nothing to correct from
    bare_prev = tmp_path / "g2" / "attempt_1"
    bare_cur = tmp_path / "g2" / "attempt_2"
    bare_cur.mkdir(parents=True)
    bare_prev.mkdir()
    assert _prev_attempt_overshoot(bare_cur) is None
