"""CRS v5 r2 (time management) on fakes: each tournament gets a SLICE of the class loop's wall, an op times out at
the slice, a defect its slice cut short gets a second turn at the round's end, the conformal pass and the diagnosis
stop at their shares, and a site's close-pair facts are reused across rounds only when the region did not change."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tests.unit.cad.repair_v4.test_b24_seq_arm import _local, _patched
from tests.unit.cad.repair_v5.test_tournament import _WS, _apply, _defect, _volume

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.ops import OpFailed
from meshpipeline.cad.repair_v5 import conformal as C
from meshpipeline.cad.repair_v5 import diagnose as D
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import tournament as T


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_a_tournament_gets_its_fair_slice_of_what_the_loop_has_left_and_ops_time_out_at_it() -> None:
    clock = _Clock()
    t = T.Tournament(menu_of=lambda d: (), verify=lambda *a, **k: _local(), clock=clock, deadline=300.0)
    counters = seq.SeqCounters(defects_left=5)
    assert t._slice(counters) == 60.0                     # 300 s left among 5 defects
    t._slice_end = 60.0
    assert t._op_timeout() == 60.0
    counters.defects_left = 50
    assert t._slice(counters) == T.MIN_SLICE_S            # never under the floor
    clock.t = 290.0
    assert t._slice(counters) == 300.0                    # never past the loop's deadline
    t._slice_end = 300.0
    assert t._local_timeout() == 10.0                     # clipped to the loop deadline: a hard cap


def test_an_op_killed_at_its_slice_is_a_SLICE_stop_and_the_defect_is_flagged_for_a_second_turn() -> None:
    clock = _Clock()
    fail = {"split_aligned": OpFailed("timeout", "worker killed"), "defeature": OpFailed("timeout", "killed")}
    t = T.Tournament(menu_of=lambda d: (("split_aligned",), ("defeature",)), verify=lambda *a, **k: _local(),
                     guard=_volume({}), clock=clock, deadline=100.0, op_timeout_s=300.0)
    counters, lines = seq.SeqCounters(defects_left=2), []
    with _patched(_apply([], fail=fail)):
        t(None, _WS(), _defect(), counters=counters, step=lines.append)
    assert counters.sliced and counters.skipped_why[T.SLICE] == 1
    assert {m.reason_key for m in t.log.records if m.op in ("split_aligned", "defeature") and not m.params} == {
        T.SLICE}


def test_a_full_timeout_is_not_a_slice_stop() -> None:
    t = T.Tournament(menu_of=lambda d: (("defeature",),), verify=lambda *a, **k: _local(), guard=_volume({}),
                     op_timeout_s=300.0)        # no deadline: the op had its whole own timeout
    counters = seq.SeqCounters()
    with _patched(_apply([], fail={"defeature": OpFailed("timeout", "killed")})):
        t(None, _WS(), _defect(), counters=counters, step=lambda line: None)
    assert not counters.sliced and counters.skipped_why["no_survivor"] == 1


def test_the_rule_seed_surviving_stops_a_tournament_before_its_queued_entrants() -> None:
    tried: list = []
    sequential = lambda n: T.Budget(T.MAX_ENTRANTS, 1, False, "test: one at a time")  # noqa: E731
    t = T.Tournament(menu_of=lambda d: (("split_aligned",), ("defeature",)), verify=lambda *a, **k: _local(),
                     guard=_volume({}), budget=sequential)
    with _patched(_apply(tried)):
        t(None, _WS(), _defect(), counters=seq.SeqCounters(), step=lambda line: None)
    assert tried == [(("split_aligned",), ())]
    assert [m.result for m in t.log.records][:2] == ["won", "not_run"]
    assert "rule seed survived" in t.log.records[1].reason


def test_a_sliced_defect_gets_one_second_turn_after_the_others_and_is_counted_once() -> None:
    """Round order a, b: a's slice runs out (its op times out under the slice), b is fixed, then a gets its second
    turn and is fixed: one found, one fixed, nothing skipped."""
    calls: list[str] = []

    def repair_defect(pool: Any, ws: Any, defect: Any, *, counters: Any, step: Any) -> Any:
        calls.append(f"{defect.id}:{counters.defects_left}")
        if len(calls) == 1:                      # a, first turn: its slice runs out
            counters.tally("skipped", defect)
            counters.skipped_why[seq.SLICE] += 1
            counters.sliced = True
            return ws
        counters.tally("fixed", defect)
        counters.kept_in_round += 1
        return ws

    from tests.unit.cad.repair_v4.test_b24_seq_arm import _V, _Judge

    from meshpipeline.cad.repair_v4.local_verify import Defect

    a = Defect("a", "mesher", "overlapping_facets", ("F1",), cls="close_faces")
    b = Defect("b", "mesher", "overlapping_facets", ("F2",), cls="close_faces")
    judge = _Judge()
    out = seq.repair_sequentially(None, _WS(), "o.step", judge=judge, sites_of=lambda *a_: [],
                                  start_verdict=_V([]), diagnose=lambda *a_: [a, b], repair_defect=repair_defect,
                                  max_rounds=1)
    assert calls == ["a:2", "b:1", "a:1"]
    c = out.counters
    assert c.fixed == {"overlapping_facets": 2} and not c.skipped and not c.skipped_why


def test_the_conformal_pass_stops_measuring_at_its_share() -> None:
    clock, lines = _Clock(), []
    p = C.ConformalPass(judge=None, original="o.step", clock=clock, budget_s=60.0)
    p._until = 60.0
    clock.t = 55.0                                         # under MIN_MEASURE_S left
    assert p._measure(None, SimpleNamespace(brep_path="x", part_diag_mm=1.0), 1.0, lines.append) is None
    assert "share of the wall is spent" in lines[0]


def test_the_diagnosis_pool_clips_timeouts_and_starts_nothing_past_its_share() -> None:
    clock, seen, spent = _Clock(), [], {}
    pool = SimpleNamespace(call=lambda fn, payload, *, timeout_s: seen.append((fn, timeout_s)) or "ok")
    dp = loop.DeadlinePool(pool, 50.0, clock, spent)
    assert dp.call("v5_pair_facts", {}, timeout_s=120.0) == "ok" and seen == [("v5_pair_facts", 50.0)]
    clock.t = 51.0
    got = dp.call("v5_pair_facts", {}, timeout_s=120.0)
    assert not got.ok and got.status == "timeout" and len(seen) == 1 and spent["skipped:v5_pair_facts"] == 1


def test_close_pair_facts_are_reused_only_where_no_op_touched_the_region() -> None:
    diff = SimpleNamespace(modified={"F9": ["F10"]}, generated={}, removed=[])
    before = SimpleNamespace(history=[], brep_path="/a.brep", ids={"F1": "", "F2": "", "F9": ""})
    after = SimpleNamespace(history=[diff], brep_path="/b.brep", ids={"F1": "", "F2": "", "F10": ""})
    cache: D.PairCache = {}
    D.store_pairs(cache, before, ("o", ("F1",), 1.0), [{"a": "F1", "b": "F2"}])
    assert D.cached_pairs(cache, after, ("o", ("F1",), 1.0), ["F1"]) == [{"a": "F1", "b": "F2"}]
    D.store_pairs(cache, before, ("o", ("F9",), 1.0), [{"a": "F9", "b": "F2"}])
    assert D.cached_pairs(cache, after, ("o", ("F9",), 1.0), ["F9"]) is None      # F9 was rewritten


def test_the_mesh_tier_runs_cheap_classes_first() -> None:
    from meshpipeline.cad.repair_v4.local_verify import Defect

    ds = [Defect(c, "mesher", "x", (), cls=c) for c in ("surface_crossing", "close_faces", "low_quality")]
    assert [d.cls for d in D.order(ds)] == ["low_quality", "close_faces", "surface_crossing"]
