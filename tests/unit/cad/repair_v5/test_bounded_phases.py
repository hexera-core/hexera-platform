"""CRS v5 r2h (owner decision 2026-11-26: every phase in a bounded share) on fakes: the full checks' own share moves the
tournaments' deadline, a whole repair is bounded by its timeout, the staged healer skips the stages past its share,
the rule-based classes go first, and a cut diagnosis keeps a defect's code class instead of `unknown`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.unit.cad.repair_v4.test_b24_seq_arm import _local

from meshpipeline.cad.repair_v4 import seq, stages
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v5 import diagnose as D
from meshpipeline.cad.repair_v5 import tournament as T


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_full_checks_are_paid_from_their_own_share_not_from_the_slices() -> None:
    clock = _Clock()
    t = T.Tournament(menu_of=lambda d: (), verify=lambda *a, **k: _local(), clock=clock, deadline=300.0,
                     panel_credit_s=75.0)
    counters = seq.SeqCounters(defects_left=1)
    counters.full_s = 50.0               # the conformal pass's full check
    t(None, SimpleNamespace(ids={}), Defect("S@1", "mesher", "x", (), cls="close_faces"), counters=counters,
      step=lambda _l: None)
    assert t._deadline() == 350.0 and t._slice(counters) == 350.0
    counters.full_s = 500.0              # past the share: the excess is the loop's again
    t(None, SimpleNamespace(ids={}), Defect("S@1", "mesher", "x", (), cls="close_faces"), counters=counters,
      step=lambda _l: None)
    assert t._deadline() == 375.0


class _SlowPool:
    """Every call takes 40 s of the fake clock; it records the timeout it was handed."""

    def __init__(self, clock: _Clock) -> None:
        self.clock, self.timeouts = clock, []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> Any:
        self.timeouts.append(timeout_s)
        self.clock.t += 40.0
        return SimpleNamespace(ok=True, value={}, status="ok", error=None)


def test_a_deadline_pool_clips_each_call_and_starts_none_past_its_end() -> None:
    clock = _Clock()
    inner = _SlowPool(clock)
    spent: dict[str, float] = {}
    pool = seq.DeadlinePool(inner, 100.0, clock, spent, why="not started: x")
    for _ in range(4):
        pool.call("op", {}, timeout_s=300.0)
    assert inner.timeouts == [100.0, 60.0, 20.0]       # a whole repair's calls add up to its timeout, no more
    assert spent["skipped:op"] == 1


def test_the_healer_skips_the_stages_past_its_share_and_says_so(monkeypatch) -> None:
    clock = _Clock()
    ran: list[str] = []

    def transact(pool, ws, stage, params, before, out, cheap, op_pool=None):
        ran.append(stage.name)
        clock.t += 30.0
        return ws, before

    monkeypatch.setattr(stages, "_transact", transact)
    result = stages.run_staged(object(), SimpleNamespace(brep_path="x"), budget_s=100.0, clock=clock)
    assert ran == ["baseline", "normalise", "edges", "faces"]
    skipped = [n for n, why in result.skipped.items() if why.startswith(stages.TIME_SHARE)]
    assert skipped == [s.name for s in stages.STAGES[4:]]


def test_the_rule_based_classes_go_first_in_the_mesh_tier() -> None:
    ds = [Defect(c, "mesher", "x", (), cls=c) for c in ("low_quality", "close_faces", "closed_periodic_face",
                                                         "invalid_brep_tolerance")]
    assert [d.cls for d in D.order(ds)][:2] == ["closed_periodic_face", "invalid_brep_tolerance"]


def test_the_class_loop_ends_in_time_for_the_delivery_panel() -> None:
    from meshpipeline.cad.repair_v5 import loop

    assert loop.fit_loop_wall(300.0, None, 100.0) == (300.0, "")           # no file wall: unchanged
    assert loop.fit_loop_wall(300.0, 2000.0, 100.0) == (300.0, "")         # room enough
    got, why = loop.fit_loop_wall(300.0, 475.0, 100.0)                     # 375 s for loop + its 25% panel share
    assert got == 300.0 * 1.0 and not why
    got, why = loop.fit_loop_wall(300.0, 350.0, 100.0)
    assert got == 200.0 and "delivery" in why
    assert loop.fit_loop_wall(300.0, 50.0, 100.0)[0] == 0.0


def test_a_cut_diagnosis_keeps_the_code_class_never_unknown() -> None:
    ds = [Defect("a", "mesher", "overlapping_facets", ("F1",), cls="unknown", rule="nothing locates"),
          Defect("b", "validity", "brep_invalid", (), cls="unknown", rule="no site for defect b"),
          Defect("c", "mesher", "periodic_surface", (), cls="unknown", rule="no site"),
          Defect("d", "mesher", "timeout", (), cls="unknown", rule="nothing locates")]
    out = {d.id: d for d in D.by_code_when_cut(ds, "cut")}
    assert out["a"].cls == "facet_crossing_local" and out["a"].cause["cut_short"] == "cut"
    assert out["b"].cls == "invalid_brep_tolerance" and out["c"].cls == "closed_periodic_face"
    assert out["d"].cls == "unknown"                     # a code that names no class stays what it is
