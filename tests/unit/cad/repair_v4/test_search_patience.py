# Responsibility: Prove the search's patience scales with the holes the part presents (Task 46), that a round which
#                 admits a new state without a worse score counts as progress, and that neither can run a search past
#                 its budgets or keep a genuinely stuck one alive.
# Boundaries: hermetic; reuses Task 18's world (real Verdicts, real score, fake ops). The 3-hole acceptance test on the
#             real kernel is `tests/native/test_native_repair_v4_patience.py`.
from __future__ import annotations

from typing import Any

from tests.unit.cad.repair_v4.test_search_wiring import (  # noqa: F401  (world is an autouse fixture)
    BIG,
    Scripted,
    World,
    _q,
    cand,
    make_ws,
    run,
    world,
)

from meshpipeline.cad.repair_v4.search import Budget, open_site_count, scaled_patience
from meshpipeline.cad.repair_v4.types import Site

CLEAN_Q = 50


def _open_site(holes: int, *, total: bool = True) -> Any:
    def sites(pool: Any, ws: Any, verdict: Any) -> list[Site]:
        graph: dict = {"free_loops": [{"edges": [f"E{i}"], "span_mm": 1.0} for i in range(min(holes, 6))]}
        if total:
            graph["free_loops_total"] = holes
        return [Site(id="S@open", kind="open", entities=[], xyz=[], meshers_failing=[], dist_over_h=None,
                     graph=graph, view_png=None)]  # type: ignore[arg-type]
    return sites


def _chain(length: int) -> Scripted:
    """`length` fills that each admit a NEW state of exactly the parent's score, then the volume that cleans it."""
    # `step`: each fill is a DIFFERENT candidate (B20 prunes the same op+targets+params on one lineage)
    return Scripted(lambda s: [cand(CLEAN_Q if s.depth >= length else _q(s.ws), step=s.depth)])


def _stop(r: Any) -> dict:
    return next(e for e in r.trace.events if e["kind"] == "stop")


def test_open_site_count_is_the_uncapped_loop_count_and_never_below_the_open_sites() -> None:
    mk = lambda graph: Site(id="S", kind="open", entities=[], xyz=[], meshers_failing=[], dist_over_h=None,  # noqa: E731
                            graph=graph, view_png=None)  # type: ignore[arg-type]
    other = Site(id="S2", kind="invalid", entities=[], xyz=[], meshers_failing=[], dist_over_h=None,
                 graph={"free_loops_total": 9}, view_png=None)  # type: ignore[arg-type]
    assert open_site_count([]) == 0
    assert open_site_count([other]) == 0, "only an open site's loops are holes"
    assert open_site_count([mk({"free_loops_total": 11, "free_loops": [{}] * 6})]) == 11, "the cap of 6 must not hide holes"
    assert open_site_count([mk({"free_loops": [{}, {}]})]) == 2
    assert open_site_count([mk({}), mk({})]) == 2, "unreadable loops still leave one hole per open site"


def test_scaled_patience_is_max_of_three_and_holes_plus_two() -> None:
    assert [scaled_patience(n) for n in (0, 1, 2, 3, 5, 10)] == [3, 3, 4, 5, 7, 12]


def test_patience_is_settled_from_the_start_states_own_sites_and_recorded() -> None:
    r = run([_chain(0)], World(target=CLEAN_Q, sites_fn=_open_site(4)))
    ev = next(e for e in r.trace.events if e["kind"] == "patience")
    assert (ev["open_sites"], ev["patience"], ev["floor"]) == (4, 6, 3)


def test_a_fixed_patience_is_still_fixed_and_records_no_scaling() -> None:
    r = run([_chain(0)], World(target=CLEAN_Q, sites_fn=_open_site(40)), patience=2)
    assert not [e for e in r.trace.events if e["kind"] == "patience"]


def test_patience_grows_with_the_holes_so_a_multi_hole_chain_survives() -> None:
    """Five tied fills then a volume. Each fill is a new state with the parent's score. The best score never moves
    until the volume, so only the tie rule (and enough patience) lets the search arrive."""
    r = run([_chain(5)], World(target=CLEAN_Q, sites_fn=_open_site(5)))
    assert r.clean and r.reason == "clean"


def test_a_new_state_that_ties_the_score_is_progress() -> None:
    """One hole means patience 3, yet a chain of six tied fills still arrives: the rounds are progress, not flatness."""
    r = run([_chain(6)], World(target=CLEAN_Q, sites_fn=_open_site(1)))
    assert r.clean and r.reason == "clean"


