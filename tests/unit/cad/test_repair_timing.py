# Responsibility: Verify the repair run records where its time went - apply, scan and measure - from an injected clock, so the numbers are exact and need no kernel.
from __future__ import annotations

from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import LoopBudget, run
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.plan import Candidate
from meshpipeline.cad.repair.session import OpVerdict, RepairSession

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)

_APPLY_S = 0.5
_SCAN_S = 1.0
_MEASURE_S = 0.25


class _Clock:
    """A clock that only moves when the work it is timing says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Op:
    name = "fix"
    scope = 1

    def __init__(self, clock):
        self._clock = clock

    def apply(self, shape, targets, *, tolerance_mm):
        self._clock.now += _APPLY_S
        return OpOutcome(shape="fixed")


class _Planner:
    label = "one-shot"

    def __init__(self, op):
        self._op = op

    def propose(self, defects, history, *, shape):
        if not defects:
            return None
        d = defects[0]
        return Candidate(op=self._op,
                         targets=(EntityRef(entity_type="edge", fingerprint=d.fingerprint,
                                            entity_index=d.entity_index),),
                         defect_code=d.code, defect_fingerprint=d.fingerprint)


def _run_timed():
    clock = _Clock()
    defect = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.error,
                          entity_type="edge", entity_index=1, message="x",
                          fingerprint="edge:a")
    scans = [[defect], []]

    def scanner(_shape):
        clock.now += _SCAN_S
        return (scans.pop(0) if len(scans) > 1 else scans[0]), True

    def measurer(_shape):
        clock.now += _MEASURE_S
        return _SOUND

    session = RepairSession("original", measurer=measurer, scanner=scanner, clock=clock)
    op = _Op(clock)
    result = run("original", planner=_Planner(op), session=session, clock=clock,
                 budget=LoopBudget(max_ops=3, stall_limit=2, timeout_s=1000.0),
                 tolerance_mm=0.01)
    return result.verdicts, result


def test_verdict_records_apply_and_scan_seconds():
    verdicts, _ = _run_timed()
    assert verdicts[0].kept
    assert verdicts[0].apply_s == _APPLY_S
    assert verdicts[0].scan_s == _SCAN_S
    # measured twice per operation: before and after the apply
    assert verdicts[0].measure_s == 2 * _MEASURE_S
    assert verdicts[0].to_dict()["apply_s"] == _APPLY_S
    assert verdicts[0].to_dict()["scan_s"] == _SCAN_S
    assert verdicts[0].to_dict()["measure_s"] == 2 * _MEASURE_S


def test_run_reports_total_and_scan_count():
    _, result = _run_timed()
    # first scan + two measures + apply + re-inspection + the end-state measure; the
    # construction-time measure happens before the run's clock starts.
    assert result.total_s == _SCAN_S + 2 * _MEASURE_S + _APPLY_S + _SCAN_S + _MEASURE_S
    assert result.scan_count == 2  # the first scan plus one re-inspection
    assert result.to_dict()["total_s"] == result.total_s
    assert result.to_dict()["scan_count"] == 2


def test_timing_fields_default_so_existing_constructions_still_work():
    v = OpVerdict(op="x", targets=(), defect_code="c", status="kept")
    assert (v.apply_s, v.scan_s, v.measure_s) == (0.0, 0.0, 0.0)
