# Responsibility: Pin the investigator's posture, its per-part compute budgets and its clock.
# Boundaries: pure. No gmsh, no OCP, no model call, no settings mutation beyond monkeypatch.
"""investigator_budget: a part's compute tier -> what an investigation may spend, and the clock
that keeps one region from eating the run.

Every budget assertion here is a TABLE across several values on purpose: a one-sided assertion
passes a mutated constant, which has already happened three times on this plan.
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from meshpipeline.cad.repair import grind, model_author
from meshpipeline.cad.repair import investigator_budget as ib
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.grind import CHEAP, HEAVY, STANDARD, UNKNOWN
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.settings import cad_repair as rcfg

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src" / "meshpipeline"


def _defect(code: DefectCode, kind: str, stage: str,
            severity: DefectSeverity = DefectSeverity.error) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type="shape", entity_index=0,
                        message="m", measurements={"mesh_kind": kind, "stage": stage})


class _Clock:
    """A clock that reads exactly what the test says it reads."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------------------------
# The posture: default-off, fail-closed.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("live", "live"), ("shadow", "shadow"), ("off", "off"),
    ("Live ", "off"), ("Live", "off"), ("LIVE", "off"), (" live", "off"),
    ("on", "off"), ("", "off"), ("true", "off"), ("Shadow", "off"), ("1", "off"),
])
def test_typo_reads_off(monkeypatch, caplog, raw, expected):
    monkeypatch.setenv("CAD_REPAIR_INVESTIGATOR", raw)
    with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
        caplog.clear()
        assert rcfg._investigator_posture() == expected
    warned = any("CAD_REPAIR_INVESTIGATOR" in r.getMessage() for r in caplog.records)
    assert warned is (raw not in ("live", "shadow", "off"))


def test_the_posture_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("CAD_REPAIR_INVESTIGATOR", raising=False)
    assert rcfg._investigator_posture() == "off"
    assert rcfg.CAD_REPAIR_INVESTIGATOR in ("live", "shadow", "off")


@pytest.mark.parametrize("raw, expected", [
    ("live", "live"), ("off", "off"), ("Live", "off"), ("on", "off"), ("", "off"), ("true", "off"),
])
def test_images_posture_fails_closed(monkeypatch, raw, expected):
    """Task 14's gate; parsed here so the inventory lists it once. It stays off everywhere."""
    monkeypatch.setenv("CAD_REPAIR_INVESTIGATOR_IMAGES", raw)
    assert rcfg._investigator_images_posture() == expected


def test_the_defaults_are_the_documented_numbers():
    assert rcfg.CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S == 20.0
    assert rcfg.CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX == 48
    assert rcfg.CAD_REPAIR_INVESTIGATOR_REGION_SHARE == 0.5
    assert rcfg.CAD_REPAIR_INVESTIGATOR_IMAGES == "off"


def test_investigator_without_region_loop_warns(caplog):
    """The posture does nothing without the region-scoped driver (the _warn_inert_author rule)."""
    for investigator, region_loop, want in (("live", "off", True), ("shadow", "off", True),
                                            ("live", "live", False), ("off", "off", False),
                                            ("off", "live", False)):
        with caplog.at_level(logging.WARNING, logger=rcfg.__name__):
            caplog.clear()
            rcfg._warn_inert_investigator(investigator, region_loop)
        said = any("CAD_REPAIR_INVESTIGATOR" in r.getMessage() for r in caplog.records)
        assert said is want, (investigator, region_loop)


def test_the_posture_is_a_registered_setting():
    from meshpipeline.settings.inventory import all_vars
    by_name = {v.name: v for v in all_vars()}
    for name, default in (("CAD_REPAIR_INVESTIGATOR", "off"),
                          ("CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S", "20.0"),
                          ("CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX", "48"),
                          ("CAD_REPAIR_INVESTIGATOR_REGION_SHARE", "0.5"),
                          ("CAD_REPAIR_INVESTIGATOR_IMAGES", "off")):
        assert name in by_name, name
        assert by_name[name].default == default, name


def test_off_imports_nothing():
    """With the posture off, importing the repair entry point imports no investigator module."""
    code = (
        "import sys\n"
        "import meshpipeline.cad.repair.conservative  # noqa: F401\n"
        "print([m for m in sys.modules if 'investigator' in m])\n"
    )
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_ROOT / "src"),
           "CAD_REPAIR_INVESTIGATOR": "off"}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip() == "[]", p.stdout


