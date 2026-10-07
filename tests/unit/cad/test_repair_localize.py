# Responsibility: Verify the localiser's judgement - its status vocabulary, its thresholds and its summary - without a CAD kernel.
from __future__ import annotations

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import (
    REGION_EXTERIOR,
    REGION_INTERIOR,
    REGION_UNKNOWN,
    SMALL_EDGE_LENGTH_FRACTION,
    SMALL_FACE_AREA_FRACTION,
    _STATUS_TO_CODE,
    EntityDefect,
    summarise,
)

# The kernel walk itself is proved in the native tier against real geometry. What is unit-testable
# here is the part that is pure judgement: which kernel status means which product defect, how big
# is "too small", and what a caller reads off the summary. Those are the pieces a future change is
# most likely to get subtly wrong, and they need no OpenCASCADE to pin.


def _defect(code, *, severity=DefectSeverity.error, entity_type="edge", index=1,
            region=REGION_UNKNOWN) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type=entity_type,
                        entity_index=index, message="x", region=region)


# THE STATUS VOCABULARY


def test_every_mapped_status_names_a_real_defect_code_and_severity():
    for status, (code, severity) in _STATUS_TO_CODE.items():
        assert isinstance(code, DefectCode), status
        assert isinstance(severity, DefectSeverity), status


def test_an_opening_is_reported_as_an_open_shell_however_the_kernel_words_it():
    # three different kernel statuses, one product meaning - a customer should not see the
    # difference between OCCT's names for the same hole
    for status in ("NotClosed", "FreeEdge", "EmptyShell"):
        assert _STATUS_TO_CODE[status][0] is DefectCode.open_shell, status


def test_an_orientation_problem_is_not_reported_as_a_non_manifold_one():
    # they route differently - orientation is repairable, non-manifold is a judgement - so
    # conflating them would send an automatic repair at a defect it must not touch
    assert _STATUS_TO_CODE["BadOrientation"][0] is DefectCode.bad_orientation
    assert _STATUS_TO_CODE["InvalidMultiConnexity"][0] is DefectCode.non_manifold_surface


def test_the_statuses_that_mean_a_judgement_is_needed_are_errors_not_warnings():
    for status in ("SelfIntersectingWire", "IntersectingWires", "InvalidMultiConnexity"):
        assert _STATUS_TO_CODE[status][1] is DefectSeverity.error, status


def test_redundancy_is_a_warning_because_it_does_not_stop_a_mesh():
    for status in ("RedundantEdge", "RedundantWire"):
        assert _STATUS_TO_CODE[status][1] is DefectSeverity.warning, status


def test_every_mapped_code_is_one_triage_has_a_rule_for():
    # a localiser that emits a code triage cannot read makes triage abstain on a part we DID
    # diagnose, which is the worst of both: we know what is wrong and we advise nothing
    from meshpipeline.cad.repair.triage import (
        _CUSTOMER_MUST_ACT,
        _NEEDS_JUDGEMENT,
        _REPAIRABLE,
    )

    known = _REPAIRABLE | _NEEDS_JUDGEMENT | _CUSTOMER_MUST_ACT
    for status, (code, _) in _STATUS_TO_CODE.items():
        assert code.value in known, f"{status} -> {code.value} has no triage rule"


def test_the_size_defects_triage_can_read_too():
    from meshpipeline.cad.repair.triage import _NEEDS_JUDGEMENT, _REPAIRABLE

    assert DefectCode.small_face.value in _NEEDS_JUDGEMENT | _REPAIRABLE
    assert DefectCode.small_edge.value in _NEEDS_JUDGEMENT | _REPAIRABLE


# THE THRESHOLDS


def test_the_thresholds_are_relative_so_they_hold_at_any_scale():
    # an absolute millimetre threshold would call every feature on a watch part a defect and miss
    # every one on an aircraft part
    assert 0 < SMALL_FACE_AREA_FRACTION < 1
    assert 0 < SMALL_EDGE_LENGTH_FRACTION < 1


def test_an_edge_threshold_tighter_than_the_face_one():
    # short edges are what forces tiny cells, so edges are watched more closely than areas
    assert SMALL_EDGE_LENGTH_FRACTION > SMALL_FACE_AREA_FRACTION


# WHAT A READER GETS


def test_the_summary_counts_by_kind_and_by_place():
    found = (
        _defect(DefectCode.open_shell, region=REGION_INTERIOR),
        _defect(DefectCode.open_shell, index=2, region=REGION_EXTERIOR),
        _defect(DefectCode.small_face, severity=DefectSeverity.warning,
                entity_type="face", region=REGION_INTERIOR),
    )

    out = summarise(found)

    assert out["total"] == 3
    assert out["by_code"] == {"open_shell": 2, "small_face": 1}
    # THE QUESTION THE REGION SPLIT ANSWERS: is something wrong inside the part, where nobody
    # spots it by rotating the model in a viewer
    assert out["by_region"] == {REGION_INTERIOR: 2, REGION_EXTERIOR: 1}


def test_an_empty_summary_is_empty_rather_than_zeroed():
    out = summarise(())
    assert out == {"total": 0, "by_code": {}, "by_region": {}}


def test_a_defect_serialises_everything_a_repair_would_need_to_aim():
    defect = EntityDefect(
        code=DefectCode.open_shell, severity=DefectSeverity.error,
        entity_type="edge", entity_index=8, message="a hole",
        region=REGION_INTERIOR,
        measurements={"boundary_edges": [8, 12, 2, 6], "boundary_length": 40.0},
        location={"centroid": [5.0, 5.0, 10.0]},
        kernel_status="FreeBounds")

    payload = defect.to_dict()

    # the stable identity a later report compares against
    assert payload["entity"] == "edge:8"
    assert payload["entity_type"] == "edge" and payload["entity_index"] == 8
    # where to look, how big, and on which side of the part
    assert payload["location"]["centroid"] == [5.0, 5.0, 10.0]
    assert payload["measurements"]["boundary_edges"] == [8, 12, 2, 6]
    assert payload["region"] == REGION_INTERIOR
    # the kernel's own word for it, kept beside our reading of it
    assert payload["kernel_status"] == "FreeBounds"


def test_an_unplaceable_defect_says_unknown_rather_than_guessing_the_outside():
    # claiming a defect is on the outside when it is in a cavity sends an operator to the wrong
    # place; `unknown` is the honest answer when the shape cannot be classified
    assert _defect(DefectCode.open_shell).to_dict()["region"] == REGION_UNKNOWN
