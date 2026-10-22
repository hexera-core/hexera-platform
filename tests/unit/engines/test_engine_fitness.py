# Responsibility: Verify the engine recommendation ranks by measured lab evidence on shapes like this one,
# backs off honestly when there is little, keeps every able engine on the list, never recommends an engine
# its own turf rules out, and that the committed table is readable, provenanced and beats the old fixed order
# on shapes it never saw.
# Boundaries: engines/fitness.py over synthetic tables and over the committed table; no mesher runs.
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from meshpipeline.cad.shape_traits import TRAITS_VERSION, ShapeTraits
from meshpipeline.engines import fitness as F
from meshpipeline.engines.base import TurfRow

REPO = Path(__file__).resolve().parents[3]

TUBE = {"flow": "internal", "form": "cad", "input_kind": "fluid-domain", "n_triangles": 100,
        "closed": True, "genus": 0, "ports": 2, "gap_vs_port": 1.0, "slenderness": 9.0}
GAP = {**TUBE, "genus": 1, "gap_vs_port": 0.5, "slenderness": 900.0}
CHAMBER = {**TUBE, "gap_vs_port": 2.8, "slenderness": 1.8}


def _row(shape, engine, traits, status="pass", **kw):
    return {"shape": shape, "case": shape, "engine": engine, "format": "step",
            "form": traits.get("form", "cad"), "status": status, "traits": dict(traits), **kw}


def _table(rows):
    return F.table_from({"version": "t", "traits_version": TRAITS_VERSION, "rows": rows})


def _t(d):
    return ShapeTraits.from_dict(d)


# ---------------------------------------------------------------- the shape class ----
@pytest.mark.parametrize("traits,cls", [
    (TUBE, F.TUBE),
    ({**TUBE, "ports": 4}, F.BRANCHED),
    (GAP, F.THIN_GAP),
    (CHAMBER, F.CHAMBER),
    ({**TUBE, "slenderness": 2.0}, F.CHAMBER),                      # short and stubby
    ({**TUBE, "genus": 12}, F.OBSTACLES),                            # a tube bank
    ({**TUBE, "input_kind": "body-surface", "genus": 3, "ports": 4}, F.BRANCHED),  # a wall's own handles
    ({"flow": "external", "thickness_ratio": 0.1}, F.SLENDER),
    ({"flow": "external", "thickness_ratio": 0.6}, F.BLUNT),
    ({"flow": "multi-region"}, F.MULTI_REGION),
    ({"flow": "internal"}, ""),                                       # nothing measured, nothing claimed
])
def test_the_class_is_read_from_measured_traits_only(traits, cls):
    assert F.shape_class(_t(traits)) == cls


# ---------------------------------------------------------------- the evidence ----
def test_engines_rank_by_their_record_on_similar_shapes_not_by_a_fixed_order():
    rows = []
    for i in range(5):
        rows += [_row(f"gap{i}", "cfmesh", GAP, "fail"), _row(f"gap{i}", "snappy", GAP),
                 _row(f"tube{i}", "cfmesh", TUBE), _row(f"tube{i}", "snappy", TUBE, "fail")]
    tab = _table(rows)
    gap = F.recommend(_t(GAP), ["cfmesh", "snappy"], table=tab)
    tube = F.recommend(_t(TUBE), ["cfmesh", "snappy"], table=tab)
    assert gap.engine == "snappy" and gap.ranking == ["snappy", "cfmesh"]
    assert tube.engine == "cfmesh" and tube.ranking == ["cfmesh", "snappy"]
    snappy = gap.fit_of("snappy")
    assert snappy.evidence.passed == 5 and snappy.evidence.shapes == 5 and not snappy.evidence.low
    assert snappy.reason.startswith("passed 5 of 5 similar shapes (thin gaps or annuli, from CAD)")
    assert gap.fit_of("cfmesh").reason.startswith("passed 0 of 5 similar shapes")


def test_thin_evidence_on_this_class_is_quoted_as_it_is_and_says_it_is_thin():
    rows = [_row("gap0", "snappy", GAP)]                       # one gap shape only
    rows += [_row(f"t{i}", "snappy", TUBE) for i in range(4)]
    ev = F.evidence_for(_table(rows), "snappy", flow="internal", cls=F.THIN_GAP, form="cad")
    assert ev.level == 0 and ev.low and (ev.runs, ev.ranked_runs) == (1, 1)
    assert ev.words == "thin gaps or annuli, from CAD"
    assert F.bound_words(ev) == "only 1 run on thin gaps or annuli, fewer than the 20 the ranking trusts"
    # with no run on this class at all, the broader record is quoted - and never ranked
    none = F.evidence_for(_table(rows[1:]), "snappy", flow="internal", cls=F.THIN_GAP, form="cad")
    assert F.evidence_words(none).startswith("no lab results on shapes exactly like this; passed 4 of 4")


