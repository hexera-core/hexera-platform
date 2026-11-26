"""v5's per-defect tournament on fakes (no kernel, no pool): who wins, what every candidate's log says, how the part's
size shapes it, the order and chaining of defects, and that every class menu repair can be aimed."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from tests.unit.cad.repair_v4.test_b24_seq_arm import _V, _Finding, _Judge, _local, _patched, _site

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.ops import OpDeclined
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5 import tournament as T


class _WS:
    def __init__(self, q: int = 0, name: str = "w0", faces: int = 2) -> None:
        self.labels, self.brep_path, self.original, self.history = {"q": q}, f"/fake/{name}.brep", "/fake/o.brep", []
        self.ids = {**{f"F{i}": "fp" for i in range(1, faces + 1)}, "B1": "fp", "B2": "fp"}

    def fork(self) -> _WS:
        return self


def _defect(code: str = "overlapping_facets", cls: str = "close_faces", kind: str = "mesher", **kw: Any) -> Defect:
    base: dict[str, Any] = {"entities": ("F1", "F2"), "member": "gmsh_default" if kind == "mesher" else None}
    return Defect("S@F1", kind, code, cls=cls, rule="near-coaxial faces F1-F2", **{**base, **kw})  # type: ignore[arg-type]


def _apply(tried: list, *, fail: dict[str, Exception] | None = None) -> Any:
    """Every repair applies (unless `fail` names its op) on a new state labelled with the op and its overrides."""
    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, pair: Any = None,
              run_stage: Any = None, overrides: Any = ()) -> Any:
        tried.append((repair, tuple(map(repr, overrides))))
        if repair[0] in (fail or {}):
            raise (fail or {})[repair[0]]
        label = "+".join(repair) + "".join(map(repr, overrides))
        return seq._Applied(_WS(int(ws.labels["q"]) + 1, name=label), [], ("B1",), [dict(o) for o in overrides])
    return apply


def _volume(by_label: dict[str, float]) -> Any:
    """A guard that refuses nothing and reports the volume change named for the candidate's state (default 0)."""
    return lambda pool, before, after: T.Guarded(None, by_label.get(after.brep_path.split("/")[-1].split("{")[0]
                                                                     .removesuffix(".brep"), 0.0))


def _play(defect: Defect, menu: tuple, *, ws: Any = None, verify: Any = None, guard: Any = None,
          budget: Any = T.budget_for) -> tuple[Any, T.Tournament, seq.SeqCounters, list[str]]:
    t = T.Tournament(menu_of=lambda d: menu, verify=verify or (lambda *a, **k: _local()),
                     guard=guard or _volume({}), budget=budget)
    counters, lines = seq.SeqCounters(), []
    out = t(None, ws or _WS(), defect, counters=counters, step=lines.append)
    return out, t, counters, lines


def test_the_rule_seed_wins_a_tie() -> None:
    with _patched(_apply([])):
        out, t, counters, _ = _play(_defect(), (("split_aligned",), ("defeature",)))
    assert out.brep_path.endswith("/split_aligned.brep")
    assert [(m.tier, m.op, m.result) for m in t.log.records][:2] == [("rule_seed", "split_aligned", "won"),
                                                                    ("class_menu", "defeature", "survived")]
    assert counters.fixed == {"overlapping_facets": 1} and counters.kept_in_round == 1


def test_a_better_non_seed_wins_on_volume_change() -> None:
    with _patched(_apply([])):
        out, t, _, _ = _play(_defect(), (("split_aligned",), ("defeature",)),
                             guard=_volume({"split_aligned": 0.02, "defeature": 0.001}))
    assert out.brep_path.endswith("/defeature.brep")
    assert {m.op: m.result for m in t.log.records if not m.params} == {"split_aligned": "survived", "defeature": "won"}


