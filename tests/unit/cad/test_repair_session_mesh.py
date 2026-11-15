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
    assert calls[0] == {"face_scan_s": 0.0}


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


def test_live_appends_after_localize(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    calls = _scan(monkeypatch, ((_OPENING,), True))
    probes = _probe(monkeypatch, (_OVERLAP,))
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 300.0)
    assert session.defects() == (_OPENING, _OVERLAP)
    # localize is called exactly as it always was: mesh defects are never passed INTO it.
    assert calls == [{"face_scan_s": 0.0}]
    assert probes == [{"diagonal_mm": 200.0, "budget_s": 120.0, "within": None}]


def test_the_budget_is_what_remains_when_less_than_the_timeout(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((), True))
    probes = _probe(monkeypatch, ())
    RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: 30.0).defects()
    assert probes[0]["budget_s"] == 30.0


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
    assert calls[-1] == {"face_scan_s": 0.0, "within": _REGION, "scope_diagonal_mm": 200.0}
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
    ((), (_NOVOL,)),                      # an unlocated whole-part verdict appears
    ((_TIMEOUT,), (_OVERLAP,)),           # it finished after timing out before: not news
    ((_OVERLAP,), (_TIMEOUT,)),           # carried forward, plus the timeout: not news
])
def test_mesh_probe_noise_does_not_roll_back_a_good_operation(monkeypatch, before, after):
    # Finding 2, both directions.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True), ((), True))
    _probe(monkeypatch, before, after)
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    opening = session.defects()[0]
    verdict = session.try_op(_Sew(), _Sew().targets_for(opening), opening.code, tolerance_mm=0.01)
    assert verdict.kept, verdict.reason


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
    _probe(monkeypatch, (volume_defect,), ())
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    verdict = session.try_op(_Remesh(), _Remesh().targets_for(target), target.code,
                             tolerance_mm=0.01, within=_REGION)
    assert not verdict.kept and verdict.reason == "target_remains"


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

    def __init__(self, *, added=(), moved=None):
        self._added = tuple(added)
        self._moved = dict(moved or {})

    def targets_for(self, defect):
        return (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        from meshpipeline.cad.repair.identity import EntityMapping

        mapping = (EntityMapping(moved=self._moved, gone=frozenset(), basis="kernel")
                   if self._moved else EntityMapping.empty())
        return OpOutcome(shape="fragmented", added=self._added, mapping=mapping)


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
