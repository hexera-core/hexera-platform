# Responsibility: Prove the goal-profile contract - what a repair run is finishing FOR - and the
#                 standalone finishing verdict it produces.
# Boundaries: no OCP and no gmsh. The tier-2 probe is injected (`prober`), the clock is injected,
#             and `Measure`, the defects and the package verdicts are stubs.
from __future__ import annotations

from dataclasses import dataclass

import pytest

from meshpipeline.cad.repair import goal as goal_mod
from meshpipeline.cad.repair import mesh_probe
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity, RepairGoal

SHAPE = object()


@dataclass(frozen=True)
class FakeMeasure:
    """Only what the verdict reads off a `caps.Measure`."""

    valid: bool = True
    diagonal_mm: float = 100.0


@dataclass(frozen=True)
class FakeDefect:
    code: DefectCode
    severity: DefectSeverity


@dataclass(frozen=True)
class FakePackage:
    """Only what the verdict reads off a `session.PackageVerdict`."""

    region_index: int
    status: str
    reviewed: str | None = None
    review_reason: str | None = None
    deviation_mm: float | None = None

    @property
    def committed(self) -> bool:
        return self.status == "committed"


def _exploding_prober(**kwargs):
    raise AssertionError(f"the prober must not be called (kwargs={kwargs})")


