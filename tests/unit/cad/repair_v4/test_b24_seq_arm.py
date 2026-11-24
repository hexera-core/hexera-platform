# Responsibility: B24's sequential arm - the defect ordering, the rule table and its derivation from lane D's
#                 oracle rows, parameters read off the measurement, the pick (table vs AI), and the loop's rules.
# Boundaries: no OCCT and no pool. The judge, the sites and lane C's `verify_local` are injected.
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.types import Site


def _site(sid: str, kind: str = "invalid", *, entities: list[str] | None = None, graph: dict | None = None,
          failures: list[dict] | None = None) -> Site:
    g = dict(graph or {})
    if failures is not None:
        g["failures"] = failures
    return Site(id=sid, kind=kind, entities=entities or ["F1"], xyz=[], meshers_failing=[], dist_over_h=None,
                graph=g, view_png=None)


class _Finding:
    def __init__(self, code: str, entities: list[str]) -> None:
        self.code, self.entities = code, entities


class _Valid:
    def __init__(self, hard: list[_Finding]) -> None:
        self.hard, self.ok = hard, not hard


class _V:
    def __init__(self, hard: list[_Finding] | None = None, clean: bool = False) -> None:
        self.valid, self.clean = _Valid(hard or []), clean


def _local(gone: bool = True, new_hard: list[dict] | None = None, seconds: float = 0.0) -> Any:
    from meshpipeline.cad.repair_v4.local_verify import LocalResult
    return LocalResult(gone=gone, new_hard=new_hard or [], detail={}, seconds=seconds)


# --- ordering ------------------------------------------------------------------------------------------------

def test_validity_defects_are_repaired_before_any_mesher_defect_and_in_the_rulings_order() -> None:
    sites = [_site("S1", entities=["B1"], failures=[{"mesher": "gmsh_default", "error_kind": "overlapping_facets"}]),
             _site("S2", entities=["B2"])]
    verdict = _V([_Finding("open_shell", ["B2"]), _Finding("non_positive_volume", ["B1"])])
    got = [(d.kind, d.code) for d in seq.defects_of(verdict, sites)]
    assert got == [("validity", "non_positive_volume"), ("validity", "open_shell"), ("mesher", "overlapping_facets")]


def test_a_code_no_ruling_names_sorts_LAST_so_it_cannot_pre_empt_a_measured_one() -> None:
    sites = [_site("S1", entities=["B1"])]
    verdict = _V([_Finding("something_nobody_ranked", ["B1"]), _Finding("open_shell", ["B1"])])
    assert [d.code for d in seq.defects_of(verdict, sites)] == ["open_shell", "something_nobody_ranked"]


def test_one_defect_per_kind_and_site_however_many_meshers_report_it() -> None:
    site = _site("S1", failures=[{"mesher": "gmsh_default", "error_kind": "overlapping_facets"},
                                 {"mesher": "gmsh_prod", "error_kind": "overlapping_facets"},
                                 {"mesher": "valid", "error_kind": "ignored"}])
    defects = seq.defects_of(_V(), [site])
    assert [d.code for d in defects] == ["overlapping_facets"]
    assert defects[0].member == "gmsh_default"      # the member lane C's local check re-runs


# --- body_overlap clusters (owner note 1) --------------------------------------------------------------------

def test_a_CHAIN_of_overlapping_bodies_is_ONE_defect_with_its_pairs() -> None:
    """B1-B2 and B2-B3 interpenetrate: one cluster, two pairs. Repairing a pair changes what the next is
    measured on, and a cut aimed at one can push a clash onto the third body."""
    site = _site("S1", entities=["B1", "B2", "B3"],
                 graph={"clashes": [{"a": "B1", "b": "B2", "dist_mm": 0.4},
                                    {"a": "B2", "b": "B3", "dist_mm": 0.1}]})
    verdict = _V([_Finding("body_overlap", ["B1", "B2"]), _Finding("body_overlap", ["B2", "B3"])])
    [defect] = seq.defects_of(verdict, [site])
    assert defect.code == "body_overlap" and defect.kind == "validity"
    assert defect.entities == ("B1", "B2", "B3")
    assert defect.pairs == (("B1", "B2"), ("B2", "B3"))