def test_not_applied_unaimable_and_refused_candidates_are_logged_with_their_reasons() -> None:
    fail = {"defeature": OpDeclined("nothing_to_remove: no feature at F1")}
    menu = (("split_aligned",), ("defeature",), ("fill_loop",), ("conformalise",))
    guard = lambda pool, before, after: T.Guarded(  # noqa: E731
        "catastrophic_loss: body_lost" if "segments" in after.brep_path else None, 0.0)
    with _patched(_apply([], fail=fail)):
        out, t, counters, lines = _play(_defect(), menu, guard=guard)
    by = {(m.op, bool(m.params)): m for m in t.log.records}
    assert (by[("defeature", False)].result, by[("defeature", False)].reason_key) == ("not_applied", "OpDeclined")
    assert "nothing_to_remove" in by[("defeature", False)].reason
    assert by[("fill_loop", False)].result == "unaimable" and "no measured free_loop_edges" in by[("fill_loop", False)].reason
    assert by[("conformalise", False)].result == "unaimable"
    assert by[("split_aligned", True)].result == "guard_refused" and by[("split_aligned", True)].heat == "guard"
    assert "body_lost" in by[("split_aligned", True)].reason
    assert by[("split_aligned", False)].result == "won"
    counts = t.log.counts()
    # seed + defeature + two segment variants (6 and 12 around the default 8) were attempted; a refused one APPLIED
    assert counts["unaimable"] == 2 and counts["not_applied"] == {"OpDeclined": 1}
    assert counts["attempts"] == 4 and counts["applied"] == 3 and counts["results"]["guard_refused"] == 2
    assert sum("match [" in line for line in lines) == len(t.log.records)


def test_no_survivor_is_unrepaired_with_every_candidates_reason() -> None:
    with _patched(_apply([])):
        out, t, counters, lines = _play(_defect(), (("split_aligned",), ("defeature",)),
                                        verify=lambda *a, **k: _local(gone=False))
    assert out.labels["q"] == 0 and counters.skipped_why["no_survivor"] == 1
    assert {m.result for m in t.log.records} == {"still_there"} and {m.heat for m in t.log.records} == {"mesh"}
    assert "unrepaired: no candidate survived the heats" in lines[0]


def test_a_new_hard_code_drops_the_candidate_at_the_validity_heat() -> None:
    with _patched(_apply([])):
        _, t, _, _ = _play(_defect(), (("defeature",),),
                           verify=lambda *a, **k: _local(new_hard=[{"code": "open_shell", "entities": ["B2"]}]))
    [m] = t.log.records
    assert (m.result, m.heat) == ("new_defect", "validity")


def test_the_largest_parts_run_sequentially_in_tier_order_and_stop_at_the_first_winner() -> None:
    tried: list = []
    big = _WS(faces=T.LARGE_PART_MAX_FACES + 1)
    with _patched(_apply(tried)):
        out, t, _, _ = _play(_defect(), (("split_aligned",), ("defeature",)), ws=big)
    assert tried == [(("split_aligned",), ())]
    assert [m.result for m in t.log.records] == ["won", "not_run", "not_run", "not_run"]
    assert "sequential early stop" in t.log.records[1].reason


def test_large_parts_enter_fewer_and_say_which_were_left_out() -> None:
    mid = _WS(faces=T.SMALL_PART_MAX_FACES + 1)
    with _patched(_apply([])):
        _, t, _, _ = _play(_defect(), (("split_aligned",), ("defeature",)), ws=mid)
    results = [m.result for m in t.log.records]
    assert results.count("not_entered") == 4 - T.LARGE_PART_MAX_ENTRANTS
    assert "entrant cap" in next(m.reason for m in t.log.records if m.result == "not_entered")


def test_defects_run_in_priority_order_each_from_the_previous_winners_state() -> None:
    """A validity defect first, then the mesher one; the second tournament starts where the first winner left."""
    seen: list = []

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, **kw: Any) -> Any:
        seen.append((defect.code, ws.brep_path))
        return seq._Applied(_WS(int(ws.labels["q"]) + 2, name=f"after_{defect.code}"), [], ("B1",), [{}])

    sites = [_site("S1", entities=["F1", "B1"],
                   failures=[{"mesher": "gmsh_default", "error_kind": "edge_through_face"}])]
    verdict = _V([_Finding("non_positive_volume", ["B1"])])
    judge, start = _Judge(), _WS()
    cls = {"non_positive_volume": "open_shell_volume", "edge_through_face": "surface_crossing"}

    def diagnose(pool: Any, ws: Any, v: Any, s: Any, defects: list) -> list:
        import dataclasses

        from meshpipeline.cad.repair_v5.diagnose import order
        return order([dataclasses.replace(d, cls=cls[d.code]) for d in reversed(defects)])

    with _patched(apply):
        out = loop.repair_v5(None, start, "o.step", judge=judge, sites_of=lambda pool, ws, v: sites, h_mm=2.0,
                             start_verdict=verdict, wall_s=1000.0, run_stage=lambda *a, **k: None,
                             verify=lambda *a, **k: _local(), guard=_volume({}), diagnose=diagnose,
                             budget=lambda n: T.Budget(T.MAX_ENTRANTS, 1, True, "test: sequential"))
    assert [c for c, _ in seen] == ["non_positive_volume", "edge_through_face"]
    assert seen[0][1] == start.brep_path and seen[1][1] == "/fake/after_non_positive_volume.brep"
    assert out.matches.counts()["won_by_tier"] == {"rule_seed": 2}


