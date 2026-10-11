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


def test_thin_evidence_backs_off_to_broader_shapes_and_says_so():
    rows = [_row("gap0", "snappy", GAP)]                       # one gap shape only
    rows += [_row(f"t{i}", "snappy", TUBE) for i in range(4)]
    ev = F.evidence_for(_table(rows), "snappy", flow="internal", cls=F.THIN_GAP, form="cad")
    assert ev.level > 0 and ev.low
    assert ev.words == "internal-flow shapes from CAD" and ev.shapes == 5
    assert ev.near_runs == 1 and ev.near_shapes == 1
    assert F.evidence_words(ev) == (
        "little evidence on shapes like this: passed 1 of 1 tries on 1 shape like this (thin gaps or "
        "annuli, from CAD); passed 5 of 5 similar shapes (internal-flow shapes from CAD)")
    none = F.evidence_for(_table(rows[1:]), "snappy", flow="internal", cls=F.THIN_GAP, form="cad")
    assert F.evidence_words(none).startswith("no lab results on shapes exactly like this; passed 4 of 4")


def test_the_few_runs_on_shapes_exactly_like_this_still_count_in_the_rate():
    # two failures on the only two thin gaps seen pull the rate down from the broad record,
    # as much as two runs can - never ignored because there are fewer than MIN_SHAPES of them
    rows = [_row(f"gap{i}", "snappy", GAP, "fail") for i in range(2)]
    rows += [_row(f"t{i}", "snappy", TUBE) for i in range(20)]
    rows += [_row(f"gap{i}", "cfmesh", GAP) for i in range(2)]
    rows += [_row(f"t{i}", "cfmesh", TUBE) for i in range(20)]
    tab = _table(rows)
    snappy = F.evidence_for(tab, "snappy", flow="internal", cls=F.THIN_GAP, form="cad")
    assert snappy.rate < 0.6, snappy
    assert F.recommend(_t(GAP), ["snappy", "cfmesh"], table=tab).engine == "cfmesh"


def test_a_few_passes_do_not_outrank_a_long_record():
    rows = [_row(f"a{i}", "snappy", TUBE) for i in range(3)]
    rows += [_row(f"b{i}", "cfmesh", TUBE) for i in range(30)]
    rows += [_row("b0", "cfmesh", TUBE, "fail"), _row("b1", "cfmesh", TUBE, "fail")]
    rows += [_row(f"c{i}", "snappy", CHAMBER, "fail") for i in range(6)]   # snappy's wider record
    rec = F.recommend(_t(TUBE), ["snappy", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh", [f.as_dict() for f in rec.fits]


def test_a_tie_on_rate_goes_to_measured_layers_then_to_time():
    rows = []
    for i in range(4):
        rows += [_row(f"s{i}", "snappy", TUBE, layers_pct=95.0, seconds=300.0),
                 _row(f"s{i}", "cfmesh", TUBE, seconds=30.0)]
    rec = F.recommend(_t(TUBE), ["cfmesh", "snappy"], table=_table(rows))
    assert rec.engine == "snappy"
    assert "near-wall layers on 95% of the wall" in rec.reason
    rows = [_row(f"s{i}", e, TUBE, seconds=s) for i in range(4) for e, s in (("snappy", 300.0),
                                                                            ("cfmesh", 30.0))]
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


def test_an_engine_never_tried_on_this_kind_of_shape_ranks_below_one_tried_and_mostly_passing():
    rows = [_row(f"t{i}", "vmtk", TUBE) for i in range(30)]               # a long record on tubes
    rows += [_row(f"g{i}", "cfmesh", GAP, "pass" if i < 7 else "fail") for i in range(10)]
    rec = F.recommend(_t(GAP), ["vmtk", "cfmesh"], table=_table(rows))
    assert rec.engine == "cfmesh", [f.as_dict() for f in rec.fits]
    vmtk = rec.fit_of("vmtk")
    assert vmtk.evidence.level > 0 and vmtk.evidence.weight == F.PRIOR_RUNS
    assert vmtk.reason.startswith("no lab results on shapes exactly like this")