def test_two_SEPARATE_overlaps_are_two_defects() -> None:
    site = _site("S1", entities=["B1", "B2", "B3", "B4"])
    verdict = _V([_Finding("body_overlap", ["B1", "B2"]), _Finding("body_overlap", ["B3", "B4"])])
    defects = seq.defects_of(verdict, [site])
    assert [d.pairs for d in defects] == [(("B1", "B2"),), (("B3", "B4"),)]


def test_a_pair_is_repaired_with_ITS_own_measured_clash_not_the_clusters_worst() -> None:
    site = _site("S1", entities=["B1", "B2", "B3"],
                 graph={"clashes": [{"a": "B1", "b": "B2", "dist_mm": 0.4},
                                    {"a": "B2", "b": "B3", "dist_mm": 0.1}]})
    [defect] = seq.defects_of(_V([_Finding("body_overlap", ["B1", "B2"]),
                                  _Finding("body_overlap", ["B2", "B3"])]), [site])
    _t, mild = seq.params_for("fuse", defect, pair=("B2", "B3"))
    _t, worst = seq.params_for("fuse", defect, pair=("B1", "B2"))
    assert mild["fuzzy_mm"] == pytest.approx(seq.FUZZY_MARGIN * 0.1)
    assert worst["fuzzy_mm"] == pytest.approx(seq.FUZZY_MARGIN * 0.4)
# --- the rule table ------------------------------------------------------------------------------------------

def test_the_table_is_what_lane_Ds_rows_derive_ranked_by_DISTINCT_FILES() -> None:
    """Two files beat four winning paths in one file - LEAD-3's ruling, and B21c's reason for it."""
    rows = [
        {"label_family": "open_shell", "path": ["fuse"], "sha256": "a" * 64},
        {"label_family": "open_shell", "path": ["fuse"], "sha256": "a" * 64},
        {"label_family": "open_shell", "path": ["fuse"], "sha256": "a" * 64},
        {"label_family": "open_shell", "path": ["fuse"], "sha256": "a" * 64},
        {"label_family": "open_shell", "path": ["defeature", "run_stage"], "sha256": "b" * 64},
        {"label_family": "open_shell", "path": ["defeature", "run_stage"], "sha256": "c" * 64},
    ]
    assert seq.rule_table_from_winners(rows)["open_shell"][0] == ("defeature", "run_stage")


def test_a_gamed_win_and_one_an_arm_already_solves_are_not_in_the_table() -> None:
    rows = [{"label_family": "open_shell", "path": ["defeature"], "sha256": "a" * 64, "gaming": True},
            {"label_family": "open_shell", "path": ["fuse"], "sha256": "b" * 64, "solved_by_v4_arm": True}]
    assert seq.rule_table_from_winners(rows) == {}


# --- parameters ----------------------------------------------------------------------------------------------

def test_remove_smalls_width_comes_from_the_measured_face_not_a_ladder() -> None:
    defect = _defect("open_shell", entities=("F7",),
                     measurement={"narrowest_face": {"id": "F7", "width_mm": 0.4, "length_mm": 11.0}})
    targets, params = seq.params_for("remove_small", defect)
    assert targets == ["F7"]
    assert params["max_width_mm"] == pytest.approx(seq.REMOVE_SMALL_MARGIN * 11.4)


def test_a_fuzzy_value_comes_from_the_measured_clash() -> None:
    defect = _defect("body_overlap", entities=("B1", "B2"),
                     measurement={"clash": {"a": "B1", "b": "B2", "dist_mm": 0.25}})
    _t, params = seq.params_for("fuse", defect)
    assert params["fuzzy_mm"] == pytest.approx(seq.FUZZY_MARGIN * 0.25)


