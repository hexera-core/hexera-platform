"""The ladder runner: one rung of oracle's tier policy, run and reported.

Hermetic. Every probe this composes is faked, so the rules - the budget caps, the
region requirement, the no-answer verdict and the flag-off tier 2b - are exercised without
gmsh, fTetWild or the kernel.
"""
from __future__ import annotations

import sys
import types

import pytest

from meshpipeline.cad.repair import ladder, oracle
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.regions import Region

BOX = Measure(valid=True, solids=1, shells=1, faces=6, edges=12, vertices=8,
              diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=600.0,
              volume_mm3=1000.0)
REGION = Region(index=0, fingerprints=frozenset({"fp0"}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(1.0, 1.0, 1.0), face_indices=frozenset({1}))


def _defect(code: DefectCode, index: int = 1) -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                        entity_index=index, message="gmsh refused", measurements={},
                        location={}, fingerprint=f"fp{index}")


def _probe(calls: list, defects: tuple = ()):
    def mesh_defects(shape, *, diagonal_mm, budget_s, within=None):
        calls.append({"diagonal_mm": diagonal_mm, "budget_s": budget_s, "within": within})
        return defects
    return mesh_defects


def test_ladder_no_answer_is_none(monkeypatch):
    """Only no-answer codes mean the mesher was never heard from: never a False verdict."""
    calls: list = []
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects",
                        _probe(calls, (_defect(DefectCode.mesh_timeout),)))
    out = ladder.run_tier(object(), "2", diagonal_mm=100.0, within=None, budget_s=10.0)
    assert out.passed is None
    assert out.tier == "2"
    assert len(out.defects) == 1
    assert len(calls) == 1


def test_a_real_defect_is_a_false_verdict(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects",
                        _probe(calls, (_defect(DefectCode.mesh_overlapping_facets),
                                       _defect(DefectCode.mesh_timeout, 2))))
    out = ladder.run_tier(object(), "2", diagonal_mm=100.0, within=None, budget_s=10.0)
    assert out.passed is False


def test_an_empty_probe_passes(monkeypatch):
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", _probe([], ()))
    out = ladder.run_tier(object(), "2", diagonal_mm=100.0, within=None, budget_s=10.0)
    assert out.passed is True
    assert out.defects == ()


