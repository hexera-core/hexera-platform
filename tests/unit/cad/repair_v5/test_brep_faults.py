"""v5 step 5: the invalid_brep_tolerance detectors' rules, the units rule, fault attachment and re-measuring - on
synthetic facts. No kernel."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v5 import brep_faults as F
from meshpipeline.cad.repair_v5 import diagnose as D
from meshpipeline.cad.repair_v5 import loop

REV = {("F", 1): "F10", ("F", 2): "F20", ("E", 3): "E30"}


def _facts(**kw: Any) -> dict:
    return {"diag_mm": 100.0, "open_box": False, "max_tolerance": 1e-6, "tol_threshold_mm": 0.1,
            "tolerance": {"edges": [], "faces": []}, "splines": [], "curve_splines": [], "poles": [], **kw}


def test_spline_thresholds_flag_degree_knots_and_parameter_speed() -> None:
    ok = {"face": 1, "udeg": 3, "vdeg": 3, "uknots": 8, "vknots": 8, "speed_ratio_u": 4.0, "speed_ratio_v": 2.0}
    fast = {**ok, "face": 2, "speed_ratio_v": F.MAX_SPEED_RATIO * 3}
    assert F.faults_of(_facts(splines=[ok]), REV) == []
    [fault] = F.faults_of(_facts(splines=[ok, fast]), REV)
    assert fault.kind == "bad_spline" and fault.entities == ("F20",) and "speed ratio" in fault.why
    high = {**ok, "udeg": F.MAX_SPLINE_DEGREE + 1}
    dense = {"edge": 3, "deg": 3, "knots": F.MAX_SPLINE_KNOTS + 1, "speed_ratio": 1.0}
    assert "degree" in F.faults_of(_facts(splines=[high]), REV)[0].why
    # an edge curve alone is aimed by its edge id
    assert F.faults_of(_facts(curve_splines=[dense]), REV)[0].entities == ("E30",)


def test_tolerance_and_pole_facts_become_faults_on_stable_ids_and_unplaceable_indices_are_dropped() -> None:
    facts = _facts(tolerance={"edges": [[3, 0.5], [99, 0.4]], "faces": [], "edges_over": 2},
                   max_tolerance=0.5, poles=[{"face": 1, "edge": 3, "why": "degenerate_not_collapsed"}])
    got = {f.kind: f for f in F.faults_of(facts, REV)}
    assert got["large_tolerance"].entities == ("E30",) and got["pole"].entities == ("F10",)


def test_an_unbounded_part_is_ONE_fault_and_nothing_else_is_judged() -> None:
    facts = _facts(diag_mm=3.5e100, splines=[{"face": 1, "udeg": 20}])
    assert [f.kind for f in F.faults_of(facts, REV)] == ["unbounded"]


# --- the units rule (owner decision A, 2026-11-25): a clear mismatch is REPORTED, never rescaled --------------------

def test_metre_values_under_a_mm_header_are_reported_as_a_mismatch_never_a_fault() -> None:
    v = F.units_verdict("mm", 0.12, 1e-7)          # a 120 mm part written in metres under a mm header
    assert v["verdict"] == "mismatch" and v["factor"] == 1000.0
    # the report field stays; the fault (and so a defect and a rescale) does not
    assert [f.kind for f in F.faults_of({"diag_mm": 0.12, "max_tolerance": 1e-7}, REV, header_unit="mm")] == []
    assert "units" not in F.KIND_REPAIRS


def test_rescale_is_in_no_class_menu_and_no_rule_seed() -> None:
    assert all(op != "rescale" for menu in loop.CLASS_REPAIRS.values() for r in menu for op in r)


def test_a_plausible_part_is_consistent_and_never_rescaled() -> None:
    assert F.units_verdict("mm", 150.0, 1e-6)["verdict"] == "consistent"
    assert F.units_verdict("inch", 150.0, 1e-6)["verdict"] == "consistent"


def test_ambiguity_or_no_header_is_report_only() -> None:
    # 0.3 mm under mm: x1000 (300 mm) and x25.4 (7.6 mm) both make a typical part -> not clear
    v = F.units_verdict("mm", 0.3, 1e-7)
    assert v["verdict"] == "report_only" and "more than one" in v["why"]
    assert F.units_verdict(None, 0.1, 1e-7)["verdict"] == "report_only"
    # a 300 m part under a metre header: the only metre mistake (mm values) gives 0.3 m - typical, so it IS clear
    assert F.units_verdict("m", 3e5, 1e-3)["factor"] == 0.001
    # a 300 m part under a mm header: no mm-header mistake shrinks a part -> report only
    assert F.units_verdict("mm", 3e5, 1e-3)["verdict"] == "report_only"


def test_an_absurd_tolerance_on_a_plausible_part_is_a_tolerance_fault_never_a_rescale() -> None:
    """jig, seam_variant on 00607178: a 10.8 mm seam tolerance on a 126 mm part. A tolerance ratio is unit-invariant."""
    v = F.units_verdict("mm", 126.3, 10.8)
    assert v["verdict"] == "report_only" and "not a units one" in v["why"]


@pytest.mark.parametrize(("text", "unit"), [
    ("#5=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));", "mm"),
    ("#5=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));", "m"),
    ("#6=(CONVERSION_BASED_UNIT('INCH',#7)LENGTH_UNIT()NAMED_UNIT(#8));#9=(SI_UNIT(.MILLI.,.METRE.));", "inch"),
    ("#1=CARTESIAN_POINT('',(0.,0.,0.));", None),
])
def test_the_step_header_length_unit_is_read(text: str, unit: str | None) -> None:
    assert F.step_length_unit(text) == unit


# --- attaching faults to defects ---------------------------------------------------------------------------

def _site(sid: str, entities: list[str]) -> Site:
    return Site(sid, "unlocated", entities, [], [], None, {}, None)


def test_an_unknown_mesher_failure_on_a_pole_face_becomes_invalid_brep_tolerance_with_its_aim() -> None:
    d = Defect("S@1", "mesher", "other", ("F10",), member="gmsh_prod", cls="unknown", rule="nothing locates other")
    pole = F.Fault("pole", ("F10", "F77"), {"poles": []}, "degenerate_not_collapsed")
    [out] = D.attach_faults([d], [pole], {"S@1": _site("S@1", ["F10"])})
    assert out.cls == "invalid_brep_tolerance" and out.cause["kind"] == "pole" and out.cause["aim"] == ["F10"]
    assert loop.table_key(out) == "invalid_brep_tolerance:pole"
    assert seq.params_for("fix_poles", out)[0] == ["F10"]


def test_a_brep_invalid_defect_takes_the_most_local_fault_kind() -> None:
    d = Defect("S@1", "validity", "brep_invalid", ("B1", "F10", "E30"), cls="invalid_brep_tolerance", rule="r")
    faults = [F.Fault("large_tolerance", ("E30",), {}, "tol"), F.Fault("bad_spline", ("F10",), {}, "speed")]
    [out] = D.attach_faults([d], faults, {"S@1": _site("S@1", ["F10"])})
    assert out.cause["kind"] == "bad_spline" and out.cause["aim"] == ["F10"]
    assert loop.table_key(out) == "invalid_brep_tolerance:bad_spline"
    # with no fault on it, the brep_invalid row (tighten_tolerances first) aimed at its own entities
    [plain] = D.attach_faults([d], [], {})
    assert loop.table_key(plain) == "invalid_brep_tolerance:brep_invalid"
    assert loop.CLASS_REPAIRS["invalid_brep_tolerance:brep_invalid"][0] == ("tighten_tolerances",)


def test_unattached_faults_become_defects_only_where_layer1_reported_them() -> None:
    tol = F.Fault("large_tolerance", ("E30",), {}, "tol")
    spline = F.Fault("bad_spline", ("F10",), {}, "speed")
    assert D.attach_faults([], [tol, spline], {}) == []
    got = D.attach_faults([], [tol, spline], {}, soft_codes=frozenset({"large_tolerance"}))
    assert [d.code for d in got] == ["large_tolerance"]


# --- re-measuring: a fault layer 1's local check cannot see is judged by the detector ----------------------------

def test_a_tolerance_repair_is_judged_by_RE_MEASURING_not_by_layer_1s_blind_local_check(monkeypatch) -> None:
    before = SimpleNamespace(history=[], brep_path="/b")
    after = SimpleNamespace(history=[], brep_path="/a")
    blind = LocalResult(gone=True, new_hard=[], detail={}, seconds=0.0)     # local_check never sees tolerances
    d = Defect("T@large_tolerance", "validity", "large_tolerance", ("E30",), cls="invalid_brep_tolerance",
               cause={"kind": "large_tolerance", "aim": ["E30"]})
    still = [F.Fault("large_tolerance", ("E30",), {}, "tolerance 0.5 mm")]
    monkeypatch.setattr(D, "measure_faults", lambda pool, ws, header_unit=None: (still, {"diag_mm": 1.0}))
    verify = loop.remeasuring(lambda *a, **k: blind)
    assert verify(None, before, after, d, [], timeout_s=1).gone is False
    monkeypatch.setattr(D, "measure_faults", lambda pool, ws, header_unit=None: ([], {"diag_mm": 1.0}))
    assert verify(None, before, after, d, [], timeout_s=1).gone is True
    # a mesher defect keeps the member's own answer
    m = Defect("S@1", "mesher", "other", ("F1",), member="gmsh_prod", cause={"kind": "pole"})
    assert verify(None, before, after, m, [], timeout_s=1) is blind


def test_a_brep_invalid_the_local_check_never_saw_is_judged_through_the_STEP_round_trip(monkeypatch) -> None:
    """jig (532be4434c61, 78cbab541afb): tighten_tolerances read "gone" on a brep_invalid that exists only after the
    STEP round trip layer 1 judges; the full check still failed. Blind 'gone' must not win a tournament."""
    from meshpipeline.cad.repair_v4 import stages
    from meshpipeline.cad.repair_v4.verdict import valid

    ws = SimpleNamespace(history=[], brep_path="/a", ids={"F9": "fp", "B1": "fp"}, indices={"F9": 3, "B1": 1})
    blind = LocalResult(gone=True, new_hard=[], detail={"before": ["open_shell B1"]}, seconds=0.0)
    d = Defect("S@1", "validity", "brep_invalid", ("F9",), cls="invalid_brep_tolerance")
    still = SimpleNamespace(hard=[SimpleNamespace(code="brep_invalid", entities=["F3"])])
    monkeypatch.setattr(stages, "step_for", lambda pool, brep: "/a.step")
    monkeypatch.setattr(valid, "check_valid", lambda pool, step, original_units=None: still)
    verify = loop.remeasuring(lambda *a, **k: blind)
    assert verify(None, ws, ws, d, [], timeout_s=1).gone is False
    monkeypatch.setattr(valid, "check_valid", lambda pool, step, original_units=None: SimpleNamespace(hard=[]))
    assert verify(None, ws, ws, d, [], timeout_s=1).gone is True
    # a local check that DID see it before keeps its own answer
    seeing = LocalResult(gone=True, new_hard=[], detail={"before": ["brep_invalid B1"]}, seconds=0.0)
    assert loop.remeasuring(lambda *a, **k: seeing)(None, ws, ws, d, [], timeout_s=1) is seeing
