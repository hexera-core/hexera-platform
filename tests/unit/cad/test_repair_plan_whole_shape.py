# Responsibility: Verify a WHOLE-SHAPE operation is not re-proposed on a shape it has already run
#                 on, that it becomes proposable again the moment the shape changes, and that a
#                 targeted operation is never caught by the rule.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import NO_CANDIDATE, RESOLVED, LoopBudget, run
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.session import RepairSession

# WHY (docs/superpowers/specs/2026-11-19-target-remains-diagnosis.md).
#
# `ShapeFix_Wireframe` is whole-shape: ONE call merges every small edge it is willing to merge on
# the part. So from the second attempt on, the target is by construction an edge the fixer has
# already declined, and `fix_small_edges` repeats the IDENTICAL call for the IDENTICAL result.
# Measured over 90 parts of the real-failure corpus: of 19 kept attempts every one was the FIRST
# on its file, and all 17 second-and-later attempts were rolled back. 17 of the run's 81
# `target_remains` are that, and every one cost a kernel call, a re-inspection and a stall slot.
#
# THE LINE THIS FILE HOLDS. The rule may only ever REDUCE wasted attempts. `ineffective` is keyed
# per (defect, operation) and so cannot catch this - the retry comes from a DIFFERENT defect - and
# a rule keyed per operation alone would ban the operation for the run, which WOULD lose repairs:
# a whole-shape fixer that was KEPT has changed the shape, and the kernel may now have something
# to do. So the key is (operation, THE SHAPE IT RAN ON), and `test_the_guard` below runs the real
# loop over an operation that really repairs, twice, to prove no repair is lost.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _defect(fingerprint: str, *, index: int = 1,
            code=DefectCode.small_edge) -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="edge",
                        entity_index=index, message="x", fingerprint=fingerprint)


class _Op:
    """A catalogue double. `whole_shape` is the declaration the planner reads."""

    answers = frozenset({DefectCode.small_edge})
    scope = 1
    whole_shape = False

    def __init__(self, name: str, *, whole_shape: bool = False, repairs: bool = False) -> None:
        self.name = name
        self.whole_shape = whole_shape
        self.calls = 0
        self._repairs = repairs

    def targets_for(self, defect):
        if defect.code not in self.answers or not defect.fingerprint:
            return ()
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls += 1
        # A REAL REPAIR RETURNS A NEW SHAPE; a declining fixer hands back the one it was given,
        # which is exactly what `ShapeFix_Wireframe` does when it will merge nothing
        # (`IsSame` TRUE, zero recorded replacements).
        return OpOutcome(shape=f"repaired-{self.calls}" if self._repairs else shape)


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 8)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 4)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)