def test_tier1_needs_a_region(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", _probe(calls, ()))
    out = ladder.run_tier(object(), "1", diagonal_mm=100.0, within=None, budget_s=10.0)
    assert out.passed is None
    assert out.detail["error"] == "tier 1 needs a region"
    assert calls == []


def test_tier1_is_scoped_to_the_region(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", _probe(calls, ()))
    out = ladder.run_tier(object(), "1", diagonal_mm=100.0, within=REGION, budget_s=7.0)
    assert out.passed is True
    assert calls[0]["within"] is REGION
    assert calls[0]["budget_s"] == 7.0  # tier 1 carries no hard cap


def test_tier2_budget_is_hard_capped(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", _probe(calls, ()))
    ladder.run_tier(object(), "2", diagonal_mm=100.0, within=REGION, budget_s=999.0)
    assert calls[0]["budget_s"] == oracle.TIER_2.hard_cap_s == 60.0
    # Tier 2 is the WHOLE part, whatever region the caller held.
    assert calls[0]["within"] is None


def test_tier2_budget_under_the_cap_is_the_callers(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", _probe(calls, ()))
    ladder.run_tier(object(), "2", diagonal_mm=100.0, within=None, budget_s=4.5)
    assert calls[0]["budget_s"] == 4.5


def test_tier2_detail_carries_the_element_cap(monkeypatch):
    """The element cap is a QUALITY signal read after the run; the defect-level probe does not
    report an element count, so `over_element_cap` is None - never a confident 0."""
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", _probe([], ()))
    out = ladder.run_tier(object(), "2", diagonal_mm=100.0, within=None, budget_s=4.5)
    assert out.detail["element_cap"] == oracle.TIER_2.element_cap
    assert out.detail["over_element_cap"] is None


def test_tier0_reads_the_kernel_measure(monkeypatch):
    monkeypatch.setattr(ladder.caps, "measure", lambda shape: BOX)
    out = ladder.run_tier(object(), "0", diagonal_mm=100.0, within=None, budget_s=0.0)
    assert out.passed is True
    assert out.detail["faces"] == 6
    assert out.detail["solids"] == 1
    assert out.defects == ()


def test_tier0_invalid_is_false(monkeypatch):
    bad = Measure(valid=False, solids=1, shells=1, faces=6, edges=12, vertices=8,
                  diagonal_mm=100.0, max_tolerance_mm=1e-7, surface_area_mm2=600.0,
                  volume_mm3=1000.0)
    monkeypatch.setattr(ladder.caps, "measure", lambda shape: bad)
    assert ladder.run_tier(object(), "0", diagonal_mm=1.0, within=None,
                           budget_s=0.0).passed is False


def test_tier2b_off_spawns_nothing(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(ladder.rcfg, "CAD_REPAIR_TOLERANT_PROBE", "off", raising=False)
    monkeypatch.setattr(ladder, "_write_brep", lambda shape, path: spawned.append(path))
    out = ladder.run_tier(object(), "2b", diagonal_mm=100.0, within=None, budget_s=99.0)
    assert out.passed is None
    assert out.detail == {"verdict": "not_asked"}
    assert spawned == []


def _fake_tolerant(verdict: str, calls: list):
    module = types.ModuleType("meshpipeline.cad.repair.tolerant_probe")
    module.EATS = "eats"
    module.REFUSES = "refuses"

    def tolerant_probe(path, *, diagonal_mm, budget_s):
        calls.append({"path": str(path), "diagonal_mm": diagonal_mm, "budget_s": budget_s})
        return verdict
    module.tolerant_probe = tolerant_probe
    return module


@pytest.mark.parametrize(("verdict", "passed"),
                         [("eats", True), ("refuses", False), ("unsure", None)])
def test_tier2b_verdicts(monkeypatch, verdict, passed):
    calls: list = []
    monkeypatch.setitem(sys.modules, "meshpipeline.cad.repair.tolerant_probe",
                        _fake_tolerant(verdict, calls))
    monkeypatch.setattr(ladder.rcfg, "CAD_REPAIR_TOLERANT_PROBE", "live", raising=False)
    monkeypatch.setattr(ladder.rcfg, "CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S", 30.0, raising=False)
    monkeypatch.setattr(ladder, "_write_brep", lambda shape, path: None)
    out = ladder.run_tier(object(), "2b", diagonal_mm=100.0, within=None, budget_s=999.0)
    assert out.passed is passed
    assert out.detail["verdict"] == verdict
    assert calls[0]["budget_s"] == 30.0  # capped by the tolerant probe's own timeout


def test_tier2b_without_the_probe_installed_is_unavailable(monkeypatch):
    monkeypatch.setattr(ladder.rcfg, "CAD_REPAIR_TOLERANT_PROBE", "live", raising=False)
    monkeypatch.setitem(sys.modules, "meshpipeline.cad.repair.tolerant_probe", None)
    out = ladder.run_tier(object(), "2b", diagonal_mm=100.0, within=None, budget_s=5.0)
    assert out.passed is None
    assert out.detail["verdict"] == "unavailable"


def test_run_tier_never_raises(monkeypatch):
    def boom(shape, *, diagonal_mm, budget_s, within=None):
        raise RuntimeError("the probe exploded")
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", boom)
    out = ladder.run_tier(object(), "2", diagonal_mm=100.0, within=None, budget_s=5.0)
    assert out.passed is None
    assert out.detail["error"] == "RuntimeError"
    assert out.seconds >= 0.0


def test_unknown_tier_is_an_answer_not_a_raise():
    out = ladder.run_tier(object(), "t2", diagonal_mm=100.0, within=None, budget_s=5.0)
    assert out.passed is None
    assert out.detail == {"error": "unknown tier"}
    assert out.tier == "t2"


def test_tier3_is_unreachable_from_the_loop():
    """The product mesher is not the loop's to start: asking for it is refused, never run."""
    out = ladder.run_tier(object(), "3", diagonal_mm=100.0, within=None, budget_s=500.0)
    assert out.passed is None
    assert "not in the loop" in out.detail["error"]


def test_the_ladder_restates_no_tier_table():
    """Every rung the runner serves comes from `oracle.TIERS`, so a policy change is one edit."""
    assert set(ladder.RUNGS) == {t.name for t in oracle.TIERS if t.in_loop}
    assert "3" not in ladder.RUNGS
