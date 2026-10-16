# Responsibility: Verify a repair's result is judged on the ORIGINAL upload and on what the operations said they would add - not on the previous iteration, and not on a blanket allowance.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import (
    Accounting,
    Measure,
    cumulative_refusal,
    sanity_refusal,
)

# The kernel is exercised in the native tier. What is unit-testable here is the whole of the
# judgement: which measured change is damage, which is accounted for, and which is the repair
# working. These are the checks that stand between a customer's part and a confident mutation of
# it, so they are tested on numbers rather than inferred from a successful run.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _changed(**fields) -> Measure:
    return Measure(**{**_SOUND.to_dict(), **fields})


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)


# PER-OPERATION SANITY - counts only, because this runs before the expensive re-inspection


def test_an_operation_that_left_no_faces_is_refused():
    assert "no faces" in sanity_refusal(_SOUND, _changed(faces=0))


def test_an_operation_that_lost_a_solid_is_refused():
    # A closed body became loose surfaces. Damage however it happened.
    assert "solid" in sanity_refusal(_SOUND, _changed(solids=0, faces=20))


def test_an_operation_that_gained_a_solid_is_accepted():
    # A patched shell promoted to a solid is the repair WORKING, and the count rising is how that
    # looks from here.
    assert sanity_refusal(_SOUND, _changed(solids=2)) == ""


def test_an_operation_that_deleted_a_face_is_refused_unless_defeaturing_is_allowed(monkeypatch):
    assert "face" in sanity_refusal(_SOUND, _changed(faces=19))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", True)
    assert sanity_refusal(_SOUND, _changed(faces=19)) == ""


def test_an_operation_that_turned_a_valid_shape_invalid_is_refused():
    assert "invalid" in sanity_refusal(_SOUND, _changed(valid=False))


def test_an_operation_on_an_already_invalid_shape_that_stays_invalid_is_not_refused_for_that():
    # Most of the shapes this loop sees are invalid on arrival. Refusing an operation for failing
    # to make the shape fully valid in one step would refuse every useful partial repair.
    was = _changed(valid=False)
    assert sanity_refusal(was, _changed(valid=False)) == ""


def test_a_sound_result_is_accepted():
    assert sanity_refusal(_SOUND, _SOUND) == ""


# CUMULATIVE - always against the ORIGINAL


