# Responsibility: Verify the rules planner's ordering, its refusal to repeat what did not work, and that it reports skipping a defect rather than skipping it silently.
from __future__ import annotations

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory

# THE PLANNER IS THE SEAT A MODEL TAKES IN SLICE 8, so what is pinned here is the INTERFACE and
# the baseline behaviour a model will be scored against - not an implementation detail. No kernel:
# ordering and bookkeeping are pure.


def _defect(code, *, fingerprint, severity=DefectSeverity.error, kind="edge", index=1,
            measurements=None) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type=kind, entity_index=index,
                        message="x", fingerprint=fingerprint,
                        measurements=measurements or {})


class _Op:
    def __init__(self, name, answers, scope):
        self.name = name
        self.answers = frozenset(answers)
        self.scope = scope

    def targets_for(self, defect):
        if defect.code not in self.answers or not defect.fingerprint:
            return ()
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)


_NARROW = _Op("narrow", {DefectCode.small_edge}, 1)
_WIDE = _Op("wide", {DefectCode.small_edge, DefectCode.open_shell}, 4)
_GLOBAL = _Op("fix_shape_global", {DefectCode.small_edge, DefectCode.invalid_brep}, 99)


# ORDERING


def test_the_worst_defect_is_addressed_first():
    planner = RulesPlanner(ops=(_NARROW, _WIDE, _GLOBAL))
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a",
                       severity=DefectSeverity.warning),
               _defect(DefectCode.open_shell, fingerprint="edge:b",
                       severity=DefectSeverity.error, index=2))

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None and candidate.defect_fingerprint == "edge:b"


def test_the_narrowest_operation_that_claims_a_defect_is_chosen():
    # A narrow fix that fails costs one entity's worth of rollback; a wide one costs the part's.
    planner = RulesPlanner(ops=(_WIDE, _NARROW, _GLOBAL))
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    assert planner.propose(defects, RunHistory(), shape=None).op.name == "narrow"


def test_the_whole_shape_fallback_is_only_proposed_when_nothing_scoped_is_left():
    planner = RulesPlanner(ops=(_NARROW, _GLOBAL))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    assert planner.propose(defects, history, shape=None).op.name == "narrow"
    history.mark_ineffective("edge:a", "narrow")
    assert planner.propose(defects, history, shape=None).op.name == "fix_shape_global"


def test_a_defect_nothing_claims_produces_no_candidate_and_is_reported():
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    defects = (_defect(DefectCode.self_intersection, fingerprint="face:a", kind="face"),)

    assert planner.propose(defects, history, shape=None) is None
    assert any(r["reason"] == "no_operation_claims_it" for r in history.refusals)


def test_an_untargetable_defect_is_reported_rather_than_skipped_silently():
    # It is the case a model most needs to see: the system found something and could not aim at
    # it. A silent skip is how that stops being visible.
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint=""),)

    assert planner.propose(defects, history, shape=None) is None
    assert any(r["reason"] == "untargetable" for r in history.refusals)


# THE INEFFECTIVE-PAIR LEDGER


def test_a_pair_that_did_not_work_is_never_proposed_again_in_this_run():
    # THE PER-OPERATION LEARNING SIGNAL. In slice 8 this is exactly what the model is shown on its
    # next turn, which is why it is recorded per (defect, operation) rather than per operation.
    planner = RulesPlanner(ops=(_NARROW, _WIDE))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    first = planner.propose(defects, history, shape=None)
    history.mark_ineffective(first.defect_fingerprint, first.op.name)
    second = planner.propose(defects, history, shape=None)

    assert second is not None and second.op.name != first.op.name


def test_the_same_operation_may_still_be_proposed_for_a_different_defect():
    # The ban is on the PAIR. An operation that could not fix one edge may be exactly right for
    # another, and banning it outright would waste the rest of the catalogue.
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    history.mark_ineffective("edge:a", "narrow")
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),
               _defect(DefectCode.small_edge, fingerprint="edge:b", index=2))

    candidate = planner.propose(defects, history, shape=None)

    assert candidate is not None and candidate.defect_fingerprint == "edge:b"


def test_when_every_pair_has_been_tried_there_is_no_candidate_left():
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    history.mark_ineffective("edge:a", "narrow")
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    assert planner.propose(defects, history, shape=None) is None


def test_proposing_is_deterministic_for_the_same_inputs():
    # Two runs over identical bytes must do identical things, or nothing about a repair is
    # reproducible and the corpus cannot be compared across a change.
    planner = RulesPlanner(ops=(_NARROW, _WIDE, _GLOBAL))
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:b", index=2),
               _defect(DefectCode.small_edge, fingerprint="edge:a", index=1))

    first = planner.propose(defects, RunHistory(), shape=None)
    again = planner.propose(defects, RunHistory(), shape=None)

    assert (first.op.name, first.defect_fingerprint) == (again.op.name, again.defect_fingerprint)


# THE HISTORY'S OWN BOOKKEEPING


def test_a_refusal_is_recorded_once_per_defect_and_reason():
    # The loop asks the planner repeatedly, and a refusal list that grew on every tick would bury
    # the report it exists to produce.
    history = RunHistory()
    history.refuse("edge:a", "untargetable")
    history.refuse("edge:a", "untargetable")

    assert len([r for r in history.refusals if r["defect"] == "edge:a"]) == 1