def test_an_entrant_source_is_one_more_function_in_the_list() -> None:
    """Step 12's agent source: appended, nothing else changes."""
    agent = lambda brief: [T.Entrant(("defeature",), "agent", note="the model's pick")]  # noqa: E731
    t = T.Tournament(menu_of=lambda d: (("split_aligned",),), verify=lambda *a, **k: _local(), guard=_volume({}),
                     sources=(*T.DEFAULT_SOURCES, agent))
    with _patched(_apply([])):
        t(None, _WS(), _defect(), counters=seq.SeqCounters(), step=lambda line: None)
    assert [m.tier for m in t.log.records][-1] == "agent"


# --- every repair in every class menu is aimable for a representative defect of its class ---------------------

_DIAG = {"diagonal_mm": 100.0}
REPRESENTATIVE: dict[str, Defect] = {
    "overlap_self_intersection": _defect("body_overlap", "overlap_self_intersection", "validity",
                                         entities=("B1", "B2"), pairs=(("B1", "B2"),),
                                         measurement={"clash": {"a": "B1", "b": "B2", "dist_mm": 0.3}}),
    "open_shell_volume": _defect("open_shell", "open_shell_volume", "validity", entities=("B1",),
                                 measurement={**_DIAG, "free_loops": 1, "free_loop_edges": [["E1", "E2", "E3"]]}),
    "open_shell_volume:closed": _defect("no_volume", "open_shell_volume", entities=("F1",),
                                        cause={"free_loops": 0}, measurement=_DIAG),
    "open_shell_volume:non_positive_volume": _defect("non_positive_volume", "open_shell_volume", "validity",
                                                     entities=("B1",), measurement=_DIAG),
    # a code with no fault kind (the round trip's): the class's general row
    "invalid_brep_tolerance": _defect("round_trip_invalid", "invalid_brep_tolerance", "validity",
                                      entities=("F1", "E2"), measurement={"site_edges": ["E2"]}),
    # v5 step 5: one row per fault KIND (brep_faults.KIND_REPAIRS); the aim is the measured fault's entities
    "invalid_brep_tolerance:brep_invalid": _defect("brep_invalid", "invalid_brep_tolerance", "validity",
                                                   entities=("B1", "F1", "E2"), measurement={"site_edges": ["E2"]},
                                                   cause={"kind": "brep_invalid"}),
    "invalid_brep_tolerance:large_tolerance": _defect("large_tolerance", "invalid_brep_tolerance", "validity",
                                                      entities=("E2", "F1"), measurement={"site_edges": ["E2"]},
                                                      cause={"kind": "large_tolerance", "aim": ["E2", "F1"]}),
    "invalid_brep_tolerance:unbounded": _defect("unbounded_geometry", "invalid_brep_tolerance", "validity",
                                                entities=(), measurement=_DIAG, cause={"kind": "unbounded"}),
    "invalid_brep_tolerance:pole": _defect("pole", "invalid_brep_tolerance", entities=("F1",),
                                           cause={"kind": "pole", "aim": ["F1"]}),
    # an `unknown` HXT failure the spline detector explained (diagnose.attach_faults)
    "invalid_brep_tolerance:bad_spline": _defect("other", "invalid_brep_tolerance", entities=("F1", "F2"),
                                                 cause={"kind": "bad_spline", "aim": ["F2"]}),
    # round 2: a brep_invalid on faces missing an edge; a periodic failure on a face whose seam is off its surface
    "invalid_brep_tolerance:missing_edge": _defect("brep_invalid", "invalid_brep_tolerance", "validity",
                                                   entities=("F1", "F2"),
                                                   cause={"kind": "missing_edge", "aim": ["F1", "F2"]}),
    "invalid_brep_tolerance:bad_seam": _defect("brep_invalid", "invalid_brep_tolerance", "validity",
                                               entities=("F1",), cause={"kind": "bad_seam", "aim": ["F1"]}),
    "closed_periodic_face:bad_seam": _defect("periodic_surface", "closed_periodic_face", entities=("B1",),
                                             cause={"kind": "bad_seam", "aim": ["F1"]}),
    "close_faces": _defect(cause={"segments_needed": 16, "body_a": "B1", "body_b": "B2", "aim": ["F1", "F2"],
                                  "face_pairs": [["F1", "F2"]]}),
    "close_faces:coaxial": _defect(cause={"segments_needed": 12, "body_a": "B1", "body_b": "B2", "coaxial": True,
                                          "aim": ["F1", "F2"], "face_pairs": [["F1", "F2"]],
                                          "aligned_faces": ["F1", "F2"]}),
    # v5 step 8: the overlap detectors' rows (overlap.attach_overlap_facts)
    "overlap_self_intersection:duplicate_body": _defect("body_overlap", "overlap_self_intersection", "validity",
                                                        entities=("B1", "B2"), pairs=(("B1", "B2"),),
                                                        cause={"kind": "duplicate_body", "duplicates": ["B1|B2"]}),
    "overlap_self_intersection:self_intersection": _defect("self_intersection", "overlap_self_intersection",
                                                           "validity", entities=("B1",),
                                                           cause={"aim": ["F3", "F4"]}),
    "overlap_self_intersection:self_intersecting_face": _defect("self_intersection", "overlap_self_intersection",
                                                                "validity", entities=("B1",),
                                                                cause={"kind": "self_intersecting_face",
                                                                       "aim": ["F3", "F4"]}),
    "surface_crossing": _defect("edge_through_face", "surface_crossing",
                                cause={"pairs": [{"a": "F1", "b": "F2", "body_a": "B1", "body_b": "B2"}]}),
    "closed_periodic_face": _defect("periodic_surface", "closed_periodic_face", entities=("F1",)),
    "low_quality": _defect("low_quality", "low_quality", entities=("F1",),
                           measurement={**_DIAG, "clash_by_pair": {"F1|F2": 0.2},
                                        "narrowest_face": {"id": "F1", "width_mm": 0.1, "length_mm": 5.0}}),
}
# v5 step 10: one row per measured low_quality KIND; the cause carries the aim and the ops' parameters
# (`repair_v5.quality._cause`), so every entrant is aimed and sized from it
_MERGE = {"max_dev_mm": 0.5, "feature_deg": 30.0, "max_width_mm": 0.2, "refit_tol_mm": 0.5}
_LQ_CAUSES = {
    "sliver": {"kind": "sliver", "aim": ["F1"], "neighbour": "F2", **_MERGE},
    "small_face": {"kind": "small_face", "aim": ["F1"], **_MERGE, "feature_deg": 180.0},
    "small_edge": {"kind": "small_edge", "aim": ["E3"], "max_length_mm": 0.1, "max_width_mm": 0.1},
    "tiny_solid": {"kind": "tiny_solid", "aim": ["B2", "B1"], "max_volume_fraction": 1e-4, "partner": "B1"},
    "floating_solid": {"kind": "floating_solid", "aim": ["B2"], "max_volume_fraction": 1e-4},
    # round 2 fix E (`narrow.region_of`): the strip face at the worst elements plus the short edges there
    "narrow_region": {"kind": "narrow_region", "aim": ["F1", "E3"], **_MERGE, "feature_deg": 180.0,
                      "max_length_mm": 0.1},
}
for _kind, _cause in _LQ_CAUSES.items():
    REPRESENTATIVE[f"low_quality:{_kind}"] = dataclasses.replace(
        REPRESENTATIVE["low_quality"], kind="validity", code="small_face", member=None, cause=_cause)