def test_a_tolerance_over_the_cap_is_refused():
    reason = cumulative_refusal(_SOUND, _changed(max_tolerance_mm=0.5),
                                declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert "tolerance" in reason


def test_a_part_whose_overall_size_moved_past_the_deviation_cap_is_refused():
    reason = cumulative_refusal(_SOUND, _changed(diagonal_mm=201.0),
                                declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert "diagonal" in reason


def test_small_steps_cannot_drift_past_a_cap_together_because_nothing_is_measured_against_the_last():
    # THE WHOLE POINT OF CUMULATIVE. Six operations each move the part 0.0009 of its original
    # diagonal, so each step is inside the 0.001 cap taken alone. Every iteration is measured against
    # the SAME original and the drift accumulates: 0.0009, 0.0018, ... 0.0054. The first step is
    # accepted; from the second on the accumulated drift is past the cap and the run is refused.
    verdicts = []
    for step in range(1, 7):
        current = _changed(diagonal_mm=200.0 * (1.0 + 0.0009 * step))
        verdicts.append(cumulative_refusal(_SOUND, current, declared_area_mm2=0.0,
                                           declared_volume_mm3=0.0))
    assert verdicts[0] == ""
    assert all("diagonal" in reason for reason in verdicts[1:])


# ACCOUNTED AREA AND VOLUME


def test_area_that_nobody_declared_is_refused_even_though_the_old_allowance_permitted_it():
    # The whole-shape repair allowed a 75% area change, because a hole patch moves area and there
    # was no way to say by how much. 5% of unexplained area is now refused.
    reason = cumulative_refusal(_SOUND, _changed(surface_area_mm2=1050.0),
                                declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert "area" in reason


def test_area_an_operation_declared_is_accepted():
    # A patch that said "I am adding 50 mm2" and added 50 mm2 is accounted for, not a surprise.
    assert cumulative_refusal(_SOUND, _changed(surface_area_mm2=1050.0),
                              declared_area_mm2=50.0, declared_volume_mm3=0.0) == ""


def test_area_that_missed_its_own_declaration_by_more_than_the_residual_is_refused():
    # Declared 50, moved 200. The operation does not understand what it did, which is worse than
    # not having declared anything.
    reason = cumulative_refusal(_SOUND, _changed(surface_area_mm2=1200.0),
                                declared_area_mm2=50.0, declared_volume_mm3=0.0)
    assert "area" in reason


def test_volume_an_operation_declared_is_accepted():
    # The volume twin of the area acceptance above: a fill that said "I am adding 500 mm3" and
    # added 500 mm3 is accounted for, not a surprise.
    assert cumulative_refusal(_SOUND, _changed(volume_mm3=8500.0),
                              declared_area_mm2=0.0, declared_volume_mm3=500.0) == ""


def test_volume_that_appears_where_there_was_none_is_accepted_because_that_is_a_shell_closing():
    # An open shell has no volume. Patching it and promoting it to a solid is the repair working,
    # and there is no original volume for the new one to be a fraction of.
    open_shell = _changed(solids=0, volume_mm3=0.0)
    closed = _changed(solids=1, volume_mm3=8000.0)
    assert cumulative_refusal(open_shell, closed, declared_area_mm2=0.0,
                              declared_volume_mm3=0.0) == ""


def test_volume_that_moved_unaccounted_on_a_part_that_already_had_some_is_refused():
    # Something moved INSIDE the part. The bounding box cannot see it; volume can.
    reason = cumulative_refusal(_SOUND, _changed(volume_mm3=7000.0),
                                declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert "volume" in reason


def test_a_zero_original_measurement_is_no_opinion_rather_than_infinite_change():
    # A shape with no extent reaches here - an empty compound, a single vertex. Dividing by its
    # zero area is the bug; having no opinion is the answer.
    nothing = Measure(valid=True, solids=0, shells=0, faces=1, edges=0, vertices=0,
                      diagonal_mm=0.0, max_tolerance_mm=0.0,
                      surface_area_mm2=0.0, volume_mm3=0.0)
    assert cumulative_refusal(nothing, nothing, declared_area_mm2=0.0,
                              declared_volume_mm3=0.0) == ""


# NON-FINITE MEASUREMENTS - an unmeasurable result must refuse, never pass


_NON_FINITE = [
    ("diagonal_mm", float("nan")),
    ("max_tolerance_mm", float("nan")),
    ("surface_area_mm2", float("nan")),
    ("volume_mm3", float("nan")),
    ("surface_area_mm2", float("inf")),
    ("volume_mm3", float("inf")),
]


@pytest.mark.parametrize("field,value", _NON_FINITE)
def test_a_non_finite_measurement_of_the_run_is_refused_rather_than_accepted(field, value):
    # NaN compares False against every cap, so without the guard a NaN would sail through.
    reason = cumulative_refusal(_SOUND, _changed(**{field: value}),
                                declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert reason != ""
    assert "the run's own measurement could not be taken" in reason


@pytest.mark.parametrize("field,value", _NON_FINITE)
def test_a_non_finite_measurement_of_the_upload_is_refused_rather_than_accepted(field, value):
    # A NaN upload makes `original > 0` False, which skips the diagonal check, and makes the
    # residual NaN, which compares False against the residual. Both would accept.
    reason = cumulative_refusal(_changed(**{field: value}), _SOUND,
                                declared_area_mm2=0.0, declared_volume_mm3=0.0)
    assert reason != ""
    assert "the upload's own measurement could not be taken" in reason


def test_a_non_finite_upload_is_refused_even_when_the_run_is_entirely_finite():
    # The case the current-side guard alone would miss: every value the run produced is sound, but
    # the thing it is bounded against was never measured.
    nan_upload = _changed(diagonal_mm=float("nan"))
    assert cumulative_refusal(nan_upload, _SOUND, declared_area_mm2=0.0,
                              declared_volume_mm3=0.0) != ""


# SPARSE AND ROUND-TRIPPED MEASUREMENTS


def test_from_dict_of_an_empty_payload_is_a_zeroed_invalid_measure():
    assert Measure.from_dict({}) == Measure(
        valid=False, solids=0, shells=0, faces=0, edges=0, vertices=0,
        diagonal_mm=0.0, max_tolerance_mm=0.0, surface_area_mm2=0.0, volume_mm3=0.0)


def test_from_dict_reads_explicit_none_values_as_zero():
    m = Measure.from_dict({"volume_mm3": None, "surface_area_mm2": None, "solids": None})
    assert m.volume_mm3 == 0.0
    assert m.surface_area_mm2 == 0.0
    assert m.solids == 0


def test_from_dict_reads_an_older_payload_that_predates_area_and_volume():
    payload = {"valid": True, "diagonal_mm": 200.0, "max_tolerance_mm": 0.01,
               "solids": 1, "shells": 1, "faces": 20, "edges": 60, "vertices": 40}
    m = Measure.from_dict(payload)
    assert m.surface_area_mm2 == 0.0
    assert m.volume_mm3 == 0.0
    assert m.diagonal_mm == 200.0
    assert m.faces == 20


def test_a_measure_survives_a_round_trip_through_its_dict():
    assert Measure.from_dict(_SOUND.to_dict()) == _SOUND


# THE ACCOUNTING LEDGER


def test_the_accounting_adds_up_what_each_operation_declared():
    led = Accounting().plus({"area_delta_mm2": 10.0, "volume_delta_mm3": 5.0}) \
                      .plus({"area_delta_mm2": 2.5})
    assert led.declared_area_mm2 == pytest.approx(12.5)
    assert led.declared_volume_mm3 == pytest.approx(5.0)


def test_an_operation_that_declared_nothing_contributes_nothing():
    # ShapeFix and sewing move no surface on purpose, so they declare zero and saying nothing
    # means zero rather than unknown.
    assert Accounting().plus({}).declared_area_mm2 == 0.0


def test_a_rolled_back_operations_declaration_is_never_added():
    # Proven by construction: the ledger only ever hears about an operation the session KEPT, and
    # `plus` returns a new ledger rather than mutating, so a discarded one leaves no trace.
    led = Accounting().plus({"area_delta_mm2": 10.0})
    assert led.plus({"area_delta_mm2": 99.0}).declared_area_mm2 == pytest.approx(109.0)
    assert led.declared_area_mm2 == pytest.approx(10.0)
