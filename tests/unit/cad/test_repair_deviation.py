# Responsibility: Verify the deviation evidence - the sampled maximum distance from a repaired
#                 surface to the upload - is the max of its samples, is None (never a raise) on any
#                 failure or spent budget, selects the right faces, and is attached to a committed
#                 package only under the mesh-probe posture.
from __future__ import annotations

import pytest
from tests.unit.cad import test_repair_package_session as pkg

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import deviation
from meshpipeline.cad.repair.deviation import surface_deviation_mm
from meshpipeline.pipeline.repair_receipt import _deviation_rows

_POINTS = [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0)]


def _sampler(*_args):
    return _POINTS


def test_deviation_is_max_of_samples():
    got = surface_deviation_mm("b", "a", sampler=_sampler, distance=lambda p: p[0] * 0.5)
    assert got == 1.5


def test_failure_is_none_not_raise():
    def boom(_p):
        raise RuntimeError("kernel")

    assert surface_deviation_mm("b", "a", sampler=_sampler, distance=boom) is None
    assert surface_deviation_mm("b", "a", sampler=lambda *a: [], distance=lambda p: 1.0) is None

    def bad_sampler(*_a):
        raise ValueError("no faces")

    assert surface_deviation_mm("b", "a", sampler=bad_sampler, distance=lambda p: 1.0) is None
    # Real shapes that are not shapes: still None, never a raise.
    assert surface_deviation_mm("not a shape", "nor this") is None


def test_budget_bounds_sampling():
    now = {"t": 0.0}
    seen: list = []

    def distance(p):
        seen.append(p)
        now["t"] += 1.0
        return 1.0

    got = surface_deviation_mm("b", "a", budget_s=1.5, sampler=_sampler, distance=distance,
                               clock=lambda: now["t"])
    assert got is None                      # a partial maximum is never reported as the answer
    assert len(seen) < len(_POINTS)         # and the work stopped when the budget did


def test_faces_filter_selects_new_fingerprints(monkeypatch):
    from meshpipeline.cad.repair import operations

    names = {"before": [("f1", "F1"), ("f2", "F2")],
             "after": [("f1", "F1"), ("f2", "F2"), ("f3", "F3"), ("f4", "F4")]}
    monkeypatch.setattr(deviation, "_named_faces", lambda shape, *, quantum: names[shape])
    monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 100.0)

    assert deviation._select_faces("before", "after", ()) == ["F3", "F4"]
    assert deviation._select_faces("before", "after", ("f2", "f4")) == ["F2", "F4"]
    assert deviation._select_faces("before", "after", ("nope",)) == []


# SESSION ATTACHMENT


@pytest.fixture
def _no_probe_scan(monkeypatch):
    monkeypatch.setattr(pkg.RepairSession, "_with_mesh",
                        lambda self, found, shape, **kw: found)


def _commit(monkeypatch, *, probe: str, measured):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", probe)
    calls: list = []

    def fake(before, after, faces=(), **kw):
        calls.append((before, after, faces, kw))
        return measured

    monkeypatch.setattr(deviation, "surface_deviation_mm", fake)
    session = pkg._session()
    verdict = session.try_package(pkg._package(session, cleared={pkg._EDGE}))
    assert verdict.committed
    return verdict, calls


def test_committed_package_carries_deviation_when_probe_live(monkeypatch, _no_probe_scan):
    verdict, calls = _commit(monkeypatch, probe="live", measured=0.25)
    assert verdict.deviation_mm == 0.25
    assert verdict.to_dict()["deviation_mm"] == 0.25
    assert calls[0][0] == "original" and calls[0][1] == "repaired"
    assert calls[0][3]["budget_s"] == 10.0


def test_deviation_absent_and_unmeasured_when_probe_off(monkeypatch):
    verdict, calls = _commit(monkeypatch, probe="off", measured=0.25)
    assert calls == []
    assert verdict.deviation_mm is None
    assert "deviation_mm" not in verdict.to_dict()


def test_a_none_measurement_is_recorded_as_absent(monkeypatch, _no_probe_scan):
    verdict, calls = _commit(monkeypatch, probe="live", measured=None)
    assert calls and verdict.deviation_mm is None
    assert "deviation_mm" not in verdict.to_dict()


def test_a_reverted_package_is_not_measured(monkeypatch, _no_probe_scan):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    calls: list = []
    monkeypatch.setattr(deviation, "surface_deviation_mm",
                        lambda *a, **k: calls.append(1) or 1.0)
    session = pkg._session(repairs=False)
    verdict = session.try_package(pkg._package(session, cleared={pkg._EDGE}))
    assert not verdict.committed and calls == [] and verdict.deviation_mm is None


# RECEIPT


def _record(*packages):
    return {"measurements": {"regions": {"packages": list(packages)}}}


def test_receipt_shows_the_largest_committed_deviation_and_is_silent_without_one():
    rows = _deviation_rows(_record({"status": "committed", "deviation_mm": 0.2},
                                   {"status": "committed", "deviation_mm": 0.31},
                                   {"status": "reverted", "deviation_mm": 9.0}))
    assert [(r["label"], r["value"]) for r in rows] == [("Repaired surface moved",
                                                         "up to 0.31 mm")]
    assert _deviation_rows(_record({"status": "committed"})) == []
    assert _deviation_rows({}) == []
