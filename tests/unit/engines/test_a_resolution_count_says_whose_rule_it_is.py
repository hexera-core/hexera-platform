# The review was handed a flow rule with nothing saying whose rule it was.
#
# MEASURED, job 22ac7054, the real Corintis plate, from the reviewer's own concern: it PASSED the
# passage axis on the measured coolant error - "coolant-volume error is within its
# resolution-specific tolerance at error-over-tolerance 0.472" - and raised a concern anyway,
# because "the resolution should not be represented as satisfying that flow-fidelity rule". It was
# right to. The ledger row it read is labelled "Every passage has the cells across it the fidelity
# asks for" and its measured list said only "the plan asked for 10 cells across the narrowest
# passage (2112 um wide), where the flow rule asks for 13" - a requirement, flatly stated, over a
# conduction-only stagnant-coolant request that no flow rule governs.
#
# 13 IS A FLOW COUNT: `engines/passage.PASSAGE_CELLS_ACROSS` measures it across a pipe, for a
# pressure drop and a coolant temperature rise. This engine does not gate on it and says why - the
# staircase runs show the count does not predict the coolant volume error on any one part - so the
# bar that decides a delivery is that error. Both halves now appear in the sentence the reviewer
# reads. The count is unchanged, unrounded and still reported.
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate.criteria import CRITERIA_ROWS
from meshpipeline.engines.cfmesh_conjugate.finalize import _resolution_short
from meshpipeline.engines.passage import PASSAGE_CELLS_ACROSS

#: Job 22ac7054's own plan numbers: 10.50 cells across its 2.112 mm passages.
PLAN = {"cells_across": 10, "narrowest_m": 0.002112}


def _row(key: str):
    return next(r for r in CRITERIA_ROWS if r.key == key)


def test_the_sentence_still_carries_the_count_and_the_rule():
    """THE NUMBER IS NOT SOFTENED. Everything below is about what is said BESIDE it."""
    got = _resolution_short(PLAN)
    assert len(got) == 1, got
    assert "10 cells across the narrowest passage" in got[0]
    assert "2112 um wide" in got[0]
    assert f"asks for {PASSAGE_CELLS_ACROSS}" in got[0]


def test_the_sentence_says_the_rule_is_a_flow_rule_and_what_it_is_for():
    got = _resolution_short(PLAN)[0]
    assert "flow-fidelity count" in got, got
    # The two things a flow count is actually for, which a conduction request asks for neither of.
    assert "pressure drop" in got and "coolant temperature rise" in got, got


def test_the_sentence_names_the_bar_that_decides_instead():
    """Without this the row reads as an unmet requirement, which is what the review said back."""
    got = _resolution_short(PLAN)[0]
    assert "not the bar this delivery is decided on" in got, got
    assert "measured volume error" in got, got


def test_the_sentence_does_not_claim_the_rule_is_met():
    """THE OPPOSITE FAILURE, and the one worth guarding: the fix must not turn a miss into a pass.
    The row is still non-empty, which is what `op="empty"` judges, and nothing in the wording
    says the count was reached."""
    got = _resolution_short(PLAN)[0].lower()
    for claim in ("satisfied", "is met", "meets the", "as asked", "no shortfall"):
        assert claim not in got, (claim, got)


@pytest.mark.parametrize("plan", [
    {"cells_across": PASSAGE_CELLS_ACROSS, "narrowest_m": 0.002112},
    {"cells_across": PASSAGE_CELLS_ACROSS + 7, "narrowest_m": 0.002112},
])
def test_a_plan_that_reaches_the_rule_says_nothing_at_all(plan):
    assert _resolution_short(plan) == []


@pytest.mark.parametrize("plan", [{}, {"cells_across": 0}, {"cells_across": None}])
def test_an_unmeasured_count_is_silence_and_not_a_pass(plan):
    """`resolution_short` is an `empty` row, so [] reads as a pass - which is correct ONLY when
    the rule was reached. A run that measured no count must not reach here at all; it is kept out
    by the engine never writing the key, and the criterion's own rationale says so."""
    assert _resolution_short(plan) == []


def test_the_width_is_left_out_when_the_run_did_not_measure_one():
    got = _resolution_short({"cells_across": 10})[0]
    assert "um wide" not in got, got
    assert "10 cells across the narrowest passage" in got


def test_the_rows_label_says_which_fidelity_and_that_it_does_not_gate():
    """The label is the first thing the reviewer reads of this row, and it used to say "the
    fidelity asks for" over a flow count on a conduction job."""
    row = _row("resolution_short")
    assert "flow fidelity" in row.label, row.label
    assert "never gated" in row.label, row.label


def test_the_row_really_is_not_gating():
    """The sentence now TELLS the reviewer the count does not decide the delivery. That has to be
    true of the code, not just of the prose - so it is asserted here, next to the prose."""
    assert _row("resolution_short").gating is False
