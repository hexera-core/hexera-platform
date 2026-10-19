# Responsibility: Verify the face-level self-intersection scan's budget, its bounded wrapper, how pairs close into components, that the session asks for a scan only with the heal enabled, and that the scan informs the heal without demoting a run (scan placement, non-demotion).
from __future__ import annotations

import time

import pytest

import meshpipeline.cad.repair.session as session_mod
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.localize import _bounded, _components, face_scan_budget_s
from meshpipeline.cad.repair.session import RepairSession

MEASURE = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)


@pytest.mark.parametrize("remaining,expected", [(200.0, 30.0), (120.0, 30.0), (40.0, 10.0),
                                                (0.0, 0.0), (-5.0, 0.0)])
def test_the_budget_is_a_quarter_of_what_remains_capped_at_thirty(remaining, expected):
    assert face_scan_budget_s(remaining) == expected


def test_a_slow_analysis_is_abandoned_not_waited_for():
    def slow():
        time.sleep(0.5)
        return [(1, 2)]
    started = time.monotonic()
    assert _bounded(slow, budget_s=0.05) is None
    assert time.monotonic() - started < 0.3


def test_a_raising_analysis_is_silence():
    def boom():
        raise RuntimeError("kernel")
    assert _bounded(boom, budget_s=1.0) is None


def test_a_non_positive_budget_never_calls_the_analysis():
    called = []
    assert _bounded(lambda: called.append(1), budget_s=0.0) is None and called == []


@pytest.mark.parametrize("budget", [float("nan"), float("inf")])
def test_a_non_finite_budget_is_silence_without_waiting_for_the_analysis(budget):
    def slow():
        time.sleep(2.0)
        return [(1, 2)]
    started = time.monotonic()
    assert _bounded(slow, budget_s=budget) is None
    assert time.monotonic() - started < 1.0


def test_a_failed_fork_leaks_no_pipe(monkeypatch):
    import os

    opened = []
    real_pipe = os.pipe

    def pipe():
        ends = real_pipe()
        opened.extend(ends)
        return ends

    def no_fork():
        raise BlockingIOError("EAGAIN")

    monkeypatch.setattr(os, "pipe", pipe)
    monkeypatch.setattr(os, "fork", no_fork)
    assert _bounded(lambda: [(1, 2)], budget_s=1.0) is None
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_pairs_close_into_components_and_each_face_lists_its_partners():
    assert _components([(1, 2), (2, 3), (5, 6)]) == {1: [2, 3], 2: [1, 3], 3: [1, 2], 5: [6], 6: [5]}
    assert _components([]) == {}


def test_with_the_heal_flag_off_the_session_never_asks_for_a_face_scan(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    seen = []
    monkeypatch.setattr(session_mod, "_localize_scan", lambda s, **kw: seen.append(kw) or ((), True))
    RepairSession(object(), measurer=lambda s: MEASURE).defects()
    assert seen == [{"face_scan_s": 0.0}]


def test_with_the_flag_on_the_session_asks_with_the_remaining_budget_slice(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    seen = []
    monkeypatch.setattr(session_mod, "_localize_scan", lambda s, **kw: seen.append(kw) or ((), True))
    RepairSession(object(), measurer=lambda s: MEASURE, remaining_s=lambda: 40.0).defects()
    assert seen == [{"face_scan_s": 10.0}]


# THE FLOOR FIX (floor-fix-brief.md, binding ruling). The analyzer's findings exist to target the
# heal and to ask a person. They must not change any other op's verdict or the run's ending: the
# corpus floor gate fell 430 -> 318 when they did.

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef  # noqa: E402
from meshpipeline.cad.repair.localize import EntityDefect  # noqa: E402
from meshpipeline.cad.repair.operations import OpOutcome  # noqa: E402
from meshpipeline.cad.repair.plan import Candidate  # noqa: E402

_CROSSING = EntityDefect(
    code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="face",
    entity_index=3, message="crosses 1", fingerprint="face:f3", kernel_status="SelfInterference",
    measurements={"crossing_face_indices": [7], "crossing_faces": ["face:f7"]})
_OPENING = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                        entity_type="edge", entity_index=1, message="free", fingerprint="edge:a")


class _Sew:
    """A sew double: closes the opening it is aimed at."""

    name = "sew_faces"
    answers = frozenset({DefectCode.open_shell})
    scope = 1

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="sewn")