def test_a_defect_with_no_measurement_gets_NO_repair_rather_than_a_guessed_parameter() -> None:
    defect = _defect("open_shell", entities=())
    with pytest.raises(seq.NoParameters):
        seq.params_for("remove_small", defect)
    with pytest.raises(seq.NoParameters):
        seq.params_for("defeature", defect)


# --- the pick ------------------------------------------------------------------------------------------------

def _defect(code: str, kind: str = "validity", *, entities: tuple = ("F1",), **kw: Any) -> seq.Defect:
    return seq.Defect(id="S1", kind=kind, code=code, entities=entities, **kw)


def test_the_table_decides_where_it_has_a_leader_and_the_model_is_not_asked() -> None:
    asked: list[Any] = []
    pick = seq.pick_repair(_defect("edge_through_face"), ai_pick=lambda d, c: asked.append(d) or ("fuse",))
    assert pick.by == "table" and pick.repair == ("defeature",) and asked == []


def test_the_model_decides_only_on_a_TIE_and_only_among_the_tables_own_candidates() -> None:
    pick = seq.pick_repair(_defect("body_overlap"), ai_pick=lambda d, c: ("fuse",))
    assert pick.by == "ai" and pick.repair == ("fuse",)
    # an answer that was not offered is ignored and the table's order stands
    other = seq.pick_repair(_defect("body_overlap"), ai_pick=lambda d, c: ("invent_something",))
    assert other.by == "table" and other.repair == ("conformalise",)


def test_an_empty_table_and_no_model_picks_NOTHING_rather_than_anything() -> None:
    pick = seq.pick_repair(_defect("a_kind_nobody_has_measured"))
    assert pick.repair is None and pick.by == "none"




# --- the loop ------------------------------------------------------------------------------------------------

class _Judge:
    """A fake judge over one integer: `labels['q']` is the panel-pass count, clean at 4."""

    def __init__(self) -> None:
        self.full_calls = 0

    def full(self, pool: Any, ws: Any, original: str) -> Any:
        self.full_calls += 1
        v = _V(clean=int(ws.labels.get("q", 0)) >= 4)
        v.q = int(ws.labels.get("q", 0))
        return v

    @staticmethod
    def score(v: Any) -> tuple:
        return (1, getattr(v, "q", 0), 0.0, 0.0, 0)


class _WS:
    def __init__(self, q: int = 0, name: str = "w0") -> None:
        self.labels, self.brep_path, self.original, self.history = {"q": q}, f"/fake/{name}.brep", "/fake/o.brep", []

    def fork(self) -> _WS:
        return self


@contextmanager
def _patched(apply: Any, relocate: Any = None) -> Any:
    """`seq.apply_repair` replaced (the loop's rules are what these tests are about, not the op layer), and
    `relocate` too: carrying ids across ops is lane C's, and these fakes have no real workspace to carry."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(seq, "apply_repair", apply)
        mp.setattr(seq, "relocate", relocate or (lambda pool, before, after, defect: defect))
        yield mp


def _bump(by: int = 1, name: str = "up") -> Any:
    """A repair that always applies and improves the part by `by`, carrying the REAL derived parameters so the
    trace line under test is the line the arm would really write."""
    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, pair: Any = None,
              run_stage: Any = None) -> Any:
        params = []
        for op in repair:
            try:
                params.append(seq.params_for(op, defect, pair=pair)[1])
            except seq.NoParameters:
                params.append({})
        return seq._Applied(_WS(int(ws.labels["q"]) + by, name=name), [], ("B1",), params)
    return apply


def _mesher_site() -> Site:
    return _site("S1", kind="invalid", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": "edge_through_face"}],
                 graph={"nodes": [{"id": "F1", "area": 4.4, "min_width": 0.4}], "mesher_faces": ["F1"]})


def _run(site: Site | None = None, **kw: Any) -> tuple:
    judge = _Judge()
    start = _WS(0)
    out = seq.repair_sequentially(None, start, "o.step", judge=judge,
                                  sites_of=lambda pool, ws, v: [site or _mesher_site()],
                                  start_verdict=judge.full(None, start, "o.step"), **kw)
    return judge, out


def test_TWO_defects_in_a_round_cost_ONE_full_panel_between_them() -> None:
    """The arm's whole claim, and the headline number: FULL CHECKS PER FILE."""
    site = _site("S1", kind="invalid", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": "edge_through_face"},
                           {"mesher": "gmsh_prod", "error_kind": "no_volume"}],
                 graph={"nodes": [{"id": "F1", "area": 4.4, "min_width": 0.4}], "mesher_faces": ["F1"]})
    with _patched(_bump(2)):
        _judge, out = _run(site, verify=lambda *a, **k: _local(seconds=0.25))
    assert sorted(out.counters.fixed) == ["edge_through_face", "no_volume"]
    assert out.counters.local_checks == 2 and out.counters.local_s == 0.5
    assert out.counters.full_checks == 1 and out.counters.rounds == 1
    assert out.verdict.clean and out.best.labels["q"] == 4


