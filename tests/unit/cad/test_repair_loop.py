# Responsibility: Verify the loop stops for the right reason, spends no more than its budget, and tells a caller apart a finished run from one that ran out of room.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import (
    NO_CANDIDATE,
    OP_BUDGET,
    RESOLVED,
    SCAN_INCOMPLETE,
    STALLED,
    TIME_BUDGET,
    LoopBudget,
    run,
)
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.plan import Candidate, RulesPlanner
from meshpipeline.cad.repair.session import RepairSession

# WHY `stopped_because` IS NOT A DETAIL. "No error defect remains" and "I ran out of operations"
# produce the same shape and mean opposite things to a reviewer, to the corpus, and to a model
# being scored. Every one of these tests is about keeping them apart.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _defect(*, fingerprint="edge:a", code=DefectCode.small_edge,
            severity=DefectSeverity.error, index=1) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type="edge", entity_index=index,
                        message="x", fingerprint=fingerprint)


class _Op:
    name = "double"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self):
        self.calls = 0

    def targets_for(self, defect):
        return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls += 1
        return OpOutcome(shape=f"fixed-{self.calls}")


class _Planner:
    """Proposes the op for every defect the op ANSWERS, until the ledger bans the pair."""

    label = "double-planner"

    def __init__(self, op):
        self._op = op
        self.calls = 0

    def propose(self, defects, history, *, shape):
        self.calls += 1
        for defect in defects:
            if defect.code not in self._op.answers:
                continue
            if history.was_ineffective(defect.fingerprint, self._op.name):
                continue
            return Candidate(op=self._op, targets=self._op.targets_for(defect),
                             defect_code=defect.code, defect_fingerprint=defect.fingerprint)
        return None


class _Tireless:
    """Proposes the same pair forever, ignoring the ledger.

    DELIBERATELY UNLIKE THE REAL PLANNER. The budget and stall tests are about the DRIVER's
    arithmetic, and a planner that honours the ineffective-pair ledger runs dry after one
    rejection - which would make those tests assert `no_candidate` and prove nothing about the
    budget. Isolating the two is the point of the double.
    """

    label = "tireless"

    def __init__(self, op):
        self._op = op
        self.calls = 0

    def propose(self, defects, history, *, shape):
        self.calls += 1
        if not defects:
            return None
        return Candidate(op=self._op, targets=self._op.targets_for(defects[0]),
                         defect_code=defects[0].code,
                         defect_fingerprint=defects[0].fingerprint)


def _session(scans):
    queue = list(scans)

    def scanner(_shape, valid=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return RepairSession("original", measurer=lambda _s: _SOUND, scanner=scanner)


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)


# THE STOP REASONS