# ---------------------------------------------------------------------------------------------
# The budgets: per-part compute tiers, fail-closed on anything unrecognised.
# ---------------------------------------------------------------------------------------------


def test_the_budget_table_is_the_plan_s_table():
    assert set(ib.BUDGETS) == {CHEAP, STANDARD, HEAVY}
    assert [(b.tier, b.tool_calls, b.model_calls, b.wall_s)
            for b in (ib.BUDGETS[CHEAP], ib.BUDGETS[STANDARD], ib.BUDGETS[HEAVY])] == [
        (CHEAP, 6, 8, 60.0), (STANDARD, 12, 16, 150.0), (HEAVY, 20, 26, 240.0)]


def test_the_budgets_rise_with_the_tier():
    cheap, standard, heavy = (ib.BUDGETS[t] for t in (CHEAP, STANDARD, HEAVY))
    for field in ("tool_calls", "model_calls", "wall_s"):
        got = [getattr(cheap, field), getattr(standard, field), getattr(heavy, field)]
        assert got == sorted(got) and len(set(got)) == 3, field


@pytest.mark.parametrize("tier", [UNKNOWN, "x", "", "light", "CHEAP", "Cheap", " cheap", "0",
                                  None, 3, object()])
def test_unknown_tier_is_cheap(tier):
    """FAIL CLOSED, per Phase 2's header: anything unrecognised gets the SMALLEST budget."""
    assert ib.budget_for(tier) == ib.BUDGETS[CHEAP]


@pytest.mark.parametrize("tier", [CHEAP, STANDARD, HEAVY])
def test_a_known_tier_gets_its_own_budget(tier):
    assert ib.budget_for(tier) is ib.BUDGETS[tier]


def test_budget_for_a_raising_tier_is_cheap(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no")

    monkeypatch.setattr(grind, "tier_of", boom)
    assert ib.budget_for_part((), solids=1, faces=139) == ib.BUDGETS[CHEAP]


@pytest.mark.parametrize("defects, faces, tier", [
    # the worst mesh-coded defect decides, and the merged grind.tier_of is the one authority
    ([_defect(DefectCode.mesh_overlapping_facets, "overlapping_facets", "3d")], 139, STANDARD),
    ([_defect(DefectCode.mesh_edge_through_face, "edge_through_face", "3d")], 173, STANDARD),
    ([_defect(DefectCode.mesh_open_loop, "open_loop", "2d")], 138, CHEAP),
    ([], 139, CHEAP),                                       # no mesh defect: a meshed part
    ([_defect(DefectCode.mesh_timeout, "timeout", "")], 139, HEAVY),
    ([_defect(DefectCode.mesh_overlapping_facets, "overlapping_facets", "3d")], 4000, HEAVY),
    ([_defect(DefectCode.mesh_overlapping_facets, "overlapping_facets", "3d")], None, CHEAP),
])
def test_tier_of_reads_the_worst_mesh_defect(defects, faces, tier):
    assert ib.budget_for_part(defects, solids=1, faces=faces) == ib.BUDGETS[tier]
    # a missing face count is UNKNOWN to the predictor, and UNKNOWN is cheap here
    if faces is None:
        assert grind.tier_of(defects, solids=1, faces=faces) == UNKNOWN


# ---------------------------------------------------------------------------------------------
# The proof reserve: a concluded package always has time for its own proof and its review.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("probe, review, remaining, expected", [
    ("off", "off", 300.0, 0.0),
    ("live", "off", 300.0, 60.0),       # min(timeout 60, half of 300)
    ("live", "off", 80.0, 40.0),        # half of what is left
    ("live", "live", 300.0, 80.0),      # plus the reviewer's 20
    ("off", "live", 300.0, 20.0),
    ("live", "live", 0.0, 20.0),
    ("live", "off", 0.0, 0.0),
])
def test_the_proof_reserve_scopes_the_probe_and_the_review(monkeypatch, probe, review, remaining,
                                                           expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", probe)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", review)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 60.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S", 20.0)
    assert ib.proof_reserve_s(remaining) == pytest.approx(expected)


