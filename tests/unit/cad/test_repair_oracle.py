# Responsibility: Verify the verification ladder is ONE policy module - the tiers 0-3 and their
#                 resource policy, the check->tier naming, and the probe budgets - and that routing
#                 the session's and the region loop's budgets through it moved no number: both are
#                 checked against the pre-refactor rule, copied here verbatim, over a table of
#                 remaining times, postures and timeouts. Tier 3 (the product mesher) must stay
#                 unreachable from the loop: no check maps to it and it has no in-loop budget.
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.oracle as oracle
import meshpipeline.cad.repair.region_loop as region_loop
import meshpipeline.cad.repair.session as session_mod
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.settings import inventory


def _legacy_budget(remaining_s: float, final: bool, timeout: float) -> float:
    """The body of `session.mesh_probe_budget_s` as it stood before the oracle, verbatim, with
    the setting read it made passed in instead."""
    remaining = float(remaining_s)
    if not remaining > 0.0:
        return 0.0
    if final:
        return max(0.0, remaining - 10.0)
    return max(0.0, min(float(timeout), 0.5 * remaining))


def _legacy_reserve(posture: str, timeout: float) -> float:
    """The body of `region_loop.final_reserve_s` as it stood before the oracle, verbatim."""
    if posture != "live":
        return 0.0
    return float(timeout) + 10.0


@pytest.mark.parametrize("remaining", [float("nan"), -1.0, 0.0, 0.5, 9.99, 10.0, 10.01, 30.0,
                                       119.0, 120.0, 300.0, 1e6])
@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("timeout", [0.0, 5.0, 60.0, 120.0])
def test_budget_table_matches_the_legacy_rule(monkeypatch, remaining, final, timeout):
    """The refactor is a move, not a change: every cell of the table is what it always was."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", timeout)
    assert session_mod.mesh_probe_budget_s(remaining, final=final) == _legacy_budget(
        remaining, final, timeout)


@pytest.mark.parametrize("posture, timeout", [("off", 60.0), ("live", 60.0), ("live", 5.0),
                                              ("shadow", 60.0)])
def test_final_reserve_matches_the_legacy_rule(monkeypatch, posture, timeout):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", posture)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", timeout)
    assert region_loop.final_reserve_s() == _legacy_reserve(posture, timeout)


def test_session_aliases_are_the_oracles():
    """`region_loop` and the existing tests import these from `session`; they must be the same
    objects the oracle owns, not a second copy that can drift."""
    assert session_mod.FINAL_PROBE_SLACK_S is oracle.FINAL_PROBE_SLACK_S
    assert session_mod.MESH_PROBE_FRACTION is oracle.MESH_PROBE_FRACTION


def test_tier_for_names_every_check():
    assert oracle.tier_for(oracle.CHECK_ATTEMPT) is oracle.TIER_0
    assert oracle.tier_for(oracle.CHECK_SCOPED) is oracle.TIER_1
    assert oracle.tier_for(oracle.CHECK_WHOLE) is oracle.TIER_2
    assert oracle.tier_for(oracle.CHECK_FINAL) is oracle.TIER_2
    assert oracle.tier_for(oracle.CHECK_VERDICT) is oracle.TIER_2
    assert oracle.tier_for(oracle.CHECK_ROUTING) is oracle.TIER_2B


def test_unknown_check_raises():
    """A caller bug must not silently pick a tier."""
    with pytest.raises(ValueError):
        oracle.tier_for("tier3")


def test_tier3_has_no_in_loop_budget():
    """The plan's invariant: the product mesher is not reachable from the loop."""
    assert oracle.TIER_3.in_loop is False
    assert all(tier.in_loop for tier in oracle.TIERS if tier is not oracle.TIER_3)
    assert all(oracle.tier_for(check) is not oracle.TIER_3 for check in oracle.CHECKS)
    for check in oracle.CHECKS:
        budget = oracle.probe_budget_s(check, 1e6, timeout_s=1e6)
        assert budget == 0.0 or oracle.tier_for(check).in_loop


def test_verdict_budget_is_hard_capped():
    assert oracle.probe_budget_s(oracle.CHECK_VERDICT, 1e6, timeout_s=999.0) == 60.0
    assert oracle.TIER2_HARD_CAP_S == 60.0
    assert oracle.TIER2_ELEMENT_CAP == 1_000_000
    assert oracle.TIER2B_MAX_ITS == 10


def test_verdict_and_routing_keep_the_slack():
    assert oracle.probe_budget_s(oracle.CHECK_VERDICT, 15.0, timeout_s=60.0) == 5.0
    assert oracle.probe_budget_s(oracle.CHECK_VERDICT, 10.0, timeout_s=60.0) == 0.0
    assert oracle.probe_budget_s(oracle.CHECK_ROUTING, 15.0, timeout_s=60.0) == 5.0
    assert oracle.probe_budget_s(oracle.CHECK_ROUTING, 10.0, timeout_s=60.0) == 0.0
    # routing is bounded by its own engine's timeout, not by tier 2's cap
    assert oracle.probe_budget_s(oracle.CHECK_ROUTING, 1e6, timeout_s=30.0) == 30.0


def test_an_attempt_costs_no_probe_time():
    """Tier 0 is kernel checks; it starts no probe, so it is never given probe seconds."""
    assert oracle.probe_budget_s(oracle.CHECK_ATTEMPT, 1e6, timeout_s=60.0) == 0.0


@pytest.mark.parametrize("check", ["attempt", "scoped", "whole", "final", "verdict", "routing"])
@pytest.mark.parametrize("remaining", [float("nan"), -1.0, 0.0])
def test_nothing_left_means_no_probe(check, remaining):
    assert oracle.probe_budget_s(check, remaining, timeout_s=60.0) == 0.0


def test_scoped_and_whole_share_one_rule():
    """Named separately so they can diverge later; today they are the same numbers."""
    for remaining in (0.5, 30.0, 119.0, 1e6):
        assert (oracle.probe_budget_s(oracle.CHECK_SCOPED, remaining, timeout_s=60.0)
                == oracle.probe_budget_s(oracle.CHECK_WHOLE, remaining, timeout_s=60.0))


def test_tier2_policy_is_one_worker_process():
    assert (oracle.TIER_2.processes, oracle.TIER_2.threads) == (1, 1)
    assert (oracle.TIER_2B.processes, oracle.TIER_2B.threads) == (1, 1)
    assert oracle.TIER_2.hard_cap_s == oracle.TIER2_HARD_CAP_S
    assert oracle.TIER_2.element_cap == oracle.TIER2_ELEMENT_CAP


def test_tiers_are_in_ladder_order():
    assert [tier.name for tier in oracle.TIERS] == ["0", "1", "2", "2b", "3"]


def test_tier2_default_timeout_equals_the_hard_cap():
    """The session's existing probes read the setting; its default is the ladder's cap, so the
    two policies agree out of the box."""
    assert float(inventory.default_of("CAD_REPAIR_MESH_PROBE_TIMEOUT_S")) == oracle.TIER2_HARD_CAP_S


def test_the_oracle_is_pure_policy():
    """No mesher, no kernel, no settings read: the oracle is a table a caller consults, so one
    rule can be exercised over a table of timeouts without touching the environment."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(oracle))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "dataclasses"}, imported
