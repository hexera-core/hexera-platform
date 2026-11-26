# Responsibility: Prove (1) of equal-scoring states the DEEPER is expanded first, deterministically, and a better score
#                 still beats a deeper worse one; (2) the deterministic sweep does not propose an op the state already
#                 rules out, proposes every one it cannot rule out, and never proposes the same call twice (Task 47).
# Boundaries: hermetic. Search world is Task 18's (real Verdicts, real score, fake ops). The sweep is the real
#             `DetGenerator`. That the topology predictor never contradicts the real `make_volume` is a NATIVE test
#             (`tests/native/test_native_repair_v4_waste.py`): this file only trusts the facts it is handed.
from __future__ import annotations

import time
from typing import Any

from tests.unit.cad.repair_v4.test_search_wiring import (  # noqa: F401  (world is an autouse fixture)
    BIG,
    Scripted,
    T18Op,
    World,
    _q,
    cand,
    make_ws,
    run,
    world,
)

from meshpipeline.cad.repair_v4 import search
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Workspace

# --- the tie-break -------------------------------------------------------------------------------------------------


def _expanded(r: Any) -> list[str]:
    return [e["state"] for e in r.trace.events if e["kind"] == "expand"]


def _two_branches(depth_two_q: int) -> Scripted:
    """S0 proposes two tied children (S1, S2). S1, when expanded, proposes one child (S3) scoring `depth_two_q`."""
    # `step` keeps each a DIFFERENT candidate: B20 drops a repeat of the same op, targets and params (in one proposal,
    # or already run on the lineage), and these are meant as distinct moves that happen to score the same
    def script(s: Any) -> list:
        if s.depth == 0:
            return [cand(10, step=1), cand(10, step=2)]
        if s.id == "S1":
            return [cand(depth_two_q, step=3)]
        return []
    return Scripted(script)


def test_of_two_equal_scores_the_deeper_state_is_expanded_first() -> None:
    """S3 (depth 2) ties S2 (depth 1). FIFO alone expands S2 first; the deeper tie must go first."""
    r = run([_two_branches(10)], World(target=99), start=make_ws(0))
    assert _expanded(r)[:4] == ["S0", "S1", "S3", "S2"], _expanded(r)


def test_equal_score_and_equal_depth_keep_proposal_order() -> None:
    r = run([_two_branches(10)], World(target=99), start=make_ws(0))
    assert _expanded(r).index("S1") < _expanded(r).index("S2")


def test_a_better_score_still_beats_a_deeper_worse_one() -> None:
    """Depth is only a tie-break. S3 is deeper than S2 but scores lower, so S2 is expanded before it."""
    r = run([_two_branches(7)], World(target=99), start=make_ws(0))
    order = _expanded(r)
    assert order.index("S2") < order.index("S3"), order


def test_score_orders_states_before_depth_does() -> None:
    """S1 = 10 and S2 = 12 are shallow; S3 (a child of S2) scores 11, so S2 > S3 > S1 whatever their depths."""
    def script(s: Any) -> list:
        if s.depth == 0:
            return [cand(10), cand(12)]
        if s.id == "S2":
            return [cand(11)]
        return []
    r = run([Scripted(script)], World(target=99), start=make_ws(0))
    assert _expanded(r)[:4] == ["S0", "S2", "S3", "S1"], _expanded(r)


class _Jitter(T18Op):
    """`t18_set` whose first candidates take LONGEST to finish, so completion order is the reverse of proposal order."""

    name = "t18_set"

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        time.sleep(float(params.get("slow", 0.0)) / 1000.0)
        return super().apply(pool, ws, targets, {k: v for k, v in params.items() if k != "slow"}, timeout_s=timeout_s)


