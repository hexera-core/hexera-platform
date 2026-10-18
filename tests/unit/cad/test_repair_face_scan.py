# Responsibility: Verify the face-level self-intersection scan's budget, its bounded wrapper, how pairs close into components, and that the session asks for a scan only with the heal enabled.
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
