# The reviewer failed an axis on a number its own evidence ledger did not hold.
#
# MEASURED, job 110daca9's 279,281,786-cell Corintis cold plate: the layer pass covered 90.6422%
# of the 16,582,198-face conjugate interface and kept 2.00 of 3 layers, both under
# `cfmesh_conjugate/layers`' own floors - and the review failed `near_interface_resolution` on
# exactly that. But `layer_coverage_pct` was declared by no criterion and asked for by no axis, so
# `agents/reviewer/assurance.derive_assurance_plan` never put it in `required_metric_keys`,
# `collect_deterministic_evidence` never filed it, and `ledger.usable_metric("layer_coverage_pct")`
# returned None. The figure reached the model only as a SENTENCE inside the advisory `layer_short`
# row, and a sentence cannot be cited: the one citable metric on file was non-orthogonality, a true
# number about a different property.
#
# `engines/cfmesh` and `engines/snappy_multiregion` have declared this key since they were written
# (their own tests sit beside this one). This engine produced the figure and declared no bar on it.
#
# These tests pin the three things that fix has to be: the bar exists on the engine's OWN floor, it
# agrees with `layers.shortfalls` at the floor itself, and an unmeasured coverage stays a stated
# gap rather than becoming missing evidence that ends a review.
from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    advisory_gaps,
    deterministic_evidence_complete,
)
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.contracts.review_evidence import MetricRequirement
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.cfmesh_conjugate import criteria as CC
from meshpipeline.engines.cfmesh_conjugate import layers as LAYERS
from meshpipeline.engines.quality_criteria import criteria_for, evaluate, measurements_from_manifest
from meshpipeline.engines.registry import get_spec
from meshpipeline.engines.review_types import Criterion

#: What the delivered run actually measured, as its own manifest recorded it.
MEASURED_COVERAGE_PCT = 90.6422
#: The hand-built run every one of this engine's layer floors was fitted on.
CLEAN_COVERAGE_PCT = 95.553

KEY = "layer_coverage_pct"
#: The bar on the PASS, as distinct from the figure about the part. See
#: layers.KEPT_OF_OFFERED_FLOOR_PCT.
KEPT_KEY = "layer_kept_of_offered_pct"
PURPOSE = "conjugate_heat_transfer"


def _crit(key=KEY):
    return next((c for c in criteria_for("cfmesh_conjugate") if c.key == key), None)


def _plan():
    return derive_assurance_plan(get_spec("cfmesh_conjugate"), PURPOSE)


def _ledger(measurements):
    led = EvidenceLedger()
    collect_deterministic_evidence(
        _plan(), led, measurements=measurements,
        criteria={c.key: c for c in criteria_for("cfmesh_conjugate")},
        gate_status={})
    return led


def test_the_engine_declares_a_bar_on_the_figure_it_measures():
    c = _crit()
    assert c is not None, "cfmesh_conjugate declares no bar on layer_coverage_pct"
    assert c.gating is False, (
        "GATING THIS WOULD THROW AWAY FINISHED MESHES. The layer pass moves the only copy of the "
        "coolant mesh and rewrites it in place, which is why layers.shortfalls says a miss "
        "rather than raising it.")


def test_the_bar_on_the_pass_is_a_different_row_now():
    """THE COVERAGE FLOOR WAS FITTED TO THE WRONG QUANTITY.

    This asserted `_crit().threshold == LAYERS.COVERAGE_FLOOR_PCT`, which was 92.0 - a number
    placed just above one defective run's 90.7146%. Job b8f09ae9 then measured what the
    extruder was OFFERED: 91.1145% of the interface, of which it kept 99.5641%. So 8.89 of the
    9.28 missing points were declined by `featureAngle` before the mover ran - the part's own
    shape - and both good runs of the plate sat under 92% because of it.

    The coverage row stays, because the reviewer has to be able to cite the figure, and the
    only requirement left on it is the one that is true: layers grew somewhere.
    """
    assert not hasattr(LAYERS, "COVERAGE_FLOOR_PCT"), \
        "the coverage floor is back; it reads the part's shape as a defect in the pass"
    assert (_crit().op, _crit().threshold) == (">", 0.0), \
        "coverage is citable, and the only bar left on it is that it is positive"
    kept = _crit(KEPT_KEY)
    assert kept is not None, "the pass is judged by nothing at all"
    # Not `== 95.0`: a restated literal is a second source of truth that drifts.
    assert (kept.op, kept.threshold) == (">=", LAYERS.KEPT_OF_OFFERED_FLOOR_PCT)
    assert kept.gating is False, "the pass has already rewritten the only copy of the mesh"