def _session(scans):
    queue = list(scans)

    def scanner(_shape, valid=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return RepairSession("part", measurer=lambda _s: _SOUND, scanner=scanner)


# THE PLANNER'S RULE, WITHOUT A LOOP


def test_a_whole_shape_operation_is_proposed_once_and_not_again_while_the_shape_is_unchanged():
    sweep = _Op("fix_small_edges", whole_shape=True)
    planner = RulesPlanner(ops=(sweep,))
    history = RunHistory()
    defects = (_defect("edge:a", index=1), _defect("edge:b", index=2))

    first = planner.propose(defects, history, shape="part")
    assert first is not None and first.defect_fingerprint == "edge:a"

    # The attempt was rolled back, so the ledger bans THAT pair - and today the sibling finding
    # `edge:b` walked straight into the identical call. The shape has not changed, so it cannot.
    history.mark_ineffective("edge:a", sweep.name)
    assert planner.propose(defects, history, shape="part") is None


def test_the_same_operation_is_proposable_again_once_the_shape_has_changed():
    # A KEPT OPERATION CHANGED THE PART, so the kernel may now have something to do. This is the
    # half of the rule that stops it being "ban the operation for the run".
    sweep = _Op("fix_small_edges", whole_shape=True)
    planner = RulesPlanner(ops=(sweep,))
    history = RunHistory()
    defects = (_defect("edge:b", index=2),)

    planner.propose((_defect("edge:a", index=1),) + defects, history, shape="part")
    history.mark_ineffective("edge:a", sweep.name)
    assert planner.propose(defects, history, shape="part") is None

    again = planner.propose(defects, history, shape="repaired")
    assert again is not None and again.op is sweep


def test_a_targeted_operation_is_proposed_for_every_target_on_one_unchanged_shape():
    # THE RULE MUST NOT REACH A TARGETED OPERATION. `fill_planar_hole`, `rebuild_face` and
    # `close_wire_gaps` legitimately apply many times to different entities of one shape, and
    # catching them would throw repairs away rather than waste.
    narrow = _Op("fill_one_thing", whole_shape=False)
    planner = RulesPlanner(ops=(narrow,))
    history = RunHistory()
    defects = (_defect("edge:a", index=1), _defect("edge:b", index=2))

    assert planner.propose(defects, history, shape="part").defect_fingerprint == "edge:a"
    history.mark_ineffective("edge:a", narrow.name)
    assert planner.propose(defects, history, shape="part").defect_fingerprint == "edge:b"


def test_a_planner_with_no_shape_to_compare_keeps_todays_behaviour():
    # FAIL OPEN, DELIBERATELY. Handed no shape there is no identity to key on, and a rule that
    # guessed would be a rule that could skip a repair. Wasting an attempt is the safe error.
    sweep = _Op("fix_small_edges", whole_shape=True)
    planner = RulesPlanner(ops=(sweep,))
    history = RunHistory()
    defects = (_defect("edge:a", index=1), _defect("edge:b", index=2))

    planner.propose(defects, history, shape=None)
    history.mark_ineffective("edge:a", sweep.name)
    assert planner.propose(defects, history, shape=None) is not None


def test_the_ledger_reads_shape_identity_the_way_the_kernel_does():
    # `IsSame` is the kernel's own notion and the one the diagnosis measured the no-op with
    # (`ShapeFix_Shape(...).Shape().IsSame(shape)` was TRUE). A shape that cannot answer it is
    # compared by handle, which is what a session's rollback restores.
    class _Shape:
        def __init__(self, same: bool) -> None:
            self._same = same

        def IsSame(self, _other) -> bool:  # noqa: N802 - the kernel's spelling
            return self._same

    history = RunHistory()
    tried = _Shape(True)
    history.mark_whole_shape_tried("fix_small_edges", tried)

    assert history.was_whole_shape_tried("fix_small_edges", _Shape(True))
    assert not history.was_whole_shape_tried("fix_small_edges", _Shape(False))
    assert not history.was_whole_shape_tried("fix_shape_global", tried)


# THE GUARD: THROUGH THE REAL LOOP


def test_an_operation_that_would_really_change_the_shape_is_still_proposed_and_still_kept():
    """THE LINE. Run the real loop over a whole-shape operation that actually repairs, twice.

    Two defects, and the sweep clears one per call. If the rule were keyed per operation rather
    than per (operation, shape) the second repair would never be proposed and the run would end
    `no_candidate` with a defect it could have fixed still on the part. It ends `resolved`.
    """
    sweep = _Op("fix_small_edges", whole_shape=True, repairs=True)
    work = _session([((_defect("edge:a", index=1), _defect("edge:b", index=2)), True),
                     ((_defect("edge:b", index=2),), True),
                     ((), True)])

    outcome = run("part", planner=RulesPlanner(ops=(sweep,)), session=work, tolerance_mm=0.01,
                  budget=LoopBudget.from_settings())

    assert sweep.calls == 2, "the second real repair was never attempted"
    assert [(v.op, v.status) for v in outcome.verdicts] == [("fix_small_edges", "kept")] * 2
    assert outcome.stopped_because == RESOLVED


def test_a_sweep_that_changes_nothing_costs_one_attempt_instead_of_one_per_defect():
    """The saving, measured the way the corpus run measured it: attempts, not verdicts.

    Three small edges on a part `ShapeFix_Wireframe` will not touch. Today that is three kernel
    calls, three re-inspections and three `target_remains`; it must now be one, and the run must
    END HONESTLY - `no_candidate`, with the defects still reported - never as a success.
    """
    sweep = _Op("fix_small_edges", whole_shape=True)
    standing = (_defect("edge:a", index=1), _defect("edge:b", index=2),
                _defect("edge:c", index=3))
    work = _session([(standing, True)])

    outcome = run("part", planner=RulesPlanner(ops=(sweep,)), session=work, tolerance_mm=0.01,
                  budget=LoopBudget.from_settings())

    assert sweep.calls == 1
    assert [v.status for v in outcome.verdicts] == ["rolled_back"]
    assert outcome.stopped_because == NO_CANDIDATE
    assert {d.fingerprint for d in outcome.defects} == {"edge:a", "edge:b", "edge:c"}
