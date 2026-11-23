# Responsibility: Verify the mesher panel runner: one bad mesher (segfault, hang) is recorded and
#                 replaced and the remaining members still run; a kernel result becomes a MemberResult.
#                 A timeout never blames the file until it repeats at twice the budget; a gmsh crash is a
#                 vote against the file; the three gmsh members are three different algorithm pairs.
# Boundaries: hermetic - test-only kernel functions (HEXERA_TEST_KERNEL_FNS=1), no gmsh, no OCCT.
#             Real gmsh behaviour is the native tier (tests/native/test_native_repair_v4_panel.py).
from __future__ import annotations

import os
import signal
import sys
import types
from collections.abc import Iterator

import pytest

from meshpipeline.cad.repair_v4.verdict import mesh_child, mesh_kernel, panel
from meshpipeline.cad.repair_v4.verdict.gmsh_members import GmshAlt, GmshDefault, GmshProd, result_from_call
from meshpipeline.cad.repair_v4.verdict.panel import (
    INFRASTRUCTURE_KIND,
    Located,
    MemberResult,
    PanelContractError,
    run_panel,
)
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
    assert results[0].seconds >= 4.0  # 1.5 s, then 3 s
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
    result = result_from_call("m", _call(None, status=status, error="gone"), timeouts=2)
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


def test_killed_worker_is_marked_infrastructure_and_a_repeated_overrun_is_a_genuine_timeout(pool: WorkerPool) -> None:
    (crashed,) = run_panel(pool, "/tmp/x.step", [_Aborts()], timeout_s=30)
    assert crashed.status == "crashed" and crashed.error_kind == INFRASTRUCTURE_KIND
    (slow,) = run_panel(pool, "/tmp/x.step", [_Hangs()], timeout_s=1.0)  # 1 s, then again at 2 s
    assert slow.status == "timeout" and slow.error_kind == "timeout"  # the file failed the limit twice
    assert slow.seconds >= 3.0  # both attempts ran


@pytest.mark.parametrize(
    "error", ["no worker for mesh_gmsh: boom", "worker died before mesh_gmsh", "worker died during mesh_gmsh"]
)
def test_any_pool_crash_is_infrastructure(error: str) -> None:
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


# ---- Critical 1: three different meshers, not one at three sizes -------------------------------------


def test_the_three_members_run_three_different_algorithm_pairs() -> None:
    triples = {m.name: mesh_child.algorithms(m.profile) for m in (GmshProd(), GmshDefault(), GmshAlt())}
    assert triples == {"gmsh_prod": (6, 10), "gmsh_default": (6, 1), "gmsh_alt": (1, 4)}
    assert len(set(triples.values())) == 3  # no two members mesh with the same pair
    # and no two share the 3D algorithm, so a 3D-only failure reaches every opinion differently
    assert len({t[1] for t in triples.values()}) == 3


def test_a_member_that_leaves_the_algorithm_unset_gets_gmsh_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (mesh_child.GMSH_DEFAULT_ALGORITHM_2D, mesh_child.GMSH_DEFAULT_ALGORITHM_3D) == (6, 1)
    monkeypatch.setattr(mesh_child, "ALT_OPTIONS", {"Mesh.Algorithm": 6, "Mesh.Algorithm3D": 1})
    assert mesh_child.algorithms("alt") == mesh_child.algorithms("default")  # the old GmshAlt, caught


def test_algorithms_are_part_of_the_cache_key() -> None:
    assert GmshAlt().config["algorithms"] == [1, 4]


# ---- Critical 2: a timeout is infrastructure until it repeats at twice the budget --------------------


class _ScriptedPool:
    """Answers `call` from a list, recording each budget it was given."""

    def __init__(self, *answers: CallResult) -> None:
        self.answers = list(answers)
        self.budgets: list[float] = []
        self.payloads: list[dict] = []

    def call(self, fn_name: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.budgets.append(timeout_s)
        self.payloads.append(payload)
        return self.answers.pop(0)


_PASS = {"status": "pass", "error_kind": None, "message": "", "located": [], "min_sicn": 0.3, "elements": 12}


def test_a_single_timeout_is_infrastructure_never_the_file() -> None:
    assert panel.timeout_kind(1) == INFRASTRUCTURE_KIND
    assert panel.timeout_kind(0) == INFRASTRUCTURE_KIND
    result = result_from_call("m", _call(None, status="timeout", error="slow"), timeouts=1)
    assert result.status == "timeout" and result.error_kind == INFRASTRUCTURE_KIND
    assert panel.is_inconclusive(result)
    assert result_from_call("m", _call(None, status="timeout", error="slow")).error_kind == INFRASTRUCTURE_KIND


def test_a_timeout_then_a_pass_at_double_the_budget_is_a_pass() -> None:
    pool = _ScriptedPool(_call(None, status="timeout", error="slow"), _call(_PASS))
    result = GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 200]
    assert [p["timeout_s"] for p in pool.payloads] == [panel.inner_timeout_s(100), panel.inner_timeout_s(200)]
    assert result.status == "pass" and result.min_sicn == 0.3
    assert result.seconds == 5.0  # both attempts counted


