# Responsibility: a plan may not carry a coolant tolerance the review would divide by and fail on.
# Boundaries: `cfmesh_conjugate/plan.read_plan`.
# Collaborates with: finalize.quality_of, which publishes coolant_error_over_tol from it.
"""The one gating metric of this engine's review plan is a ratio, and nothing refused a zero.

`finalize.quality_of` publishes `coolant_error_over_tol` as `coolant_error_pct / coolant_tol_pct`,
inside `if q["coolant_tol_pct"] > 0.0`. That key is the ONLY gating metric in the whole conjugate
review plan. An absent gating metric is not an advisory gap - `eligibility.advisory_gaps` will not
tolerate it - so `deterministic_evidence_complete` returns `['metric:coolant_error_over_tol']` and
the review ends `reviewer_evidence_missing`, which is NOT rerunnable, on a mesh that passed every
gate. finalize.py states the rule one block below: "a gating key missing from the quality block
fails closed".

Omitting the ratio is correct - a ratio with no denominator has no value, and
`test_the_ratio_is_absent_when_there_is_no_tolerance_to_divide_by` pins that. What was wrong is
that a plan could ask for a zero denominator at all. `volume_tol` was not on `read_plan`'s
non-defaultable list, so `volume_tol: 0` was accepted silently and cost the mesh its verdict two
hours later instead of failing in the second it takes to read the plan.

A planner-built plan cannot reach it: `_tolerance` is floored at `DEFAULT_VOLUME_TOL` 0.05 and is
positive for every cells-across. A hand-written or legacy `conjugate_plan.json` can.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate.conjugate import DEFAULT_VOLUME_TOL
from meshpipeline.engines.cfmesh_conjugate.plan import PLAN_NAME, _tolerance, read_plan

GOOD = {
    "background_cell_m": 0.000402525,
    "levels": 4,
    "expect_fluid_volume_m3": 5.2395e-05,
    "expect_fluid_pieces": 2,
    "volume_tol": 0.05,
    "narrowest_m": 0.002112,
    "cells_across": 10,
}


def written(tmp_path: Path, **over) -> Path:
    body = {**GOOD, **over}
    for k, v in list(body.items()):
        if v is None:
            body.pop(k)
    (tmp_path / PLAN_NAME).write_text(json.dumps(body), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ the refusal

def test_a_zero_tolerance_is_refused():
    """THE REGRESSION. This was accepted, and the cost landed two hours later."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError) as exc:
            read_plan(written(Path(d), volume_tol=0.0))
    said = str(exc.value)
    assert "volume_tol" in said
    assert "reviewer_evidence_missing" in said, "the refusal must say what it is preventing"


@pytest.mark.parametrize("bad", [0.0, 0, -0.01, -1.0])
def test_no_non_positive_tolerance_gets_through(bad, tmp_path):
    with pytest.raises(ValueError, match="volume_tol"):
        read_plan(written(tmp_path, volume_tol=bad))


def test_an_absent_tolerance_still_fails_but_is_not_this_check(tmp_path):
    """DELIBERATELY NOT PROMOTED TO A NAMED REFUSAL, and a test taught me why.

    I first added `volume_tol` to `read_plan`'s non-defaultable list so an omitted one would be
    named. That check runs before every other validation in the function, so
    `test_a_plan_with_no_process_at_all_is_refused` began reporting a missing tolerance instead of
    the missing process it exists to catch - a field requirement at the top of a validator
    rewrites the error of every plan that is invalid for any other reason.

    An omitted tolerance already fails on its own: `ConjugatePlan.volume_tol` has no default, so
    the construction raises TypeError. That is a worse message than a named ValueError, and it
    arrives after the checks that have something more useful to say, which is the right order.
    What this module now refuses is the case that was silently ACCEPTED - a tolerance that is
    present and zero.
    """
    with pytest.raises((ValueError, TypeError)) as exc:
        read_plan(written(tmp_path, volume_tol=None))
    assert "volume_tol" in str(exc.value)


# ------------------------------------------------------------------ nothing real is refused

def test_the_planners_own_tolerance_is_always_accepted(tmp_path):
    """The guard must be unreachable from a plan the product built itself."""
    for cells_across in (0, 1, 5, 10, 13, 20, 39):
        tol = _tolerance(cells_across)
        assert tol > 0.0, f"_tolerance({cells_across}) is not positive"
        got = read_plan(written(tmp_path, volume_tol=tol, cells_across=cells_across))
        assert got.volume_tol == tol


def test_the_default_floor_is_what_it_was():
    assert DEFAULT_VOLUME_TOL == 0.05
    assert _tolerance(0) == DEFAULT_VOLUME_TOL


def test_a_good_plan_still_reads_back_unchanged(tmp_path):
    got = read_plan(written(tmp_path))
    assert got.volume_tol == 0.05
    assert got.expect_fluid_volume_m3 == 5.2395e-05
    assert got.narrowest_m == 0.002112
    assert got.background_cell_m == 0.000402525


@pytest.mark.parametrize("key", ["background_cell_m", "expect_fluid_volume_m3", "narrowest_m"])
def test_the_other_non_defaultable_keys_are_unchanged(key, tmp_path):
    """The list gained one entry; it must not have lost any."""
    with pytest.raises(ValueError) as exc:
        read_plan(written(tmp_path, **{key: None}))
    assert key in str(exc.value)


# The ratio this guard protects is pinned where it is produced, not here:
# test_the_ratio_is_absent_when_there_is_no_tolerance_to_divide_by (test_conjugate_row.py) asserts
# the omission, and the conjugate row tests cover the value. finalize.quality_of reads a WORKSPACE
# and not a report dict, so exercising it from here would mean standing up a polyMesh tree in
# order to re-test somebody else's assertion.