def test_the_order_does_not_depend_on_which_worker_finishes_first(monkeypatch: Any) -> None:
    """Task 44's race, for the tie-break: children are pushed in PROPOSAL order, not completion order, so equal score
    and equal depth are ordered by the proposal's position, every time, however the workers interleave."""
    from meshpipeline.cad.repair_v4.ops import REGISTRY

    jitter = _Jitter()
    jitter.params_schema = {**T18Op.params_schema, "properties": {**T18Op.params_schema["properties"],
                                                                  "slow": {"type": "integer"}}}
    monkeypatch.setitem(REGISTRY, "t18_set", jitter)  # type: ignore[arg-type]

    def script(s: Any) -> list:
        if s.depth == 0:
            return [cand(10, slow=120), cand(10, slow=60), cand(10, slow=0)]
        return []
    seen = set()
    for _ in range(4):
        r = search.search(None, make_ws(0), "/fake/orig.step", [Scripted(script)], BIG, workers=4,
                          judge=World(target=99).judge())
        seen.add(tuple(_expanded(r)))
    assert seen == {("S0", "S1", "S2", "S3")}, seen


def test_a_deeper_tie_chain_is_followed_to_its_end_before_a_shallow_sibling() -> None:
    """The multi-op shape the tie-break exists for: every op ties; the repair needs `depth` of them in a row."""
    def script(s: Any) -> list:
        if s.depth >= 3:
            return [cand(50)]
        return [cand(_q(s.ws), step=2 * s.depth), cand(_q(s.ws), step=2 * s.depth + 1)]   # distinct moves (B20)
    r = run([Scripted(script)], World(target=50), start=make_ws(0))
    spent = next(e for e in r.trace.events if e["kind"] == "stop")["spent"]["ops"]
    assert r.clean and spent == 2 + 2 + 2 + 1, spent  # 3 expansions of 2 siblings, then the one that cleans
    # breadth-first would have expanded every depth-1 state and every depth-2 state before reaching depth 3
    assert spent < 2 + 4 + 8 + 1


def test_a_genuinely_stuck_search_still_stops_and_not_by_the_op_budget() -> None:
    """Depth-first on ties must not turn a dead end into a dive to the op budget: strictly worse states are never
    progress, so patience ends it long before 1000 ops."""
    r = run([Scripted(lambda s: [cand(max(0, _q(s.ws) - 1)), cand(max(0, _q(s.ws) - 2))])], World(target=99),
            start=make_ws(30), budget=BIG)
    spent = next(e for e in r.trace.events if e["kind"] == "stop")["spent"]["ops"]
    assert r.reason == "patience" and spent < 30, (r.reason, spent)


# --- the sweep proposes nothing the state already rules out ----------------------------------------------------------


def _site(kind: str, **graph: Any) -> Site:
    return Site(id=f"S@{kind}", kind=kind, entities=[], xyz=[], meshers_failing=[], dist_over_h=None,
                graph=dict(graph), view_png=None)  # type: ignore[arg-type]


LOOP = {"edges": ["E1", "E2", "E3", "E4"], "span_mm": 3.4}


def _open(volume: dict | None) -> Site:
    graph: dict = {"free_loops": [LOOP], "free_loops_total": 1, "diagonal_mm": 120.0}
    if volume is not None:
        graph["volume"] = volume
    return _site("open", **graph)


def _invalid() -> Site:
    return _site("invalid", hard_codes=["no_solids"])


def _propose(sites: list[Site], ids: dict | None = None) -> list:
    ws = Workspace(brep_path="/fake/a.brep", ids=ids or {"F1": "fp1"}, labels={}, history=[], original="/fake/o.brep")
    state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=sites, depth=0,
                        parent=None, via=None)
    return DetGenerator(diagonal=lambda w: 120.0).propose(state, 50)


def _ops(cands: list) -> list[str]:
    return [c.op for c in cands]


def test_a_shell_with_no_closed_subset_is_not_offered_to_make_volume() -> None:
    got = _propose([_open({"loose": False, "any": False, "free_edges": 12}), _invalid()])
    assert "make_volume" not in _ops(got)


def test_make_volume_is_proposed_once_when_nothing_rules_it_out() -> None:
    """The open site and the invalid/no_solids site both ask for the same make_volume; it is one call, not two."""
    got = _propose([_open({"loose": True, "any": True, "free_edges": 4}), _invalid()])
    assert _ops(got).count("make_volume") == 1


def test_make_volume_is_proposed_when_the_kernel_could_not_say() -> None:
    """The direction that loses repairs: no volume facts (the free-loop call failed) means unknown, so propose."""
    got = _propose([_open(None), _invalid()])
    assert _ops(got).count("make_volume") == 1 and _ops(got).count("sew") == 1