def test_a_local_pass_the_whole_part_panel_does_not_confirm_keeps_the_BETTER_state() -> None:
    with _patched(_bump(0, name="worse")):
        _judge, out = _run(verify=lambda *a, **k: _local())
    assert out.counters.rounds == 1 and out.counters.full_checks == 1
    assert out.best.brep_path == "/fake/w0.brep"


def test_the_arm_stops_at_three_rounds_however_well_it_is_going() -> None:
    with _patched(_bump(1)):
        _judge, out = _run(verify=lambda *a, **k: _local())
    assert out.counters.rounds == 3 and out.counters.full_checks == 3
    assert out.best.labels["q"] == 3 and not out.verdict.clean


def test_a_round_that_keeps_nothing_stops_and_pays_NO_panel() -> None:
    with _patched(_bump(1)):
        _judge, out = _run(verify=lambda *a, **k: _local(gone=False))
    assert out.counters.full_checks == 0 and out.counters.rounds == 1
    assert out.rounds[0].full_check == "not run: the round kept nothing"


def test_a_repair_that_breaks_something_else_is_NOT_kept() -> None:
    with _patched(_bump(1)):
        _judge, out = _run(verify=lambda *a, **k: _local(gone=True, new_hard=[{"code": "open_shell",
                                                                              "entities": ["B3"]}]))
    assert out.counters.fixed == {} and out.counters.skipped["edge_through_face"] == 1
    assert "new defect: open_shell on B3" in out.rounds[0].steps[-1]


def test_a_check_that_could_not_be_had_is_not_a_failed_repair_and_stops_the_alternatives() -> None:
    """`CheckerUnavailable` is infrastructure. The row must say the check could not be had, and the arm must not
    apply two more repairs that nothing will check either."""
    tried: list[Any] = []

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, pair: Any = None,
              run_stage: Any = None) -> Any:
        tried.append(repair)
        return seq._Applied(_WS(1), [], ("B1",), [{}])

    def dead(*a: Any, **k: Any) -> Any:
        raise seq.CheckerUnavailable("the worker died")

    with _patched(apply):
        _judge, out = _run(verify=dead)
    assert len(tried) == 1
    assert out.counters.skipped_why["local_check_unavailable"] == 1 and out.counters.local_checks == 0
    assert "NOT CHECKED: CheckerUnavailable" in out.rounds[0].steps[-1]


def test_a_measured_local_FAILURE_moves_on_to_the_next_alternative() -> None:
    tried: list[Any] = []

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, pair: Any = None,
              run_stage: Any = None) -> Any:
        tried.append(repair)
        return seq._Applied(_WS(1), [], ("B1",), [{}])

    with _patched(apply):
        _judge, out = _run(verify=lambda *a, **k: _local(gone=False))
    assert tried == [("defeature",), ("conformalise",), ("defeature", "defeature")]
    assert out.counters.local_checks == 3 and out.counters.skipped["edge_through_face"] == 1


# --- clusters, pair by pair (owner note 1) -------------------------------------------------------------------

def _cluster_site() -> Site:
    return _site("S1", kind="overlap", entities=["B1", "B2", "B3"],
                 graph={"clashes": [{"a": "B1", "b": "B2", "dist_mm": 0.4},
                                    {"a": "B2", "b": "B3", "dist_mm": 0.1}]})


