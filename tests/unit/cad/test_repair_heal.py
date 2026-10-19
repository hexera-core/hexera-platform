# Responsibility: Verify heal_self_intersections' claim rule, its decline order, its area declaration before the splice and its never-grow rule, with every kernel call replaced by a double.
# Boundaries: no kernel - the private seams, the face lookup, the splice and the measuring helpers are monkeypatched.
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.localize as localize
import meshpipeline.cad.repair.operations as operations
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityMapping, EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import (
    AREA_DELTA,
    FILL_OPS,
    MAX_HEAL_FACES,
    HealSelfIntersections,
    _grew,
)


class _Untouchable:
    """The input shape. Any attribute read on it - the first step of any in-place mutation - is
    recorded and fails, so the operation itself can only pass it on to a read-only seam."""

    touched: list[str] = []

    def __getattr__(self, name):
        _Untouchable.touched.append(name)
        raise AssertionError(f"the operation touched the input shape: .{name}")


SHAPE = _Untouchable()
#: The seams that may be handed the input shape, each of which only reads it. Everything that
#: writes (`_rebuild_face`, `splice`) must be handed the operation's own copy instead; that the
#: copy really protects the input is pinned natively, byte for byte.
_READ_ONLY = {"sub_shape", "_analyze_faces", "_owners", "_total_face_area", "_copied"}


def _face_ref(index: int) -> EntityRef:
    return EntityRef(entity_type="face", fingerprint=f"face:f{index}", entity_index=index)


TARGETS = (_face_ref(3), _face_ref(7), _face_ref(9))

WIRE_LEVEL_SELF_INTERSECTION = EntityDefect(
    code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="wire",
    entity_index=4, message="SelfIntersectingWire", fingerprint="wire:w4",
    kernel_status="SelfIntersectingWire")
FACE_FINDING_WITHOUT_CROSSINGS = EntityDefect(
    code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="face",
    entity_index=3, message="IntersectingWires", fingerprint="face:f3",
    kernel_status="IntersectingWires")
FACE_FINDING_WITH_PARTNERS = EntityDefect(
    code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="face",
    entity_index=3, message="crosses 2", fingerprint="face:f3", kernel_status="SelfInterference",
    measurements={"crossing_face_indices": [7, 9], "crossing_faces": ["face:f7", "face:f9"]})

_MAPPING = EntityMapping(moved={"face:f3": "face:g3"}, gone=frozenset(), basis="kernel")


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)


class _Harness:
    """The operation, plus the record of which seams it called and with what.

    `n_targets`, when set, replaces the targets the test passes with that many distinct face refs,
    so the isolation bound can be tested through the same `apply(SHAPE, TARGETS, ...)` call."""

    def __init__(self, calls, received, n_targets):
        self.op = HealSelfIntersections()
        self.calls = calls
        self.received = received
        self._n_targets = n_targets

    def apply(self, shape, targets, *, tolerance_mm):
        if self._n_targets is not None:
            targets = tuple(_face_ref(10 + k) for k in range(self._n_targets))
        return self.op.apply(shape, targets, tolerance_mm=tolerance_mm)