# ---------------------------------------------------------------------------------------------
# The clock.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("budget_wall, share, expected", [
    (60.0, 0.5, 60.0),      # the budget is the lesser
    (150.0, 0.5, 120.0),    # the share is the lesser: 0.5 * (300 - 60)
    (240.0, 0.5, 120.0),
    (240.0, 0.25, 60.0),
    (240.0, 1.0, 240.0),    # the whole of what is left above the reserve, capped by the budget
    (600.0, 1.0, 240.0),
])
def test_region_share_leaves_time_for_others(budget_wall, share, expected):
    clock = _Clock(1000.0)
    sut = ib.InvestigationClock(1300.0, reserve_s=60.0, proof_reserve_s=0.0, share=share,
                                clock=clock)
    wall = sut.region_wall(ib.InvestigationBudget(STANDARD, 1, 1, budget_wall))
    assert wall == pytest.approx(expected)
    assert wall <= budget_wall and wall <= share * (300.0 - 60.0) + 1e-9


@pytest.mark.parametrize("now, expected", [
    (1000.0, 120.0), (1200.0, 20.0), (1240.0, 0.0), (1300.0, 0.0), (1400.0, 0.0),
])
def test_the_region_wall_is_never_negative(now, expected):
    sut = ib.InvestigationClock(1300.0, reserve_s=60.0, proof_reserve_s=0.0, share=0.5,
                                clock=_Clock(now))
    assert sut.region_wall(ib.BUDGETS[HEAVY]) == pytest.approx(expected)
    assert sut.region_wall(ib.BUDGETS[HEAVY]) >= 0.0


@pytest.mark.parametrize("budget", ["cheap", "standard", "heavy"])
def test_no_deadline_uses_budget_wall(budget):
    sut = ib.InvestigationClock(None, reserve_s=60.0, proof_reserve_s=10.0, share=0.5,
                                clock=_Clock(0.0))
    # The budget's wall is looking time; the proof reserve (10.0) is held on top of it.
    assert sut.region_wall(ib.BUDGETS[budget]) == ib.BUDGETS[budget].wall_s + 10.0


@pytest.mark.parametrize("now, wall, expected", [
    (1000.0, 100.0, 100.0),     # the wall is the lesser
    (1050.0, 100.0, 50.0),      # elapsed 50 of the wall
    (1000.0, 500.0, 240.0),     # the run above the reserve is the lesser
    (1280.0, 500.0, 0.0),       # past the reserve
    (1400.0, 500.0, 0.0),
])
def test_left_is_the_lesser_of_the_wall_and_the_run(now, wall, expected):
    clock = _Clock(now)
    sut = ib.InvestigationClock(1300.0, reserve_s=60.0, proof_reserve_s=0.0, share=0.5,
                                clock=clock)
    assert sut.left(1000.0, wall) == pytest.approx(expected)


@pytest.mark.parametrize("now, expected", [
    (1000.0, 240.0), (1100.0, 140.0), (1240.0, 0.0), (1400.0, 0.0),
])
def test_left_without_a_wall_is_the_run_above_the_reserve(now, expected):
    sut = ib.InvestigationClock(1300.0, reserve_s=60.0, proof_reserve_s=0.0, share=0.5,
                                clock=_Clock(now))
    assert sut.left(1000.0) == pytest.approx(expected)


@pytest.mark.parametrize("now, must", [
    (1000.0, False),    # 100 s of the wall left, 240 of the run; reserve 30
    (1060.0, False),    # 40 left of the wall
    (1070.0, False),    # exactly 30 left: the reserve still REMAINS
    (1071.0, True),     # under 30: conclude now, while the proof time is still there
    (1099.0, True),
    (1100.0, True),
])
def test_conclude_forced_before_proof_reserve(now, must):
    clock = _Clock(now)
    sut = ib.InvestigationClock(1300.0, reserve_s=60.0, proof_reserve_s=30.0, share=0.5,
                                clock=clock)
    assert sut.must_conclude(1000.0, 100.0) is must
    # and the reserve genuinely remains at the moment it flips
    if must and now <= 1099.0:
        assert sut.left(1000.0) > 0.0


@pytest.mark.parametrize("now, must", [
    (1000.0, False), (1200.0, False), (1211.0, True), (1280.0, True),
])
def test_the_run_forces_conclusion_too(now, must):
    """The run's clock is the other half of the rule: a generous wall cannot outlive the run."""
    sut = ib.InvestigationClock(1300.0, reserve_s=60.0, proof_reserve_s=30.0, share=0.5,
                                clock=_Clock(now))
    assert sut.must_conclude(1000.0, 10_000.0) is must