def _cluster_run(verify: Any) -> Any:
    judge = _Judge()
    start = _WS(0)
    verdict = _V([_Finding("body_overlap", ["B1", "B2"]), _Finding("body_overlap", ["B2", "B3"])])
    verdict.q = 0
    return seq.repair_sequentially(None, start, "o.step", judge=judge,
                                   sites_of=lambda pool, ws, v: [_cluster_site()], start_verdict=verdict,
                                   verify=verify)


def test_one_bad_pair_is_skipped_WITHOUT_blocking_the_rest_of_the_cluster() -> None:
    seen: list[Any] = []

    def verify(pool: Any, before: Any, after: Any, defect: Any, touched: Any = None, *, pair: Any = None,
               timeout_s: float = 0.0) -> Any:
        seen.append(pair)
        return _local(gone=pair != ("B1", "B2"))      # the first pair cannot be repaired, the second can

    with _patched(_bump(1)):
        out = _cluster_run(verify)
    # every pair was attempted, and each was checked on its own
    assert [p for p in seen if p is not None] and ("B2", "B3") in seen
    assert out.counters.pairs_fixed == 1 and out.counters.pairs_skipped == 1
    # the cluster did not end repaired, and the row says so rather than counting a half-fixed part as fixed
    assert out.counters.fixed == {} and out.counters.skipped["body_overlap"] == 1
    assert "cluster partly repaired: 1 of 2 pairs" in out.rounds[0].steps[-1]


def test_a_cluster_whose_every_pair_goes_counts_as_ONE_fixed_defect() -> None:
    with _patched(_bump(2)):
        out = _cluster_run(lambda *a, **k: _local())
    assert out.counters.pairs_fixed == 2 and out.counters.pairs_skipped == 0
    assert out.counters.fixed == {"body_overlap": 1}


# --- the readable trace (owner note 2) -----------------------------------------------------------------------

def test_every_step_is_one_readable_line_defect_then_repair_then_local_result() -> None:
    with _patched(_bump(4, name="clean")):
        _judge, out = _run(verify=lambda *a, **k: _local(seconds=1.2))
    [line] = out.rounds[0].steps
    assert line == ("edge_through_face on F1 (narrowest face F1 0.4 mm, member gmsh_default) -> "
                    "defeature{source=located} [table: the top rule for 'edge_through_face'] -> gone (1.2 s)")
    assert out.lines()[0] == "round 1:"
    assert out.lines()[-1].startswith("  full check: clean, score (1, 4, 0.0, 0.0, 0), improved")


def test_the_line_says_WHICH_pair_of_a_cluster_and_its_own_measurement() -> None:
    with _patched(_bump(2)):
        out = _cluster_run(lambda *a, **k: _local(seconds=0.5))
    assert out.rounds[0].steps[0].startswith("body_overlap on B1+B2 (clash 0.4 mm) -> conformalise{")
    assert out.rounds[0].steps[1].startswith("body_overlap on B2+B3 (clash 0.1 mm) -> conformalise{")
    assert "fuzzy_mm=0.11" in out.rounds[0].steps[1]       # the pair's OWN clash, not the cluster's worst


def test_an_INCONCLUSIVE_verdict_stops_the_arm_and_says_so_rather_than_crashing() -> None:
    """Found by the first end-to-end run on a real part: this host has no netgen, so the panel came back
    inconclusive and `judge.score` refused it (by design) - and the arm crashed. An inconclusive verdict says
    nothing about the file, so it can neither rank a state nor end a round: the arm stops and the row counts it."""
    class _Inconclusive(_Judge):
        def score(self, v: Any) -> tuple:                      # type: ignore[override]
            from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
            raise InconclusiveVerdict("a panel member failed for pool reasons")

    judge = _Inconclusive()
    start = _WS(0)
    out = seq.repair_sequentially(None, start, "o.step", judge=judge,
                                  sites_of=lambda pool, ws, v: [_mesher_site()],
                                  start_verdict=judge.full(None, start, "o.step"))
    assert out.best is start and out.rounds == []
    assert out.counters.inconclusive_verdicts == 1
    assert out.counters.skipped_why["start_verdict_inconclusive"] == 1