@pytest.fixture
def heal_doubles(monkeypatch):
    """A factory: configure every seam, get back a harness whose `.calls` logs seam order."""
    calls: list[str] = []
    received: list[str] = []

    def configure(*, flag=True, resolved=None, n_targets=None, pre_pairs=((3, 7), (3, 9)),
                  owners=1, area_delta_ratio=0.001, grew=False, seams_lost=False,
                  post_pairs=(), box_moves=True):
        calls.clear()
        received.clear()
        _Untouchable.touched.clear()

        def logged(name, value):
            def double(*args, **kwargs):
                calls.append(name)
                if any(a is SHAPE for a in (*args, *kwargs.values())):
                    received.append(name)
                return value(*args, **kwargs) if callable(value) else value
            return double

        def resolve(shape, entity_type, index):
            if resolved is not None and resolved == 0:
                return None
            return f"FACE{index}"

        analyses = [None if pairs is None else list(pairs) for pairs in (pre_pairs, post_pairs)]

        def analyze(shape, faces):
            return analyses.pop(0)

        def area(face):
            rebuilt = str(face).startswith("REBUILT")
            calls[-1] = f"_face_area:{'rebuilt' if rebuilt else 'original'}"
            return 100.0 + (area_delta_ratio * 1000.0 / 3 if rebuilt else 0.0)

        identities = [frozenset({"a", "b"}), frozenset({"a"} if grew else {"a", "b"})]
        seams = [frozenset({"s1", "s2"}), frozenset({"s1"} if seams_lost else {"s1", "s2"})]

        def box(face):
            moved = box_moves and str(face).startswith("REBUILT")
            x = float("".join(ch for ch in str(face) if ch.isdigit()) or 0)
            return {"bbox_min": [x, 0.0, 0.0],
                    "bbox_max": [x + 10.0, 10.0, 10.5 if moved else 10.0]}

        monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", flag)
        monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
        monkeypatch.setattr(operations, "sub_shape", logged("sub_shape", resolve))
        monkeypatch.setattr(operations, "_analyze_faces", logged("_analyze_faces", analyze))
        monkeypatch.setattr(operations, "_owners",
                            logged("_owners", lambda s, f: set(range(owners))))
        monkeypatch.setattr(operations, "_total_face_area", logged("_total_face_area", 1000.0))
        monkeypatch.setattr(operations, "_copied", logged("_copied", "COPY"))
        monkeypatch.setattr(localize, "_diagonal", logged("_diagonal", 100.0))
        monkeypatch.setattr(localize, "_face_area", logged("_face_area", area))
        monkeypatch.setattr(operations, "_rebuild_face", logged(
            "_rebuild_face", lambda face, *, tolerance_mm: f"REBUILT-{face}"))
        monkeypatch.setattr(operations, "_outside_identities", logged(
            "_outside_identities", lambda s, f: identities.pop(0)))
        monkeypatch.setattr(operations, "_seams", logged("_seams", lambda s, f: seams.pop(0)))
        monkeypatch.setattr(operations, "splice", logged("splice", ("SPLICED", "CONTEXT")))
        monkeypatch.setattr(operations, "box_of", logged("box_of", box))
        monkeypatch.setattr(operations, "mapping_from_context",
                            logged("mapping_from_context", _MAPPING))
        return _Harness(calls, received, n_targets)

    return configure


def _assert_untouched(harness):
    assert _Untouchable.touched == []
    assert set(harness.received) <= _READ_ONLY, (
        f"handed the input to {set(harness.received) - _READ_ONLY}")


# WHAT IT CLAIMS


def test_it_claims_only_face_findings_that_carry_crossing_evidence(flag_on):
    heal = HealSelfIntersections()
    assert heal.targets_for(WIRE_LEVEL_SELF_INTERSECTION) == ()
    assert heal.targets_for(FACE_FINDING_WITHOUT_CROSSINGS) == ()
    refs = heal.targets_for(FACE_FINDING_WITH_PARTNERS)        # face 3, partners 7 and 9
    assert [(r.entity_type, r.entity_index) for r in refs] == [("face", 3), ("face", 7), ("face", 9)]


