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


# --- ordering ------------------------------------------------------------------------------------------------

def test_validity_defects_are_repaired_before_any_mesher_defect_and_in_the_rulings_order() -> None:
    sites = [_site("S1", entities=["S1"], failures=[{"mesher": "gmsh_default", "error_kind": "overlapping_facets"}]),
             _site("S2", entities=["S2"])]
    verdict = _V([_Finding("open_shell", ["S2"]), _Finding("non_positive_volume", ["S1"])])
    got = [(d.layer, d.kind) for d in seq.defects_of(verdict, sites)]
    assert got == [(1, "non_positive_volume"), (1, "open_shell"), (2, "overlapping_facets")]


def test_one_defect_per_kind_and_site_however_many_meshers_report_it() -> None:
    site = _site("S1", failures=[{"mesher": "gmsh_default", "error_kind": "overlapping_facets"},
                                 {"mesher": "gmsh_prod", "error_kind": "overlapping_facets"},
                                 {"mesher": "valid", "error_kind": "ignored"}])
    assert [d.kind for d in seq.defects_of(_V(), [site])] == ["overlapping_facets"]


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
    defect = seq.Defect(id="d", kind="open_shell", layer=1, site=_site("S1"), entities=("F7",),
                        measurement={"narrowest_face": {"id": "F7", "width_mm": 0.4, "length_mm": 11.0}})
    targets, params = seq.params_for("remove_small", defect)
    assert targets == ["F7"]
    assert params["max_width_mm"] == pytest.approx(seq.REMOVE_SMALL_MARGIN * 11.4)


def test_a_fuzzy_value_comes_from_the_measured_clash() -> None:
    defect = seq.Defect(id="d", kind="body_overlap", layer=1, site=_site("S1", entities=["S1", "S2"]),
                        entities=("S1", "S2"), measurement={"clash": {"a": "S1", "b": "S2", "dist_mm": 0.25}})
    _t, params = seq.params_for("fuse", defect)
    assert params["fuzzy_mm"] == pytest.approx(seq.FUZZY_MARGIN * 0.25)


def test_a_defect_with_no_measurement_gets_NO_repair_rather_than_a_guessed_parameter() -> None:
    defect = seq.Defect(id="d", kind="open_shell", layer=1, site=_site("S1"), entities=(), measurement={})
    with pytest.raises(seq.NoParameters):
        seq.params_for("remove_small", defect)
    with pytest.raises(seq.NoParameters):
        seq.params_for("defeature", defect)


# --- the pick ------------------------------------------------------------------------------------------------

def _defect(kind: str) -> seq.Defect:
    return seq.Defect(id="d", kind=kind, layer=1, site=_site("S1"), entities=("F1",))


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
        q = int(ws.labels.get("q", 0))
        v = _V(clean=q >= 4)
        v.q = q
        return v

    @staticmethod
    def score(v: Any) -> tuple:
        return (1, getattr(v, "q", 0), 0.0, 0.0, 0)


class _WS:
    """The least workspace the loop touches: labels, a fork, and a brep path."""

    def __init__(self, q: int = 0, name: str = "w0") -> None:
        self.labels, self.brep_path, self.original, self.history = {"q": q}, f"/fake/{name}.brep", "/fake/o.brep", []

    def fork(self) -> _WS:
        return self


def _one_mesher_site() -> Site:
    return _site("S1", kind="invalid", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": "edge_through_face"}],
                 graph={"nodes": [{"id": "F1", "area": 4.4, "min_width": 0.4}], "mesher_faces": ["F1"]})


def _run(**kw: Any) -> tuple:
    judge = _Judge()
    start = _WS(0)
    best, verdict, counters, rounds = seq.repair_sequentially(
        None, start, "o.step", judge=judge, sites_of=lambda pool, ws, v: [_one_mesher_site()],
        start_verdict=judge.full(None, start, "o.step"), **kw)
    return judge, best, verdict, counters, rounds


