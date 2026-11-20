# Responsibility: Verify the repair session carries gmsh's mesh defects only when CAD_REPAIR_MESH_PROBE is exactly live - appended after localize's findings on every scan path (whole, per-operation re-inspection, scoped package proof), never on top of an injected scanner, and never at the cost of the scan's completeness.
from __future__ import annotations

import logging

import pytest

import meshpipeline.cad.repair.mesh_probe as mesh_probe
import meshpipeline.cad.repair.region_loop as region_loop
import meshpipeline.cad.repair.session as session_mod
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import RepairSession

MEASURE = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)

_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}
_OPENING = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                        entity_type="edge", entity_index=1, message="free",
                        fingerprint="edge:a", location=dict(_BOX))
_OVERLAP = EntityDefect(code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
                        entity_type="face", entity_index=4, message="overlapping facets",
                        fingerprint="face:f4", location=dict(_BOX),
                        measurements={"mesh_kind": "overlapping_facets", "gmsh_surfaces": [4]})
_TIMEOUT = EntityDefect(code=DefectCode.mesh_timeout, severity=DefectSeverity.error,
                        entity_type="shape", entity_index=0, message="ran past its budget",
                        measurements={"mesh_kind": "timeout", "gmsh_surfaces": []})
_NOVOL = EntityDefect(code=DefectCode.mesh_no_volume, severity=DefectSeverity.error,
                      entity_type="shape", entity_index=0, message="no volume",
                      measurements={"mesh_kind": "no_volume", "gmsh_surfaces": []})
_NEW_WARNING = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                            entity_type="edge", entity_index=9, message="short",
                            fingerprint="edge:w", location=dict(_BOX))
_REGION = Region(index=0, fingerprints=frozenset({"edge:a", "face:f4"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0))


@pytest.fixture(autouse=True)
def _heal_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 120.0)


def _probe(monkeypatch, *results):
    """Patch the probe: each call records its kwargs and returns the next result (the last repeats)."""
    calls: list[dict] = []

    def fake(shape, **kw):
        calls.append(kw)
        return results[min(len(calls), len(results)) - 1]
    monkeypatch.setattr(mesh_probe, "mesh_defects", fake)
    return calls


def _scan(monkeypatch, *results):
    calls: list[dict] = []

    def fake(shape, **kw):
        calls.append(kw)
        return results[min(len(calls), len(results)) - 1]
    monkeypatch.setattr(session_mod, "_localize_scan", fake)
    return calls


class _Remesh:
    """An operation double aimed at a mesh defect on a face; returns a new shape."""

    name = "remesh_face"
    answers = frozenset({DefectCode.mesh_overlapping_facets})
    scope = 1

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="rebuilt")


def test_off_never_calls_probe(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "off")

    def boom(*a, **kw):
        raise AssertionError("the mesh probe ran with the posture off")
    monkeypatch.setattr(mesh_probe, "mesh_defects", boom)
    found = ((_OPENING,), True)
    calls = _scan(monkeypatch, found)
    session = RepairSession(object(), measurer=lambda s: MEASURE)
    assert session.defects() == (_OPENING,)
    session._rescan(face_scan=False)
    session._rescan(face_scan=False, within=_REGION)
    session._region_scan(_REGION)
    # The whole-shape call is still exactly the call it always was.
    assert calls[0] == {"face_scan_s": 0.0, "quantum_diagonal_mm": MEASURE.diagonal_mm}


@pytest.mark.parametrize("raw,expected", [
    ("live", "live"), ("Live ", "off"), ("LIVE", "off"), (" live", "off"), ("on", "off"),
    ("true", "off"), ("", "off"), ("off", "off")])
def test_typo_reads_off(monkeypatch, raw, expected):
    monkeypatch.setenv("CAD_REPAIR_MESH_PROBE", raw)
    assert rcfg._mesh_probe_posture() == expected


def test_unset_reads_off(monkeypatch):
    monkeypatch.delenv("CAD_REPAIR_MESH_PROBE", raising=False)
    assert rcfg._mesh_probe_posture() == "off"


@pytest.mark.parametrize("raw,warned", [("Live ", True), ("on", True), ("live", False),
                                        ("off", False)])
def test_an_unrecognised_posture_is_warned_about(monkeypatch, caplog, raw, warned):
    monkeypatch.setenv("CAD_REPAIR_MESH_PROBE", raw)
    with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
        rcfg._mesh_probe_posture()
    assert any("CAD_REPAIR_MESH_PROBE" in r.getMessage() for r in caplog.records) is warned


@pytest.mark.parametrize("posture,probe_s,run_s,warns", [
    ("live", 60.0, 70.0, True),     # 60 + 15 >= 70: the reserve is the whole run
    ("live", 60.0, 75.0, True),     # the boundary
    ("live", 60.0, 120.0, False),   # the defaults
    ("off", 60.0, 70.0, False),     # probe off: no reserve
])
def test_a_probe_timeout_that_leaves_no_time_to_author_is_warned(caplog, posture, probe_s, run_s,
                                                                 warns):
    with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
        rcfg._warn_no_authoring_time(posture, probe_s, run_s)
    assert any("CAD_REPAIR_MESH_PROBE_TIMEOUT_S" in r.getMessage()
               for r in caplog.records) is warns


