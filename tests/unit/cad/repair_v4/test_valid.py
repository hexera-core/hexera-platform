# Responsibility: Verify the hermetic parts of layer 1: how a report is assembled from kernel
#                 results. The geometry itself is proved in the native tier.
# Boundaries: hermetic - a scripted pool stands in for the kernel workers; no OCCT.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4.verdict.valid import HARD_CODES, SOFT_CODES, CheckerUnavailable, check_valid
from meshpipeline.cad.repair_v4.worker_pool import CallResult


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=0.0)


class ScriptedPool:
    def __init__(
        self,
        *,
        units="mm",
        hard=(),
        soft=(),
        rt_units=None,
        rt_hard=(),
        write_ok=True,
        fail=None,
        extra_rt_labels=False,
    ) -> None:
        self.fail, self.extra_rt_labels = fail, extra_rt_labels
        self.units, self.hard, self.soft = units, list(hard), list(soft)
        self.rt_units, self.rt_hard, self.write_ok = rt_units or units, list(rt_hard), write_ok
        self.checks = 0

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        if self.fail and self.fail[0] == fn:
            return self.fail[1]
        labels = {"0": {"name": "a", "color": None, "parent": None}}
        if self.extra_rt_labels and "round_trip" in str(payload.get("path")):
            labels["1"] = {"name": "extra", "color": None, "parent": None}
        if fn == "read_step_xcaf":
            rt = "round_trip" in payload["path"]
            brep = "round_trip.brep" if rt else "x.brep"
            return _ok(
                {"brep": brep, "labels": labels, "assembly_ids": [], "units": self.rt_units if rt else self.units}
            )
        if fn == "valid_extent":
            return _ok({"unbounded": "unbounded_geometry" in self.hard})
        if fn == "write_step_xcaf":
            if not self.write_ok:
                return CallResult(False, None, "ValueError: boom", "error", 0.0)
            return _ok({"path": payload["path"]})
        self.checks += 1  # the input and its round trip are checked concurrently: tell them apart by BREP
        hard = self.rt_hard if "round_trip" in payload["brep"] else self.hard
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
        "unbounded_geometry",
    }
    assert set(SOFT_CODES) == {"small_edge", "small_face", "large_tolerance", "orientation_unchecked"}


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


def _fail(status: str, error: str) -> CallResult:
    return CallResult(False, None, error, status, 0.0)


@pytest.mark.parametrize("fn", ["read_step_xcaf", "check_valid", "write_step_xcaf"])
@pytest.mark.parametrize("status", ["timeout", "crashed"])
def test_timeout_or_crash_is_checker_unavailable_not_a_finding(fn: str, status: str) -> None:
    pool = ScriptedPool(fail=(fn, _fail(status, "worker gone")))
    with pytest.raises(CheckerUnavailable):
        check_valid(pool, "a.step", original_units=None)


def test_kernel_defect_in_check_valid_is_checker_unavailable() -> None:
    pool = ScriptedPool(fail=("check_valid", _fail("error", "AttributeError: bug")))
    with pytest.raises(CheckerUnavailable):
        check_valid(pool, "a.step", original_units=None)


def test_unreadable_step_is_brep_invalid() -> None:
    pool = ScriptedPool(fail=("read_step_xcaf", _fail("error", "ValueError: STEP file could not be read")))
    report = check_valid(pool, "a.step", original_units=None)
    assert [f.code for f in report.hard] == ["brep_invalid"]
    assert report.ok is False


def test_extra_labels_after_round_trip_are_not_a_failure() -> None:
    report = check_valid(ScriptedPool(extra_rt_labels=True), "a.step", original_units=None)
    assert report.ok is True and report.round_trip_ok is True


@pytest.mark.parametrize("spelling", ["in", "inches", "Inch", "INCH"])
def test_unit_spellings_of_the_original_are_normalised(spelling: str) -> None:
    report = check_valid(ScriptedPool(units="inch"), "a.step", original_units=spelling)
    assert report.ok is True


def test_unwritable_unit_round_trips_in_mm_and_is_not_a_failure() -> None:
    """A yard file: the round trip is written (and expected back) in mm; the report keeps 'yard'."""
    pool = ScriptedPool(units="yard", rt_units="mm")
    report = check_valid(pool, "a.step", original_units="yard")
    assert report.ok is True and report.round_trip_ok is True
    assert report.units == "yard"


def test_an_unbounded_finding_stops_layer_1_before_the_round_trip() -> None:
    pool = ScriptedPool(hard=["unbounded_geometry"])
    report = check_valid(pool, "a.step", original_units="inch")  # a units mismatch is NOT also reported
    assert report.unbounded and not report.ok
    assert [f.code for f in report.hard] == ["unbounded_geometry"]
    assert pool.checks == 1  # the shape was checked once; no round trip re-checked it