class Recorder:
    """A prober that answers with a fixed `ProbeResult` and remembers how it was asked."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result


def _meshed(elements: int = 500):
    return mesh_probe.ProbeResult(True, elements, None)


def _failed(kind: str, stage: str = ""):
    return mesh_probe.ProbeResult(
        False, 0, mesh_probe.MeshFailure(kind, (), None, f"{kind} said so", stage=stage))


def _verdict(which, *, prober, defects=(), packages=(), final=None, remaining_s=1_000.0):
    """`finishing_verdict` with an injected clock, so `deadline_s - clock()` is `remaining_s`."""
    return goal_mod.finishing_verdict(
        which, shape=SHAPE, final=final if final is not None else FakeMeasure(),
        defects=tuple(defects), packages=tuple(packages), deadline_s=remaining_s,
        clock=lambda: 0.0, prober=prober)


def test_unset_goal_adds_nothing():
    assert _verdict(None, prober=_exploding_prober) is None


def test_unset_goal_keeps_the_driver_deadline():
    assert goal_mod.reserve_s(None, tolerant_live=False, tolerant_timeout_s=30.0) == 0.0
    assert goal_mod.reserve_s(RepairGoal.meshing, tolerant_live=True,
                              tolerant_timeout_s=30.0) == 0.0


def test_meshing_runs_no_probe():
    assert _verdict(RepairGoal.meshing, prober=_exploding_prober) == {
        "profile": "meshing", "final_verdict": "tier3_product_mesher"}


def test_standalone_meshes_scores_one():
    verdict = _verdict(RepairGoal.standalone, prober=Recorder(_meshed()))
    assert verdict["profile"] == "standalone"
    assert verdict["score"] == 1.0
    assert verdict["meshability"]["tier"] == "2"
    assert verdict["meshability"]["meshes"] is True
    assert verdict["meshability"]["volume_elements"] == 500
    assert verdict["meshability"]["over_element_cap"] is False
    assert verdict["meshability"]["skipped"] == ""
    assert verdict["meshability"]["kind"] == ""
    assert verdict["meshability"]["stage"] == ""


def test_a_gmsh_refusal_scores_zero():
    verdict = _verdict(RepairGoal.standalone,
                       prober=Recorder(_failed(mesh_probe.OVERLAPPING_FACETS,
                                               stage=mesh_probe.STAGE_3D)))
    assert verdict["score"] == 0.0
    assert verdict["meshability"]["meshes"] is False
    assert verdict["meshability"]["kind"] == mesh_probe.OVERLAPPING_FACETS
    assert verdict["meshability"]["stage"] == mesh_probe.STAGE_3D


@pytest.mark.parametrize("kind", [mesh_probe.TIMEOUT, mesh_probe.PROBE_ERROR])
def test_a_timed_out_tier2_scores_none_not_zero(kind):
    verdict = _verdict(RepairGoal.standalone, prober=Recorder(_failed(kind)))
    assert verdict["meshability"]["meshes"] is None
    assert verdict["score"] is None
    assert verdict["meshability"]["kind"] == kind


def test_no_time_left_skips_and_says_so():
    verdict = _verdict(RepairGoal.standalone, prober=_exploding_prober, remaining_s=-1.0)
    assert verdict["meshability"]["skipped"] == "no_time"
    assert verdict["meshability"]["meshes"] is None
    assert verdict["meshability"]["budget_s"] == 0.0
    assert verdict["score"] is None


def test_verdict_budget_is_the_oracles():
    prober = Recorder(_meshed())
    verdict = _verdict(RepairGoal.standalone, prober=prober, remaining_s=1e6)
    assert prober.calls[0]["budget_s"] == 60.0
    assert verdict["meshability"]["budget_s"] == 60.0


def test_over_element_cap_is_flagged_not_failed():
    verdict = _verdict(RepairGoal.standalone,
                       prober=Recorder(_meshed(goal_mod.TIER2_ELEMENT_CAP + 1)))
    assert verdict["meshability"]["meshes"] is True
    assert verdict["meshability"]["over_element_cap"] is True
    assert verdict["score"] == 1.0


def test_kernel_clean_ignores_mesh_codes():
    verdict = _verdict(
        RepairGoal.standalone, prober=Recorder(_meshed()),
        final=FakeMeasure(valid=True),
        defects=[FakeDefect(DefectCode.mesh_open_loop, DefectSeverity.error)])
    assert verdict["kernel_clean"] is True


def test_kernel_clean_is_false_on_a_kernel_error():
    verdict = _verdict(
        RepairGoal.standalone, prober=Recorder(_meshed()),
        final=FakeMeasure(valid=True),
        defects=[FakeDefect(DefectCode.open_shell, DefectSeverity.error)])
    assert verdict["kernel_clean"] is False


def test_kernel_clean_is_false_on_an_invalid_shape():
    verdict = _verdict(RepairGoal.standalone, prober=Recorder(_meshed()),
                       final=FakeMeasure(valid=False))
    assert verdict["kernel_clean"] is False


def test_review_lists_committed_packages_only():
    verdict = _verdict(
        RepairGoal.standalone, prober=Recorder(_meshed()),
        packages=[FakePackage(0, "committed", reviewed="accept", review_reason="small",
                              deviation_mm=0.25),
                  FakePackage(1, "reverted", reviewed="reject", review_reason="moved too far")])
    assert verdict["review"] == [
        {"region_index": 0, "reviewed": "accept", "review_reason": "small",
         "deviation_mm": 0.25}]


def test_benchmark_has_the_standalone_shape():
    standalone = _verdict(RepairGoal.standalone, prober=Recorder(_meshed()))
    benchmark = _verdict(RepairGoal.benchmark, prober=Recorder(_meshed()))
    assert benchmark["profile"] == "benchmark"
    assert sorted(benchmark) == sorted(standalone)
    assert sorted(benchmark["meshability"]) == sorted(standalone["meshability"])


def test_verdict_never_raises():
    def boom(**kwargs):
        raise RuntimeError("the probe exploded")

    assert _verdict(RepairGoal.standalone, prober=boom) == {
        "profile": "standalone", "error": "RuntimeError"}


def test_reserve_adds_the_tolerant_budget_when_live():
    assert goal_mod.reserve_s(RepairGoal.standalone, tolerant_live=True,
                              tolerant_timeout_s=30.0) == 60.0 + 10.0 + 30.0
    assert goal_mod.reserve_s(RepairGoal.standalone, tolerant_live=False,
                              tolerant_timeout_s=30.0) == 60.0 + 10.0
    assert goal_mod.reserve_s(RepairGoal.benchmark, tolerant_live=False,
                              tolerant_timeout_s=30.0) == 60.0 + 10.0


def test_the_goal_enum_is_its_own_contract():
    # RepairTarget is left untouched: the inspect path and the policy payload read it.
    from meshpipeline.cad.repair.contracts import RepairTarget

    assert [g.value for g in RepairGoal] == ["standalone", "meshing", "benchmark"]
    assert RepairGoal is not RepairTarget
    assert [t.value for t in RepairTarget] == ["standalone", "meshing"]


def test_repair_step_file_takes_a_goal_and_defaults_to_none():
    import inspect

    from meshpipeline.cad.repair.conservative import repair_step_file

    parameter = inspect.signature(repair_step_file).parameters["goal"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