def test_must_conclude_without_a_deadline_still_honours_the_wall():
    clock = _Clock(1000.0)
    sut = ib.InvestigationClock(None, reserve_s=60.0, proof_reserve_s=30.0, share=0.5,
                                clock=clock)
    assert sut.must_conclude(1000.0, 100.0) is False
    clock.now = 1080.0
    assert sut.must_conclude(1000.0, 100.0) is True


@pytest.mark.parametrize("delta, fits", [
    (-1.0, False),      # left == wall + grace - 1: the whole wall does NOT fit
    (-0.5, False),
    (0.0, True),        # exactly wall + grace
    (1.0, True),
    (100.0, True),
])
def test_shadow_fit_needs_whole_wall(delta, fits):
    """A shadow investigation must fit entirely above the reserve: `_bounded_in_thread` abandons
    its worker at timeout + model_author._GRACE_S, and an abandoned thread still burns the run."""
    wall = 100.0
    left = wall + model_author._GRACE_S + delta
    # deadline chosen so that `left` is exactly what is above the reserve
    sut = ib.InvestigationClock(1000.0 + left + 60.0, reserve_s=60.0, proof_reserve_s=0.0,
                                share=0.5, clock=_Clock(1000.0))
    assert sut.fits_shadow(wall) is fits


def test_shadow_fit_reads_the_grace_live(monkeypatch):
    """The grace is model_author's, read at call time - not a copy of the number."""
    sut = ib.InvestigationClock(1200.0, reserve_s=0.0, proof_reserve_s=0.0, share=0.5,
                                clock=_Clock(1000.0))
    monkeypatch.setattr(model_author, "_GRACE_S", 0.0)
    assert sut.fits_shadow(200.0) is True
    monkeypatch.setattr(model_author, "_GRACE_S", 50.0)
    assert sut.fits_shadow(200.0) is False


def test_without_a_deadline_a_shadow_investigation_always_fits():
    sut = ib.InvestigationClock(None, reserve_s=60.0, proof_reserve_s=0.0, share=0.5,
                                clock=_Clock(0.0))
    assert sut.fits_shadow(240.0) is True


# ---------------------------------------------------------------------------------------------
# Invariants.
# ---------------------------------------------------------------------------------------------


def test_the_module_reaches_no_big_machine():
    """The Phase 2 invariant, pinned here too; V3_MODULES carries the general sweep."""
    banned = ("meshpipeline.contracts.mesh_machine", "meshpipeline.contracts.mesh_execution",
              "meshpipeline.adapters", "meshpipeline.pipeline", "meshpipeline.engines.gmsh",
              "meshpipeline.engines.fea_driver", "meshpipeline.engines.fea_ladder",
              "meshpipeline.engines.mesher_stage", "meshpipeline.agents")
    tree = ast.parse((_SRC / "cad/repair/investigator_budget.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    assert [n for n in names if any(n == b or n.startswith(b + ".") for b in banned)] == []


def test_the_module_is_listed_in_v3_modules():
    """Every v3 `cad/repair` module joins the invariant sweep's list (the plan's global rule)."""
    invariants = Path(__file__).with_name("test_repair_oracle_invariants.py")
    tree = ast.parse(invariants.read_text(encoding="utf-8"))
    listed: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "V3_MODULES" for t in node.targets)):
            listed = [e.value for e in node.value.elts  # type: ignore[attr-defined]
                      if isinstance(e, ast.Constant)]
    assert "cad/repair/investigator_budget.py" in listed, listed


def test_the_live_posture_buys_more_turns_and_the_same_wall(monkeypatch):
    """A policy that asks for many attempts pays for the calls, not for the clock: the wall is
    what a region's successors and the proof reserve are measured against."""
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "live", raising=False)
    for tier, base in ib.BUDGETS.items():
        live = ib.budget_for(tier)
        assert live.tool_calls == base.tool_calls + ib.CLONE_FREEDOM_TURNS
        assert live.model_calls == base.model_calls + ib.CLONE_FREEDOM_TURNS
        assert live.wall_s == base.wall_s

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "off", raising=False)
    for tier, base in ib.BUDGETS.items():
        assert ib.budget_for(tier) == base