def test_the_bar_and_the_shortfall_agree_at_the_floor_itself():
    # The one value where ">" and ">=" disagree. `layers.shortfalls` calls `kept < FLOOR` the
    # shortfall, so the floor itself is a PASS, and the criterion has to say the same.
    floor = float(LAYERS.KEPT_OF_OFFERED_FLOOR_PCT)
    said = LAYERS.shortfalls({"coverage_pct": floor, "kept_of_offered_pct": floor,
                              "patches": {}},
                             interface_patch="interface", n_layers=3, cells=1)
    assert not [s for s in said if "offered it" in s], "shortfalls refuses the floor itself"
    assert _crit(KEPT_KEY).evaluate({KEPT_KEY: floor}) is True, \
        "the criterion refuses the floor itself"


def test_the_measured_run_now_passes_on_coverage_and_is_judged_on_what_it_kept():
    """The same 90.6422% that used to fail. It passes the coverage row now - layers DID grow -
    and the row that judges the pass is the kept-of-offered one, which cites its own floor."""
    row = next(r for r in evaluate("cfmesh_conjugate", {KEY: MEASURED_COVERAGE_PCT})
               if r["key"] == KEY)
    assert row["measured"] == MEASURED_COVERAGE_PCT
    assert row["passed"] is True, "coverage is no longer a bar a real cold plate fails"
    assert row["gating"] is False
    kept = next(r for r in evaluate("cfmesh_conjugate", {KEPT_KEY: 99.5641})
                if r["key"] == KEPT_KEY)
    assert kept["passed"] is True
    assert str(LAYERS.KEPT_OF_OFFERED_FLOOR_PCT) in kept["ok_when"]
    # ...and a mover that lost a quarter of what it was handed still fails
    bad = next(r for r in evaluate("cfmesh_conjugate", {KEPT_KEY: 75.0})
               if r["key"] == KEPT_KEY)
    assert bad["passed"] is False


def test_the_run_the_floors_were_fitted_on_clears_the_bar():
    assert _crit().evaluate({KEY: CLEAN_COVERAGE_PCT}) is True


def test_the_figure_reaches_the_reviewers_required_metrics():
    assert KEY in _plan().required_metric_keys


def test_the_key_is_a_manifest_quality_key_and_arrives_through_the_manifest():
    manifest = {"quality": {KEY: MEASURED_COVERAGE_PCT, "rc": 0, "timed_out": False},
                "patch_face_counts": {"wall": 10}, "patch_types": {"wall": "wall"}}
    assert measurements_from_manifest(manifest).get(KEY) == MEASURED_COVERAGE_PCT


def test_a_finding_can_cite_the_measured_shortfall():
    """Both numbers are citable, and only one of them is a miss now.

    Coverage is on file so a finding can quote it - that was the whole reason the row exists -
    and it PASSES at 90.6422%, because the bar on coverage is only that layers grew. The miss a
    finding cites is the kept-of-offered one, which is about the pass rather than the part.
    """
    m = _ledger({KEY: MEASURED_COVERAGE_PCT}).usable_metric(KEY)
    assert m is not None, "the figure is on file as nothing a finding can cite"
    assert m.passed is True, "a real cold plate no longer reads as a coverage miss"
    assert "90.6422" in m.summary, m.summary
    short = _ledger({KEPT_KEY: 75.0}).usable_metric(KEPT_KEY)
    assert short is not None and short.passed is False
    assert "75" in short.summary, short.summary


def test_an_unmeasured_coverage_is_a_stated_gap_and_never_missing_evidence():
    # An unmeasured ADVISORY number must not end a review - it did once, on a mesh that passed
    # every gate (eligibility.advisory_gaps).
    led = _ledger({"max_non_ortho": 60.0})
    assert KEY in advisory_gaps(led)
    _ok, missing = deterministic_evidence_complete(_plan(), led)
    assert f"metric:{KEY}" not in missing


def test_a_non_finite_coverage_is_not_a_measurement():
    assert _crit().evaluate({KEY: float("nan")}) is None
    assert _crit().evaluate({KEY: float("inf")}) is None


def test_the_shared_cht_axis_is_not_made_to_oblige_a_key_other_engines_never_write():
    # The obligation sits on this engine's OWN interface axis. `near_interface_resolution` is the
    # purpose's, shared by every CHT engine, and must not demand a figure they do not publish.
    shared = next(a for a in _plan().axes if a.name == "near_interface_resolution")
    assert MetricRequirement(KEY) not in tuple(getattr(shared, "requires", ()))
    mine = next(a for a in CC.REVIEW_AXES if a.name == "interface_continuity")
    assert MetricRequirement(KEY) in mine.requires


def test_a_floor_that_includes_its_own_value_is_an_op_a_criterion_can_hold():
    # Without ">=" this bar cannot be written at all: the only switch on op raises on it.
    c = Criterion(key="k", label="l", op=">=", threshold=92.0, gating=False,
                  rationale="", evidence_url="")
    assert c.evaluate({"k": 92.0}) is True
    assert c.evaluate({"k": 91.9}) is False
    assert Criterion(key="k", label="l", op=">", threshold=92.0, gating=False,
                     rationale="", evidence_url="").evaluate({"k": 92.0}) is False