# v5 step 9: one row per crossing KIND (repair_v5.crossing): the aim is the measured pair, the bodies its owners
for _kind in ("duplicate_faces", "true_crossing", "near_coincident"):
    REPRESENTATIVE[f"surface_crossing:{_kind}"] = _defect(
        "edge_through_face", "surface_crossing", cause={"kind": _kind, "aim": ["F1", "F2"], "body_a": "B1",
                                                        "body_b": "B2", "crossing": [{"a": "F1", "b": "F2"}],
                                                        "duplicate_pairs": [["F1", "F2"]]})
# v5 step 11: one row per open_shell_volume cause KIND (open_shell.KIND_REPAIRS)
REPRESENTATIVE.update({
    "open_shell_volume:inside_out": _defect("non_positive_volume", "open_shell_volume", "validity", entities=("B1",),
                                            cause={"kind": "inside_out", "aim": ["B1"]}),
    "open_shell_volume:non_manifold": _defect("non_manifold", "open_shell_volume", "validity", entities=("B1", "E3"),
                                              cause={"kind": "non_manifold", "aim": ["E3"]}),
    "open_shell_volume:gap": _defect("open_shell", "open_shell_volume", "validity", entities=("B1",),
                                     measurement={**_DIAG, "free_loop_edges": [["E1", "E2", "E3"]]},
                                     cause={"kind": "gap", "sew_tol_mm": 0.011, "sew_ladder_mm": [0.0011, 0.011]}),
    "open_shell_volume:hole": _defect("open_shell", "open_shell_volume", "validity", entities=("B1",),
                                      cause={"kind": "hole", "aim": ["E4", "E5", "E6"]}),
    "open_shell_volume:closed_shell": _defect("no_solids", "open_shell_volume", "validity", entities=(),
                                              cause={"kind": "closed_shell"}),
    "open_shell_volume:wire_gap": _defect("open_loop", "open_shell_volume", entities=("F1",),
                                          cause={"kind": "wire_gap", "aim": ["F1"]}),
    "open_shell_volume:mesher_closed": _defect("no_volume", "open_shell_volume", entities=("F1",),
                                               measurement={**_DIAG, "clash_by_pair": {"F1|F2": 0.2},
                                                            "narrowest_face": {"id": "F1", "width_mm": 0.1,
                                                                               "length_mm": 5.0}},
                                               cause={"kind": "mesher_closed"}),
    # round 2: a stray sheet beside closed solids (no `unrepairable`): drop_sheet, whole part
    "open_shell_volume:sheet": _defect("open_shell", "open_shell_volume", "validity", entities=(),
                                       cause={"kind": "sheet"}),
})