def test_live_appends_after_localize(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    calls = _scan(monkeypatch, ((_OPENING,), True))
    probes = _probe(monkeypatch, (_OVERLAP,))
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 300.0)
    assert session.defects() == (_OPENING, _OVERLAP)
    # localize is called exactly as it always was: mesh defects are never passed INTO it.
    assert calls == [{"face_scan_s": 0.0, "quantum_diagonal_mm": MEASURE.diagonal_mm}]
    assert probes == [{"diagonal_mm": 200.0, "budget_s": 120.0, "within": None}]


def test_the_budget_is_what_remains_when_less_than_the_timeout(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    probes = _probe(monkeypatch, ())
    RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 30.0).defects()
    # HALF of what is left (final review I2): a probe that times out leaves the run as much again.
    assert probes[0]["budget_s"] == 15.0


@pytest.mark.parametrize("remaining,expected", [(300.0, 120.0), (200.0, 100.0), (100.0, 50.0)])
def test_a_probe_is_capped_at_half_the_time_left(monkeypatch, remaining, expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    probes = _probe(monkeypatch, ())
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: remaining)
    session.defects()
    session._rescan(face_scan=False)
    session._rescan(face_scan=False, within=_REGION)
    assert [p["budget_s"] for p in probes] == [expected] * 3
    assert all(p["budget_s"] <= 0.5 * remaining for p in probes)


def test_the_final_whole_shape_probe_is_never_handed_the_slack(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    probes = _probe(monkeypatch, ())
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 70.0)
    session._rescan(face_scan=True, final=True)
    assert probes[-1]["budget_s"] == 70.0 - session_mod.FINAL_PROBE_SLACK_S


@pytest.mark.parametrize("remaining", [200.0, 70.0, 25.0, 10.5])
def test_a_final_probe_that_spends_its_budget_leaves_the_slack(monkeypatch, remaining):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    now = [0.0]
    spent = []

    def probe(shape, **kw):
        now[0] += kw["budget_s"]  # the probe uses its whole budget
        spent.append(kw["budget_s"])
        return ()
    monkeypatch.setattr(mesh_probe, "mesh_defects", probe)
    session = RepairSession("shape", measurer=lambda s: MEASURE,
                            remaining_s=lambda: remaining - now[0])
    session._rescan(face_scan=True, final=True)
    assert remaining - now[0] >= session_mod.FINAL_PROBE_SLACK_S


@pytest.mark.parametrize("remaining", [10.0, 4.0])
def test_a_final_probe_with_only_the_slack_left_is_skipped(monkeypatch, remaining):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    probes = _probe(monkeypatch, ())
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: remaining)
    session._rescan(face_scan=True, final=True)
    assert probes == []


@pytest.mark.parametrize("remaining", [0.0, -5.0, float("nan")])
def test_no_time_left_skips_the_probe_and_says_so(monkeypatch, remaining):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True))

    def boom(*a, **kw):
        raise AssertionError("probed with no budget")
    monkeypatch.setattr(mesh_probe, "mesh_defects", boom)
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: remaining)
    # Never probed, so there is nothing to carry: one unlocated timeout, not a claim of absence.
    opening, timeout = session.defects()
    assert opening == _OPENING
    assert timeout.code is DefectCode.mesh_timeout and timeout.fingerprint == ""
    assert session.complete_scan is True


def test_a_final_scan_with_no_time_left_keeps_the_mesh_error(monkeypatch):
    # Finding 1: the region loop's final whole-shape scan runs exactly when time is out. A probe
    # skipped there must not erase the mesh error the run started with and call the part resolved.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_OVERLAP, _NOVOL))
    left = iter([300.0, 0.0])
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: next(left))
    assert session.defects() == (_OVERLAP, _NOVOL)
    after = session._rescan(face_scan=True)
    assert after == (_OVERLAP, _NOVOL) and session.defects() == after
    assert region_loop._terminal(session, after, escalated=[], packages=(),
                                 fallback="time_budget") != region_loop.RESOLVED


def test_a_probe_that_times_out_carries_forward_what_it_knew(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_OVERLAP,), (_TIMEOUT,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    assert session._rescan(face_scan=False) == (_OVERLAP, _TIMEOUT)


def test_a_skipped_scoped_probe_carries_the_regions_located_mesh_findings(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_OVERLAP, _NOVOL))
    left = iter([300.0, 0.0])
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: next(left))
    session.defects()
    found, _ = session._region_scan(_REGION)
    assert found == (_OVERLAP,)


