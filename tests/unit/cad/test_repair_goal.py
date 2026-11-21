# Responsibility: Prove the goal-profile contract - what a repair run is finishing FOR - and the
#                 standalone finishing verdict it produces.
# Boundaries: no OCP and no gmsh. The tier-2 probe is injected (`prober`), the clock is injected,
#             and `Measure`, the defects and the package verdicts are stubs.
from __future__ import annotations

from dataclasses import dataclass

import pytest

from meshpipeline.cad.repair import goal as goal_mod
from meshpipeline.cad.repair import grind, mesh_probe, oracle
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity, RepairGoal

SHAPE = object()


@dataclass(frozen=True)
class FakeMeasure:
    """Only what the verdict reads off a `caps.Measure`."""

    valid: bool = True
    diagonal_mm: float = 100.0


@dataclass(frozen=True)
class SizedMeasure(FakeMeasure):
    """A measure that also carries the counts `grind` predicts from."""

    solids: int = 1
    faces: int = 200


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


# --------------------------------------------------------------------------------------------
# THE ORACLE SEAM. Every budget this module spends is the oracle's, held by identity rather than
# by a second copy of the number - two constants that could drift apart were the bug being fixed.
# --------------------------------------------------------------------------------------------


def test_the_budget_constants_are_the_oracles():
    assert goal_mod.TIER2_HARD_CAP_S is oracle.TIER2_HARD_CAP_S
    assert goal_mod.TIER2_ELEMENT_CAP is oracle.TIER2_ELEMENT_CAP
    assert goal_mod.FINAL_PROBE_SLACK_S is oracle.FINAL_PROBE_SLACK_S


def test_goal_states_no_budget_number_of_its_own():
    """No literal copy of a budget survives in goal.py: the oracle is the only source.

    An AST read, because the failure mode is textual - somebody re-typing `60.0` here is exactly
    how the two numbers drifted apart before.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(goal_mod.__file__).read_text())
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                and not isinstance(n.value, bool)}
    assert 60.0 not in literals
    assert 1_000_000 not in literals
    assert 10.0 not in literals


@pytest.mark.parametrize("remaining_s", [-1.0, 0.0, 1.0, 9.0, 10.0, 11.0, 30.0, 69.9, 70.0,
                                         100.0, 1e6])
def test_the_verdict_budget_tracks_the_oracles_rule(remaining_s):
    """The probe's budget equals the oracle's `verdict` rule AT EVERY REMAINDER, not just one.

    A TABLE, because a single remainder of 1e6 saturates at the hard cap and would keep passing
    if the rule underneath were replaced by `min(remaining, 60)` - the slack term only shows up
    near the deadline, which is precisely where a repair run ends.
    """
    expected = oracle.probe_budget_s(oracle.CHECK_VERDICT, remaining_s,
                                     timeout_s=oracle.TIER2_HARD_CAP_S)
    prober = Recorder(_meshed())
    verdict = _verdict(RepairGoal.standalone, prober=prober, remaining_s=remaining_s,
                       final=SizedMeasure())
    assert verdict["meshability"]["budget_s"] == expected
    if expected > 0.0:
        assert prober.calls[0]["budget_s"] == expected
    else:
        assert prober.calls == []


# --------------------------------------------------------------------------------------------
# THE GRIND SEAM. The expected mesh cost is RECORDED beside the verdict. Nothing in cad/ branches
# on it (tests/unit/cad/test_repair_grind.py holds that), so these pin the recorded value only.
# --------------------------------------------------------------------------------------------


def test_the_verdict_records_a_volume_refusal_as_standard():
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(faces=200),
                       prober=Recorder(_failed(mesh_probe.OVERLAPPING_FACETS,
                                               stage=mesh_probe.STAGE_3D)))
    assert verdict["grind"] == {"tier": grind.STANDARD, "rule": grind.RULE_VOLUME_REFUSAL}


def test_the_verdict_records_a_surface_refusal_as_cheap():
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(faces=138),
                       prober=Recorder(_failed(mesh_probe.OPEN_LOOP,
                                               stage=mesh_probe.STAGE_2D)))
    assert verdict["grind"] == {"tier": grind.CHEAP, "rule": grind.RULE_SURFACE_REFUSAL}


def test_the_verdict_records_a_meshed_part_as_cheap():
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(faces=50),
                       prober=Recorder(_meshed()))
    assert verdict["grind"] == {"tier": grind.CHEAP, "rule": grind.RULE_MESHED}


def test_the_verdict_records_a_big_part_as_heavy():
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(faces=2_000),
                       prober=Recorder(_meshed()))
    assert verdict["grind"] == {"tier": grind.HEAVY, "rule": grind.RULE_MANY_FACES}


def test_the_verdict_records_a_timeout_as_heavy():
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(faces=200),
                       prober=Recorder(_failed(mesh_probe.TIMEOUT)))
    assert verdict["grind"] == {"tier": grind.HEAVY, "rule": grind.RULE_NO_ANSWER}


def test_grind_is_unknown_without_a_face_count():
    """A measure with no face count is `UNKNOWN`, NEVER A GUESS - grind's own rule, held here
    because the verdict is what reads the count off the measure."""
    verdict = _verdict(RepairGoal.standalone, final=FakeMeasure(), prober=Recorder(_meshed()))
    assert verdict["grind"] == {"tier": grind.UNKNOWN, "rule": grind.RULE_FACES_UNKNOWN}


def test_a_skipped_probe_predicts_nothing():
    """NO READING, NO PREDICTION. A skipped probe leaves `kind` empty, which grind would read as
    "this meshed" and call cheap - a claim about a part nobody probed. It must say UNKNOWN."""
    verdict = _verdict(RepairGoal.standalone, prober=_exploding_prober, remaining_s=-1.0,
                       final=SizedMeasure(faces=50))
    assert verdict["meshability"]["skipped"] == "no_time"
    assert verdict["grind"]["tier"] == grind.UNKNOWN


def test_the_grind_entry_is_exactly_tier_and_rule():
    """The shape devtools/validation/repair_corpus.py `goal_row` reads: `grind{tier,rule}`."""
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(), prober=Recorder(_meshed()))
    assert sorted(verdict["grind"]) == ["rule", "tier"]
    assert verdict["grind"]["tier"] in {grind.CHEAP, grind.STANDARD, grind.HEAVY, grind.UNKNOWN}
    assert verdict["grind"]["rule"] in grind.RULES


def test_the_verdict_keys_are_what_the_corpus_runner_consumes():
    """Producer/consumer parity with `goal_row`, which reads `profile`, `kernel_clean`,
    `meshability`, `score` and `grind`, and treats `routing` as ABSENT until tier 2b is live."""
    verdict = _verdict(RepairGoal.standalone, final=SizedMeasure(), prober=Recorder(_meshed()))
    assert sorted(verdict) == ["grind", "kernel_clean", "meshability", "profile", "review",
                               "score"]
    assert "routing" not in verdict
    assert {"meshes", "kind", "seconds"} <= set(verdict["meshability"])


def test_a_grind_failure_cannot_break_the_verdict(monkeypatch):
    """grind never raises; if it somehow did, the verdict still must not lose the repair."""
    def boom(*args, **kwargs):
        raise RuntimeError("grind exploded")

    monkeypatch.setattr(goal_mod.grind, "record", boom)
    assert _verdict(RepairGoal.standalone, final=SizedMeasure(),
                    prober=Recorder(_meshed())) == {"profile": "standalone",
                                                    "error": "RuntimeError"}
