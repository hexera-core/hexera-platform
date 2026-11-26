"""CRS v5 r2 fix I on fakes: the fallback searches from search_ai's OWN start first (the class state first only where
the loop made an invalid part valid), the second start gets what the first left, the best state is kept; and an
overlap cluster opens a bounded number of pair tournaments, none once the class loop's wall has ended."""

from __future__ import annotations

from typing import Any

from tests.unit.cad.repair_v4.test_b24_seq_arm import _local, _patched
from tests.unit.cad.repair_v5.test_tournament import _WS, _apply, _defect, _volume

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import tournament as T


class _V:
    def __init__(self, valid: int, passes: int, clean: bool = False) -> None:
        self.valid, self.passes, self.clean = valid, passes, clean


class _Judge:
    @staticmethod
    def score(v: Any) -> tuple:
        return (v.valid, v.passes, 0.0, 0.0, 0)


class _Result:
    def __init__(self, ws: Any, v: _V, *, changed: bool = True) -> None:
        self.best, self.best_verdict, self.changed = ws, v, changed
        self.clean, self.reason = v.clean, "clean" if v.clean else "wall"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


START, CLASS = _WS(0, "start"), _WS(3, "class")


def test_search_ais_own_start_goes_first_unless_the_class_loop_made_the_part_valid() -> None:
    # 0daa964f7139 (round-2 wave): the class state had MORE panel passes (3 vs 1) but was no more valid; it lost
    starts = loop.fallback_starts(_Judge(), START, _V(1, 1), CLASS, _V(1, 3))
    assert [name for name, _, _ in starts] == ["start", "class-repaired"]
    starts = loop.fallback_starts(_Judge(), START, _V(0, 0), CLASS, _V(1, 0))
    assert [name for name, _, _ in starts] == ["class-repaired", "start"]
    # a loop that kept nothing: one start, the same one search_ai has
    assert [n for n, _, _ in loop.fallback_starts(_Judge(), START, _V(1, 1), START, _V(1, 1))] == ["start"]


def test_a_clean_first_search_ends_the_fallback_and_its_state_is_kept() -> None:
    calls: list = []

    def fallback(ws: Any, v: Any, left: float) -> Any:
        calls.append((ws.brep_path, left))
        return _Result(_WS(4, "fb"), _V(1, 4, clean=True))

    got, best, verdict, improved, why = loop._run_fallback(fallback, _Judge(), START, _V(1, 1), CLASS, _V(1, 3),
                                                           600.0, _Clock())
    assert calls == [("/fake/start.brep", 600.0)]          # search_ai's start, with search_ai's whole wall
    assert improved and verdict.clean and best.brep_path == "/fake/fb.brep"
    assert why.startswith("search_ai from the start state with 600 s: clean") and "not run" not in why


def test_the_second_start_gets_only_what_the_first_left_and_the_best_of_all_is_kept() -> None:
    clock, calls = _Clock(), []

    def fallback(ws: Any, v: Any, left: float) -> Any:
        calls.append((ws.brep_path, left))
        clock.t += 250.0                                        # the first stops on ops after 250 s
        if ws is START:
            return _Result(_WS(2, "from_start"), _V(1, 2))      # worse than the class state: not kept
        return _Result(_WS(4, "from_class"), _V(1, 4, clean=True))

    got, best, verdict, improved, why = loop._run_fallback(fallback, _Judge(), START, _V(1, 1), CLASS, _V(1, 3),
                                                           600.0, clock)
    assert calls == [("/fake/start.brep", 600.0), ("/fake/class.brep", 350.0)]
    assert improved and best.brep_path == "/fake/from_class.brep"
    assert "search_ai from the class-repaired state with 350 s: clean" in why


def test_a_first_search_that_used_the_wall_leaves_no_second_and_a_worse_result_keeps_the_class_state() -> None:
    clock = _Clock()

    def fallback(ws: Any, v: Any, left: float) -> Any:
        clock.t += left - 10.0
        return _Result(_WS(2, "from_start"), _V(1, 2))

    got, best, verdict, improved, why = loop._run_fallback(fallback, _Judge(), START, _V(1, 1), CLASS, _V(1, 3),
                                                           600.0, clock)
    assert not improved and best is CLASS and verdict.passes == 3
    assert "the class-repaired start not run: 10 s left" in why and "no better than the class loop" in why


def _cluster(n: int) -> Any:
    pairs = tuple((f"B{k}", f"B{k + 1}") for k in range(1, n + 1))
    return _defect("body_overlap", "overlap_self_intersection", "validity", entities=tuple({b for p in pairs
                                                                                            for b in p}),
                   pairs=pairs, measurement={"clash": {"a": "B1", "b": "B2", "dist_mm": 0.3}})


def test_a_cluster_opens_no_pair_tournament_once_the_class_loops_wall_has_ended() -> None:
    """f027b94e6e67 (round-2 wave): 326 pair tournaments, 323 of them with no entrant run - the wall had ended."""
    clock = _Clock()

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, **kw: Any) -> Any:
        clock.t += 60.0                                        # each pair's op uses a minute of a 100 s loop wall
        return _apply([])(pool, ws, repair, defect, **kw)

    t = T.Tournament(menu_of=lambda d: (("fuse",),), verify=lambda *a, **k: _local(gone=False),
                     guard=_volume({}), clock=clock, deadline=100.0)
    counters, lines = seq.SeqCounters(defects_left=1), []
    with _patched(apply):
        t(None, _WS(), _cluster(50), counters=counters, step=lines.append)
    assert t._games == 2 and counters.pairs_skipped == 50
    assert counters.skipped_why[T.SLICE] == 48 + 1          # the 48 not played, and the 2nd pair cut at the wall
    assert any("48 of 50 pairs not played: the class loop's wall has ended" in line for line in lines)


def test_a_cluster_plays_at_most_its_cap_of_pair_tournaments_per_round() -> None:
    t = T.Tournament(menu_of=lambda d: (("fuse",),), verify=lambda *a, **k: _local(gone=False),
                     guard=_volume({}))
    counters, lines = seq.SeqCounters(), []
    with _patched(_apply([])):
        t(None, _WS(), _cluster(40), counters=counters, step=lines.append)
    assert t._games == T.MAX_CLUSTER_PAIRS
    assert counters.skipped_why[T.CLUSTER_CAP] == 40 - T.MAX_CLUSTER_PAIRS