def test_make_volume_with_no_known_facts_at_all_is_proposed_from_the_invalid_site() -> None:
    """The final state of a multi-hole repair: the shell just closed, so there is no open site and no facts."""
    got = _propose([_invalid()])
    assert "make_volume" in _ops(got)


def test_explicit_target_faces_are_judged_against_every_face_not_only_the_loose_ones() -> None:
    """`loose` False says the faces outside a solid enclose nothing; targets may name faces inside a solid, which `any`
    covers. A target list with `any` True must not be suppressed on `loose` alone."""
    site = _site("invalid", hard_codes=["no_solids"])
    site.entities = ["F1", "F2"]
    got = _propose([_open({"loose": False, "any": True, "free_edges": 4}), site])
    targeted = [c for c in got if c.op == "make_volume" and c.targets]
    assert len(targeted) == 1, _ops(got)
    none_any = _propose([_open({"loose": False, "any": False, "free_edges": 4}), site])
    assert not [c for c in none_any if c.op == "make_volume" and c.targets]


def test_sew_is_proposed_when_free_edges_exist_even_if_the_kernel_will_find_no_partner_for_them() -> None:
    """Whether a free edge has a partner within the sew window is geometry the kernel discovers: left to it."""
    got = _propose([_open({"loose": False, "any": False, "free_edges": 12}), _invalid()])
    assert _ops(got).count("sew") == 1


def test_sew_is_not_proposed_when_there_is_no_free_edge_to_join() -> None:
    got = _propose([_open({"loose": True, "any": True, "free_edges": 0})])
    assert "sew" not in _ops(got)


def test_sew_is_not_proposed_on_a_state_that_carries_a_solid() -> None:
    got = _propose([_open(None)], ids={"F1": "fp1", "B1": "fpb"})
    assert "sew" not in _ops(got)
    assert "sew" in _ops(_propose([_open(None)], ids={"F1": "fp1"})), "no solid: the same site gets its sew"


def test_fills_orient_and_unknowns_are_never_suppressed_by_the_volume_facts() -> None:
    """Even a state that rules out make_volume and sew keeps its fill and its orient: neither is predicted."""
    got = _propose([_open({"loose": False, "any": False, "free_edges": 0}), _invalid()])
    # B19 orders the families by the site's kind (the invalid site prefers the shell family's orient); the claim is
    # that neither is suppressed, not their order
    assert sorted(_ops(got)) == ["fill_loop", "orient_and_close"], _ops(got)


def test_a_dropped_proposal_is_counted_and_on_the_record() -> None:
    gen = DetGenerator(diagonal=lambda w: 120.0)
    ws = Workspace(brep_path="/fake/a.brep", ids={"F1": "fp1"}, labels={}, history=[], original="/fake/o.brep")
    state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0),
                        sites=[_open({"loose": False, "any": False, "free_edges": 0}), _invalid()], depth=0,
                        parent=None, via=None)
    gen.propose(state, 50)
    assert gen.skipped["make_volume_without_a_closable_shell"] == 1
    assert gen.skipped["sew_without_free_edges"] == 1
    # B19: every family is offered at every kind, so the open and the invalid site now ask for more of the same shell
    # rows; each repeat is dropped and counted, never proposed twice
    assert gen.skipped["identical_to_an_earlier_candidate"] >= 2
    assert any("make_volume not proposed" in n for n in gen.notes)


def test_a_budget_of_k_is_spent_on_distinct_candidates() -> None:
    """Dedupe happens before the k cut, so a duplicate does not take a slot a real candidate could have had."""
    ws = Workspace(brep_path="/fake/a.brep", ids={"F1": "fp1"}, labels={}, history=[], original="/fake/o.brep")
    state = SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0),
                        sites=[_open({"loose": True, "any": True, "free_edges": 4}), _invalid()], depth=0,
                        parent=None, via=None)
    got = DetGenerator(diagonal=lambda w: 120.0).propose(state, 4)
    # four DISTINCT ops in the four slots (B19/B20 decide their order)
    assert sorted(_ops(got)) == ["fill_loop", "make_volume", "orient_and_close", "sew"], _ops(got)