def test_a_search_whose_new_states_only_get_worse_stops_on_patience_not_on_the_op_budget() -> None:
    """The guard that must be able to fail: every round admits a NEW state, but strictly worse than its parent. That
    is no progress. With a 1000-op budget the search must stop on patience after a handful of ops."""
    gens = [Scripted(lambda s: [cand(max(0, _q(s.ws) - 1))])]
    r = run(gens, World(target=CLEAN_Q, sites_fn=_open_site(2)), start=make_ws(30), budget=BIG)
    stop = _stop(r)
    assert r.reason == "patience" and stop["spent"]["ops"] <= 6, (r.reason, stop["spent"])


def test_a_search_that_only_finds_duplicates_does_not_run_to_the_op_budget() -> None:
    r = run([Scripted(lambda s: [cand(_q(s.ws), dup=True)])], World(target=CLEAN_Q, sites_fn=_open_site(2)),
            start=make_ws(0, fp="same"))
    assert r.reason in ("patience", "exhausted") and _stop(r)["spent"]["ops"] < 10


def test_new_tied_states_without_end_stop_on_the_op_budget_and_the_worst_case_is_the_op_count() -> None:
    """The backstop, stated as a test: tied states that go nowhere keep the search alive until the op budget cuts
    it off; patience cannot, by design (they are progress). So the worst case is `ops`, never unbounded."""
    r = run([Scripted(lambda s: [cand(_q(s.ws), step=s.depth)])], World(target=CLEAN_Q, sites_fn=_open_site(1)),
            budget=Budget(wall_s=1000, ops=15, model_calls=0, model_usd=0.0))
    assert r.reason == "ops" and _stop(r)["spent"]["ops"] == 15 and not r.clean


def test_new_tied_states_without_end_stop_on_the_wall_clock_too() -> None:
    ticks = iter(range(10_000))
    r = run([Scripted(lambda s: [cand(_q(s.ws), step=s.depth)])], World(target=CLEAN_Q, sites_fn=_open_site(1)),
            budget=Budget(wall_s=30, ops=10_000, model_calls=0, model_usd=0.0), clock=lambda: float(next(ticks)))
    assert r.reason == "wall" or "wall" in str(r.reason), r.reason


def test_scaled_patience_never_exceeds_what_the_op_budget_allows() -> None:
    """A pathological part with 500 loops asks for patience 502; a 6-op budget still ends the run in 6 ops."""
    r = run([Scripted(lambda s: [cand(max(0, _q(s.ws) - 1))])], World(target=CLEAN_Q, sites_fn=_open_site(500)),
            start=make_ws(30), budget=Budget(wall_s=1000, ops=6, model_calls=0, model_usd=0.0))
    assert next(e for e in r.trace.events if e["kind"] == "patience")["patience"] == 502
    assert r.reason == "ops" and _stop(r)["spent"]["ops"] == 6


def _dead_ends_then_the_way(siblings: int) -> Scripted:
    """The start proposes `siblings` tied states, the last of which is the way out. Every other one only proposes a
    strictly worse state, so expanding it is a flat round. This is the shape patience exists for even WITH the tie rule:
    the part presents many holes, and the search must be willing to try the dead ends first."""
    def script(s: Any) -> list:
        if s.depth == 0:
            return [cand(_q(s.ws), to="GOOD" if i == siblings - 1 else f"X{i}") for i in range(siblings)]
        return [cand(CLEAN_Q)] if "GOOD" in s.ws.ids else [cand(max(0, _q(s.ws) - 1))]
    return Scripted(script)


def test_patience_scales_so_dead_ends_before_the_way_out_do_not_end_the_search() -> None:
    r = run([_dead_ends_then_the_way(5)], World(target=CLEAN_Q, sites_fn=_open_site(5)), start=make_ws(30))
    assert r.clean and r.reason == "clean", (r.reason, _stop(r)["spent"])


def test_the_same_dead_ends_end_the_search_on_a_part_that_presents_few_holes() -> None:
    """The control: the identical search on a 1-hole part has patience 3, which four dead ends exceed. This is what
    makes the test above able to fail: it is red under a constant patience."""
    r = run([_dead_ends_then_the_way(5)], World(target=CLEAN_Q, sites_fn=_open_site(1)), start=make_ws(30))
    assert r.reason == "patience" and not r.clean, (r.reason, _stop(r)["spent"])


def test_a_state_that_only_ties_a_parent_worse_than_the_best_is_not_progress() -> None:
    """Found by `test_search.py::test_patience_stops_flat_search` on the first version of the rule, which compared a
    child with its PARENT: the child of a worse state that ties that worse state ran the search to the op budget. The
    reference is the best so far."""
    r = run([Scripted(lambda s: [cand(0, step=s.depth)])], World(target=CLEAN_Q, sites_fn=_open_site(1)),
            start=make_ws(5))
    assert r.reason == "patience" and _stop(r)["spent"]["ops"] <= 4, (r.reason, _stop(r)["spent"])
