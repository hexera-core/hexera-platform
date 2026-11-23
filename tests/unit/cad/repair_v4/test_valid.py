# Responsibility: Verify the hermetic parts of layer 1: how a report is assembled from kernel
#                 results. The geometry itself is proved in the native tier.
# Boundaries: hermetic - a scripted pool stands in for the kernel workers; no OCCT.
from __future__ import annotations

from meshpipeline.cad.repair_v4.verdict.valid import HARD_CODES, SOFT_CODES, check_valid
from meshpipeline.cad.repair_v4.worker_pool import CallResult


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=0.0)


class ScriptedPool:
    def __init__(self, *, units="mm", hard=(), soft=(), rt_units=None, rt_hard=(), write_ok=True) -> None:
        self.units, self.hard, self.soft = units, list(hard), list(soft)
        self.rt_units, self.rt_hard, self.write_ok = rt_units or units, list(rt_hard), write_ok
        self.checks = 0

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        labels = {"0": {"name": "a", "color": None, "parent": None}}
        if fn == "read_step_xcaf":
            rt = "round_trip" in payload["path"]
            return _ok(
                {"brep": "x.brep", "labels": labels, "assembly_ids": [], "units": self.rt_units if rt else self.units}
            )
        if fn == "write_step_xcaf":
            if not self.write_ok:
                return CallResult(False, None, "ValueError: boom", "error", 0.0)
            return _ok({"path": payload["path"]})
        self.checks += 1
        hard = self.hard if self.checks == 1 else self.rt_hard
        return _ok(
            {
                "solids": 1,
                "hard": [{"code": c, "entities": [], "detail": {}} for c in hard],
                "soft": [{"code": c, "entities": [], "detail": {}} for c in self.soft],
            }
        )


def test_code_sets_are_exactly_the_binding_ones() -> None:
    assert set(HARD_CODES) == {
        "brep_invalid",
        "open_shell",
        "non_manifold",
        "non_positive_volume",
        "self_intersection",
        "body_overlap",
        "units_changed",
        "round_trip_invalid",
        "no_solids",
    }
    assert set(SOFT_CODES) == {"small_edge", "small_face", "large_tolerance"}


def test_clean_file_is_ok() -> None:
    report = check_valid(ScriptedPool(), "a.step", original_units="mm")
    assert report.ok and report.hard == [] and report.round_trip_ok and report.units == "mm"


def test_soft_findings_never_fail() -> None:
    report = check_valid(ScriptedPool(soft=SOFT_CODES), "a.step", original_units=None)
    assert report.ok is True
    assert [f.code for f in report.soft] == list(SOFT_CODES)


def test_unit_mismatch_is_units_changed_and_unit_is_kept() -> None:
    report = check_valid(ScriptedPool(units="inch"), "a.step", original_units="mm")
    assert report.units == "inch"
    assert [f.code for f in report.hard] == ["units_changed"]
    assert report.ok is False


def test_matching_unit_is_not_units_changed() -> None:
    report = check_valid(ScriptedPool(units="inch"), "a.step", original_units="inch")
    assert report.ok is True


def test_round_trip_that_loses_the_unit_is_units_changed() -> None:
    report = check_valid(ScriptedPool(units="inch", rt_units="mm"), "a.step", original_units=None)
    assert "units_changed" in [f.code for f in report.hard]
    assert report.round_trip_ok is False


def test_new_defect_after_round_trip_is_round_trip_invalid() -> None:
    report = check_valid(ScriptedPool(rt_hard=["open_shell"]), "a.step", original_units=None)
    assert [f.code for f in report.hard] == ["round_trip_invalid"]
    assert report.round_trip_ok is False


def test_defect_already_present_is_not_blamed_on_the_round_trip() -> None:
    pool = ScriptedPool(hard=["body_overlap"], rt_hard=["body_overlap"])
    report = check_valid(pool, "a.step", original_units=None)
    assert [f.code for f in report.hard] == ["body_overlap"]
    assert report.round_trip_ok is True


def test_failed_write_is_round_trip_invalid() -> None:
    report = check_valid(ScriptedPool(write_ok=False), "a.step", original_units=None)
    assert [f.code for f in report.hard] == ["round_trip_invalid"]
