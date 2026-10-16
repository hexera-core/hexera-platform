# Responsibility: Verify the session's bookkeeping - the order it judges things in, that a rolled-back operation leaves no trace, and that it never claims the kernel confirmed something it inferred.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityMapping, EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.session import FAILED, KEPT, ROLLED_BACK, RepairSession

# NO KERNEL HERE ON PURPOSE. The session's job is ORDER and BOOKKEEPING: cheap checks before
# expensive ones, a rollback that restores exactly what was there, and a verdict that distinguishes
# a kernel fact from a geometric guess. All of that is pure logic, and all of it is what a future
# change is most likely to break without any test noticing.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _defect(code=DefectCode.small_edge, *, fingerprint="edge:aaaa", index=1,
            severity=DefectSeverity.warning, kind="edge") -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type=kind, entity_index=index,
                        message="x", fingerprint=fingerprint)


class _Op:
    """An operation double. Produces whatever the test tells it to, and records being called."""

    name = "double"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self, *, outcome=None, raises=None):
        self._outcome = outcome
        self._raises = raises
        self.calls = 0

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._outcome or OpOutcome(shape="fixed")


def _session(*, scans, measures=None, shape="original"):
    """A session whose scanner and measurer hand back a scripted sequence."""
    scan_queue = list(scans)
    measure_queue = list(measures or [])

    def scanner(_shape, valid=None):
        return scan_queue.pop(0) if len(scan_queue) > 1 else scan_queue[0]

    def measurer(_shape):
        return measure_queue.pop(0) if len(measure_queue) > 1 else (
            measure_queue[0] if measure_queue else _SOUND)

    return RepairSession(shape, measurer=measurer, scanner=scanner)


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)


# THE SCAN


def test_the_defect_list_is_scanned_once_and_cached():
    calls = []

    def scanner(_shape, valid=None):
        calls.append(1)
        return (_defect(),), True

    session = RepairSession("original", measurer=lambda _s: _SOUND, scanner=scanner)
    assert session.defects() == session.defects()
    assert len(calls) == 1, "the re-inspection is the expensive step; it must not be repeated"


def test_the_session_reports_whether_the_scan_was_exhaustive():
    # A walk that stopped at its budget has a defect list nobody may read as a full diagnosis.
    session = _session(scans=[((_defect(),), False)])
    session.defects()
    assert session.complete_scan is False


# KEEPING AN OPERATION


