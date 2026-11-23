# Responsibility: Verify the mesher panel runner: one bad mesher (segfault, hang) is recorded and
#                 replaced and the remaining members still run; a kernel result becomes a MemberResult.
# Boundaries: hermetic - test-only kernel functions (HEXERA_TEST_KERNEL_FNS=1), no gmsh, no OCCT.
#             Real gmsh behaviour is the native tier (tests/native/test_native_repair_v4_panel.py).
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from meshpipeline.cad.repair_v4.verdict.gmsh_members import GmshAlt, GmshDefault, GmshProd, result_from_call
from meshpipeline.cad.repair_v4.verdict.panel import Located, MemberResult, run_panel
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import CallResult, WorkerPool


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkerPool]:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


class _Aborts(GmshProd):
    """A gmsh member whose mesher dies like a segfault: the worker process aborts."""

    name = "aborts"
    fn_name = "abort"


class _Hangs(GmshDefault):
    name = "hangs"
    fn_name = "sleep_forever"


class _Echo:
    """A member that only proves the worker pool still serves calls, and from which process."""

    name = "echo"
    config: dict = {"member": "echo"}

    def __init__(self) -> None:
        self.pid: int | None = None

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult:
        call = pool.call("echo", {"step": step_path}, timeout_s=timeout_s)
        assert call.ok and call.value is not None
        self.pid = call.value["pid"]
        return MemberResult(self.name, "pass", None, "", [], 1.0, 1, call.seconds)


def test_member_crash_isolated(pool: WorkerPool) -> None:
    survivor = _Echo()
    results = run_panel(pool, "/tmp/x.step", [_Aborts(), survivor], timeout_s=30)
    assert [r.name for r in results] == ["aborts", "echo"]
    assert results[0].status == "crashed"
    assert results[0].located == [] and results[0].min_sicn is None and results[0].elements == 0
    assert results[1].status == "pass"  # the member after the crash ran, on a replacement worker


def test_member_crash_replaces_the_worker(pool: WorkerPool) -> None:
    before = _Echo()
    before.run(pool, "/tmp/x.step", timeout_s=30)
    run_panel(pool, "/tmp/x.step", [_Aborts()], timeout_s=30)
    after = _Echo()
    after.run(pool, "/tmp/x.step", timeout_s=30)
    assert before.pid is not None and after.pid is not None and before.pid != after.pid
    assert after.pid != os.getpid()


def test_member_timeout_isolated(pool: WorkerPool) -> None:
    survivor = _Echo()
    results = run_panel(pool, "/tmp/x.step", [_Hangs(), survivor], timeout_s=1.5)
    assert results[0].status == "timeout"
    assert results[0].seconds >= 1.0
    assert results[1].status == "pass"


def test_panel_returns_every_member_in_order(pool: WorkerPool) -> None:
    results = run_panel(pool, "/tmp/x.step", [_Echo(), _Aborts(), _Echo()], timeout_s=30)
    assert [r.status for r in results] == ["pass", "crashed", "pass"]


def _call(value: dict | None, *, status: str = "ok", error: str | None = None) -> CallResult:
    return CallResult(ok=status == "ok", value=value, error=error, status=status, seconds=2.5)


def test_kernel_pass_becomes_member_pass() -> None:
    value: dict = {"status": "pass", "error_kind": None, "message": "", "located": [], "min_sicn": 0.3, "elements": 12}
    result = result_from_call("gmsh_prod", _call(value))
    assert result == MemberResult("gmsh_prod", "pass", None, "", [], 0.3, 12, 2.5)


def test_kernel_fail_carries_kind_and_located() -> None:
    value = {
        "status": "fail",
        "error_kind": "overlapping_facets",
        "message": "Invalid boundary mesh",
        "located": [
            {"entity": "face:ab12", "xyz": None, "mesher_tag": "surface 8"},
            {"entity": None, "xyz": [1.0, 2.0, 3.0], "mesher_tag": "point"},
        ],
        "min_sicn": None,
        "elements": 0,
    }
    result = result_from_call("gmsh_default", _call(value))
    assert result.status == "fail" and result.error_kind == "overlapping_facets"
    assert result.located == [Located("face:ab12", None, "surface 8"), Located(None, (1.0, 2.0, 3.0), "point")]


@pytest.mark.parametrize("status", ["timeout", "crashed"])
def test_timeout_and_crash_are_member_statuses(status: str) -> None:
    result = result_from_call("m", _call(None, status=status, error="gone"))
    assert result.status == status and result.message == "gone" and result.seconds == 2.5


def test_kernel_error_is_infrastructure_not_a_verdict() -> None:
    with pytest.raises(CheckerUnavailable):
        result_from_call("m", _call(None, status="error", error="ModuleNotFoundError: gmsh"))


def test_member_names_are_distinct() -> None:
    assert [m.name for m in (GmshProd(), GmshDefault(), GmshAlt())] == ["gmsh_prod", "gmsh_default", "gmsh_alt"]


def test_judge_requires_sicn_floor_and_no_inverted() -> None:
    from meshpipeline.cad.repair_v4.verdict.mesh_kernel import MIN_SICN, judge

    assert MIN_SICN == 0.01
    assert judge([0.4, 0.01, 0.9]) == ("pass", None)  # the floor itself passes
    assert judge([0.4, 0.0099]) == ("fail", "low_quality")
    assert judge([0.4, -0.2, 0.9]) == ("fail", "inverted_elements")
    assert judge([0.4, float("nan")]) == ("fail", "inverted_elements")
    assert judge([]) == ("fail", "no_volume")


def test_killed_worker_is_marked_infrastructure_and_overrun_is_a_genuine_timeout(pool: WorkerPool) -> None:
    from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND

    (crashed,) = run_panel(pool, "/tmp/x.step", [_Aborts()], timeout_s=30)
    assert crashed.status == "crashed" and crashed.error_kind == INFRASTRUCTURE_KIND
    (slow,) = run_panel(pool, "/tmp/x.step", [_Hangs()], timeout_s=1.5)
    assert slow.status == "timeout" and slow.error_kind == "timeout"  # the file failed the limit


@pytest.mark.parametrize(
    "error", ["no worker for mesh_gmsh: boom", "worker died before mesh_gmsh", "worker died during mesh_gmsh"]
)
def test_any_pool_crash_is_infrastructure(error: str) -> None:
    from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND

    result = result_from_call("m", _call(None, status="crashed", error=error))
    assert result.status == "crashed" and result.error_kind == INFRASTRUCTURE_KIND


def test_member_config_is_json_able_complete_and_distinct() -> None:
    import json

    configs = [m.config for m in (GmshProd(), GmshDefault(), GmshAlt())]
    assert all(isinstance(c, dict) and c for c in configs)
    assert len({json.dumps(c, sort_keys=True) for c in configs}) == 3
    assert json.loads(json.dumps(configs[0])) == configs[0]


def test_prod_config_follows_the_production_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.engines import fea_mesh_config

    before = GmshProd().config
    monkeypatch.setattr(fea_mesh_config, "fixed_options", lambda: {"Mesh.RandomSeed": 2})
    assert GmshProd().config != before