def test_a_defect_an_earlier_repair_took_WITH_IT_is_resolved_not_skipped() -> None:
    """Ops rename faces, so a defect diagnosed earlier in the round may no longer be locatable. The design's
    reading: re-check it, and if nothing of it is left, it was resolved by the earlier repair - not a failure
    and not a repair of its own. (The first end-to-end run hit the other half of this: the NEXT defect was
    refused for naming an id `remove_small` had replaced.)"""
    site = _site("S1", kind="invalid", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": "edge_through_face"},
                           {"mesher": "gmsh_prod", "error_kind": "no_volume"}],
                 graph={"nodes": [{"id": "F1", "area": 4.4, "min_width": 0.4}], "mesher_faces": ["F1"]})
    judge = _Judge()
    start = _WS(0)
    with _patched(_bump(2), relocate=lambda pool, before, after, defect: None):
        out = seq.repair_sequentially(None, start, "o.step", judge=judge, sites_of=lambda pool, ws, v: [site],
                                      start_verdict=judge.full(None, start, "o.step"), max_rounds=1,
                                      verify=lambda *a, **k: _local())
    # the first defect was repaired; the second could not be located afterwards and is RESOLVED, not skipped
    assert out.counters.fixed == {"edge_through_face": 1}
    assert out.counters.resolved_by_earlier_repair == 1
    assert out.counters.skipped == {}
    assert "resolved_by_earlier_repair" in out.rounds[0].steps[-1]


def test_a_parameter_is_never_derived_from_an_id_the_workspace_no_longer_has() -> None:
    """The second end-to-end run's remaining bug: a measurement taken on the diagnosis state still named F11
    after `remove_small` replaced it, and `split_by_proximity` was refused for naming it. Derived parameters are
    filtered to the LIVE ids, and a defect with nothing live left is skipped rather than guessed at."""
    defect = _defect("low_quality", entities=("F11", "F12"),
                     measurement={"diagonal_mm": 250.0, "clash_by_pair": {"F11|F12": 0.4}})
    # with both ids live the pairs are handed over as measured
    _t, params = seq.params_for("split_by_proximity", defect, live={"F11", "F12"})
    assert params["face_pairs"] == [["F11", "F12"]]
    # with F11 replaced, the pair is dropped - and with nothing left there is no repair, not a guess
    with pytest.raises(seq.NoParameters, match="still live"):
        seq.params_for("split_by_proximity", defect, live={"F12", "F16"})
    with pytest.raises(seq.NoParameters, match="live any more"):
        seq.params_for("defeature", defect, live={"F16"})


def test_a_face_op_on_a_defect_that_re_located_onto_a_BODY_says_so() -> None:
    defect = _defect("low_quality", entities=("B1",))
    with pytest.raises(seq.NoParameters, match="re-located onto B1"):
        seq.params_for("reapproximate", defect, live={"B1"})


# --- B24 fix: layer 1 names solids, the workspace names bodies ------------------------------------------------

class _WSIds:
    """The least workspace `body_ids_of` reads: stable id -> that entity's index in its kind's map."""

    def __init__(self, indices: dict[str, int]) -> None:
        self.indices = indices
        self.ids = dict.fromkeys(indices, "fp")
        self.labels, self.brep_path, self.original, self.history = {}, "/fake/w.brep", "/fake/o.brep", []


def test_a_layer1_solid_name_is_translated_to_the_workspace_BODY_id() -> None:
    """The wave-202611242007 bug: `valid_kernel` emits S1, S4 over its own solid list while the workspace names
    bodies B..., so every body defect was built with ids the workspace had never heard of and B26's live-id
    filter refused them - 18 of 52 steps, none of them after a kept repair."""
    ws = _WSIds({"B7": 1, "B9": 2, "B4": 3, "B2": 4, "F1": 1})
    mapped, unmapped = seq.body_ids_of(ws, ("S1", "S4"))
    assert mapped == ["B7", "B2"] and unmapped == []
    # a workspace id passes through untouched, whatever its kind
    assert seq.body_ids_of(ws, ("B9", "F1")) == (["B9", "F1"], [])