def test_an_operation_whose_defect_disappeared_is_kept():
    op = _Op(outcome=OpOutcome(shape="fixed",
                               mapping=EntityMapping(moved={}, gone=frozenset({"edge:aaaa"}),
                                                     basis="kernel")))
    session = _session(scans=[((_defect(),), True), ((), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == KEPT and verdict.kept
    assert verdict.basis == "kernel", "the kernel said the entity is gone; say so"
    assert session.shape == "fixed"


def test_a_kept_operations_declaration_goes_into_the_accounting():
    # 15.0 ON A 1000 mm2 ORIGINAL IS 1.5%, inside the 2% residual. The scripted measurer reports
    # no actual area change, so a declaration the cap cannot account for would be refused - an
    # earlier version of this test declared 25.0 (2.5%) and was correctly rolled back, which
    # proved the cap works but not the thing this test is about.
    op = _Op(outcome=OpOutcome(shape="fixed", evidence={"area_delta_mm2": 15.0}))
    session = _session(scans=[((_defect(),), True), ((), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.kept, f"rolled back because {verdict.reason}"
    assert session.accounting.declared_area_mm2 == pytest.approx(15.0)


def test_the_cached_defect_list_is_refreshed_after_a_kept_operation():
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(),), True), ((), True)])
    assert len(session.defects()) == 1
    defect = session.defects()[0]

    session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert session.defects() == (), "the planner would keep proposing a defect that is gone"


# ROLLING BACK


def test_an_operation_whose_defect_survived_is_rolled_back():
    # The whole progress rule. An operation that ran, stayed inside every cap and left the defect
    # it was run for has not repaired anything, and keeping it would spend a cap allowance on
    # nothing.
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(),), True), ((_defect(),), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "target_remains"
    assert session.shape == "original", "the rollback did not restore the shape"


def test_a_rollback_restores_the_defect_list_and_scan_completeness():
    # The post-operation scan is made DISTINGUISHABLE (another fingerprint, and incomplete) so that
    # a rollback which merely re-scanned, or left the new scan cached, cannot pass.
    op = _Op(outcome=OpOutcome(shape="fixed"))
    leftover = _defect(fingerprint="edge:zzzz", index=9)
    session = _session(scans=[((_defect(),), True), ((_defect(), leftover), False)])
    before = session.defects()
    assert session.complete_scan is True

    verdict = session.try_op(op, op.targets_for(before[0]), before[0].code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK
    assert session._defects is before, "the cached tuple was not the pre-operation one"
    assert session.defects() is before
    assert session.complete_scan is True, "an incomplete post-operation scan leaked through"


def test_a_rolled_back_operation_contributes_nothing_to_the_accounting():
    # Otherwise a run could spend its area allowance on operations it discarded.
    op = _Op(outcome=OpOutcome(shape="fixed", evidence={"area_delta_mm2": 500.0}))
    session = _session(scans=[((_defect(),), True), ((_defect(),), True)])
    defect = session.defects()[0]

    session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert session.accounting.declared_area_mm2 == 0.0


def test_an_operation_that_introduced_a_new_defect_is_rolled_back():
    op = _Op(outcome=OpOutcome(shape="fixed"))
    worse = _defect(DefectCode.self_intersection, fingerprint="face:bbbb",
                    severity=DefectSeverity.error, kind="face", index=2)
    session = _session(scans=[((_defect(),), True), ((worse,), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "new_defect"


def test_a_new_defect_on_geometry_the_operation_declared_it_added_is_not_held_against_it():
    # A patch face is new surface the operation SAID it was adding. A warning on it is the
    # expected cost of the repair, not evidence the repair went wrong - and refusing it would make
    # every hole fill impossible.
    patch_warning = _defect(DefectCode.small_face, fingerprint="face:patch",
                            severity=DefectSeverity.warning, kind="face", index=7)
    op = _Op(outcome=OpOutcome(shape="fixed", added=("face:patch",)))
    session = _session(scans=[((_defect(),), True), ((patch_warning,), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == KEPT


def test_a_new_defect_below_warning_severity_is_not_held_against_an_operation():
    info = _defect(DefectCode.duplicate_surface_data, fingerprint="face:cccc",
                   severity=DefectSeverity.info, kind="face", index=3)
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(),), True), ((info,), True)])
    defect = session.defects()[0]

    assert session.try_op(op, op.targets_for(defect), defect.code,
                          tolerance_mm=0.01).status == KEPT


# THE ORDER OF JUDGEMENT


def test_a_cap_refusal_happens_before_the_re_inspection_is_paid_for():
    # THE ORDER IS THE POINT. The re-inspection is the expensive step, so a cheap check that
    # catches a destroyed solid must save it. Counted rather than asserted about, because the
    # saving is the behaviour.
    scans = 0

    def scanner(_shape, valid=None):
        nonlocal scans
        scans += 1
        return (_defect(),), True

    # THREE ENTRIES, NOT TWO. RepairSession.__init__ measures the original, then try_op measures
    # `before` and `after` - so a two-entry script is off by one and the damaged reading lands on
    # `before` instead of `after`.
    damaged = Measure(**{**_SOUND.to_dict(), "solids": 0})
    measures = [_SOUND, _SOUND, damaged]

    def measurer(_shape):
        return measures.pop(0) if len(measures) > 1 else measures[0]

    session = RepairSession("original", measurer=measurer, scanner=scanner)
    defect = session.defects()
    assert scans == 1
    op = _Op(outcome=OpOutcome(shape="fixed"))

    verdict = session.try_op(op, op.targets_for(defect[0]), defect[0].code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason.startswith("cap:")
    assert scans == 1, "a shape nobody will keep was re-inspected anyway"


def test_a_cumulative_cap_is_measured_against_the_original_not_the_previous_shape():
    # Proof that a loop cannot drift: the session holds the ORIGINAL measurement from
    # construction and never replaces it.
    drifted = Measure(**{**_SOUND.to_dict(), "diagonal_mm": 200.0 * 1.05})
    # Three entries: the constructor's measurement of the original, then before and after.
    measures = [_SOUND, _SOUND, drifted]

    def measurer(_shape):
        return measures.pop(0) if len(measures) > 1 else measures[0]

    session = RepairSession("original", measurer=measurer,
                            scanner=lambda _s, valid=None: ((_defect(),), True))
    defect = session.defects()[0]
    op = _Op(outcome=OpOutcome(shape="fixed"))

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.reason.startswith("cap:") and "diagonal" in verdict.reason


# FAILURE


def test_a_kernel_exception_is_a_failed_verdict_and_not_a_crash():
    # A repair that breaks is never the customer's file's fault, and the loop carries on to the
    # next candidate rather than abandoning the run.
    op = _Op(raises=RuntimeError("StdFail_NotDone"))
    session = _session(scans=[((_defect(),), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == FAILED and "RuntimeError" in verdict.reason
    assert session.shape == "original"


# BASIS - WHAT THE KERNEL SAID VERSUS WHAT WE INFERRED


def test_a_verdict_says_geometric_when_the_operation_offered_no_history():
    op = _Op(outcome=OpOutcome(shape="fixed"))          # default mapping is empty/geometric
    session = _session(scans=[((_defect(),), True), ((), True)])
    defect = session.defects()[0]

    assert session.try_op(op, op.targets_for(defect), defect.code,
                          tolerance_mm=0.01).basis == "geometric"


def test_a_verdict_about_an_ambiguous_target_never_claims_the_kernel_confirmed_it():
    # Two entities share a box, so the fingerprint cannot tell them apart. The kernel's mapping
    # may be perfect and still be about the other one.
    op = _Op(outcome=OpOutcome(shape="fixed",
                               mapping=EntityMapping(moved={}, gone=frozenset({"edge:aaaa#0"}),
                                                     basis="kernel")))
    session = _session(scans=[((_defect(fingerprint="edge:aaaa#0"),), True), ((), True)])
    defect = session.defects()[0]
    ambiguous = (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                           entity_index=1, ambiguous=True),)

    verdict = session.try_op(op, ambiguous, defect.code, tolerance_mm=0.01)

    assert verdict.status == KEPT
    assert verdict.basis == "geometric"


def test_an_untargetable_defect_cannot_be_handed_to_an_operation():
    # An unlocated defect has no fingerprint, so there is nothing for the progress rule to look
    # for. Accepting it would make every verdict about it vacuously true.
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(fingerprint=""),), True)])
    defect = session.defects()[0]
    nameless = (EntityRef(entity_type="edge", fingerprint="", entity_index=1),)

    verdict = session.try_op(op, nameless, defect.code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "untargetable"
    assert op.calls == 0, "the kernel was asked to fix something nothing could verify"


def test_an_operation_aimed_at_a_defect_the_target_does_not_have_is_refused_before_it_runs():
    # WITHOUT THIS, EVERY LATER CHECK PASSES VACUOUSLY. The targeted defect is "gone" because it
    # was never there, nothing new appears, and the operation is KEPT for doing nothing - so
    # `kept` stops meaning "repaired something", which is the signal this slice accumulates.
    #
    # The rules planner proposes only from the defect list, but a MODEL planner (slice 8) can
    # propose any target it likes, and this is what stops a confident wrong guess being recorded
    # as a success.
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(fingerprint="edge:aaaa"),), True)])
    elsewhere = (EntityRef(entity_type="edge", fingerprint="edge:zzzz", entity_index=9),)

    verdict = session.try_op(op, elsewhere, DefectCode.small_edge, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "target_absent"
    assert op.calls == 0, "the kernel was called for a target carrying no such defect"


def test_an_operation_aimed_at_the_wrong_defect_code_on_a_real_target_is_also_refused():
    # The entity exists and is broken, but not in the way this operation claims to fix.
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(code=DefectCode.open_shell,
                                       fingerprint="edge:aaaa"),), True)])
    real = (EntityRef(entity_type="edge", fingerprint="edge:aaaa", entity_index=1),)

    verdict = session.try_op(op, real, DefectCode.small_edge, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "target_absent"


def test_one_target_carrying_the_defect_is_enough_even_when_others_do_not():
    # A scoped operation legitimately widens its aim to a defect's NEIGHBOURS - the faces that own
    # a broken edge, the shells that own a face. Demanding every target carry the defect would
    # refuse the scoped operations this whole slice is built on.
    op = _Op(outcome=OpOutcome(shape="fixed"))
    session = _session(scans=[((_defect(fingerprint="edge:aaaa"),), True), ((), True)])
    mixed = (EntityRef(entity_type="edge", fingerprint="edge:aaaa", entity_index=1),
             EntityRef(entity_type="face", fingerprint="face:neighbour", entity_index=2))

    verdict = session.try_op(op, mixed, DefectCode.small_edge, tolerance_mm=0.01)

    assert verdict.kept, f"rolled back because {verdict.reason}"


# THE VERDICT'S PAYLOAD


def test_a_verdict_records_the_measured_delta_and_the_operations_evidence():
    op = _Op(outcome=OpOutcome(shape="fixed", evidence={"faces_fixed": 2}))
    session = _session(scans=[((_defect(),), True), ((), True)])
    defect = session.defects()[0]

    payload = session.try_op(op, op.targets_for(defect), defect.code,
                             tolerance_mm=0.01).to_dict()

    assert payload["op"] == "double" and payload["targets"] == ["edge:aaaa"]
    assert payload["evidence"]["faces_fixed"] == 2
    assert "before" in payload["delta"] and "after" in payload["delta"]
    assert payload["defect_code"] == DefectCode.small_edge.value


# HISTORY IS A HINT, THE RE-INSPECTION IS THE AUTHORITY


def test_a_gone_claim_does_not_override_a_scan_that_still_shows_the_defect():
    op = _Op(outcome=OpOutcome(shape="fixed",
                               mapping=EntityMapping(moved={}, gone=frozenset({"edge:aaaa"}),
                                                     basis="kernel")))
    session = _session(scans=[((_defect(),), True), ((_defect(),), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "target_remains"
    assert session.accounting.declared_area_mm2 == 0.0


def test_a_fatal_defect_on_declared_added_geometry_is_still_held_against_the_operation():
    fatal = _defect(DefectCode.self_intersection, fingerprint="face:patch",
                    severity=DefectSeverity.fatal, kind="face", index=7)
    op = _Op(outcome=OpOutcome(shape="fixed", added=("face:patch",)))
    session = _session(scans=[((_defect(),), True), ((fatal,), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == ROLLED_BACK and verdict.reason == "new_defect"


def test_a_verdict_is_geometric_when_the_history_recorded_the_targets_as_unmoved():
    # The mapping was built from a consulted history (basis "kernel") but explains nothing about
    # these targets, so the conclusion came from diffing the scans.
    op = _Op(outcome=OpOutcome(shape="fixed",
                               mapping=EntityMapping(moved={}, gone=frozenset(), basis="kernel")))
    session = _session(scans=[((_defect(),), True), ((), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == KEPT and verdict.basis == "geometric"


def test_a_failed_verdict_keeps_the_measurement_it_already_paid_for():
    op = _Op(raises=RuntimeError("boom"))
    session = _session(scans=[((_defect(),), True)])
    defect = session.defects()[0]

    verdict = session.try_op(op, op.targets_for(defect), defect.code, tolerance_mm=0.01)

    assert verdict.status == FAILED and "before" in verdict.delta