def test_the_rank_reads_the_record_on_this_shape_class_only():
    # two failures on the only two thin gaps seen count; a long record on tubes says nothing
    # about thin gaps and never lifts the rank there
    rows = [_row(f"gap{i}", "snappy", GAP, "fail") for i in range(2)]
    rows += [_row(f"t{i}", "snappy", TUBE) for i in range(20)]
    rows += [_row(f"gap{i}", "cfmesh", GAP) for i in range(2)]
    rows += [_row(f"t{i}", "cfmesh", TUBE) for i in range(20)]
    tab = _table(rows)
    snappy = F.evidence_for(tab, "snappy", flow="internal", cls=F.THIN_GAP, form="cad")
    assert (snappy.ranked_passed, snappy.ranked_runs) == (0, 2) and not snappy.proven
    assert snappy.ranked_words == "thin gaps or annuli"
    assert F.recommend(_t(GAP), ["snappy", "cfmesh"], table=tab).engine == "cfmesh"


def test_a_few_passes_do_not_outrank_a_long_record():
    rows = [_row(f"a{i}", "snappy", TUBE) for i in range(3)]
    rows += [_row(f"b{i}", "cfmesh", TUBE) for i in range(30)]
    rows += [_row("b0", "cfmesh", TUBE, "fail"), _row("b1", "cfmesh", TUBE, "fail")]
    rows += [_row(f"c{i}", "snappy", CHAMBER, "fail") for i in range(6)]   # snappy's wider record
    rec = F.recommend(_t(TUBE), ["snappy", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh", [f.as_dict() for f in rec.fits]


def test_a_tie_goes_to_measured_layers_and_never_to_time():
    rows = []
    for i in range(4):
        rows += [_row(f"s{i}", "snappy", TUBE, layers_pct=95.0, seconds=300.0),
                 _row(f"s{i}", "cfmesh", TUBE, seconds=30.0)]
    rec = F.recommend(_t(TUBE), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.engine == "snappy"
    assert "near-wall layers on 95% of the wall" in rec.reason
    # the same record either way: the faster engine does not win the tie; the declared ladder does
    rows = [_row(f"s{i}", e, TUBE, seconds=s) for i in range(4) for e, s in (("snappy", 30.0),
                                                                            ("cfmesh", 300.0))]
    assert F.recommend(_t(TUBE), ["snappy", "cfmesh"], table=_table(rows)).engine == "cfmesh"


def test_no_evidence_at_all_is_said_plainly_and_every_engine_stays_on_the_list():
    rec = F.recommend(_t(TUBE), ["vmtk", "snappy", "cfmesh", "gmsh"], table=F.Table())
    assert sorted(rec.ranking) == ["cfmesh", "gmsh", "snappy", "vmtk"]
    assert all(f.reason.startswith("no lab results yet") for f in rec.fits)
    assert all(F.fit_words(f) in ("best fit", "untested") for f in rec.fits)


def test_an_engine_without_layers_gives_way_when_the_brief_asks_for_layers():
    # gmsh declares no reliable prism layers (its DeliveredMesh); snappyHexMesh declares them
    rows = [_row(f"s{i}", e, TUBE) for i in range(4) for e in ("gmsh", "snappy")]
    rows += [_row("s0", "snappy", TUBE, "fail")]
    tab = _table(rows)
    plain = F.recommend(_t(TUBE), ["gmsh", "snappy"], table=tab)
    layered = F.recommend(_t(TUBE), ["gmsh", "snappy"], table=tab, brief={"layers_requested": True})
    assert plain.engine == "gmsh"
    assert layered.engine == "snappy"
    assert "no reliable near-wall layers" in layered.fit_of("gmsh").reason


def test_an_engine_its_own_turf_puts_outside_is_listed_but_never_recommended(monkeypatch):
    class Spec:
        name, ladder_rank, delivered_mesh = "gmsh", 40, None
        home_turf = (TurfRow("gap_vs_port", "<", 0.6, "outside", "Not for a thin gap.",
                             flow="internal"),)
    real = F._spec
    monkeypatch.setattr(F, "_spec", lambda e: Spec() if e == "gmsh" else real(e))
    rows = [_row(f"g{i}", "gmsh", GAP) for i in range(5)]
    rows += [_row(f"g{i}", "snappy", GAP, "fail" if i < 2 else "pass") for i in range(5)]
    rec = F.recommend(_t(GAP), ["gmsh", "snappy"], table=_table(rows))
    assert rec.engine == "snappy"
    gmsh = rec.fit_of("gmsh")
    assert gmsh.outside and not gmsh.recommended and gmsh.rank == 2
    assert gmsh.heads_up == "Not for a thin gap." and F.fit_words(gmsh) == "not suited"


def test_the_facts_a_turf_row_reads_include_the_brief_and_the_cells_the_budget_buys():
    t = _t({**TUBE, "narrow_vs_volume": 0.2})
    facts = F.facts_of(t, {"cell_budget": 1_000_000, "layers_requested": True, "ground": None})
    assert facts["shape_class"] == F.TUBE and facts["handles"] == 0
    assert facts["cells_across_at_budget"] == pytest.approx(20.0, rel=1e-3)
    assert facts["layers_requested"] is True and "ground" not in facts
    assert "cells_across_at_budget" not in F.facts_of(_t(TUBE))


def test_the_brief_says_what_it_can():
    got = F.brief_facts(request_txt="Internal flow. 5 prism layers on the wall.",
                        patches=[{"name": "inlet", "type": "inlet"}], cell_budget=2e6)
    assert got["layers_requested"] is True and got["cell_budget"] == 2e6
    assert F.brief_facts() == {}


def test_the_ladder_takes_the_measured_order_and_keeps_the_rest_in_their_declared_order():
    assert F.ranked_engines(["cfmesh", "snappy", "vmtk", "gmsh"], ["gmsh", "snappy"]) == \
        ["gmsh", "snappy", "cfmesh", "vmtk"]
    assert F.ranked_engines(["cfmesh", "snappy"], []) == ["cfmesh", "snappy"]


# ---------------------------------------------------------------- the committed table ----
def test_the_committed_table_is_readable_and_says_where_it_came_from():
    data = json.loads(F.TABLE_PATH.read_text())
    prov = data["provenance"]
    assert data["traits_version"] == TRAITS_VERSION
    assert len(prov["base_commit"]) == 40 and len(prov["since_commit"]) == 40
    assert prov["runs"] == len(data["rows"]) > 100
    assert prov["commits"] and prov["tag_prefixes"] and "rule" in prov
    assert data["command"].startswith("devtools/fitness/build_engine_fitness.py")
    from meshpipeline.engines.registry import engine_names
    known = set(engine_names())
    table = F.committed_table()
    assert len(table.runs) == len(data["rows"])
    assert {r.engine for r in table.runs} <= known
    assert {r["status"] for r in data["rows"]} <= {"pass", "fail"}
    # the end-to-end sweeps are in it, counted by their own code rule
    e2e = prov["e2e"]
    assert e2e["runs"] == sum(r.get("source") == "e2e" for r in data["rows"]) > 100
    assert len(e2e["base_commit"]) == 40 and e2e["sweeps"]


def test_the_committed_table_sends_a_detailed_car_to_the_engine_that_meshed_detailed_cars():
    rec = F.recommend(_t(CAR), ["cfmesh", "snappy"], brief={"layers_requested": True})
    assert rec.engine == "snappy", [f.as_dict() for f in rec.fits]
    assert rec.fit_of("cfmesh").evidence.invalid > 0


def _held_out():
    path = REPO / "devtools" / "fitness" / "held_out.py"
    spec = importlib.util.spec_from_file_location("held_out", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("by", ["family", "shape"])
def test_on_shapes_the_table_never_saw_the_recommendation_beats_the_fixed_order(by):
    res = _held_out().evaluate(json.loads(F.TABLE_PATH.read_text()), by=by)
    assert res["cases"] >= 50
    # the measured recommendation picks an engine that passes more often than the declared ladder's
    # first engine did, and comes within a few cases of the best any choice could do
    assert res["recommended_passed"] >= res["ladder_passed"], res
    assert res["recommended_passed"] >= 0.9 * res["any_passed"], res


# ---------------------------------------------------------------- detail, validity, cell shape ----
SMOOTH = {"flow": "external", "form": "surface", "input_kind": "body-surface", "n_triangles": 900,
          "closed": True, "genus": 0, "n_solids": 1, "thickness_ratio": 0.35, "sharp_edges": 3.0}
CAR = {**SMOOTH, "n_triangles": 750_000, "genus": 44, "sharp_edges": 31.6}


def test_an_external_body_is_read_against_bodies_with_as_much_detail_as_it_has():
    assert F.detail_of(_t(CAR)) == F.DETAILED and F.detail_of(_t(SMOOTH)) == F.PLAIN
    assert F.detail_of(_t(TUBE)) == ""
    # cfMesh's long record on smooth bodies, then broken meshes on detailed cars; snappy meshed both
    rows = [_row(f"s{i}", "cfmesh", SMOOTH, seconds=200.0) for i in range(25)]
    rows += [_row(f"car{i}", "cfmesh", CAR, "fail", invalid=True) for i in range(5)]
    rows += [_row(f"car{i}", "cfmesh", CAR, seconds=500.0) for i in range(5, 6)]
    rows += [_row(f"s{i}", "snappy", SMOOTH, seconds=400.0) for i in range(6)]
    rows += [_row(f"car{i}", "snappy", CAR, seconds=700.0) for i in range(6)]
    tab = _table(rows)
    car = F.recommend(_t(CAR), ["cfmesh", "snappy"], table=tab)
    assert car.engine == "snappy", [f.as_dict() for f in car.fits]
    cf = car.fit_of("cfmesh")
    assert cf.evidence.words == "thick bodies in external flow with fine detail, from surface meshes"
    assert "5 of those 6 tries made a broken mesh" in cf.reason
    assert F.shape_words(_t(CAR)) == "a thick body in external flow, with fine detail"
    # the smooth bodies keep their own record: cfMesh, faster, wins the tie there
    assert F.recommend(_t(SMOOTH), ["cfmesh", "snappy"], table=tab).engine == "cfmesh"


def test_a_tie_is_broken_on_broken_meshes_before_the_declared_turf_layers_or_time():
    # the same rate either way; the fast engine's failures were broken meshes, the slow one's not
    rows = [_row(f"b{i}", "cfmesh", SMOOTH, seconds=100.0, layers_pct=100.0) for i in range(19)]
    rows += [_row("b19", "cfmesh", SMOOTH, "fail", quality={"fatal": ["negative-volume cells"]})]
    rows += [_row(f"b{i}", "snappy", SMOOTH, seconds=600.0, layers_pct=90.0) for i in range(19)]
    rows += [_row("b19", "snappy", SMOOTH, "fail", failure_class="mesher_timeout")]
    rec = F.recommend(_t(SMOOTH), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.engine == "snappy", [f.as_dict() for f in rec.fits]
    ev = rec.fit_of("cfmesh").evidence
    assert ev.invalid == 1 and F.validity_step(ev) >= 1 and F.validity_step(rec.fit_of("snappy").evidence) == 0


def test_a_tie_is_broken_on_cells_past_the_shape_limit_before_layers_or_time():
    rows = [_row(f"w{i}", "cfmesh", SMOOTH, seconds=100.0, layers_pct=100.0,
                 quality={"max_non_ortho": 84.0 if i < 6 else 60.0}) for i in range(10)]
    rows += [_row(f"w{i}", "snappy", SMOOTH, seconds=600.0, layers_pct=95.0,
                  quality={"max_non_ortho": 64.9}) for i in range(10)]
    rec = F.recommend(_t(SMOOTH), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.engine == "snappy"
    cf = rec.fit_of("cfmesh")
    assert (cf.evidence.over_limit, cf.evidence.shaped) == (6, 10) and F.quality_step(cf.evidence) == 3
    assert "6 of its 10 meshes had cells more than 70 degrees out of square" in cf.reason
    # a few meshes over the line in a long record is no step: the faster engine keeps the tie
    few = [_row(f"t{i}", "cfmesh", TUBE, seconds=30.0,
                quality={"max_non_ortho": 75.0 if i < 3 else 40.0}) for i in range(37)]
    few += [_row(f"t{i}", "snappy", TUBE, seconds=300.0, quality={"max_non_ortho": 55.0})
            for i in range(37)]
    tube = F.recommend(_t(TUBE), ["cfmesh", "snappy"], table=_table(few))
    assert tube.engine == "cfmesh" and F.quality_step(tube.fit_of("cfmesh").evidence) == 0


def _builder():
    path = REPO / "devtools" / "fitness" / "build_engine_fitness.py"
    spec = importlib.util.spec_from_file_location("build_engine_fitness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _e2e(outcome, stage="done", failure_class="", reason="", **kw):
    return {"case_id": "cars__drivaerml_run_1__drivaer_1.stl.stl", "category": "cars",
            "format": "stl", "outcome": outcome, "stage": stage, "failure_class": failure_class,
            "engine": "cfmesh", "cells": 1124112, "git": {"commit": "aa071f690d17"},
            "tag": "e2e-baseline-main-x", "intake": {"request_txt": "5 prism layers", "patches": []},
            "recommend": {"traits": {**CAR, "version": TRAITS_VERSION}},
            "mesh": {"reason": reason, "quality": {"max_non_ortho": 99.4},
                     "timings": {"total": 670.5}, "layers": {"layer_coverage_pct": 99.3}}, **kw}


def test_the_e2e_sweeps_feed_the_table_with_what_the_engine_did():
    B = _builder()
    assert B.shape_id("cars__drivaerml_run_1__drivaer_1.stl.stl") == "cars__drivaerml_run_1__drivaer_1"
    broken, _ = B.e2e_row(_e2e("failed", "mesh:mesher", "mesher_failed:cfmesh",
                               "[CFMESH] polyMesh cells=1124112 fatal=['negative-volume cells'] "
                               "non_ortho=99.4"))
    assert broken["status"] == "fail" and broken["invalid"] is True
    assert broken["quality"]["fatal"] == ["negative-volume cells"] and broken["seconds"] == 670.5
    assert broken["source"] == "e2e" and broken["brief"]["layers_requested"] is True
    delivered, _ = B.e2e_row(_e2e("concerns"))
    assert delivered["status"] == "pass" and "invalid" not in delivered
    timed_out, _ = B.e2e_row(_e2e("timeout", "mesh:mesher", "mesher_timeout"))
    assert timed_out["status"] == "fail" and "invalid" not in timed_out
    # nothing an engine did not do counts
    for d in (_e2e("refused", "intake", "refused:intake_no_openings"),
              _e2e("failed", "check", "check:scout"),
              _e2e("failed", "mesh:builder", "no_mesh_run:mesh_run_mesh_failed_workspaceextractionerror"),
              _e2e("error", "mesh", "harness:Timeout")):
        row, why = B.e2e_row(d)
        assert row is None and why, d["failure_class"]
    stale = _e2e("pass", recommend={"traits": {**CAR, "version": TRAITS_VERSION + 1}})
    assert B.e2e_row(stale)[0] is None
    # the table reads the row as a broken run of a detailed body
    run = F.Run.from_row(broken)
    assert run.invalid and run.detail == F.DETAILED and run.cls == F.BLUNT


def test_an_engine_never_tried_on_this_kind_of_shape_ranks_below_one_tried_and_mostly_passing():
    rows = [_row(f"t{i}", "vmtk", TUBE) for i in range(30)]               # a long record on tubes
    rows += [_row(f"g{i}", "cfmesh", GAP, "pass" if i < 7 else "fail") for i in range(10)]
    rec = F.recommend(_t(GAP), ["vmtk", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh", [f.as_dict() for f in rec.fits]
    vmtk = rec.fit_of("vmtk")
    assert vmtk.evidence.level > 0 and vmtk.evidence.ranked_runs == 0 and vmtk.score == 0.0
    assert vmtk.reason.startswith("no lab results on shapes exactly like this")


def test_a_quality_fault_outweighs_the_pass_rate_and_the_reason_says_so():
    # the long record passes every try, but most of its meshes are past the cell-shape limit; the
    # short record passes every try with none past it. The rate alone recommended the long record.
    rows = [_row(f"w{i}", "cfmesh", SMOOTH, seconds=300.0, layers_pct=100.0,
                 quality={"max_non_ortho": 80.0 if i < 24 else 60.0}) for i in range(38)]
    rows += [_row(f"w{i}", "snappy", SMOOTH, seconds=240.0, layers_pct=91.0,
                  quality={"max_non_ortho": 64.0}) for i in range(6)]
    rec = F.recommend(_t(SMOOTH), ["cfmesh", "snappy"], table=_table(rows))
    cf, sn = rec.fit_of("cfmesh"), rec.fit_of("snappy")
    assert cf.score > sn.score                       # cfMesh still has the better pass record
    assert rec.engine == "snappy", [f.as_dict() for f in rec.fits]
    assert F.quality_fault(cf.evidence) and not F.quality_fault(sn.evidence)
    assert "recommended over cfMesh" in rec.reason and "24 of its 38 meshes" in rec.reason
    assert F.fit_words(cf) == "quality concerns"
    assert [f.engine for f in rec.fits] == ["snappy", "cfmesh"]      # both stay; the user picks


def test_a_quality_fault_never_lifts_an_engine_that_mostly_fails_or_was_never_tried():
    rows = [_row(f"w{i}", "cfmesh", SMOOTH, quality={"max_non_ortho": 80.0}) for i in range(20)]
    rows += [_row(f"w{i}", "snappy", SMOOTH, "pass" if i < 2 else "fail") for i in range(10)]
    rec = F.recommend(_t(SMOOTH), ["cfmesh", "snappy", "gmsh"], table=_table(rows))
    assert rec.engine == "cfmesh", [f.as_dict() for f in rec.fits]
    assert "recommended over" not in rec.reason


def test_layers_on_under_half_the_wall_are_a_quality_fault():
    rows = [_row(f"t{i}", "cfmesh", TUBE, seconds=30.0, layers_pct=20.0) for i in range(30)]
    rows += [_row(f"t{i}", "snappy", TUBE, seconds=300.0, layers_pct=95.0) for i in range(8)]
    rec = F.recommend(_t(TUBE), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.engine == "snappy"
    assert "near-wall layers on only 20% of the wall" in rec.reason


def test_a_layer_fault_needs_as_many_measured_layers_as_shapes():
    # one measured coverage among many passes is not a record of thin layers
    rows = [_row(f"t{i}", "cfmesh", TUBE, seconds=30.0, layers_pct=20.0 if i == 0 else None)
            for i in range(30)]
    rows += [_row(f"t{i}", "snappy", TUBE, seconds=300.0, layers_pct=95.0) for i in range(8)]
    rec = F.recommend(_t(TUBE), ["cfmesh", "snappy"], table=_table(rows))
    assert not F.quality_fault(rec.fit_of("cfmesh").evidence)
    assert rec.engine == "cfmesh"


# ---------------------------------------------------------------- the written rules (ENG-1..5) ----
@pytest.mark.parametrize("passed,runs,lower,upper", [
    (7, 8, 0.529, 0.978), (14, 19, 0.512, 0.882), (30, 30, 0.886, 1.0), (19, 20, 0.764, 0.991),
    (0, 0, 0.0, 1.0)])
def test_the_bounds_are_wilsons_95_percent_interval(passed, runs, lower, upper):
    assert F.wilson_lower(passed, runs) == pytest.approx(lower, abs=1e-3)
    assert F.wilson_upper(passed, runs) == pytest.approx(upper, abs=1e-3)


def _record(engine, traits, passed, runs, *, shapes=None, prefix="s", **kw):
    shapes = shapes or runs
    return [_row(f"{prefix}{engine}{i % shapes}", engine, traits, "pass" if i < passed else "fail", **kw)
            for i in range(runs)]


def test_seven_of_eight_never_outranks_fourteen_of_nineteen():
    # a lower bound alone would put 7/8 (52.9%) over 14/19 (51.2%): a run short of the 20 the
    # ranking trusts counts as a miss, so the longer record ranks first
    rows = _record("cfmesh", GAP, 7, 8) + _record("snappy", GAP, 14, 19)
    rec = F.recommend(_t(GAP), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.ranking == ["snappy", "cfmesh"], [f.as_dict() for f in rec.fits]
    cf = rec.fit_of("cfmesh")
    assert not cf.evidence.proven and cf.rule == "ENG-4"
    assert "only 8 runs on thin gaps or annuli, fewer than the 20 the ranking trusts" in cf.reason


def test_soundness_never_reorders_two_engines_without_a_fault():
    # 7/8's own lower bound (52.9%) clears the "sound" bar and 14/20's (48.1%) does not; that bar
    # only separates sound engines from FAULTED ones, so the padded lower bound ranks these two
    rows = _record("cfmesh", GAP, 7, 8) + _record("snappy", GAP, 14, 20)
    rec = F.recommend(_t(GAP), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.ranking == ["snappy", "cfmesh"], [f.as_dict() for f in rec.fits]


def test_a_proven_record_says_how_sure_it_is():
    rows = _record("cfmesh", GAP, 30, 30)
    fit = F.recommend(_t(GAP), ["cfmesh"], table=_table(rows)).fit_of("cfmesh")
    assert fit.evidence.proven
    assert "from 30 of 30 on thin gaps or annuli its pass rate is at least 89%, 95% sure" in fit.reason


class _Vmtk:
    name, ladder_rank, delivered_mesh = "vmtk", 30, None
    home_classes = (F.TUBE, F.BRANCHED)
    home_turf = (TurfRow("slenderness", ">", 100.0, "weak", "Long and thin.", flow="internal"),)


@pytest.fixture
def vmtk_home(monkeypatch):
    real = F._spec
    monkeypatch.setattr(F, "_spec", lambda e: _Vmtk() if e == "vmtk" else real(e))


def test_the_home_engine_goes_first_even_behind_on_the_record(vmtk_home):
    rows = _record("vmtk", TUBE, 14, 19) + _record("cfmesh", TUBE, 19, 20, prefix="c")
    rec = F.recommend(_t(TUBE), ["cfmesh", "vmtk"], table=_table(rows))
    assert rec.engine == "vmtk" and rec.ranking == ["vmtk", "cfmesh"], [f.as_dict() for f in rec.fits]
    top = rec.fit_of("vmtk")
    assert top.home and top.rule == "ENG-2" and rec.fit_of("cfmesh").rule == "ENG-4"
    # 19/20 is good but not clearly better: its lower bound (76%) is under VMTK's upper bound (88%)
    assert "first as the home engine for long passages about as wide as their openings" in rec.reason
    assert "cfMesh's record here does not clearly beat it (19 of 20 against 14 of 19)" in rec.reason


def test_a_challenger_overrides_home_only_when_it_clearly_wins(vmtk_home):
    # 30/30 on 10 shapes: lower bound 88.6% above the home engine's upper bound 88.2%
    rows = _record("vmtk", TUBE, 14, 19) + _record("cfmesh", TUBE, 30, 30, shapes=10, prefix="c")
    rec = F.recommend(_t(TUBE), ["vmtk", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh", [f.as_dict() for f in rec.fits]
    assert rec.fit_of("cfmesh").rule == "ENG-3" and not rec.fit_of("vmtk").home
    assert "recommended over the home engine VMTK: 30 of 30 clearly beats its 14 of 19" in rec.reason
    # the same record on too few different shapes does not override
    few = _record("vmtk", TUBE, 14, 19) + _record("cfmesh", TUBE, 30, 30, shapes=7, prefix="c")
    assert F.recommend(_t(TUBE), ["vmtk", "cfmesh"], table=_table(few)).engine == "vmtk"
    # nor does a record with a quality fault
    bad = _record("vmtk", TUBE, 14, 19) + _record("cfmesh", TUBE, 30, 30, shapes=10, prefix="c",
                                                     quality={"max_non_ortho": 85.0})
    assert F.recommend(_t(TUBE), ["vmtk", "cfmesh"], table=_table(bad)).engine == "vmtk"


def test_an_engine_that_did_not_clear_eng3_never_jumps_the_home_engine(vmtk_home):
    # cfMesh clears ENG-3 against VMTK; snappy's 40/40 sits on only 7 shapes, so it does not. VMTK
    # loses first place to cfMesh, never to snappy
    rows = (_record("vmtk", TUBE, 14, 19) + _record("cfmesh", TUBE, 30, 30, shapes=10, prefix="c")
            + _record("snappy", TUBE, 40, 40, shapes=7, prefix="n"))
    rec = F.recommend(_t(TUBE), ["vmtk", "cfmesh", "snappy"], table=_table(rows))
    assert rec.ranking == ["cfmesh", "vmtk", "snappy"], [f.as_dict() for f in rec.fits]
    assert rec.fit_of("cfmesh").rule == "ENG-3"


def test_a_home_engine_never_tried_on_its_class_cannot_be_overridden(vmtk_home):
    rows = _record("cfmesh", TUBE, 40, 40, shapes=20)
    rec = F.recommend(_t(TUBE), ["cfmesh", "vmtk"], table=_table(rows))
    assert rec.engine == "vmtk" and rec.fit_of("vmtk").evidence.upper == 1.0
    assert "its home turf is unproven here" in rec.reason


def test_home_turf_holds_only_where_its_own_turf_does_not_mark_the_case_weak(vmtk_home):
    long_thin = {**TUBE, "slenderness": 300.0}
    rows = _record("vmtk", TUBE, 14, 19) + _record("cfmesh", long_thin, 19, 20, prefix="c")
    rec = F.recommend(_t(long_thin), ["vmtk", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh" and not rec.fit_of("vmtk").home
    assert rec.fit_of("vmtk").heads_up == "Long and thin."
    # and only on its declared classes: a chamber is ranked on the record alone
    rows = _record("vmtk", CHAMBER, 4, 20) + _record("cfmesh", CHAMBER, 18, 20, prefix="c")
    assert F.recommend(_t(CHAMBER), ["vmtk", "cfmesh"], table=_table(rows)).engine == "cfmesh"


def test_a_home_engine_whose_own_long_record_is_faulted_loses_first_place(vmtk_home):
    rows = _record("vmtk", TUBE, 20, 20, quality={"fatal": ["negative-volume cells"]})
    rows = [{**r, "status": "fail"} if i < 15 else r for i, r in enumerate(rows)]
    rows += _record("cfmesh", TUBE, 12, 12, prefix="c")
    rec = F.recommend(_t(TUBE), ["vmtk", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh" and not rec.fit_of("vmtk").home, [f.as_dict() for f in rec.fits]


def test_every_engine_declares_home_only_on_classes_the_measurement_knows():
    from meshpipeline.engines.registry import engine_names, get_spec
    known = {F.TUBE, F.BRANCHED, F.CHAMBER, F.THIN_GAP, F.OBSTACLES, F.SLENDER, F.BLUNT, F.MULTI_REGION}
    declared = {n: tuple(get_spec(n).home_classes) for n in engine_names()}
    assert all(set(c) <= known for c in declared.values()), declared
    # VMTK is the home engine for every tubular passage
    assert set(declared["vmtk"]) == {F.TUBE, F.BRANCHED}


def test_the_ladder_walks_the_same_ranking_and_never_a_fixed_order(monkeypatch):
    from meshpipeline.engines.capability import ladder_order
    from meshpipeline.pipeline import engine_fallback as EF
    measured = {"engine_fallback": {"order": ["gmsh", "snappy"]}}
    monkeypatch.setattr(EF, "record_of", lambda s: s.get("engine_fallback") or {})
    order = EF.fallback_order("internal", measured)
    assert order[:2] == ("gmsh", "snappy") and set(order) == set(ladder_order("internal"))
    # nothing measured: the same rules on the whole flow's record, not the declared ladder_rank
    unmeasured = EF.fallback_order("internal", {})
    assert unmeasured == F.flow_ranking("internal")
    assert set(unmeasured) == set(ladder_order("internal"))


# ---------------------------------------------------------------- the committed table's guards ----
def _held_out_digests() -> set[str]:
    path = REPO / "devtools" / "fitness" / "held_out.sha256"
    return {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def test_no_held_out_file_ever_reaches_the_committed_table():
    data = json.loads(F.TABLE_PATH.read_text())
    held = _held_out_digests()
    assert len(held) == data["split"]["held_out_files"] > 100
    assert all(r.get("split") == "dev" for r in data["rows"])
    assert all(len(str(r.get("input_sha256") or "")) == 64 for r in data["rows"])
    leaked = [r["input_sha256"] for r in data["rows"] if r["input_sha256"] in held]
    assert not leaked, f"{len(leaked)} held-out rows in the engine table"


def test_the_committed_table_holds_only_runs_where_a_mesher_ran_and_no_relabels():
    rows = json.loads(F.TABLE_PATH.read_text())["rows"]
    classes = {str(r.get("failure_class") or "").split(":", 1)[0] for r in rows}
    assert not classes & {"no_mesh_run", "geometry_refused", "recounted"}, classes


def test_the_table_is_versioned_and_every_version_is_in_the_changelog():
    data = json.loads(F.TABLE_PATH.read_text())
    B = _builder()
    assert data["rules_version"] == B.RULES_VERSION
    assert data["rows_sha256"] == B.rows_digest(data["rows"]), "rows changed without a new digest"
    assert data["version"].endswith(data["rows_sha256"][:8])
    log = (REPO / "docs" / "engines" / "fitness-changelog.md").read_text()
    assert f"## {data['version']}" in log and data["rows_sha256"] in log
    assert F.committed_table().version == data["version"]


def test_no_setting_swaps_the_table_at_run_time():
    src = (REPO / "src" / "meshpipeline" / "engines" / "fitness.py").read_text()
    for reader in ("os.environ", "getenv", "optional_env", "bool_env", "settings."):
        assert reader not in src, reader


def test_the_builder_keeps_dev_rows_only_and_counts_the_rest():
    B = _builder()
    split = B.Split({"version": "t", "manifest_sha256": "m",
                     "dev": [{"sha256": "d" * 64, "case_ids": ["c1"]}],
                     "holdout": [{"sha256": "h" * 64, "case_ids": ["c2"]}]})
    from collections import Counter
    excluded: Counter = Counter()
    rows = [{"case": "a", "input_sha256": "d" * 64, "failure_class": ""},
            {"case": "b", "input_sha256": "h" * 64, "failure_class": ""},
            {"case": "c", "input_sha256": "x" * 64, "failure_class": ""},
            {"case": "d", "input_sha256": "d" * 64, "failure_class": "no_mesh_run:port_binding"},
            {"case": "e", "input_sha256": "d" * 64, "failure_class": "geometry_refused:x"},
            {"case": "f", "input_sha256": "d" * 64, "failure_class": "recounted:wrong region"},
            {"case": "g", "failure_class": ""}]
    kept = B.keep_dev_rows(rows, split, excluded)
    assert [r["case"] for r in kept] == ["a"] and kept[0]["split"] == "dev"
    assert excluded == Counter({"held out (frozen split)": 1, "file not in the frozen split": 1,
                                "never meshed": 2, "hand relabel (re-run the case instead)": 1,
                                "input file not found": 1})
    assert split.held_out == ["h" * 64]


def test_the_builder_refuses_to_build_without_the_split(capsys):
    with pytest.raises(SystemExit):
        _builder().main(["--lab-rows-from", str(F.TABLE_PATH)])
    assert "--split is required" in capsys.readouterr().err


def test_an_e2e_run_that_never_meshed_is_no_evidence_whatever_its_stage_says():
    B = _builder()
    for fc in ("no_mesh_run:port_binding", "no_mesh_run:no_openings", "geometry_refused:geometry_rejected"):
        row, why = B.e2e_row(_e2e("failed", "mesh", fc))
        assert row is None and why == "e2e never meshed", fc


# the NASA juncture-flow model on its 14x22 sting (AoA -2.5 deg, a dev-side CAD file): its measured
# traits as the geometry check read them (e2e verify-round5-final-merged-v). On the old bundle cfMesh
# was picked on its raw record (38 of 38 passed, 24 of them past 70 degrees) and the real path
# delivered it with concerns; snappyHexMesh delivered it clean (rp5c). Pinned on the committed table.
STING = {"version": TRAITS_VERSION, "flow": "external", "form": "cad", "input_kind": "solid-body",
         "n_triangles": 2508, "n_solids": 1, "closed": True, "genus": 0, "sharp_edges": 0.7321,
         "knife_edges": 0.0, "scale_ratio": 24.6613, "thin_wall_fraction": 0.0,
         "thickness_ratio": 0.0673, "gap_share": 0.0}


def test_the_committed_table_keeps_the_juncture_flow_sting_on_snappy():
    brief = F.brief_facts(request_txt="External aerodynamics around the body in air. Near-wall: "
                                      "wall functions, 5 prism layers.",
                          patches=[{"name": "body", "type": "wall"},
                                   {"name": "farfield", "type": "farfield"}])
    rec = F.recommend(_t(STING), ["cfmesh", "snappy", "gmsh"], brief=brief)
    assert F.shape_class(_t(STING)) == F.SLENDER and F.detail_of(_t(STING)) == F.PLAIN
    assert rec.engine == "snappy", [f.as_dict() for f in rec.fits]
    cf = rec.fit_of("cfmesh")
    assert F.quality_fault(cf.evidence), cf.as_dict()
    assert "recommended over cfMesh" in rec.reason