def test_two_timeouts_are_a_genuine_non_pass() -> None:
    pool = _ScriptedPool(_call(None, status="timeout", error="slow"), _call(None, status="timeout", error="slower"))
    result = GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 200]
    assert result.status == "timeout" and result.error_kind == "timeout" and not panel.is_inconclusive(result)


def test_gmshs_own_clock_gets_the_same_treatment() -> None:
    expired = _call({"status": "timeout", "error_kind": None, "cause": None, "message": "gmsh exceeded 70s",
                     "located": [], "min_sicn": None, "elements": 0})
    once = _ScriptedPool(expired, _call(_PASS))
    assert GmshDefault().run(once, "/x.step", timeout_s=100).status == "pass"  # type: ignore[arg-type]
    twice = _ScriptedPool(expired, expired)
    result = GmshDefault().run(twice, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert (result.status, result.error_kind) == ("timeout", "timeout")


def test_a_timeout_then_a_pool_crash_is_still_infrastructure() -> None:
    pool = _ScriptedPool(_call(None, status="timeout"), _call(None, status="crashed", error="worker died during mesh_gmsh"))
    result = GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert result.status == "crashed" and result.error_kind == INFRASTRUCTURE_KIND


# ---- Critical 3: gmsh runs as a child process, so its crash is the file's vote -----------------------


def _child_that(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setattr(mesh_kernel, "_child_argv", lambda _payload, _answer: [sys.executable, "-c", code])


def _mesh(timeout_s: float = 30) -> dict:
    return mesh_kernel.mesh_gmsh({"step": "/x.step", "profile": "default", "timeout_s": timeout_s})


@pytest.mark.parametrize("sig", [signal.SIGSEGV, signal.SIGABRT, signal.SIGFPE])
def test_a_gmsh_crash_is_a_vote_against_the_file(monkeypatch: pytest.MonkeyPatch, sig: signal.Signals) -> None:
    _child_that(monkeypatch, f"import os,signal; os.kill(os.getpid(), signal.{sig.name})")
    value = _mesh()
    assert value["status"] == "crashed" and value["error_kind"] == panel.MESHER_CRASH_KIND
    result = result_from_call("gmsh_default", _call(value))
    assert result.status == "crashed" and result.error_kind == panel.MESHER_CRASH_KIND
    assert not panel.is_inconclusive(result)


def test_sigkill_is_inconclusive_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    _child_that(monkeypatch, "import os,signal; os.kill(os.getpid(), signal.SIGKILL)")
    result = result_from_call("gmsh_prod", _call(_mesh()))
    assert result.status == "crashed" and result.error_kind == INFRASTRUCTURE_KIND and result.cause == "sigkill"
    assert panel.inconclusive_tally([result, MemberResult("ok", "pass")]) == {"gmsh_prod:sigkill": 1}


def test_a_python_error_in_the_child_is_infrastructure_not_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _child_that(monkeypatch, "raise MemoryError('x')")
    with pytest.raises(RuntimeError, match="MemoryError"):
        _mesh()


def test_a_child_exit_without_a_traceback_is_gmsh_dying(monkeypatch: pytest.MonkeyPatch) -> None:
    _child_that(monkeypatch, "import os; os._exit(3)")
    assert _mesh()["error_kind"] == panel.MESHER_CRASH_KIND


def test_a_child_that_outlives_its_clock_is_a_timeout_and_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    _child_that(monkeypatch, "import time; time.sleep(60)")
    value = _mesh(timeout_s=1.0)
    assert value["status"] == "timeout"


# ---- Important 5: only gmsh's own errors are the file's answer ---------------------------------------


@pytest.mark.parametrize(
    ("exc", "is_gmsh"),
    [(Exception("Invalid boundary mesh"), True), (MemoryError(), False), (ValueError("x"), False),
     (RuntimeError("gmsh option Mesh.Algorithm did not take"), False), (ImportError("libGLU"), False)],
)
def test_only_a_plain_exception_is_gmshs(exc: BaseException, is_gmsh: bool) -> None:
    assert mesh_child.is_gmsh_error(exc) is is_gmsh


def _fake_gmsh(import_error: BaseException) -> types.ModuleType:
    mod = types.ModuleType("gmsh")
    values: dict[str, float] = {}

    def raise_it(*_a: object, **_k: object) -> None:
        raise import_error

    mod.initialize = lambda **_k: None  # type: ignore[attr-defined]
    mod.finalize = lambda: None  # type: ignore[attr-defined]
    mod.option = types.SimpleNamespace(  # type: ignore[attr-defined]
        setNumber=lambda k, v: values.__setitem__(k, v), getNumber=lambda k: values[k]
    )
    mod.logger = types.SimpleNamespace(start=lambda: None, get=lambda: [])  # type: ignore[attr-defined]
    mod.model = types.SimpleNamespace(occ=types.SimpleNamespace(importShapes=raise_it, synchronize=lambda: None))  # type: ignore[attr-defined]
    return mod


def test_a_non_gmsh_error_inside_the_meshing_block_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gmsh", _fake_gmsh(MemoryError("out of memory")))
    with pytest.raises(MemoryError):
        mesh_child.run_in_process({"step": "/x.step", "profile": "default"})


def test_a_gmsh_error_inside_the_meshing_block_is_the_files_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gmsh", _fake_gmsh(Exception("Could not open file")))
    answer = mesh_child.run_in_process({"step": "/x.step", "profile": "default"})
    assert answer["status"] == "fail"


# ---- Important 6: the marker is enforced where the panel runs ----------------------------------------


class _Unmarked:
    name = "unmarked"
    config: dict = {"member": "unmarked"}

    def __init__(self, status: str) -> None:
        self.status = status

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult:
        return MemberResult(self.name, self.status, None, "gone")  # type: ignore[arg-type]


@pytest.mark.parametrize("status", ["crashed", "timeout"])
def test_run_panel_refuses_a_failure_with_no_kind(pool: WorkerPool, status: str) -> None:
    with pytest.raises(PanelContractError, match="no error_kind"):
        run_panel(pool, "/x.step", [_Unmarked(status)], timeout_s=5)


def test_one_infrastructure_constant() -> None:
    """The marker is one object: the other modules that name it must import it, not retype it."""
    assert INFRASTRUCTURE_KIND == "infrastructure"
    for module_name, attr in (
        ("meshpipeline.cad.repair_v4.verdict.assemble", "INFRASTRUCTURE_KIND"),
        ("meshpipeline.cad.repair_v4.verdict.netgen_member", "INFRASTRUCTURE"),
    ):
        module = pytest.importorskip(module_name, reason="lands with Task 4's branch")
        assert getattr(module, attr) is INFRASTRUCTURE_KIND, f"{module_name}.{attr} is a copy"


def test_gmsh_members_use_the_one_constant() -> None:
    import meshpipeline.cad.repair_v4.verdict.gmsh_members as gm

    assert gm.INFRASTRUCTURE_KIND is INFRASTRUCTURE_KIND


def test_run_panel_logs_every_inconclusive_member(pool: WorkerPool, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        run_panel(pool, "/x.step", [_Aborts()], timeout_s=30)
    assert "INCONCLUSIVE aborts" in caplog.text and "pool_worker" in caplog.text


# ---- Important 7: the defaults Task 4's callers rely on ----------------------------------------------


def test_member_result_defaults() -> None:
    bare = MemberResult("gmsh", "pass")
    assert (bare.error_kind, bare.message, bare.located, bare.min_sicn, bare.elements, bare.seconds) == (
        None, "", [], None, 0, 0.0,
    )
    assert MemberResult("a", "fail", "k", "m").located is not MemberResult("b", "fail", "k", "m").located
    assert MemberResult("n", "crashed", "crash", "boom").located == []


# ---- Important 4 (cache key): prod's size rule is sampled widely ------------------------------------


def test_prod_config_samples_the_size_rule_across_decades(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.engines import fea_mesh_config

    before = GmshProd().config
    real = fea_mesh_config.options
    # a clamp that only bites on a very large part: invisible at diagonals 1 and 2, visible across decades
    monkeypatch.setattr(
        fea_mesh_config, "options", lambda d: {**real(d), "Mesh.MeshSizeMax": min(real(d)["Mesh.MeshSizeMax"], 5.0)}
    )
    assert GmshProd().config != before