# round 2: facet_crossing_local, one row per measured cause KIND (facet_local.KIND_REPAIRS) plus the general row; the
# cause aims the faces and carries the measured parameters
_LOCAL_PARAMS = {"split_angle": {"max_deg": 45.0}, "to_canonical": {"tol_mm": 0.01},
                 "reapproximate": {"tol_mm": 0.025, "max_degree": 5}, "split_continuity": {"order": 1},
                 # fix G: the fine angular split and the knife cut (aimed by turn_faces / pair)
                 "split_turn": {"max_deg": 28.7}, "cut_knife": {"width_mm": 0.1, "max_reach_mm": 7.0}}
REPRESENTATIVE["facet_crossing_local"] = _defect("overlapping_facets", "facet_crossing_local",
                                                 cause={"kind": "unexplained", "aim": ["F1", "F2"],
                                                        "op_params": _LOCAL_PARAMS, "pair": ["F1", "F2"],
                                                        "turn_faces": ["F1"]})
for _kind in ("curved_face", "closed_face", "c1_break", "bad_param", "spline_analytic", "near_tangent_edge",
              "knife_edge", "sliver", "touching"):
    REPRESENTATIVE[f"facet_crossing_local:{_kind}"] = _defect(
        "overlapping_facets", "facet_crossing_local", cause={"kind": _kind, "aim": ["F1", "F2"],
                                                            "op_params": _LOCAL_PARAMS, "pair": ["F1", "F2"],
                                                            "turn_faces": ["F1"]})


def test_every_class_menu_row_has_a_representative_defect() -> None:
    assert set(REPRESENTATIVE) == set(loop.CLASS_REPAIRS)


@pytest.mark.parametrize(("key", "op"), sorted({(k, op) for k, menu in loop.CLASS_REPAIRS.items()
                                                for r in menu for op in r}))
