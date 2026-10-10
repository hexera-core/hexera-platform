"""HOME TURF: every engine's declared rows are well formed, name only known facts, and read into the
verdict the rows say (outside beats weak beats home; an unmeasured fact claims nothing)."""
from __future__ import annotations

import pytest

from meshpipeline.engines.base import TURF_OPS, TURF_SEVERITIES, TurfRow
from meshpipeline.engines.home_turf import TURF_FACTS, row_problems, rows_for, verdict
from meshpipeline.engines.registry import ENGINE_CATALOG

IMPLEMENTED = {n: s for n, s in ENGINE_CATALOG.items() if s.implemented}


@pytest.mark.parametrize("name", sorted(IMPLEMENTED))
def test_rows_are_well_formed(name):
    assert row_problems(IMPLEMENTED[name]) == []


@pytest.mark.parametrize("name", sorted(IMPLEMENTED))
def test_rows_only_speak_about_flows_the_engine_is_designed_for(name):
    spec = IMPLEMENTED[name]
    for row in spec.home_turf:
        if row.flow:
            assert spec.forms_for(row.flow) is not None, (name, row)


def test_the_four_general_engines_declare_a_turf():
    for name in ("snappy", "cfmesh", "gmsh", "snappy_multiregion"):
        sevs = {r.severity for r in IMPLEMENTED[name].home_turf}
        assert "home" in sevs, name
        assert sevs & {"weak", "outside"}, f"{name} declares no weak spot"


def test_every_fact_says_where_it_comes_from():
    for fact, meta in TURF_FACTS.items():
        assert meta.source and meta.words and meta.kind in ("number", "bool", "str", "count"), fact


@pytest.mark.parametrize("op,value,got,expect", [
    ("<", 12.0, 7.9, True), ("<", 12.0, 14.0, False), (">=", 2, 2, True), ("<=", 1, 2, False),
    (">", 0.3, 0.5, True), ("==", "fluid-domain", "fluid-domain", True), ("!=", "x", "x", False),
    ("in", ("a", "b"), "a", True), ("not in", ("a", "b"), "a", False),
    ("is", True, True, True), ("is", True, False, False), ("is", False, 0, True),
])
def test_ops(op, value, got, expect):
    assert op in TURF_OPS
    assert TurfRow("passage", op, value, "weak", "w").holds({"passage": got}) is expect


def test_an_unmeasured_fact_claims_nothing():
    row = TurfRow("thin_wall_fraction", ">", 0.3, "weak", "w")
    assert row.holds({}) is None
    assert row.holds({"thin_wall_fraction": None}) is None
    assert row.holds({"thin_wall_fraction": "n/a"}) is None


def test_flow_scoping():
    row = TurfRow("layers_requested", "is", True, "outside", "w", flow="external")
    assert row.applies_to("external") and not row.applies_to("internal")
    assert TurfRow("closed", "is", False, "weak", "w").applies_to("internal")


def test_verdict_order_outside_then_weak_then_home():
    gmsh = IMPLEMENTED["gmsh"]
    ext = verdict(gmsh, "external", {"layers_requested": True, "sharp_edges": 20.0})
    assert ext.severity == "outside" and ext.outside and ext.weak_spots
    assert ext.reasons == tuple(r.words for r in ext.outside)
    internal = verdict(gmsh, "internal", {"input_kind": "fluid-domain", "layers_requested": False})
    assert internal.severity == "home" and internal.strengths
    weak = verdict(gmsh, "internal", {"input_kind": "fluid-domain", "layers_requested": True})
    assert weak.severity == "weak"


def test_verdict_with_no_facts_is_home_and_lists_what_was_not_measured():
    v = verdict(IMPLEMENTED["snappy"], "external", {})
    assert v.severity == "home" and not v.strengths
    assert "thin_wall_fraction" in v.unmeasured


def test_multiregion_is_not_for_one_solid():
    mr = IMPLEMENTED["snappy_multiregion"]
    assert verdict(mr, "multi-region", {"n_solids": 1}).severity == "outside"
    assert verdict(mr, "multi-region", {"n_solids": 2}).severity == "home"


def test_cfmesh_ground_is_outside_and_snappy_ground_is_home():
    facts = {"ground": True}
    assert verdict(IMPLEMENTED["cfmesh"], "external", facts).severity == "outside"
    assert verdict(IMPLEMENTED["snappy"], "external", facts).severity == "home"


def test_rows_for_filters_by_flow():
    snappy = IMPLEMENTED["snappy"]
    assert all(r.applies_to("internal") for r in rows_for(snappy, "internal"))
    assert len(rows_for(snappy, "")) == len(snappy.home_turf)


def test_severity_vocabulary():
    assert TURF_SEVERITIES == ("home", "weak", "outside")