def test_only_the_unbounded_code_makes_a_report_unbounded() -> None:
    assert not check_valid(ScriptedPool(hard=["open_shell"]), "a.step", original_units=None).unbounded
    assert not check_valid(ScriptedPool(), "a.step", original_units=None).unbounded


# ------------------------------------------------------------------ the checker's one-call wall (task 55)

def test_the_one_call_wall_has_exactly_one_definition_and_every_reader_reads_it() -> None:
    """`limits.CHECKER_CALL_TIMEOUT_S` is the wall. valid.py publishes it as CALL_TIMEOUT_S (faithful.py reads
    it from there) and memo.py as READ_TIMEOUT_S; none of them may hold a number of its own, because a wall
    that is retyped drifts and then the raise of one half is silently undone by the other."""
    import inspect
    import re

    from meshpipeline.cad.repair_v4 import limits
    from meshpipeline.cad.repair_v4.verdict import faithful, memo, valid

    wall = limits.CHECKER_CALL_TIMEOUT_S
    assert wall > 0
    assert valid.CALL_TIMEOUT_S is wall
    assert memo.READ_TIMEOUT_S is wall
    assert faithful.CALL_TIMEOUT_S is wall

    # no module but limits.py may ASSIGN the number; a comment that quotes a measurement may mention it
    for mod in (valid, memo, faithful):
        for line in inspect.getsource(mod).splitlines():
            code = line.split("#")[0]
            assert not re.search(rf"^\s*\w*TIMEOUT\w*\s*=\s*{wall:g}", code), f"{mod.__name__}: {line!r}"


def test_the_per_file_wall_is_larger_than_one_call_may_take() -> None:
    """The per-file wall (evalspec.DEFAULT_FILE_TIMEOUT_S) bounds the SUM of a file's calls, so a per-file wall
    at or below the per-call wall could only ever kill a file mid-call and would make the call wall unreachable."""
    import importlib.util
    import sys
    from pathlib import Path

    from meshpipeline.cad.repair_v4 import limits

    # By PATH, under a unique name: v3_eval's modules claim the bare name `evalspec` in sys.modules.
    path = Path(__file__).resolve().parents[4] / "devtools/validation/v4_eval/evalspec.py"
    spec = importlib.util.spec_from_file_location("v4_evalspec_for_wall_check", path)
    assert spec is not None and spec.loader is not None
    evalspec = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = evalspec  # before exec: @dataclass resolves its module through sys.modules
    spec.loader.exec_module(evalspec)

    assert evalspec.DEFAULT_FILE_TIMEOUT_S > limits.CHECKER_CALL_TIMEOUT_S


class RecordingPool(ScriptedPool):
    """Records whether each check asked for the Booleans, and runs the round trip only after the input's check
    (the workers=1 order) so the skip is exercised."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.booleans: dict[str, bool] = {}

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        if fn == "valid_extent":
            return _ok({"unbounded": None})  # not known bounded: the plain order, input check first
        if fn == "check_valid":
            self.booleans["round_trip" if "round_trip" in payload["brep"] else "input"] = payload["booleans"]
        return super().call(fn, payload, timeout_s=timeout_s)


def test_round_trip_skips_its_booleans_only_when_the_input_is_already_invalid() -> None:
    pool = RecordingPool(hard=["open_shell"])
    report = check_valid(pool, "a.step", original_units=None)
    assert pool.booleans == {"input": True, "round_trip": False}
    assert report.ok is False and [f.code for f in report.hard] == ["open_shell"]

    pool = RecordingPool()
    assert check_valid(pool, "a.step", original_units=None).ok is True
    assert pool.booleans == {"input": True, "round_trip": True}


@pytest.mark.parametrize("ran_booleans", [True, False])
def test_a_new_boolean_code_on_an_invalid_input_is_ignored_so_the_skip_cannot_change_the_report(
    ran_booleans: bool,
) -> None:
    rt_hard = ["open_shell", "body_overlap"] if ran_booleans else ["open_shell"]
    report = check_valid(ScriptedPool(hard=["open_shell"], rt_hard=rt_hard), "a.step", original_units=None)
    assert [f.code for f in report.hard] == ["open_shell"] and report.round_trip_ok is True


def test_a_new_boolean_code_on_a_valid_input_still_fails_the_round_trip() -> None:
    report = check_valid(ScriptedPool(rt_hard=["self_intersection"]), "a.step", original_units=None)
    assert [f.code for f in report.hard] == ["round_trip_invalid"] and report.round_trip_ok is False