def test_scoped_scan_passes_region(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    calls = _scan(monkeypatch, ((_OPENING,), True))
    probes = _probe(monkeypatch, (_OVERLAP,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)

    found, complete = session._region_scan(_REGION)
    assert found == (_OPENING, _OVERLAP) and complete
    assert calls[-1] == {"face_scan_s": 0.0, "within": _REGION, "scope_diagonal_mm": 200.0,
                         "quantum_diagonal_mm": 200.0}
    assert probes[-1] == {"diagonal_mm": 200.0, "budget_s": 120.0, "within": _REGION}

    found = session._rescan(face_scan=False, within=_REGION)
    assert found == (_OPENING, _OVERLAP)
    assert probes[-1]["within"] is _REGION


def test_sound_part_adds_no_mesh_defects(monkeypatch):
    found = ((_OPENING,), True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "off")
    _scan(monkeypatch, found)
    off = RepairSession("shape", measurer=lambda s: MEASURE)
    off_defects, off_complete = off.defects(), off.complete_scan

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _probe(monkeypatch, ())
    live = RepairSession("shape", measurer=lambda s: MEASURE)
    assert live.defects() == off_defects and live.complete_scan == off_complete


def test_probe_failure_does_not_mark_scan_incomplete(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True))
    _probe(monkeypatch, (_TIMEOUT,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    assert session.defects() == (_OPENING, _TIMEOUT)
    assert session.complete_scan is True
    session._rescan(face_scan=False)
    assert session.complete_scan is True


def test_per_operation_rescan_includes_mesh_defects(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True))
    # The defect survives the operation: the progress rule must see it and roll back.
    probes = _probe(monkeypatch, (_OVERLAP,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[1]
    verdict = session.try_op(_Remesh(), _Remesh().targets_for(target), target.code,
                             tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert len(probes) == 2 and session.shape == "shape"

    # The defect goes: the same operation is kept, judged on a rescan that probed again.
    probes = _probe(monkeypatch, (_OVERLAP,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[1]
    verdict = session.try_op(_Remesh(), _Remesh().targets_for(target), target.code,
                             tolerance_mm=0.01)
    assert verdict.kept, verdict.reason
    assert len(probes) == 2 and session.defects() == (_OPENING,)


def test_an_injected_scanner_is_the_whole_truth(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")

    def boom(*a, **kw):
        raise AssertionError("probed on top of an injected scanner")
    monkeypatch.setattr(mesh_probe, "mesh_defects", boom)
    session = RepairSession("shape", measurer=lambda s: MEASURE,
                            scanner=lambda s: ((_OPENING,), True))
    assert session.defects() == (_OPENING,)
    session._rescan(face_scan=False)
    session._region_scan(_REGION)


class _Sew:
    """A sew double aimed at the opening; returns a new shape."""

    name = "sew_faces"
    answers = frozenset({DefectCode.open_shell})
    scope = 1

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="sewn")


@pytest.mark.parametrize("before,after", [
    ((), (_TIMEOUT,)),                    # the probe times out after the operation
    ((_TIMEOUT,), (_OVERLAP,)),           # it finished after timing out before: not news
    ((_OVERLAP,), (_TIMEOUT,)),           # carried forward, plus the timeout: not news
    ((_NOVOL,), (_NOVOL,)),               # an unlocated verdict that was already there
])
def test_mesh_probe_noise_does_not_roll_back_a_good_operation(monkeypatch, before, after):
    # Finding 2, both directions. ONLY a no-answer (timeout, probe error) is exempt from the
    # progress rule; an unlocated EVIDENCE code is judged (final review I1).
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, before, after)
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01)
    assert verdict.kept, verdict.reason


_OTHER = EntityDefect(code=DefectCode.mesh_other, severity=DefectSeverity.error,
                      entity_type="shape", entity_index=0, message="unplaceable failure",
                      measurements={"mesh_kind": "other", "gmsh_surfaces": []})
_OVERLAP_UNPLACED = EntityDefect(code=DefectCode.mesh_overlapping_facets,
                                 severity=DefectSeverity.error, entity_type="shape",
                                 entity_index=0, message="overlapping facets, unplaceable",
                                 measurements={"mesh_kind": "overlapping_facets",
                                               "gmsh_surfaces": []})


@pytest.mark.parametrize("before,after", [
    ((), (_NOVOL,)),                      # a part that meshed now has no volume
    ((), (_OTHER,)),                      # an unplaceable failure where there was none
    ((), (_OVERLAP_UNPLACED,)),           # a located-family failure the probe could not place
    ((_OVERLAP,), (_OVERLAP, _NOVOL)),    # a kernel op earns no reveal amnesty
], ids=["no_volume", "other", "unplaced_overlap", "kernel_op_beside_mesh"])
def test_a_new_unlocated_mesh_failure_rolls_back(monkeypatch, before, after):
    # Final review I1(a): the mesh test says the operation made meshing worse; it is not kept.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, before, after)
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"
    assert session.shape == "shape"


def test_a_scoped_judgement_after_a_whole_part_timeout_does_not_read_unlocated_as_news(
        monkeypatch):
    # The pre-op whole-shape probe timed out, so the before side was never walked; the region's
    # previous list drops unlocated findings, so the timeout must reach the judgement another way.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, (_TIMEOUT,), (_OTHER,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01,
                             within=_REGION)
    assert verdict.kept, verdict.reason


def test_a_mesh_target_that_persists_unplaceable_remains(monkeypatch):
    # Final review I1(b): the rescan still fails with the targeted kind but cannot place it. The
    # failure persisted; the target's fingerprint is merely gone.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_OVERLAP,), (_OVERLAP_UNPLACED,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    verdict = session.try_op(_Remesh(), _Remesh().targets_for(target), target.code,
                             tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert session.shape == "shape"


def test_an_unlocated_failure_behind_a_cleared_mesh_target_is_revealed(monkeypatch):
    # The pre-op list held mesh findings, the mesh op cleared its target, and the probe then met
    # a DIFFERENT failure it could not place: revealed, not damage (it names no geometry, so it
    # cannot be the op's own) - and it stays on the list.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_THROUGH,), (_OTHER,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    op = _Fragment(rebuilt=("face:f4",))
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert verdict.kept, verdict.reason
    assert verdict.delta["revealed"] == [{"fingerprint": "", "code": "mesh_other"}]
    assert _OTHER in session.defects()


def test_a_new_located_mesh_error_still_rolls_back_when_both_probes_finished(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, (), (_OVERLAP,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"


def test_a_scoped_rescan_keeps_the_whole_parts_unlocated_mesh_findings(monkeypatch):
    # Finding 3a: a 2D-only scoped probe cannot see `no volume`, so `_merged` must not drop it.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True))
    _probe(monkeypatch, (_NOVOL,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    session._rescan(face_scan=False, within=_REGION)
    assert _NOVOL in session.defects()


def test_a_whole_part_mesh_error_does_not_excuse_a_new_warning_in_the_region(monkeypatch):
    # Finding 3b: left in the scoped `previous`, the unlocated error makes the operation look like
    # it improved the region to warnings, and the warning it introduced is excused.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((_NEW_WARNING,), True))
    _probe(monkeypatch, (_NOVOL,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01,
                             within=_REGION)
    assert not verdict.kept and verdict.reason == "new_defect"


# CONTROLLER RULING R11a. A scoped probe meshes surfaces only, so a LOCATED volume-stage mesh
# failure is invisible to it: without a carry it would vanish from a scoped rescan and read as
# cleared, crediting a package or an operation with a repair nobody could see. Only a whole-shape
# probe may clear one.
_THROUGH = EntityDefect(code=DefectCode.mesh_edge_through_face, severity=DefectSeverity.error,
                        entity_type="face", entity_index=4, message="a segment and a facet intersect",
                        fingerprint="face:f4", location=dict(_BOX),
                        measurements={"mesh_kind": "edge_through_face", "gmsh_surfaces": [],
                                      "stage": mesh_probe.STAGE_3D})
_OVERLAP_3D = EntityDefect(code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
                           entity_type="face", entity_index=4, message="overlapping facets",
                           fingerprint="face:f4", location=dict(_BOX),
                           measurements={"mesh_kind": "overlapping_facets", "gmsh_surfaces": [4],
                                         "stage": mesh_probe.STAGE_3D})
_VOLUME_STAGE = pytest.mark.parametrize("volume_defect", [_THROUGH, _OVERLAP_3D],
                                        ids=["edge_through_face", "overlap_stage_3d"])


@_VOLUME_STAGE
def test_a_scoped_rescan_keeps_a_located_volume_stage_mesh_defect(monkeypatch, volume_defect):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    # Whole-shape probe sees it; the scoped (2D-only) probe that follows cannot.
    _probe(monkeypatch, (volume_defect,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    assert session.defects() == (volume_defect,)
    found = session._rescan(face_scan=False, within=_REGION)
    assert volume_defect in found
    assert volume_defect in session.defects()


@_VOLUME_STAGE
def test_only_a_whole_shape_probe_clears_a_volume_stage_mesh_defect(monkeypatch, volume_defect):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (volume_defect,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    session._rescan(face_scan=False, within=_REGION)
    assert session._rescan(face_scan=False) == ()
    assert session.defects() == ()


def test_a_scoped_rescan_still_clears_a_surface_stage_mesh_defect(monkeypatch):
    # The carry is for what the scoped probe CANNOT see; what it can see, it may clear.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_OVERLAP,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    assert session._rescan(face_scan=False, within=_REGION) == ()
    assert _OVERLAP not in session.defects()


def test_an_injected_scanners_scoped_rescan_keeps_a_located_volume_stage_mesh_defect(monkeypatch):
    # `_merged` carries it forward exactly as it carries an unlocated mesh finding.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    answers = iter([((_THROUGH,), True), ((), True)])
    session = RepairSession("shape", measurer=lambda s: MEASURE, scanner=lambda s: next(answers))
    session.defects()
    session._rescan(face_scan=False, within=_REGION)
    assert _THROUGH in session.defects()


@_VOLUME_STAGE
def test_a_package_cannot_claim_a_volume_stage_mesh_defect_cleared(monkeypatch, volume_defect):
    from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    # The sew really does clear the opening; the scoped probes really do report nothing.
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, (volume_defect,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    package = TargetPackage(
        region=_REGION, intent="claim the volume-stage mesh failure cleared",
        plan=({"op": _Sew(), "targets": _Sew().targets_for(opening),
               "defect_code": opening.code},),
        predicate=Predicate(cleared=frozenset({"face:f4"}), reclassified=frozenset(),
                            unchanged=frozenset(), max_area_delta_mm2=10.0,
                            max_volume_delta_mm3=10.0),
        budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=0, deviation_mm=0.0))
    verdict = session.try_package(package)
    assert verdict.verdicts and verdict.verdicts[0].kept, verdict.verdicts
    assert verdict.status == session_mod.REVERTED
    assert "still present" in verdict.reason and "face:f4" in verdict.reason
    assert session.shape == "shape"


@_VOLUME_STAGE
def test_a_scoped_operation_aimed_at_a_volume_stage_mesh_defect_is_not_kept(monkeypatch,
                                                                            volume_defect):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    # The scoped (2D-only) probe sees nothing; the WHOLE-shape probe the mesh cure pays for still
    # sees the volume failure, so nothing has proved it gone (ruling R11a).
    monkeypatch.setattr(session_mod, "_as_delivered", lambda shape: shape)
    probes = _probe(monkeypatch, (volume_defect,), (), (volume_defect,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    verdict = session.try_op(_Remesh(), _Remesh().targets_for(target), target.code,
                             tolerance_mm=0.01, within=_REGION)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert len(probes) == 3 and probes[2].get("within") is None


# REVEALED, NOT DAMAGE (P2 Task 3 fix round 1; applies to EVERY mesh operation). gmsh reports only
# its first failure, so clearing the targeted mesh failure lets the probe meet the next one.

_BOX_9 = {"centroid": [25.0, 5.0, 5.0], "bbox_min": [20.0, 0.0, 0.0],
          "bbox_max": [30.0, 10.0, 10.0]}
_NEXT = EntityDefect(code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
                     entity_type="face", entity_index=9, message="overlapping facets",
                     fingerprint="face:f9", location=dict(_BOX_9),
                     measurements={"mesh_kind": "overlapping_facets", "gmsh_surfaces": [9],
                                   "stage": mesh_probe.STAGE_3D})


class _Fragment:
    """An operation double answering the edge through a face (as fragment_bodies does)."""

    name = "fragment_bodies"
    answers = frozenset({DefectCode.mesh_edge_through_face})
    scope = 8

    def __init__(self, *, added=(), moved=None, rebuilt=()):
        self._added = tuple(added)
        self._moved = dict(moved or {})
        self._rebuilt = tuple(rebuilt)

    def targets_for(self, defect):
        return (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        from meshpipeline.cad.repair.identity import EntityMapping

        mapping = (EntityMapping(moved=self._moved, gone=frozenset(), basis="kernel")
                   if self._moved else EntityMapping.empty())
        return OpOutcome(shape="fragmented", added=self._added, mapping=mapping,
                         rebuilt=self._rebuilt)


def _revealing_session(monkeypatch, after):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (_THROUGH,), after)
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    return session, session.defects()[0]


def test_a_mesh_failure_revealed_behind_a_cleared_one_does_not_roll_back(monkeypatch):
    session, target = _revealing_session(monkeypatch, (_NEXT,))
    op = _Fragment()
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert verdict.kept, verdict.reason
    assert verdict.delta["revealed"] == [{"fingerprint": "face:f9",
                                          "code": "mesh_overlapping_facets"}]
    # NOT DROPPED: the loop must still answer or escalate it.
    assert _NEXT in session.defects()
    assert session.shape == "fragmented"


@pytest.mark.parametrize("op", [_Fragment(added=("face:f9",)),
                                _Fragment(moved={"face:f4": "face:f9"})],
                         ids=["on_added_geometry", "on_moved_geometry"])
def test_a_mesh_failure_on_geometry_the_operation_made_is_damage(monkeypatch, op):
    session, target = _revealing_session(monkeypatch, (_NEXT,))
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"
    assert session.shape == "shape"


def test_nothing_is_revealed_while_the_target_remains(monkeypatch):
    session, target = _revealing_session(monkeypatch, (_THROUGH, _NEXT))
    op = _Fragment()
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "target_remains"


def test_a_new_kernel_finding_is_never_excused_as_revealed(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    broken = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                          entity_type="edge", entity_index=7, message="free",
                          fingerprint="edge:new", location=dict(_BOX_9))
    _scan(monkeypatch, ((), True), ((broken,), True))
    _probe(monkeypatch, (_THROUGH,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    op = _Fragment()
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"


def test_a_kernel_operation_reveals_nothing(monkeypatch):
    # Only an operation aimed at a MESH defect earns the excuse.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, (), (_NEXT,))
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"


# THE OPERATION'S OWN GEOMETRY INCLUDES WHAT ITS REBUILD CHANGED (fix rounds 2-3, ruling P2-R7):
# a face of a rebuilt body is the op's own only when its fingerprint did not exist before the op.

_PERIODIC = EntityDefect(code=DefectCode.mesh_periodic_surface, severity=DefectSeverity.error,
                         entity_type="face", entity_index=4, message="periodic surface",
                         fingerprint="face:f4", location=dict(_BOX),
                         measurements={"mesh_kind": "periodic_surface", "gmsh_surfaces": [4],
                                       "stage": mesh_probe.STAGE_2D})


class _Split(_Fragment):
    """An operation double answering a periodic surface (as split_closed_faces does)."""

    name = "split_closed_faces"
    answers = frozenset({DefectCode.mesh_periodic_surface})
    scope = 1


def _pre_op(monkeypatch, *names):
    """The pre-op shape's face names, as `_face_names_of` would read them off the kernel."""
    calls: list = []
    monkeypatch.setattr(session_mod, "_face_names_of",
                        lambda shape: calls.append(shape) or frozenset(names))
    return calls


_BOTH = pytest.mark.parametrize("op, first", [
    (_Fragment(rebuilt=("face:f4", "face:f9")), _THROUGH),
    (_Split(rebuilt=("face:f4", "face:f9")), _PERIODIC)],
    ids=["fragment_bodies", "split_closed_faces"])


def _try(monkeypatch, op, first, after=(_NEXT,)):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    _probe(monkeypatch, (first,), after)
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    return session, session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)


@_BOTH
def test_a_mesh_failure_on_a_face_the_rebuild_renamed_is_damage(monkeypatch, op, first):
    calls = _pre_op(monkeypatch, "face:f4")          # face:f9 is a name the rebuild made
    session, verdict = _try(monkeypatch, op, first)
    assert not verdict.kept and verdict.reason == "new_defect"
    # A ROLLED-BACK VERDICT LISTS NO REVEALS: nothing it names reached the defect list.
    assert "revealed" not in verdict.delta
    assert session.shape == "shape"
    assert calls == ["shape"]                         # the PRE-op shape was the one named


@_BOTH
def test_a_mesh_failure_on_a_rebuilt_face_whose_name_survived_is_revealed(monkeypatch, op, first):
    _pre_op(monkeypatch, "face:f4", "face:f9")       # face:f9 crossed the rebuild unchanged
    session, verdict = _try(monkeypatch, op, first)
    assert verdict.kept, verdict.reason
    assert verdict.delta["revealed"] == [{"fingerprint": "face:f9",
                                          "code": "mesh_overlapping_facets"}]
    assert _NEXT in session.defects()


@pytest.mark.parametrize("op, first", [(_Fragment(rebuilt=("face:f4",)), _THROUGH),
                                       (_Split(rebuilt=("face:f4",)), _PERIODIC)],
                         ids=["fragment_bodies", "split_closed_faces"])
def test_a_mesh_failure_off_the_rebuilt_bodies_is_revealed_without_naming_faces(
        monkeypatch, op, first):
    calls = _pre_op(monkeypatch)
    _session, verdict = _try(monkeypatch, op, first)
    assert verdict.kept, verdict.reason
    assert verdict.delta["revealed"] == [{"fingerprint": "face:f9",
                                          "code": "mesh_overlapping_facets"}]
    assert calls == []                                # no candidate on a rebuilt face: no kernel


def test_a_rolled_back_verdict_lists_no_reveals_even_beside_damage(monkeypatch):
    # One finding off the op's geometry (would be revealed) and one on it (damage): rolled back,
    # and the verdict does not claim the first was revealed.
    damaged = EntityDefect(code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
                           entity_type="face", entity_index=5, message="overlap",
                           fingerprint="face:f5", location=dict(_BOX_9),
                           measurements={"mesh_kind": "overlapping_facets", "stage": "3d"})
    _pre_op(monkeypatch, "face:f4")                  # face:f5 is a name the rebuild made
    session, target = _revealing_session(monkeypatch, (_NEXT, damaged))
    op = _Fragment(rebuilt=("face:f5",))
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"
    assert "revealed" not in verdict.delta


class _KernelFixer(_Fragment):
    """A kernel operation (answers no mesh code) that nevertheless clears the mesh failure."""

    name = "fix_shape_global"
    answers = frozenset({DefectCode.invalid_brep})


def test_a_non_mesh_operation_aimed_at_a_mesh_defect_gets_no_amnesty(monkeypatch):
    # A directive (or MESH_OPS) may aim a kernel fixer at a mesh failure. It clears it, and the
    # probe meets the next failure - but the fixer declares nothing it rebuilt, so the amnesty
    # would be hollow for it: the new mesh finding is new damage, as it always was.
    session, target = _revealing_session(monkeypatch, (_NEXT,))
    op = _KernelFixer()
    verdict = session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)
    assert not verdict.kept and verdict.reason == "new_defect"
    assert "revealed" not in verdict.delta


# ONE ABSOLUTE DEADLINE (final review I2). A probe that times out spends its whole budget, so the
# fake below advances the clock by exactly the budget it was handed whenever it answers a timeout
# (and answers anything else at once).


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _spending_probe(monkeypatch, clock, answers):
    """Each call returns the next answer (the last repeats); a timeout spends its whole budget."""
    calls: list[dict] = []

    def fake(shape, **kw):
        calls.append({**kw, "at": clock.now})
        answer = answers[min(len(calls), len(answers)) - 1]
        if any(d.code is DefectCode.mesh_timeout for d in answer):
            clock.now += float(kw["budget_s"])
        return answer
    monkeypatch.setattr(mesh_probe, "mesh_defects", fake)
    return calls


def test_a_run_whose_probes_time_out_still_verifies_inside_its_deadline(monkeypatch):
    from meshpipeline.cad.repair import deviation
    from meshpipeline.cad.repair.loop import LoopBudget
    from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 60.0)
    monkeypatch.setattr(deviation, "measure_deviation",
                        lambda *a, **kw: deviation.DeviationResult(seconds=0.0))
    clock = _Clock()
    deadline = 115.0                       # CAD_REPAIR_TIMEOUT_S 120, less the 5 s margin
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    # The first probe answers at once; every later one runs out of time.
    probes = _spending_probe(monkeypatch, clock, ((), (_TIMEOUT,)))
    asked: list = []

    def author(region, mine, tried):
        asked.append(clock.now)
        if tried:
            return None
        opening = mine[0]
        return TargetPackage(
            region=region, intent="sew the opening",
            plan=({"op": _Sew(), "targets": _Sew().targets_for(opening),
                   "defect_code": opening.code},),
            predicate=Predicate(cleared=frozenset({"edge:a"}), reclassified=frozenset(),
                                unchanged=frozenset(), max_area_delta_mm2=10.0,
                                max_volume_delta_mm3=10.0),
            budget=PackageBudget(max_ops=1, timeout_s=100.0, model_calls=0, deviation_mm=0.0))

    run = region_loop.run_regions(
        "shape", author=author, tolerance_mm=0.01, clock=clock, deadline_s=deadline,
        budget=LoopBudget(max_ops=10, stall_limit=3, timeout_s=120.0),
        session=RepairSession("shape", measurer=lambda s: MEASURE, clock=clock))
    assert [p.status for p in run.packages] == [session_mod.COMMITTED]
    # Every probe but the last took at most HALF of what was left; the last - the final
    # whole-shape verification - took what remained LESS the slack, so the run ended
    # FINAL_PROBE_SLACK_S before its deadline, leaving room for the after-checks and STEP write.
    *walk, final = probes
    assert all(p["budget_s"] <= 0.5 * (deadline - p["at"]) + 1e-9 for p in walk)
    assert final["within"] is None and final["budget_s"] == pytest.approx(
        deadline - final["at"] - session_mod.FINAL_PROBE_SLACK_S)
    assert clock.now <= deadline - session_mod.FINAL_PROBE_SLACK_S + 1e-9
    # Authoring stopped with the final probe's reserve still in hand (one timeout + 10 s).
    assert len(asked) == 1 and deadline - final["at"] < 60.0 + 10.0


def test_a_first_probe_that_times_out_ends_the_walk_with_time_left(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 60.0)
    clock = _Clock(1000.0)
    deadline = 1115.0
    _scan(monkeypatch, ((_OPENING,), True))
    probes = _spending_probe(monkeypatch, clock, ((_TIMEOUT,),))

    def author(region, mine, tried):
        raise AssertionError("authored with less than the final probe's reserve left")

    from meshpipeline.cad.repair.loop import LoopBudget

    run = region_loop.run_regions(
        "shape", author=author, tolerance_mm=0.01, clock=clock, deadline_s=deadline,
        budget=LoopBudget(max_ops=10, stall_limit=3, timeout_s=120.0),
        session=RepairSession("shape", measurer=lambda s: MEASURE, clock=clock))
    assert probes[0]["budget_s"] == 57.5          # half of 115, not the whole run
    assert run.stopped_because != region_loop.RESOLVED
    assert deadline - clock.now == 57.5           # as much again left for everything after


def test_with_the_probe_off_the_walk_keeps_no_reserve(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "off")
    assert region_loop.final_reserve_s() == 0.0
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 60.0)
    assert region_loop.final_reserve_s() == 70.0


def test_the_default_probe_timeout_is_below_the_run_timeout():
    from meshpipeline.settings.inventory import default_of

    probe = float(default_of("CAD_REPAIR_MESH_PROBE_TIMEOUT_S"))
    assert probe == 60.0 and probe < float(default_of("CAD_REPAIR_TIMEOUT_S"))


def test_the_author_is_shown_the_current_shape_after_a_commit(monkeypatch):
    # Final review M1: the region loop tells the author the session's shape before every call.
    from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "off")
    other = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                         entity_type="edge", entity_index=2, message="free",
                         fingerprint="edge:b", location=dict(_BOX))
    answers = iter([((_OPENING, other), True)])
    session = RepairSession("upload", measurer=lambda s: MEASURE,
                            scanner=lambda s: next(answers, ((other,), True)))
    shown: list = []

    def author(region, mine, tried):
        if shown[-1] != "upload":
            return None
        return TargetPackage(
            region=region, intent="sew edge a",
            plan=({"op": _Sew(), "targets": _Sew().targets_for(_OPENING),
                   "defect_code": _OPENING.code},),
            predicate=Predicate(cleared=frozenset({"edge:a"}), reclassified=frozenset(),
                                unchanged=frozenset(), max_area_delta_mm2=10.0,
                                max_volume_delta_mm3=10.0),
            budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=0, deviation_mm=0.0))
    author.observe_shape = shown.append
    run = region_loop.run_regions("upload", author=author, tolerance_mm=0.01, session=session)
    assert [p.status for p in run.packages] == [session_mod.COMMITTED]
    assert shown == ["upload", "sewn"]



# --- wave 4 (abc 0060_00608290): a clash defect never displaces the kernel's own finding ----------

_SMALL_FACE = EntityDefect(code=DefectCode.small_face, severity=DefectSeverity.warning,
                           entity_type="face", entity_index=4, message="small",
                           fingerprint="face:f4", location=dict(_BOX),
                           measurements={"area": 0.19, "area_fraction": 6e-7})
_CLASH_MEASURED = {"mesh_kind": "edge_through_face", "stage": "3d",
                   "located_by": mesh_probe.LOCATED_BY_CLASH, "clash_pair": [4, 7],
                   "clash_gap_mm": 1.46, "clash_kind": "gap", "clash_mesh_kind": "crossing"}


def _clash(**measured) -> EntityDefect:
    return EntityDefect(code=DefectCode.mesh_edge_through_face, severity=DefectSeverity.error,
                        entity_type="face", entity_index=4,
                        message="a segment and a facet intersect", fingerprint="face:f4",
                        location=dict(_BOX), measurements={**_CLASH_MEASURED, **measured})


def test_a_clash_on_a_face_is_added_to_its_kernel_defect_never_instead(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    clash = _clash()
    _scan(monkeypatch, ((_SMALL_FACE,), True))
    _probe(monkeypatch, (clash,))
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 300.0)

    # Whole shape, the first scan and a rescan: both findings on the one face, kernel first.
    assert session.defects() == (_SMALL_FACE, clash)
    assert session._rescan(face_scan=False) == (_SMALL_FACE, clash)
    # Scoped (a package's proof): the same face, the same two findings.
    found, _ = session._region_scan(_REGION)
    assert _SMALL_FACE in found and clash in found


def test_the_rules_author_keeps_the_kernel_cure_beside_a_sub_element_gap(monkeypatch):
    # The deterministic arm of wave 4 aimed fragment_bodies at the clash instead of
    # fix_small_faces at the small face. A clash whose faces are measured APART is the mesher's
    # chord: it does not jump a kernel warning's cure.
    import meshpipeline.cad.repair.rules_author as rules_author_module
    from meshpipeline.cad.repair.operations import catalogue
    from meshpipeline.cad.repair.rules_author import mesh_ops, rules_author

    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(fragment_bodies=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(fragment_bodies=True)})
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    gap = _clash()
    _scan(monkeypatch, ((_SMALL_FACE,), True))
    _probe(monkeypatch, (gap,))
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 300.0)
    region = Region(index=0, fingerprints=frozenset({"face:f4"}),
                    bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                    face_indices=frozenset({4}))

    package = rules_author(None, original=MEASURE)(region, session.defects(), ())
    assert package is not None
    assert package.plan[0]["op"].name == "fix_small_faces"
    assert package.plan[0]["defect_code"] is DefectCode.small_face

    # A clash whose faces really MEET still goes first, as P2-R8 says.
    meeting = _clash(clash_gap_mm=0.0, clash_kind="crossing")
    package = rules_author(None, original=MEASURE)(region, (_SMALL_FACE, meeting), ())
    assert package is not None and package.plan[0]["op"].name == "fragment_bodies"

    # And with no kernel cure at all, the gap is still aimed at, exactly as before.
    package = rules_author(None, original=MEASURE)(region, (gap,), ())
    assert package is not None and package.plan[0]["op"].name == "fragment_bodies"


# --- review and the keep rule read ONE measurement of ONE artefact ----------------------------
def test_the_keep_rule_is_the_delivered_probe_read_as_a_boolean(monkeypatch):
    """`_meshes_now` and the reviewer's `mesh_after` must never judge different objects: the keep
    rule is now the same `_delivered_defects` call read as a yes/no. A delivered probe that could
    not be taken is False, because a cure may not rest on a reading nobody took."""
    session = RepairSession("shape", measurer=lambda s: MEASURE,
                            remaining_s=lambda: 300.0)
    calls: list = []

    def answer(found):
        def _delivered(*, cap_s=None):
            calls.append(cap_s)
            return found
        return _delivered

    monkeypatch.setattr(session, "_delivered_defects", answer(()))
    assert session._meshes_now() is True

    monkeypatch.setattr(session, "_delivered_defects", answer((object(),)))
    assert session._meshes_now() is False

    monkeypatch.setattr(session, "_delivered_defects", answer(None))
    assert session._meshes_now() is False, "an unmeasured delivered probe is not a pass"
    assert len(calls) == 3