def test_with_its_flag_off_it_offers_no_targets(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    assert HealSelfIntersections().targets_for(FACE_FINDING_WITH_PARTNERS) == ()


def test_the_partner_refs_carry_the_stamped_partner_fingerprints(flag_on):
    refs = HealSelfIntersections().targets_for(FACE_FINDING_WITH_PARTNERS)
    assert [r.fingerprint for r in refs] == ["face:f3", "face:f7", "face:f9"]


@pytest.mark.parametrize("measurements", [
    {"crossing_face_indices": [7, 9], "crossing_faces": []},              # no stamped partner
    {"crossing_face_indices": [7, 9], "crossing_faces": ["face:f7"]},     # one partner lost
    {"crossing_faces": ["face:f7"]},                                      # no indices at all
    {"crossing_face_indices": [7], "crossing_faces": [""]},               # an unnamed partner
])
def test_partner_evidence_that_does_not_line_up_offers_no_targets(flag_on, measurements):
    # `_linked` drops a partner it has no fingerprint for, so a short name list no longer says
    # which index each name belongs to. Pairing them anyway would aim at the wrong face.
    from dataclasses import replace

    finding = replace(FACE_FINDING_WITH_PARTNERS, measurements=measurements)
    assert HealSelfIntersections().targets_for(finding) == ()


def test_a_face_finding_without_a_fingerprint_offers_no_targets(flag_on):
    from dataclasses import replace

    assert HealSelfIntersections().targets_for(
        replace(FACE_FINDING_WITH_PARTNERS, fingerprint="")) == ()


def test_a_partner_named_twice_is_aimed_at_once(flag_on):
    from dataclasses import replace

    finding = replace(FACE_FINDING_WITH_PARTNERS, measurements={
        "crossing_face_indices": [7, 7, 3], "crossing_faces": ["face:f7", "face:f7", "face:f3"]})
    refs = HealSelfIntersections().targets_for(finding)
    assert [(r.fingerprint, r.entity_index) for r in refs] == [("face:f3", 3), ("face:f7", 7)]


def test_another_code_on_a_crossing_face_is_not_claimed(flag_on):
    from dataclasses import replace

    assert HealSelfIntersections().targets_for(
        replace(FACE_FINDING_WITH_PARTNERS, code=DefectCode.small_face)) == ()


# WHY IT DECLINES, AND IN WHICH ORDER


@pytest.mark.parametrize("setup,reason", [
    ({"flag": False}, "healing_disabled"), ({"resolved": 0}, "not_isolated"),
    ({"n_targets": MAX_HEAL_FACES + 1}, "not_isolated"), ({"pre_pairs": []}, "not_isolated"),
    ({"owners": 2}, "multi_body"), ({"area_delta_ratio": 0.03}, "too_much_surface"),
    ({"grew": True}, "grew_beyond_targets"), ({"post_pairs": [(3, 7)]}, "crossing_remains")])
def test_each_decline_returns_the_input_shape_itself(setup, reason, heal_doubles):
    out = heal_doubles(**setup).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.shape is SHAPE and out.evidence["declined"].startswith(reason)


#: Every decline's failing setup, in the order apply must check them.
_ORDER = [("healing_disabled", {"flag": False}), ("not_isolated", {"resolved": 0}),
          ("not_isolated", {"n_targets": MAX_HEAL_FACES + 1}),
          ("not_isolated", {"pre_pairs": []}), ("multi_body", {"owners": 2}),
          ("too_much_surface", {"area_delta_ratio": 0.03}),
          ("grew_beyond_targets", {"grew": True}),
          ("crossing_remains", {"post_pairs": [(3, 7)]})]


@pytest.mark.parametrize("first", range(len(_ORDER)))
def test_with_every_later_check_also_failing_the_earliest_reason_wins(first, heal_doubles):
    # Flipping one seam at a time only proves each decline exists. Failing EVERY check from
    # `first` onward at once pins the whole order: the reason reported must be the earliest.
    setup: dict = {}
    for _, failing in _ORDER[first:]:
        setup.update(failing)
    harness = heal_doubles(**setup)
    out = harness.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    reason = _ORDER[first][0]
    assert out.shape is SHAPE and out.evidence["declined"].startswith(reason)
    later = {name for name, _ in _ORDER[first + 1:]} - {reason}
    assert not any(out.evidence["declined"].startswith(name) for name in later)
    _assert_untouched(harness)


def test_three_not_isolated_declines_say_which_isolation_failed(heal_doubles):
    details = [heal_doubles(**setup).apply(SHAPE, TARGETS, tolerance_mm=0.01).evidence["declined"]
               for setup in ({"resolved": 0}, {"n_targets": MAX_HEAL_FACES + 1},
                             {"pre_pairs": []})]
    assert len(set(details)) == 3, details


def test_a_lost_seam_with_an_outside_face_is_growth_too(heal_doubles):
    # A target that let go of an edge it shared with an untouched neighbour has torn the shell
    # open there: nothing outside changed TShape, yet the part is no longer the part.
    out = heal_doubles(seams_lost=True).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.shape is SHAPE and out.evidence["declined"].startswith("grew_beyond_targets")


def test_nothing_is_rebuilt_before_the_analysis_has_shown_a_crossing(heal_doubles):
    harness = heal_doubles(pre_pairs=[])
    harness.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert "_rebuild_face" not in harness.calls and "_copied" not in harness.calls


def test_the_input_is_handed_only_to_read_only_seams_on_the_kept_path(heal_doubles):
    harness = heal_doubles()
    out = harness.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.shape == "SPLICED"
    _assert_untouched(harness)


def test_more_than_the_bound_never_reaches_the_kernel(heal_doubles):
    harness = heal_doubles(n_targets=MAX_HEAL_FACES + 1)
    harness.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert "_analyze_faces" not in harness.calls


def test_exactly_the_bound_is_still_healable(heal_doubles):
    out = heal_doubles(n_targets=MAX_HEAL_FACES, pre_pairs=[(10, 11)]).apply(
        SHAPE, TARGETS, tolerance_mm=0.01)
    assert "declined" not in out.evidence and out.evidence["faces_targeted"] == MAX_HEAL_FACES


def test_an_analysis_that_gave_no_answer_is_never_read_as_no_crossing(heal_doubles):
    # The kernel analysis runs bounded; silence (a timeout, a crash) proves nothing. Before the
    # fix that is "not shown to cross"; after it, it is "not shown to be uncrossed".
    before = heal_doubles(pre_pairs=None).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert before.shape is SHAPE and before.evidence["declined"].startswith("not_isolated")
    after = heal_doubles(post_pairs=None).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert after.shape is SHAPE and after.evidence["declined"].startswith("crossing_remains")


# WHAT A KEPT HEAL SAYS


def test_the_area_is_declared_from_measurements_taken_before_the_splice(heal_doubles):
    d = heal_doubles(); d.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert d.calls.index("_face_area:rebuilt") < d.calls.index("splice")


def test_a_kept_heal_declares_its_area_and_names_what_it_rebuilt(heal_doubles):
    harness = heal_doubles(area_delta_ratio=0.003)
    out = harness.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.shape == "SPLICED" and out.mapping is _MAPPING
    ev = out.evidence
    assert "declined" not in ev
    assert ev[AREA_DELTA] == pytest.approx(3.0)
    assert ev["faces_targeted"] == 3 and ev["faces_rebuilt"] == 3
    assert ev["precision_mm"] == pytest.approx(0.01)
    assert len(out.added) == 3 and all(out.added)


def test_a_rebuilt_face_whose_box_did_not_move_is_not_reported_as_added(heal_doubles):
    out = heal_doubles(box_moves=False).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert "declined" not in out.evidence and out.added == ()


def test_the_area_bound_is_judged_against_the_shape_handed_in(heal_doubles, monkeypatch):
    # 0.019 x 1000 is inside 2%; the same delta against a 900 mm2 shape is not.
    assert "declined" not in heal_doubles(area_delta_ratio=0.019).apply(
        SHAPE, TARGETS, tolerance_mm=0.01).evidence
    harness = heal_doubles(area_delta_ratio=0.019)
    monkeypatch.setattr(operations, "_total_face_area", lambda shape: 900.0)
    assert harness.apply(SHAPE, TARGETS, tolerance_mm=0.01).evidence["declined"].startswith(
        "too_much_surface")


def test_a_shrinking_heal_is_bounded_as_well(heal_doubles):
    out = heal_doubles(area_delta_ratio=-0.03).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.evidence["declined"].startswith("too_much_surface")


def test_the_mapping_is_read_from_the_splice_over_the_copy(heal_doubles, monkeypatch):
    seen = {}

    def mapping(context, targets, *, shape_before, diagonal_mm):
        seen.update(context=context, shape_before=shape_before, targets=targets)
        return _MAPPING

    harness = heal_doubles()
    monkeypatch.setattr(operations, "mapping_from_context", mapping)
    harness.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert seen == {"context": "CONTEXT", "shape_before": "COPY", "targets": TARGETS}


# THE PURE PIECES AND THE CATALOGUE


def test_grew_is_any_outside_identity_lost():
    assert _grew(frozenset({"a", "b"}), frozenset({"a", "b", "new"})) is False
    assert _grew(frozenset({"a", "b"}), frozenset({"a", "c"})) is True


def test_the_heal_face_bound_is_four():
    assert MAX_HEAL_FACES == 4


def test_it_sits_after_the_nonplanar_fill_and_before_the_whole_shape_fix():
    names = [op.name for op in operations.OPERATIONS]
    assert names.index("fill_nonplanar_hole") + 1 == names.index("heal_self_intersections")
    assert names.index("heal_self_intersections") + 1 == names.index("fix_shape_global")
    assert (operations.FillNonplanarHole.scope < HealSelfIntersections.scope
            < operations.FixShapeGlobal.scope)
    assert HealSelfIntersections.scope == 7
    assert HealSelfIntersections.answers == frozenset({DefectCode.self_intersection})


def test_the_heal_is_not_a_fill():
    assert HealSelfIntersections.name not in FILL_OPS


# A DECLINE DECLARES NOTHING


@pytest.mark.parametrize("setup", [setup for _, setup in _ORDER], ids=[r for r, _ in _ORDER])
def test_no_decline_carries_an_area_declaration(setup, heal_doubles):
    # The session adds AREA_DELTA to the run's accounting and runs the cumulative cap BEFORE it
    # reads `declined`, so a declaration on a decline turned too_much_surface (and, after earlier
    # kept ops, crossing_remains or grew_beyond_targets) into a false "cap:" verdict.
    out = heal_doubles(**setup).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert "declined" in out.evidence and AREA_DELTA not in out.evidence


def test_a_decline_after_the_rebuild_still_reports_the_area_it_measured(heal_doubles):
    out = heal_doubles(area_delta_ratio=0.03).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.evidence["area_delta_measured_mm2"] == pytest.approx(30.0)


def test_faces_targeted_counts_distinct_faces(heal_doubles):
    doubled = (*TARGETS, _face_ref(3), _face_ref(7))
    out = heal_doubles().op.apply(SHAPE, doubled, tolerance_mm=0.01)
    assert out.evidence["faces_targeted"] == 3