def test_every_repair_in_every_class_menu_is_aimable_and_parameterised(key: str, op: str) -> None:
    defect = REPRESENTATIVE[key]
    assert loop.table_key(defect) == key
    assert seq.aim_reason(op, defect) is None
    targets, params = seq.params_for(op, defect)
    if op in seq.REGISTRY:
        assert seq.validate_call(op, targets, params, _Live(targets)) == [], (op, targets, params)


class _Live:
    """The least workspace `validate_call` reads: every target is a live id of its own kind."""

    def __init__(self, ids: list[str]) -> None:
        self.ids = dict.fromkeys(ids, "fp")

    def resolve(self, ids: list[str]) -> list[str]:
        return ids


def test_a_local_mesh_the_file_wall_could_not_start_is_a_WALL_stop_not_a_failed_repair() -> None:
    from meshpipeline.cad.repair_v4.verdict.assemble import PanelWallExhausted

    def walled(*a: Any, **k: Any) -> Any:
        raise PanelWallExhausted("not starting a local gmsh_default mesh: 12 s left")

    big = _WS(faces=T.LARGE_PART_MAX_FACES + 1)
    with _patched(_apply([])):
        out, t, counters, _ = _play(_defect(), (("split_aligned",), ("defeature",)), ws=big, verify=walled)
    assert counters.wall_stop and counters.skipped_why["wall"] == 1 and out.labels["q"] == 0
    assert (t.log.records[0].result, t.log.records[0].reason_key) == ("not_checked", "wall")
    assert {m.result for m in t.log.records[1:]} == {"not_run"} and t.log.records[1].reason == "the file's wall"


# --- step 7: the harness's file-wall kill inside the tournament is a WALL STOP, never a lost file ----------------


class _FileWallKill(BaseException):
    """Stands in for `reprobe.FileTimeout` (the SIGALRM kill): a BaseException carrying the `FILE_WALL` marker."""

    FILE_WALL = True


def _killed_on(op: str) -> Any:
    def verify(pool: Any, before: Any, after: Any, defect: Any, touched: Any = None, **kw: Any) -> Any:
        if after.brep_path.endswith(f"/{op}.brep"):
            raise _FileWallKill("file wall clock 600s exceeded")
        return _local()
    return verify


def test_a_file_wall_kill_inside_the_parallel_pool_is_a_wall_stop_that_keeps_the_finished_winner() -> None:
    with _patched(_apply([])):
        out, t, counters, _ = _play(_defect(), (("split_aligned",), ("defeature",)), verify=_killed_on("defeature"))
    assert out.brep_path.endswith("/split_aligned.brep")          # the seed had finished: it still wins
    assert counters.wall_stop and "file wall clock" in counters.wall_stop
    killed = next(m for m in t.log.records if m.op == "defeature")
    assert (killed.result, killed.reason_key) == ("not_run", T.WALL)
    assert t.log.records[0].result == "won"


def test_a_file_wall_kill_in_a_sequential_tournament_walls_the_rest_and_keeps_the_state() -> None:
    big = _WS(faces=T.LARGE_PART_MAX_FACES + 1)
    with _patched(_apply([])):
        out, t, counters, _ = _play(_defect(), (("split_aligned",), ("defeature",)), ws=big,
                                    verify=_killed_on("split_aligned"))
    assert out is big and counters.wall_stop and counters.skipped_why["wall"] == 1
    assert {(m.result, m.reason_key) for m in t.log.records} == {("not_run", T.WALL)}


def test_any_other_base_exception_still_propagates() -> None:
    def interrupted(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt

    with _patched(_apply([])), pytest.raises(KeyboardInterrupt):
        _play(_defect(), (("split_aligned",), ("defeature",)), verify=interrupted)


def test_an_op_timeout_is_clipped_to_the_file_wall_left(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4.verdict import wall

    t = T.Tournament(menu_of=lambda d: (), verify=lambda *a, **k: _local(), op_timeout_s=300.0)
    monkeypatch.setattr(wall, "remaining_s", lambda: 42.0)
    assert t._op_timeout() == 42.0
    monkeypatch.setattr(wall, "remaining_s", lambda: None)
    assert t._op_timeout() == 300.0
