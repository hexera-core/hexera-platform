# Responsibility: Verify the localiser's judgement - its status vocabulary, its thresholds and its summary - without a CAD kernel.
from __future__ import annotations

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import (
    _STATUS_TO_CODE,
    REGION_EXTERIOR,
    REGION_INTERIOR,
    REGION_UNKNOWN,
    SMALL_EDGE_LENGTH_FRACTION,
    SMALL_FACE_AREA_FRACTION,
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


# THE FINGERPRINT A DEFECT CARRIES


def test_a_defect_carries_a_fingerprint_so_a_repair_can_aim_at_it_and_a_scan_can_look_for_it():
    # `entity_index` is an index into ONE shape's map and ShapeFix renumbers everything it
    # touches, so after a repair "is edge 12 fixed?" is unanswerable from the index alone. The
    # fingerprint is the name that survives the renumbering.
    defect = _defect(DefectCode.small_edge)
    assert hasattr(defect, "fingerprint")
    assert defect.fingerprint == "", "a defect built without one must not invent it"


def test_a_defects_fingerprint_reaches_its_payload():
    from meshpipeline.cad.repair.localize import EntityDefect

    defect = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                          entity_type="edge", entity_index=4, message="x",
                          fingerprint="edge:abcd1234")
    assert defect.to_dict()["fingerprint"] == "edge:abcd1234"


def test_stamping_names_every_defect_that_has_a_box_and_leaves_the_rest_empty():
    from meshpipeline.cad.repair.localize import EntityDefect, _stamped

    located = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                           entity_type="edge", entity_index=1, message="x",
                           location={"unit": "mm", "bbox_min": [0, 0, 0], "bbox_max": [1, 0, 0]})
    # UNLOCATED IS A REAL CASE: `_centroid_and_box` returns {} for a degenerate entity, and the
    # status walk attributes some findings to a sub-shape it cannot index. Such a defect is
    # UNTARGETABLE, and the planner reports skipping it rather than aiming at a guess.
    unlocated = EntityDefect(code=DefectCode.invalid_brep, severity=DefectSeverity.error,
                             entity_type="shell", entity_index=0, message="x")

    stamped = _stamped((located, unlocated), diagonal_mm=100.0)

    assert stamped[0].fingerprint.startswith("edge:")
    assert stamped[1].fingerprint == ""
    # Nothing else about the defect changed - the stamp is additive, so a report comparison across
    # this change sees only the new key.
    assert stamped[0].code is located.code and stamped[0].entity_index == 1


def test_two_defects_on_entities_sharing_a_box_are_still_distinguishable():
    # A duplicated face carries two defects whose boxes are identical. They must not collapse onto
    # one name, or the session would think fixing one fixed both.
    from meshpipeline.cad.repair.localize import EntityDefect, _stamped

    box = {"unit": "mm", "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 0]}
    pair = tuple(EntityDefect(code=DefectCode.curve_inconsistency, severity=DefectSeverity.error,
                              entity_type="face", entity_index=i, message="x", location=box)
                 for i in (3, 4))

    stamped = _stamped(pair, diagonal_mm=100.0)

    assert stamped[0].fingerprint != stamped[1].fingerprint


# ONE ENTITY, ONE DEFECT PER CODE
#
# `_status_defects` emits one defect per kernel status, and `TopExp_Explorer` enumerates a shared
# sub-shape once per owning parent - so one edge can be visited twice and two kernel statuses can
# map to one product code. Left in, the duplicate records make `refs_for` believe two entities
# share a box, which suffixes both names `#0`/`#1`. After a repair clears one of them the survivor
# is stamped WITHOUT a suffix, so the progress rule looks for a name that no longer exists and
# keeps an operation whose targeted defect is still there.


def test_the_same_code_reported_twice_on_one_entity_is_one_defect():
    from meshpipeline.cad.repair.localize import EntityDefect, _deduped

    box = {"unit": "mm", "bbox_min": [0, 0, 0], "bbox_max": [1, 0, 0]}
    # "NotClosed" and "FreeEdge" are different kernel words for one opening, and the explorer can
    # hand back the same edge twice. Neither duplicate carries information the first does not.
    twice = [EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                          entity_type="edge", entity_index=4, message="x", location=box,
                          kernel_status=status)
             for status in ("NotClosed", "FreeEdge")]

    assert len(_deduped(twice)) == 1
    assert _deduped(twice)[0].kernel_status == "NotClosed", "the first record must be the kept one"


def test_two_different_codes_on_one_entity_are_two_defects():
    # Deduplication is per CODE: one edge can genuinely be both a sliver and a broken curve, and
    # collapsing those would hide one of them.
    from meshpipeline.cad.repair.localize import EntityDefect, _deduped

    box = {"unit": "mm", "bbox_min": [0, 0, 0], "bbox_max": [1, 0, 0]}
    pair = [EntityDefect(code=code, severity=DefectSeverity.error, entity_type="edge",
                         entity_index=4, message="x", location=box)
            for code in (DefectCode.small_edge, DefectCode.curve_inconsistency)]

    assert len(_deduped(pair)) == 2


def test_two_codes_on_one_entity_keep_the_unsuffixed_name_so_a_later_scan_can_find_them():
    # THE BUG THIS PINS: suffixed `edge:abc#0` / `edge:abc#1` here, then `edge:abc` after the
    # repair that cleared one of them, so `_progress_refusal` found neither and kept an operation
    # that had fixed nothing.
    from meshpipeline.cad.repair.localize import EntityDefect, _stamped

    box = {"unit": "mm", "bbox_min": [0, 0, 0], "bbox_max": [1, 0, 0]}
    pair = tuple(EntityDefect(code=code, severity=DefectSeverity.error, entity_type="edge",
                              entity_index=4, message="x", location=box)
                 for code in (DefectCode.curve_inconsistency, DefectCode.small_edge))

    stamped = _stamped(pair, diagonal_mm=100.0)

    assert "#" not in stamped[0].fingerprint
    assert stamped[0].fingerprint == stamped[1].fingerprint