def test_a_layer1_name_the_workspace_cannot_place_is_reported_NOT_guessed() -> None:
    """A wrong body id would aim a cut at the wrong solid, which is worse than skipping the defect."""
    ws = _WSIds({"B7": 1})
    assert seq.body_ids_of(ws, ("S1", "S4", "Sx")) == (["B7"], ["S4", "Sx"])


def test_body_defects_are_built_with_workspace_ids_when_the_workspace_is_given() -> None:
    ws = _WSIds({"B7": 1, "B2": 4})
    site = _site("S1", kind="overlap", entities=["B7", "B2"])
    verdict = _V([_Finding("body_overlap", ["S1", "S4"])])
    [defect] = seq.defects_of(verdict, [site], ws)
    assert defect.entities == ("B2", "B7") and defect.pairs == (("B2", "B7"),)


def test_an_unmappable_body_defect_is_skipped_with_its_OWN_reason() -> None:
    """Never again indistinguishable from "the ids died in an earlier repair"."""
    ws = _WSIds({"B7": 1})
    site = _site("S1", kind="invalid", entities=["B7"])
    verdict = _V([_Finding("open_shell", ["S9"])])
    [defect] = seq.defects_of(verdict, [site], ws)
    assert defect.entities == () and defect.measurement["unmapped_layer1_entities"] == ["S9"]

    judge = _Judge()
    start = _WS(0)
    with _patched(_bump(1)):
        out = seq.repair_sequentially(None, start, "o.step", judge=judge,
                                      sites_of=lambda pool, w, v: [site], start_verdict=_V([_Finding("open_shell",
                                                                                                     ["S9"])]),
                                      verify=lambda *a, **k: _local())
    assert out.counters.skipped_why["layer1_entity_unmapped"] == 1
    assert "could not be mapped to a body" in out.rounds[0].steps[0]


# --- B24 fix: a removal is sized only from a face that is actually a sliver -----------------------------------

def test_a_removal_is_NOT_sized_from_a_face_that_is_not_narrow() -> None:
    """Wave 202611242007 derived limits of 1211 mm and 974 mm from `area/width` on big faces and the op declined
    all 13 ("nothing at or under X mm to remove"). The det sweep's margin rule only ever applied to slivers."""
    wide = _defect("overlapping_facets", entities=("F7",),
                   measurement={"narrowest_face": {"id": "F7", "width_mm": 40.0, "length_mm": 120.0}})
    with pytest.raises(seq.NoParameters, match="nothing here is a sliver"):
        seq.params_for("remove_small", wide)


def test_a_removal_limit_LARGER_THAN_THE_PART_is_refused_as_a_derivation_bug() -> None:
    huge = _defect("overlapping_facets", entities=("F7",),
                   measurement={"diagonal_mm": 250.0,
                                "narrowest_face": {"id": "F7", "width_mm": 0.5, "length_mm": 1100.0}})
    with pytest.raises(seq.NoParameters, match="larger than the part"):
        seq.params_for("remove_small", huge)
    # and a real sliver on the same part still sizes one
    fine = _defect("overlapping_facets", entities=("F7",),
                   measurement={"diagonal_mm": 250.0,
                                "narrowest_face": {"id": "F7", "width_mm": 0.4, "length_mm": 11.0}})
    assert seq.params_for("remove_small", fine)[1]["max_width_mm"] == pytest.approx(1.05 * 11.4)


def test_low_quality_now_leads_with_the_repair_that_actually_applied() -> None:
    """`reapproximate` declined no_valid_face on all 10 real attempts; `split_by_proximity` made every fix."""
    assert seq.RULE_TABLE["low_quality"][0] == ("split_by_proximity",)
