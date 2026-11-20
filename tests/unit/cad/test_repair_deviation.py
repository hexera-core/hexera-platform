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


# A FACE-REMOVING PACKAGE IS MEASURED (lane A's 0053_00531146 objection run: fix_small_faces
# removes faces and declares none added or rebuilt, so the deviation selected nothing, read
# "not measured: no faces", and the reviewer rejected on a field no author could ever satisfy).


def _box(x=10.0, y=10.0, z=10.0, at=(0.0, 0.0, 0.0)):
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(*at), x, y, z).Shape()


def _top_fingerprint(shape):
    from meshpipeline.cad.repair.identity import quantum_for
    from meshpipeline.cad.repair.operations import _diagonal_of, box_of

    named = deviation._named_faces(shape, quantum=quantum_for(_diagonal_of(shape)))
    for name, face in named:
        box = box_of(face)
        if box["bbox_min"][2] > 9.0:
            return name
    raise AssertionError("no top face")


def test_a_removal_is_measured_on_the_faces_that_took_over_its_region():
    before = after = _box()
    result = deviation.measure_deviation(before, after,
                                         removed=((before, (_top_fingerprint(before),)),))
    assert result.basis == deviation.BASIS_ADJACENT
    assert result.mm is not None and result.mm == pytest.approx(0.0, abs=1e-6)
    assert result.samples > 0 and result.faces >= 4


def test_a_removal_with_no_neighbour_left_falls_back_to_a_whole_part_sample():
    after = _box()
    elsewhere = _box(at=(100.0, 100.0, 100.0))
    result = deviation.measure_deviation(after, after,
                                         removed=((elsewhere, (_top_fingerprint(elsewhere),)),))
    assert result.basis == deviation.BASIS_WHOLE
    assert result.mm is not None and result.faces == 6 and result.samples > 0


def test_no_removal_and_no_changed_face_still_reads_no_faces():
    # Missing still reads missing: only a REMOVAL reaches the fallbacks.
    shape = _box()
    result = deviation.measure_deviation(shape, shape)
    assert result.mm is None and result.faces == 0
    assert result.basis == deviation.BASIS_CHANGED


def test_a_kept_removal_is_handed_to_the_measurement_and_its_basis_reported(monkeypatch,
                                                                         _no_probe_scan):
    def removing(self, shape, targets, *, tolerance_mm):
        self._clock.now += pkg._APPLY_S
        return pkg.OpOutcome(shape="repaired",
                             evidence={"area_delta_mm2": 0.0, "faces_removed": 1,
                                       "removed_fingerprints": ["face:sliver"]})

    monkeypatch.setattr(pkg._Op, "apply", removing)
    result = deviation.DeviationResult(0.004, 32, 3, 0, 0.2, deviation.BASIS_ADJACENT)
    verdict, calls = _commit(monkeypatch, probe="live", result=result)
    assert calls[0][3]["removed"] == (("original", ("face:sliver",)),)
    shown = verdict.to_dict()
    assert shown["deviation_mm"] == 0.004
    assert shown["deviation_basis"] == deviation.BASIS_ADJACENT


def test_the_reviewer_is_shown_the_basis_and_a_measured_status():
    from meshpipeline.cad.repair import package_review

    evidence = package_review.evidence_of(
        None, {"verdicts": [], "before": {}, "after": {}},
        {"deviation_mm": 0.004, "deviation_samples": 32, "deviation_faces": 3,
         "deviation_faces_skipped": 0, "deviation_basis": deviation.BASIS_ADJACENT,
         "deviation_status": package_review.DEVIATION_MEASURED}, {}, {})
    assert evidence["deviation_status"] == package_review.DEVIATION_MEASURED
    assert evidence["deviation_basis"] == deviation.BASIS_ADJACENT
    assert evidence["deviation_mm"] == 0.004


# --- the samples are measured in a bounded child (audit row 3) --------------------------------


def _vertices(monkeypatch):
    """The one kernel call deviation still makes in the parent is making a vertex; stub it."""
    monkeypatch.setattr(deviation, "_vertex", lambda point: f"vertex{point}")


def _batch(monkeypatch, answers):
    seen: dict = {}

    def _distances(shapes, pairs, *, budget_s, bounds=None, tol=0.0):
        seen.setdefault("calls", 0)
        seen["calls"] += 1
        seen.update(shapes=shapes, pairs=pairs, budget_s=budget_s)
        return answers

    monkeypatch.setattr(deviation.occ_distance, "distances", _distances)
    return seen


def _ok(value):
    return {"min_distance_mm": value, "point_a": None, "point_b": None,
            "distance_status": "measured"}


def test_every_sample_is_measured_in_one_child(monkeypatch):
    """THE K-FOLD COST IS WHY IT IS ONE CHILD. The deviation runs inside every tournament
    rehearsal and measures up to `samples` points; a child per point would pay interpreter
    start-up per sample. One child measures all of them against the before shape."""
    _vertices(monkeypatch)
    seen = _batch(monkeypatch, [_ok(0.5), _ok(2.5), _ok(1.0)])

    got = deviation.measure_deviation("before", "after", sampler=lambda *a: (_POINTS, 2, 0),
                                      budget_s=9.0)

    assert seen["calls"] == 1
    assert seen["shapes"][0] == "before", "every sample is measured against the BEFORE shape"
    assert seen["pairs"] == [(0, 1), (0, 2), (0, 3)]
    assert got.mm == 2.5 and got.samples == 3


def test_a_sample_the_child_never_answered_is_not_a_deviation(monkeypatch):
    """A MAXIMUM OVER PART OF THE SAMPLES IS A LOWER BOUND, NOT THE DEVIATION. Elsewhere a
    partial batch is worth keeping; here it is not, because the answer is the WORST point and the
    unmeasured one may be it. So an incomplete batch reads exactly as the spent budget did: not
    measured, and the reviewer sees no number rather than a flattering one."""
    from meshpipeline.cad.repair import occ_distance

    _vertices(monkeypatch)
    _batch(monkeypatch, [_ok(0.5),
                         {**occ_distance.UNMEASURED, "distance_status": "unmeasured: "
                          "budget_spent"},
                         _ok(1.0)])

    got = deviation.measure_deviation("before", "after", sampler=lambda *a: (_POINTS, 2, 0),
                                      budget_s=9.0)

    assert got.mm is None and got.faces is None


def test_a_kernel_refusal_is_not_a_deviation_either(monkeypatch):
    """`_distance_to` raised when the kernel did not finish, and the caller turned that into "not
    measured". A refused pair keeps that meaning - it is not a measurement of zero."""
    from meshpipeline.cad.repair import occ_distance

    _vertices(monkeypatch)
    _batch(monkeypatch, [_ok(0.5),
                         {**occ_distance.UNMEASURED,
                          "distance_status": "unmeasured: the kernel gave no distance"},
                         _ok(1.0)])

    assert deviation.measure_deviation("before", "after", sampler=lambda *a: (_POINTS, 2, 0),
                                       budget_s=9.0).mm is None


def test_the_batch_gets_what_is_left_of_the_budget_not_the_whole_of_it(monkeypatch):
    """The budget covers the sampling too, so the child may only have what the selection left."""
    _vertices(monkeypatch)
    seen = _batch(monkeypatch, [_ok(1.0)] * 3)
    ticks = iter([100.0, 104.0, 104.0, 104.0])

    deviation.measure_deviation("before", "after", sampler=lambda *a: (_POINTS, 2, 0),
                                budget_s=10.0, clock=lambda: next(ticks))

    assert seen["budget_s"] == pytest.approx(6.0)