def test_a_shape_with_no_defects_is_resolved_without_trying_anything():
    # IT DOES NOT RUN THE WHOLE-SHAPE FALLBACK "JUST IN CASE". A sound part must come out of the
    # loop untouched - a conservative pass over sound geometry has nothing to fix, and anything it
    # changed would be damage.
    op = _Op()
    result = run(None, planner=_Planner(op), session=_session([((), True)]), tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved
    assert result.verdicts == () and op.calls == 0
    assert result.shape == "original"


def test_a_run_that_cleared_every_error_defect_is_resolved():
    op = _Op()
    result = run(None, planner=_Planner(op),
                 session=_session([((_defect(),), True), ((), True)]), tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved
    assert len(result.verdicts) == 1 and result.verdicts[0].kept


def test_a_warning_present_all_along_does_not_stop_the_run_being_resolved():
    # `resolved` is about ERRORS. A sliver-face warning the repair never claimed is a remark for a
    # person, not a failure. It must be in BOTH scans - a warning that APPEARS after an operation
    # is a new defect and the session rightly rolls that operation back.
    warning = _defect(fingerprint="face:w", code=DefectCode.small_face,
                      severity=DefectSeverity.warning, index=2)
    op = _Op()
    result = run(None, planner=_Planner(op),
                 session=_session([((_defect(), warning), True), ((warning,), True)]),
                 tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved


def test_a_truncated_scan_is_never_reported_as_resolved():
    # A walk that stopped at its budget has a short list because it stopped, not because the part
    # is sound. Calling that resolved is how a repairable defect gets delivered as a clean bill.
    result = run(None, planner=_Planner(_Op()), session=_session([((), False)]),
                 tolerance_mm=0.01)

    assert result.stopped_because == SCAN_INCOMPLETE
    assert not result.resolved
    assert result.verdicts == ()


def test_a_scan_truncated_after_an_operation_is_never_reported_as_resolved():
    # The first scan is complete and finds a defect; the RE-scan after the kept operation is cut
    # short. The truncated list lacks the targeted defect, so the progress rule passes vacuously and
    # the operation is kept - which must not be read as a clean bill of health.
    result = run(None, planner=_Planner(_Op()),
                 session=_session([((_defect(),), True), ((), False)]), tolerance_mm=0.01)

    assert result.stopped_because == SCAN_INCOMPLETE
    assert not result.resolved and not result.complete_scan


def test_a_run_that_clears_the_last_defect_on_its_last_operation_is_resolved():
    # The budget is spent exactly as the part becomes sound. Labelling that op_budget would be
    # recorded downstream as "the repair did not finish".
    result = run(None, planner=_Planner(_Op()),
                 session=_session([((_defect(),), True), ((), True)]),
                 budget=LoopBudget(max_ops=1, stall_limit=99, timeout_s=120.0), tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved
    assert len(result.verdicts) == 1


def test_a_run_that_clears_the_last_defect_as_the_time_runs_out_is_resolved():
    ticks = iter([0.0, 10.0, 200.0, 300.0, 400.0])
    result = run(None, planner=_Planner(_Op()),
                 session=_session([((_defect(),), True), ((), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01, clock=lambda: next(ticks))

    assert result.stopped_because == RESOLVED and result.resolved


class _Named(_Op):
    def __init__(self, name):
        super().__init__()
        self.name = name


class _Ladder:
    """Offers its operations in order for the first defect, skipping banned pairs."""

    label = "ladder"

    def __init__(self, *ops):
        self._ops = ops

    def propose(self, defects, history, *, shape):
        for defect in defects:
            for op in self._ops:
                if not history.was_ineffective(defect.fingerprint, op.name):
                    return Candidate(op=op, targets=op.targets_for(defect),
                                     defect_code=defect.code, defect_fingerprint=defect.fingerprint)
        return None


def test_a_narrow_attempt_rolled_back_and_a_wider_one_kept_is_still_resolved():
    # THE CASE THE "ABANDONED" CLAUSE MUST NOT BREAK. The defect is gone from the final list, so
    # the earlier rollback does not count against the run.
    result = run(None, planner=_Ladder(_Named("narrow"), _Named("wide")),
                 session=_session([((_defect(),), True), ((_defect(),), True), ((), True)]),
                 tolerance_mm=0.01)

    assert [(v.op, v.status) for v in result.verdicts] == [("narrow", "rolled_back"),
                                                           ("wide", "kept")]
    assert result.stopped_because == RESOLVED and result.resolved


def test_a_rolled_back_attempt_whose_defect_remains_is_not_resolved_even_if_the_planner_ran_dry():
    # No error remains (a warning), and the planner has nothing left rather than the run stalling.
    # `resolved` must still refuse: the loop tried, failed, and the defect is still there.
    warning = _defect(severity=DefectSeverity.warning)
    result = run(None, planner=_Planner(_Op()), session=_session([((warning,), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=99, timeout_s=120.0), tolerance_mm=0.01)

    assert result.stopped_because == NO_CANDIDATE and not result.resolved
    assert [d.fingerprint for d in result.defects] == ["edge:a"]


def test_an_abandoned_warning_stops_as_stalled_not_resolved():
    warning = _defect(severity=DefectSeverity.warning)
    result = run(None, planner=_Tireless(_Op()), session=_session([((warning,), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=2, timeout_s=120.0), tolerance_mm=0.01)

    assert result.stopped_because == STALLED and not result.resolved


def test_an_error_the_planner_cannot_address_stops_the_run_as_no_candidate():
    class _Silent:
        label = "silent"

        def propose(self, defects, history, *, shape):
            return None

    result = run(None, planner=_Silent(),
                 session=_session([((_defect(),), True)]), tolerance_mm=0.01)

    assert result.stopped_because == NO_CANDIDATE and not result.resolved
    assert result.verdicts == ()


def test_the_operation_budget_stops_a_run_that_keeps_finding_work():
    op = _Op()
    result = run(None, planner=_Tireless(op),
                 session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=3, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01)

    assert result.stopped_because == OP_BUDGET
    assert len(result.verdicts) == 3


def test_consecutive_rollbacks_stop_the_run_as_stalled():
    op = _Op()
    result = run(None, planner=_Tireless(op),
                 session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=2, timeout_s=120.0),
                 tolerance_mm=0.01)

    assert result.stopped_because == STALLED
    assert len(result.verdicts) == 2
    assert all(not v.kept for v in result.verdicts)


def test_a_decline_spends_no_stall_budget_and_the_next_defect_is_still_reached():
    # THE hero.stp SHAPE OF FAILURE, in miniature. A tangle of crossing faces is reported once per
    # member face; the heal-like op aims at the SAME pair from either finding and DECLINES - it ran
    # nothing, it judged. Before the fix the planner re-proposed the identical aim from the sibling
    # finding (the ineffective ledger is keyed per proposing defect), the two declines spent a
    # stall budget of 2, and the sew that would have fixed the open shell was never proposed.
    class _Declining:
        name = "healish"
        answers = frozenset({DefectCode.self_intersection})
        scope = 7

        def __init__(self):
            self.calls = 0

        def targets_for(self, defect):
            # The whole tangle, whichever member finding proposes it - as the real heal does.
            return (EntityRef(entity_type="face", fingerprint="face:x", entity_index=1),
                    EntityRef(entity_type="face", fingerprint="face:y", entity_index=2))

        def apply(self, shape, targets, *, tolerance_mm):
            self.calls += 1
            return OpOutcome(shape=shape, evidence={"declined": "multi_body"})

    class _Sewing:
        name = "sewish"
        answers = frozenset({DefectCode.open_shell})
        scope = 4

        def targets_for(self, defect):
            return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                              entity_index=defect.entity_index),)

        def apply(self, shape, targets, *, tolerance_mm):
            return OpOutcome(shape="sewn")

    crossing_x = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                              entity_type="face", entity_index=1, message="x",
                              fingerprint="face:x")
    crossing_y = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                              entity_type="face", entity_index=2, message="x",
                              fingerprint="face:y")
    open_edge = _defect(fingerprint="edge:open", code=DefectCode.open_shell, index=5)
    heal, sew = _Declining(), _Sewing()

    result = run(None, planner=RulesPlanner(ops=(heal, sew)),
                 session=_session([((crossing_x, crossing_y, open_edge), True),
                                   ((crossing_x, crossing_y), True)]),
                 budget=LoopBudget(max_ops=6, stall_limit=2, timeout_s=120.0),
                 tolerance_mm=0.01)

    # The decline happened ONCE, the sew was reached and kept, and the run did not stall: the
    # crossings remain only because the one operation answering them said no, which is
    # `no_candidate`, not `stalled`.
    assert heal.calls == 1
    assert [(v.op, v.status) for v in result.verdicts] == [("healish", "rolled_back"),
                                                           ("sewish", "kept")]
    assert result.verdicts[0].reason.startswith("declined:")
    assert result.stopped_because == NO_CANDIDATE


def test_the_time_budget_covers_the_whole_loop_not_one_operation():
    # CHANGED MEANING, DELIBERATELY. CAD_REPAIR_TIMEOUT_S used to bound one repair; a loop that
    # applied it per operation could spend six times a job's allowance.
    ticks = iter([0.0, 10.0, 200.0, 300.0, 400.0])
    op = _Op()
    result = run(None, planner=_Tireless(op),
                 session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01, clock=lambda: next(ticks))

    assert result.stopped_because == TIME_BUDGET


def test_the_budget_reads_the_settings_by_default(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 11)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 4)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 7.5)

    budget = LoopBudget.from_settings()

    assert (budget.max_ops, budget.stall_limit, budget.timeout_s) == (11, 4, 7.5)


# THE LEDGER AND THE RECORD


def test_a_rolled_back_pair_is_fed_back_to_the_planner():
    # THE FEEDBACK HALF OF THE LOOP. Without this the planner proposes the same failing pair until
    # the budget runs out, and nothing in the run has learned anything.
    op = _Op()
    planner = _Planner(op)
    result = run(None, planner=planner, session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01)

    # One attempt, then the pair is banned and the planner has nothing left.
    assert len(result.verdicts) == 1 and not result.verdicts[0].kept
    assert result.stopped_because == NO_CANDIDATE
    assert planner.calls >= 2


def test_the_run_records_every_attempt_kept_or_not():
    # THE SLICE 8 CORPUS. A rolled-back operation is as much a training example as a kept one -
    # more, arguably - so the log is of attempts, not of successes.
    op = _Op()
    result = run(None, planner=_Tireless(op), session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=2, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01)

    payload = result.to_dict()
    assert len(payload["operations"]) == 2
    assert payload["stopped_because"] == OP_BUDGET
    assert payload["planner"] == "tireless"
    assert payload["resolved"] is False


def test_the_run_carries_the_planners_refusals_so_a_report_can_state_them():
    class _Refusing:
        label = "refusing"

        def propose(self, defects, history, *, shape):
            history.refuse("edge:a", "paired_rim", "would seal a through-hole")
            return None

    result = run(None, planner=_Refusing(), session=_session([((_defect(),), True)]),
                 tolerance_mm=0.01)

    assert result.refusals and result.refusals[0]["reason"] == "paired_rim"
    assert result.to_dict()["refusals"][0]["detail"] == "would seal a through-hole"


def test_the_final_defect_list_is_what_the_run_ended_with():
    # `edge:left` is in BOTH scans, so it is pre-existing rather than a new defect the session
    # would hold against the operation.
    left = _defect(fingerprint="edge:left", index=9)
    op = _Op()
    result = run(None, planner=_Planner(op),
                 session=_session([((_defect(), left), True), ((left,), True)]),
                 tolerance_mm=0.01)

    assert [d.fingerprint for d in result.defects] == ["edge:left"]
