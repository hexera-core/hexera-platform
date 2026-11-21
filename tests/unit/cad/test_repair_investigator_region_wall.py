# Responsibility: Pin that a region's investigation wall leaves room to LOOK before the proof
#                 reserve forces a conclusion, for both the driver's clock and the tournament's.
# Boundaries: Pure clock arithmetic with an injected clock; no kernel, no model.
from __future__ import annotations

import types

import pytest

from meshpipeline.cad.repair import investigator as inv
from meshpipeline.cad.repair.investigator_budget import InvestigationClock

BIG_WALL = types.SimpleNamespace(wall_s=1e9, tier="cheap", tool_calls=6, model_calls=8)


def _clocks(run_left: float, proof: float, share: float):
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    deadline = now[0] + run_left
    return (inv._Clock(deadline, reserve_s=0.0, proof_reserve_s=proof, share=share, clock=clock),
            InvestigationClock(deadline, reserve_s=0.0, proof_reserve_s=proof, share=share,
                               clock=clock),
            now)


# TABLE. The production proof reserve is min(60, L/2) + 20, and the configured share is 0.5, so a
# wall of `share * L` sits BELOW the reserve for every L: a live eval at 859cbf7c made 0 tool calls
# on every region ("there was only proof time left"). The wall must be the proof reserve PLUS the
# share of what lies above it.
@pytest.mark.parametrize("run_left,proof,share", [
    (45.0, 42.5, 0.5), (120.0, 80.0, 0.5), (420.0, 80.0, 0.5), (1000.0, 80.0, 0.5),
    (120.0, 80.0, 1.0), (300.0, 20.0, 0.25),
])
def test_a_region_gets_time_to_look_before_it_must_conclude(run_left, proof, share):
    for c in _clocks(run_left, proof, share)[:2]:
        wall = c.region_wall(BIG_WALL)
        assert wall == pytest.approx(proof + share * (run_left - proof))
        assert not c.must_conclude(1000.0, wall)          # the first turn may look


@pytest.mark.parametrize("run_left", [0.0, 10.0, 42.0])
def test_a_run_with_no_time_above_the_proof_reserve_concludes_at_once(run_left):
    for c in _clocks(run_left, 42.5, 0.5)[:2]:
        wall = c.region_wall(BIG_WALL)
        assert wall == pytest.approx(max(0.0, run_left))
        assert c.must_conclude(1000.0, wall)


def test_the_budget_wall_still_caps_the_region():
    small = types.SimpleNamespace(wall_s=30.0, tier="cheap", tool_calls=6, model_calls=8)
    for c in _clocks(1000.0, 80.0, 0.5)[:2]:
        assert c.region_wall(small) == 30.0 + 80.0     # looking time, plus the proof reserve


# The tier budgets' walls are LOOKING time. CHEAP's 60 s sat below the shipping proof reserve
# (60 s scoped probe + 20 s review = 80 s), so capping a region at the bare budget wall made every
# cheap region conclude before its first tool call, at any run length (0 packages on crsv3 at
# 0ff8db35). The proof reserve is held ON TOP of the budget's wall, never carved out of it.
@pytest.mark.parametrize("tier", ["cheap", "standard", "heavy"])
def test_every_tier_budget_may_look_before_the_shipping_proof_reserve_forces_a_conclusion(tier):
    from meshpipeline.cad.repair.investigator_budget import budget_for

    budget = budget_for(tier)
    for c in _clocks(1e6, 80.0, 0.5)[:2]:
        wall = c.region_wall(budget)
        assert wall == pytest.approx(budget.wall_s + 80.0)
        assert not c.must_conclude(1000.0, wall)
