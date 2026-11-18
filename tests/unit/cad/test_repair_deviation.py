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
    return _POINTS, 1, 0


def test_deviation_is_max_of_samples():
    got = surface_deviation_mm("b", "a", sampler=_sampler, distance=lambda p: p[0] * 0.5)
    assert got == 1.5


def test_failure_is_none_not_raise():
    def boom(_p):
        raise RuntimeError("kernel")

    assert surface_deviation_mm("b", "a", sampler=_sampler, distance=boom) is None
    assert surface_deviation_mm("b", "a", sampler=lambda *a: ([], 0, 0), distance=lambda p: 1.0) is None

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


def test_result_carries_its_own_sparseness_and_measured_empty_is_not_a_failure():
    got = deviation.measure_deviation("b", "a", sampler=lambda *a: (_POINTS, 2, 1),
                                      distance=lambda p: 1.0)
    assert (got.mm, got.samples, got.faces, got.faces_skipped) == (1.0, 3, 2, 1)
    empty = deviation.measure_deviation("b", "a", sampler=lambda *a: ([], 0, 0),
                                        distance=lambda p: 1.0)
    assert (empty.mm, empty.faces) == (None, 0)           # nothing to measure
    failed = deviation.measure_deviation("b", "a", sampler=lambda *a: 1 / 0)
    assert (failed.mm, failed.faces) == (None, None)      # could not measure


def test_faces_are_named_at_the_quantum_of_the_ops_input_shape(monkeypatch):
    from meshpipeline.cad.repair import operations

    seen: list[float] = []

    def named(shape, *, quantum):
        seen.append(quantum)
        # the face is named "q<quantum>" so only the matching quantum finds it in `faces`
        return [(f"q{quantum}", "F")]

    monkeypatch.setattr(deviation, "_named_faces", named)
    monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 100.0)
    ops_quantum = deviation.quantum_for(250.0)
    assert deviation.quantum_for(100.0) != ops_quantum
    # the op named it at the diagonal of ITS input (250); before_shape's own (100) would miss it
    assert deviation._select_faces("b", "a", (f"q{ops_quantum}",), (250.0,)) == ["F"]
    assert deviation._select_faces("b", "a", (f"q{ops_quantum}",)) == []


# SESSION ATTACHMENT


@pytest.fixture
def _no_probe_scan(monkeypatch):
    monkeypatch.setattr(pkg.RepairSession, "_with_mesh",
                        lambda self, found, shape, **kw: found)


def _commit(monkeypatch, *, probe: str, result, remaining_s=None):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", probe)
    calls: list = []

    def fake(before, after, faces=(), **kw):
        calls.append((before, after, faces, kw))
        return result

    monkeypatch.setattr(deviation, "measure_deviation", fake)
    session = pkg._session()
    session.remaining_s = remaining_s
    verdict = session.try_package(pkg._package(session, cleared={pkg._EDGE}))
    assert verdict.committed
    return verdict, calls


def test_committed_package_carries_deviation_when_probe_live(monkeypatch, _no_probe_scan):
    result = deviation.DeviationResult(0.25, 40, 5, 1, 0.3)
    verdict, calls = _commit(monkeypatch, probe="live", result=result)
    assert verdict.deviation_mm == 0.25
    shown = verdict.to_dict()
    assert (shown["deviation_mm"], shown["deviation_samples"], shown["deviation_faces"],
            shown["deviation_faces_skipped"], shown["deviation_s"]) == (0.25, 40, 5, 1, 0.3)
    assert calls[0][0] == "original" and calls[0][1] == "repaired"
    assert calls[0][3]["budget_s"] == 10.0


def test_budget_is_the_least_of_ten_the_run_and_the_package(monkeypatch, _no_probe_scan):
    _, calls = _commit(monkeypatch, probe="live", result=deviation.DeviationResult(),
                       remaining_s=lambda: 3.0)
    assert calls[0][3]["budget_s"] == 3.0


def test_deviation_absent_and_unmeasured_when_probe_off(monkeypatch):
    verdict, calls = _commit(monkeypatch, probe="off", result=deviation.DeviationResult(1.0))
    assert calls == []
    assert not [k for k in verdict.to_dict() if k.startswith("deviation")]


def test_a_none_measurement_is_absent_and_measured_empty_says_zero_faces(monkeypatch,
                                                                         _no_probe_scan):
    verdict, _ = _commit(monkeypatch, probe="live",
                         result=deviation.DeviationResult(None, 0, 0, 0, 0.1))
    shown = verdict.to_dict()
    assert "deviation_mm" not in shown and shown["deviation_faces"] == 0
    failed, _ = _commit(monkeypatch, probe="live", result=deviation.DeviationResult(None))
    assert "deviation_mm" not in failed.to_dict() and "deviation_faces" not in failed.to_dict()


def test_a_reverted_package_is_not_measured(monkeypatch, _no_probe_scan):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    calls: list = []
    monkeypatch.setattr(deviation, "measure_deviation",
                        lambda *a, **k: calls.append(1) or deviation.DeviationResult(1.0))
    session = pkg._session(repairs=False)
    verdict = session.try_package(pkg._package(session, cleared={pkg._EDGE}))
    assert not verdict.committed and calls == [] and verdict.deviation_mm is None


# RECEIPT


def _record(*packages):
    return {"measurements": {"regions": {"packages": list(packages)}}}


def test_receipt_words_the_largest_committed_deviation_as_a_sampled_lower_bound():
    rows = _deviation_rows(_record(
        {"status": "committed", "deviation_mm": 0.2},
        {"status": "committed", "deviation_mm": 125.0, "deviation_samples": 36,
         "deviation_faces": 3},
        {"status": "reverted", "deviation_mm": 900.0}))
    assert len(rows) == 1
    assert rows[0]["value"] == "at least 125 mm"
    assert "sampled: 36 points on 3 faces" in rows[0]["why"]
    assert "new surface filling an opening" in rows[0]["why"]
    assert _deviation_rows(_record({"status": "committed"})) == []
    assert _deviation_rows({}) == []


def test_a_later_package_is_measured_against_the_upload_not_its_own_start(monkeypatch,
                                                                          _no_probe_scan):
    # Fix round 1 (I2): the spec's "deviation from the upload". A second package starts from the
    # first one's result, and is still measured against what repair received.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    calls: list = []
    monkeypatch.setattr(deviation, "measure_deviation",
                        lambda before, after, faces=(), **kw: calls.append(before)
                        or deviation.DeviationResult(0.1, 4, 1, 0, 0.0))
    session = pkg._session()
    first = session.try_package(pkg._package(session, cleared={pkg._EDGE}))
    assert first.committed and session.shape == "repaired"
    second = session.try_package(pkg._package(session, unchanged={pkg._FACE}))
    assert second.committed, second.reason
    assert calls == ["original", "original"]
