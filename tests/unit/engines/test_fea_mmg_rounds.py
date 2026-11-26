# Responsibility: Verify MESH-mmg-2 - with the user's own quality limits the quality pass works in rounds and stops when a round makes good fewer than MIN_GAIN_SHARE of the elements still past them, after MAX_ROUNDS, or past its share of the run's time; it says why; with no limits it is one round, as before.
# Boundaries: fea_mmg.improve with its mmg call, splice and figures replaced by fakes (no mmg binary, no mesh); the counts are the owner's (26 Nov: 113 past before, 111 after).
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.engines import fea_mmg as M

TARGETS = [{"id": "L1", "figure": "edge_ratio", "op": "<=", "target": 5.0}]


def _fake(monkeypatch, pasts: list[int], seconds: float = 1.0):
    """Each mmg round brings the elements past the limit to the next number in `pasts` (pasts[0] = before)."""
    state = {"round": 0, "calls": 0}
    clock = {"t": 0.0}
    monkeypatch.setattr(M.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(M, "_volume_arrays", lambda g, v: (np.arange(4), np.zeros((4, 3)), np.zeros((1, 4), int),
                                                           np.zeros((4, 3), int), np.zeros(4, int)))
    monkeypatch.setattr(M, "around", lambda tets, seed, rings: np.ones(len(tets), dtype=bool))
    monkeypatch.setattr(M, "flagged", lambda *a, **k: np.ones(1, dtype=bool))

    def worst(*_a, **_k):
        past = pasts[min(state["round"], len(pasts) - 1)]
        return {"elements": 10, "invalid": 0, "min_sj": 0.5, "max_aspect": 6.0, "min_dihedral": 20.0,
                "max_dihedral": 140.0, "over_limits": 0, "triggered": past, "triggered_pinned": 0,
                "user_past": past, "user_past_by": {"L1": past}}

    def remesh(*_a, timeout, **_k):
        state["calls"] += 1
        state.setdefault("timeouts", []).append(timeout)
        clock["t"] += seconds
        state["round"] += 1
        return {"ok": True, "xyz": np.zeros((4, 3)), "tets": np.zeros((1, 4), int), "boundary_map": np.zeros(4, int)}

    monkeypatch.setattr(M, "worst", worst)
    monkeypatch.setattr(M, "remesh", remesh)
    monkeypatch.setattr(M, "splice", lambda xyz, tets, free, out, targets=None: {**out, "cavities": 1, "improved": 1})
    monkeypatch.setattr(M, "_replace", lambda *a: None)
    gmsh = SimpleNamespace(model=SimpleNamespace(mesh=SimpleNamespace(getEmbedded=lambda d, v: [])))
    return gmsh, state


def test_a_round_that_buys_two_of_113_ends_the_pass_and_says_so(monkeypatch):
    gmsh, state = _fake(monkeypatch, [113, 111, 50])
    rec = M.improve(gmsh, [1], exe="mmg", targets=TARGETS, budget_s=600)
    v = rec["volumes"][0]
    assert state["calls"] == 1 and v["stopped"] == "little gained" and v["kept"]
    assert v["rounds"] == [{"seconds": 1.0, "past_before": 113, "past_after": 111}]
    report = {"rows": [{"id": "L1", "delivered": "measured"}]}
    words = M.tell(report, rec)["rows"][0]["lever"]["words"]
    assert "113 past it before, 111 after" in words and "another round would have made few more good" in words


def test_rounds_go_on_while_they_pay_and_stop_at_max_rounds(monkeypatch):
    gmsh, state = _fake(monkeypatch, [100, 60, 30, 10, 5])
    v = M.improve(gmsh, [1], exe="mmg", targets=TARGETS, budget_s=600)["volumes"][0]
    assert state["calls"] == M.MAX_ROUNDS and v["stopped"] == "rounds"
    assert [r["past_after"] for r in v["rounds"]] == [60, 30, 10]


def test_the_pass_keeps_to_its_share_of_the_run(monkeypatch):
    gmsh, state = _fake(monkeypatch, [100, 50, 25, 10], seconds=100.0)
    v = M.improve(gmsh, [1], exe="mmg", targets=TARGETS, budget_s=80)["volumes"][0]
    assert state["calls"] == 1 and v["stopped"] == "time"
    assert state["timeouts"][0] == pytest.approx(80)          # the first round is held to the budget too


def test_without_limits_it_is_one_round_with_the_old_time_and_record(monkeypatch):
    gmsh, state = _fake(monkeypatch, [3, 1, 0])
    rec = M.improve(gmsh, [1], exe="mmg", targets=None, budget_s=10)
    v = rec["volumes"][0]
    assert state["calls"] == 1 and "rounds" not in v and "stopped" not in v and "budget_s" not in rec
    assert state["timeouts"][0] == pytest.approx(M.TIME_BASE_S + M.TIME_PER_ELEMENT_S * 1)
    assert list(v) == ["volume", "mmg", "before", "free_elements", "seconds", "cavities", "cavities_improved",
                       "after", "kept", "why"]


def test_the_budget_is_a_share_of_the_run_with_a_floor():
    assert M.budget_for(None) is None
    assert M.budget_for(100) == M.ROUND_FLOOR_S
    assert M.budget_for(2400) == pytest.approx(600)