class _Heal(_Sew):
    """A heal double that declines, as the real heal does on a tangle it may not re-trim."""

    name = "heal_self_intersections"
    answers = frozenset({DefectCode.self_intersection})

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape=shape, evidence={"declined": "not_isolated (double)"})


class _HealThatRebuilds(_Heal):
    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="healed")


def _scripted(initial, post):
    """A scanner double: `initial` on the first scan, `post` on every scan after an operation."""
    calls = []

    def scanner(_shape, valid=None):
        calls.append(1)
        return (initial if len(calls) == 1 else post), True
    return scanner


def test_a_kept_sew_is_not_rolled_back_by_an_analyzer_only_crossing(monkeypatch):
    # Mechanism B: trj4_k1_geo-pe-203/-214 lost a baseline-kept sew_faces to `new_defect`
    # because the post-op re-inspection saw a crossing the flag-off world never measured.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    session = RepairSession("original", measurer=lambda s: MEASURE,
                            scanner=_scripted((_OPENING,), (_CROSSING,)))
    defect = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(defect), defect.code, tolerance_mm=0.01)
    assert verdict.kept, verdict.reason
    assert session.shape == "sewn"


def test_the_heal_is_still_judged_on_the_face_scan(monkeypatch):
    # Scan placement (b): the re-inspection judging a heal keeps the analyzer's findings, so a
    # crossing that remains (or appears) still rolls the heal back.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    session = RepairSession("original", measurer=lambda s: MEASURE,
                            scanner=_scripted((_CROSSING,), (_CROSSING,)))
    defect = session.defects()[0]
    verdict = session.try_op(_HealThatRebuilds(), _Heal().targets_for(defect), defect.code,
                             tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "target_remains"


def test_the_default_scanner_runs_the_face_scan_only_initially_and_for_the_heal(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    seen = []
    scans = iter([((_OPENING, _CROSSING), True), ((), True), ((), True)])
    monkeypatch.setattr(session_mod, "_localize_scan",
                        lambda s, **kw: seen.append(kw["face_scan_s"]) or next(scans))
    session = RepairSession(object(), measurer=lambda s: MEASURE, remaining_s=lambda: 40.0)
    opening, crossing = session.defects()
    session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01)
    session.try_op(_HealThatRebuilds(), _Heal().targets_for(crossing), crossing.code,
                   tolerance_mm=0.01)
    assert seen == [10.0, 0.0, 10.0]


class _AnswerEach:
    """Proposes each op for the first defect it answers that the ledger has not banned."""

    label = "double"

    def __init__(self, *ops):
        self._ops = ops

    def propose(self, defects, history, *, shape):
        for defect in defects:
            for op in self._ops:
                if defect.code in op.answers and not history.was_ineffective(
                        defect.fingerprint, op.name):
                    return Candidate(op=op, targets=op.targets_for(defect),
                                     defect_code=defect.code,
                                     defect_fingerprint=defect.fingerprint)
        return None


def test_an_unresolved_analyzer_finding_ends_the_run_as_the_flag_off_run_ends(monkeypatch):
    # Mechanism A, at the loop: the same part with and without the flag. Flag off it carries an
    # opening the sew closes; flag on the initial scan also carries a crossing the heal declines.
    from meshpipeline.cad.repair.loop import RESOLVED, LoopBudget, run

    budget = LoopBudget(max_ops=6, stall_limit=2, timeout_s=120.0)

    def one_run(flag, initial):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", flag)
        session = RepairSession("original", measurer=lambda s: MEASURE,
                                scanner=_scripted(initial, ()))
        return run("original", planner=_AnswerEach(_Sew(), _Heal()), budget=budget,
                   tolerance_mm=0.01, session=session)

    off = one_run(False, (_OPENING,))
    on = one_run(True, (_OPENING, _CROSSING))
    assert off.stopped_because == RESOLVED
    assert on.stopped_because == off.stopped_because
    assert [v.op for v in on.verdicts] == ["sew_faces", "heal_self_intersections"]
    assert on.verdicts[0].kept and not on.verdicts[1].kept
    # STILL LISTED, with its own marker: the finding is reported, it just does not decide the end.
    remaining = on.to_dict()["defects_remaining"]
    assert [d["fingerprint"] for d in remaining] == ["face:f3"]
    assert remaining[0]["analyzer_only"] is True


def test_an_analyzer_only_finding_leaves_the_inspection_status_and_triage_route_alone(
        monkeypatch, tmp_path):
    # Mechanism A, at triage: 19 files routed to manual_cleanup with review off because of a
    # finding only the analyzer made. The route and status must be the flag-off run's.
    from meshpipeline.cad.repair import brep, localize
    from meshpipeline.cad.repair.contracts import status_from_defects
    from meshpipeline.cad.repair.triage import recommend

    path = tmp_path / "part.step"
    path.write_text("ISO-10303-21;")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", False)
    monkeypatch.setattr(brep, "_read_shape", lambda p: object())
    monkeypatch.setattr(brep, "_count_subshapes", lambda s: {
        "solids": 1, "shells": 1, "faces": 6, "edges": 12, "vertices": 8})

    def inspected(flag, found):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", flag)
        monkeypatch.setattr(brep, "_is_valid", lambda s: True)
        monkeypatch.setattr(localize, "localize_scan", lambda s, **kw: (found, True))
        report = brep.inspect_brep_file(path)
        status = status_from_defects(report.defects)
        return report, status, recommend(repair_status=status.value, report=report.to_dict())

    for others in ((), (_OPENING,)):
        off_report, off_status, off_advice = inspected(False, others)
        on_report, on_status, on_advice = inspected(True, (*others, _CROSSING))
        assert on_status == off_status
        assert on_advice.route == off_advice.route
        assert [d.code for d in on_report.defects] == [d.code for d in off_report.defects]
        listed = [e for e in on_report.entities if e["code"] == "self_intersection"]
        assert listed and listed[0]["analyzer_only"] is True


def test_a_crossing_the_status_walk_also_reported_is_not_analyzer_only():
    # The analyzer's record survives dedupe for its partner evidence, but the flag-off world
    # measured this face too, so it keeps its weight in the ending and the route.
    from meshpipeline.cad.repair.localize import _deduped, analyzer_only

    walk = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                        entity_type="face", entity_index=3, message="walk",
                        kernel_status="SelfIntersectingWire")
    assert analyzer_only(_CROSSING) and analyzer_only(_CROSSING.to_dict())
    (survivor,) = _deduped([_CROSSING, walk])
    assert survivor.measurements["crossing_faces"] == ["face:f7"]
    assert not analyzer_only(survivor) and "analyzer_only" not in survivor.to_dict()
    assert not analyzer_only(walk) and "analyzer_only" not in walk.to_dict()


def test_a_crossing_present_before_and_after_a_kept_sew_is_carried_not_rescanned(monkeypatch):
    # Reviewer probe (finding 2), the symmetric case: the analyzer reports the crossing both
    # before and after the sew. The sew is judged on the status walk alone and kept; the finding
    # stays on the list, once, marked as not re-measured since the sew.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    session = RepairSession("original", measurer=lambda s: MEASURE,
                            scanner=_scripted((_OPENING, _CROSSING), (_CROSSING,)))
    defect = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(defect), defect.code, tolerance_mm=0.01)
    assert verdict.kept, verdict.reason
    (left,) = session.defects()
    assert left.fingerprint == "face:f3"
    assert left.measurements["not_rescanned_after"] == "sew_faces"