@contextmanager
def _patched(apply: Any) -> Any:
    """`seq.apply_repair` replaced: these tests are about the LOOP's rules, not about the op layer."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(seq, "apply_repair", apply)
        yield mp


def _bump(by: int = 1, name: str = "up") -> Any:
    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, run_stage: Any = None) -> Any:
        return seq._Applied(_WS(int(ws.labels["q"]) + by, name=name), [], ("F1",))
    return apply


def test_lane_Cs_stub_answers_nothing_so_the_arm_KEEPS_nothing_and_says_why() -> None:
    """An arm whose verifier is not wired yet must read as "not measured", never as "cannot repair" - and it must
    not apply two more repairs that nothing will check."""
    tried: list[Any] = []

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, run_stage: Any = None) -> Any:
        tried.append(repair)
        return seq._Applied(_WS(1, name="x"), [], ("F1",))

    with _patched(apply) as _mp:
        judge, _best, _v, counters, rounds = _run()
    assert len(tried) == 1                                  # one attempt, not one per alternative
    assert counters.fixed == {} and counters.skipped["edge_through_face"] == 1
    assert counters.skipped_why["local_verification_unavailable"] == 1
    assert counters.rounds == 1 and rounds[0].kept == 0
    # a round that kept nothing stops, so the end-of-round panel is never paid for
    assert counters.full_checks == 0 and judge.full_calls == 1   # the one call is the start verdict, passed in


def test_TWO_defects_in_a_round_cost_ONE_full_panel_between_them() -> None:
    """The arm's whole claim: the panel is paid per ROUND, never per repair."""
    site = _site("S1", kind="invalid", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": "edge_through_face"},
                           {"mesher": "gmsh_prod", "error_kind": "no_volume"}],
                 graph={"nodes": [{"id": "F1", "area": 4.4, "min_width": 0.4}], "mesher_faces": ["F1"]})
    judge = _Judge()
    start = _WS(0)
    with _patched(_bump(2)) as _mp:
        best, verdict, counters, rounds = seq.repair_sequentially(
            None, start, "o.step", judge=judge, sites_of=lambda pool, ws, v: [site],
            start_verdict=judge.full(None, start, "o.step"),
            verify_local=lambda *a: seq.LocalResult(gone=True, seconds=0.25))
    assert sorted(counters.fixed) == ["edge_through_face", "no_volume"]
    assert counters.local_checks == 2 and counters.local_s == 0.5
    assert counters.full_checks == 1 and counters.rounds == 1     # ONE panel for both repairs
    assert verdict.clean and best.labels["q"] == 4


def test_a_local_pass_that_the_whole_part_panel_does_not_confirm_ends_the_arm_on_the_better_state() -> None:
    """A local pass is not a whole-part pass. The round's panel is the measurement: if it did not improve the
    part, the arm keeps the better state rather than diagnosing a worse one again."""
    with _patched(_bump(0, name="worse")) as _mp:
        _judge, best, _v, counters, _rounds = _run(verify_local=lambda *a: seq.LocalResult(gone=True))
    assert counters.rounds == 1 and counters.full_checks == 1
    assert best.brep_path == "/fake/w0.brep"                  # the start state, not the unimproved repair


def test_the_arm_stops_at_three_rounds_however_well_it_is_going() -> None:
    with _patched(_bump(1)) as _mp:
        _judge, best, verdict, counters, rounds = _run(verify_local=lambda *a: seq.LocalResult(gone=True))
    assert counters.rounds == 3 and len(rounds) == 3
    assert counters.full_checks == 3                          # one per round, never one per repair
    assert best.labels["q"] == 3 and not verdict.clean        # clean is 4: it ran out of rounds, not of defects


def test_a_measured_local_FAILURE_moves_on_to_the_next_alternative() -> None:
    tried: list[Any] = []

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, run_stage: Any = None) -> Any:
        tried.append(repair)
        return seq._Applied(_WS(1, name="x"), [], ("F1",))

    with _patched(apply) as _mp:
        _judge, _best, _v, counters, _rounds = _run(
            verify_local=lambda *a: seq.LocalResult(gone=False, new_hard=("self_intersection",)))
    assert tried == [("defeature",), ("conformalise",), ("defeature", "defeature")]   # at most MAX_ALTERNATIVES
    assert counters.local_checks == 3 and counters.skipped["edge_through_face"] == 1


def test_a_clean_round_stops_immediately_and_pays_no_further_panel() -> None:
    with _patched(_bump(4, name="clean")) as _mp:
        _judge, _best, verdict, counters, _rounds = _run(verify_local=lambda *a: seq.LocalResult(gone=True))
    assert verdict.clean and counters.rounds == 1 and counters.full_checks == 1
