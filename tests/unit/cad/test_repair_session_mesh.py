# Responsibility: Verify the repair session carries gmsh's mesh defects only when CAD_REPAIR_MESH_PROBE is exactly live - appended after localize's findings on every scan path (whole, per-operation re-inspection, scoped package proof), never on top of an injected scanner, and never at the cost of the scan's completeness.
from __future__ import annotations

import importlib

import pytest

import meshpipeline.cad.repair.mesh_probe as mesh_probe
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
    try:
        assert importlib.reload(rcfg).CAD_REPAIR_MESH_PROBE == expected
    finally:
        monkeypatch.delenv("CAD_REPAIR_MESH_PROBE")
        importlib.reload(rcfg)


def test_unset_reads_off_and_timeout_defaults_to_120(monkeypatch):
    monkeypatch.delenv("CAD_REPAIR_MESH_PROBE", raising=False)
    monkeypatch.delenv("CAD_REPAIR_MESH_PROBE_TIMEOUT_S", raising=False)
    reloaded = importlib.reload(rcfg)
    assert reloaded.CAD_REPAIR_MESH_PROBE == "off"
    assert reloaded.CAD_REPAIR_MESH_PROBE_TIMEOUT_S == 120.0


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
def test_no_time_left_skips_the_probe(monkeypatch, remaining):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    _scan(monkeypatch, ((_OPENING,), True))

    def boom(*a, **kw):
        raise AssertionError("probed with no budget")
    monkeypatch.setattr(mesh_probe, "mesh_defects", boom)
    session = RepairSession("shape", measurer=lambda s: MEASURE, remaining_s=lambda: remaining)
    assert session.defects() == (_OPENING,)


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
