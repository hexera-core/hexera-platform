# Responsibility: Verify the mesher panel runner: one bad mesher (segfault, hang) is recorded and
#                 replaced and the remaining members still run; a kernel result becomes a MemberResult.
#                 A timeout never blames the file until it repeats at twice the budget; a gmsh crash is a
#                 vote against the file; the three gmsh members are three different algorithm pairs.
# Boundaries: hermetic - test-only kernel functions (HEXERA_TEST_KERNEL_FNS=1), no gmsh, no OCCT.
#             Real gmsh behaviour is the native tier (tests/native/test_native_repair_v4_panel.py).
from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path

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


_TIMED_OUT = _call(None, status="timeout", error="slow")


def test_one_retry_per_file_caps_the_panel_at_members_plus_two_budgets() -> None:
    """Three members that all hang: only the first retries (T, 2T), the others run once (T each), and a
    later member's single timeout is already the file's. 5T, not 9T."""
    pool = _ScriptedPool(*[_TIMED_OUT] * 4)
    results = run_panel(pool, "/x.step", [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 200, 100, 100]
    assert sum(pool.budgets) == (3 + 2) * 100
    assert [(r.status, r.error_kind) for r in results] == [("timeout", "timeout")] * 3
    assert [r.cause for r in results] == ["timeout_twice", "timeout_after_hang", "timeout_after_hang"]
    assert not any(panel.is_inconclusive(r) for r in results)  # a hung file is not excluded as 'unknown'


def test_a_spent_retry_that_passed_leaves_a_later_single_timeout_inconclusive() -> None:
    """The first member was merely slow (timeout, then pass): that is not evidence against the file, so a
    later member's single timeout is not promoted, and has no retry left."""
    pool = _ScriptedPool(_TIMED_OUT, _call(_PASS), _TIMED_OUT, _call(_PASS))
    results = run_panel(pool, "/x.step", [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 200, 100, 100]
    assert [r.status for r in results] == ["pass", "timeout", "pass"]
    assert results[1].error_kind == INFRASTRUCTURE_KIND and results[1].cause == "timeout_retry_spent"


def test_a_sigkill_shares_the_same_one_retry() -> None:
    pool = _ScriptedPool(_call(_SIGKILLED), _call(_SIGKILLED), _call(_SIGKILLED), _call(_SIGKILLED))
    results = run_panel(pool, "/x.step", [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 100, 100, 100]
    assert [r.cause for r in results] == ["sigkill_twice", "sigkill_after_doom", "sigkill_after_doom"]
    assert all(r.error_kind == panel.MESHER_CRASH_KIND for r in results)


def test_the_budget_is_per_file_not_per_process() -> None:
    pool = _ScriptedPool(*[_TIMED_OUT] * 4, *[_TIMED_OUT] * 4)
    for _ in range(2):  # a second file gets its own retry
        run_panel(pool, "/x.step", [GmshProd(), GmshDefault(), GmshAlt()], timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 200, 100, 100] * 2


def test_outside_run_panel_a_member_still_retries() -> None:
    pool = _ScriptedPool(_TIMED_OUT, _TIMED_OUT)
    GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 200]


# ---- Critical 3: gmsh runs as a child process, so its crash is the file's vote -----------------------


def _child_that(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setattr(mesh_kernel, "_child_argv", lambda _payload, _answer: [sys.executable, "-c", code])


def _mesh(timeout_s: float = 30) -> dict:
    return mesh_kernel.mesh_gmsh({"step": "/x.step", "profile": "default", "timeout_s": timeout_s})


_CRASH_NAMES = ("SIGILL", "SIGABRT", "SIGBUS", "SIGFPE", "SIGSEGV")


def test_the_crash_signal_set_is_exactly_the_five_by_name() -> None:
    assert panel.CRASH_SIGNALS == {int(getattr(signal, n)) for n in _CRASH_NAMES}  # SIGBUS is 7 on Linux, 10 on macOS


@pytest.mark.parametrize("name", _CRASH_NAMES)
def test_a_gmsh_crash_is_a_vote_against_the_file(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _child_that(monkeypatch, f"import os,signal; os.kill(os.getpid(), signal.{name})")
    value = _mesh()
    assert value["status"] == "crashed" and value["error_kind"] == panel.MESHER_CRASH_KIND
    assert value["cause"] == f"signal_{int(getattr(signal, name))}"
    result = result_from_call("gmsh_default", _call(value))
    assert result.status == "crashed" and result.error_kind == panel.MESHER_CRASH_KIND
    assert not panel.is_inconclusive(result)


def test_a_non_crash_signal_is_infrastructure_with_its_number() -> None:
    assert panel.signal_verdict(int(signal.SIGTERM)) == (INFRASTRUCTURE_KIND, f"signal_{int(signal.SIGTERM)}")
    assert panel.signal_verdict(int(signal.SIGKILL)) == (INFRASTRUCTURE_KIND, "sigkill")


def test_sigkill_once_is_inconclusive_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    _child_that(monkeypatch, "import os,signal; os.kill(os.getpid(), signal.SIGKILL)")
    result = result_from_call("gmsh_prod", _call(_mesh()), kills=1)
    assert result.status == "crashed" and result.error_kind == INFRASTRUCTURE_KIND and result.cause == "sigkill"
    assert panel.inconclusive_tally([result, MemberResult("ok", "pass")]) == {"gmsh_prod:sigkill": 1}


_SIGKILLED = {"status": "crashed", "error_kind": INFRASTRUCTURE_KIND, "cause": "sigkill",
              "message": "gmsh process killed by signal 9", "located": [], "min_sicn": None, "elements": 0}


def test_sigkill_is_retried_once_at_the_same_budget() -> None:
    pool = _ScriptedPool(_call(_SIGKILLED), _call(_PASS))
    result = GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 100]
    assert result.status == "pass" and "re-run after a SIGKILL" in result.message and result.seconds == 5.0


def test_sigkill_twice_is_a_vote_against_the_file_and_stays_auditable() -> None:
    pool = _ScriptedPool(_call(_SIGKILLED), _call(_SIGKILLED))
    result = GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert pool.budgets == [100, 100]
    assert (result.status, result.error_kind, result.cause) == ("crashed", panel.MESHER_CRASH_KIND, "sigkill_twice")
    assert not panel.is_inconclusive(result)


def test_a_kill_then_a_timeout_blames_nobody_and_the_tally_says_why() -> None:
    pool = _ScriptedPool(_call(_SIGKILLED), _call(None, status="timeout", error="slow"))
    result = GmshProd().run(pool, "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert panel.is_inconclusive(result) and result.cause == "timeout_once"


def test_mesh_gmsh_refuses_a_call_with_no_clock() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        mesh_kernel.mesh_gmsh({"step": "/x.step", "profile": "default"})


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
        setNumber=lambda k, v: values.__setitem__(k, v), getNumber=lambda k: values.get(k, 0.0)
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
    assert answer["algorithms"] == [0, 0]  # read back from gmsh, whatever it holds, not recomputed


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


_VERDICT_DIR = Path(panel.__file__).parent


def _literal_sites(text: str, literal: str) -> list[int]:
    return [n.lineno for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and n.value == literal]


def test_no_verdict_module_but_panel_spells_the_infrastructure_literal() -> None:
    """The marker is defined once. This reads the SOURCE, because comparing two modules' constants with `is`
    proves nothing: CPython interns identifier-like string constants, so a retyped copy is the same object."""
    assert INFRASTRUCTURE_KIND == "infrastructure"
    assert _literal_sites((_VERDICT_DIR / "panel.py").read_text(), "infrastructure"), "panel.py defines it"
    copies = {
        path.name: sites
        for path in sorted(_VERDICT_DIR.glob("*.py"))
        if path.name != "panel.py" and (sites := _literal_sites(path.read_text(), "infrastructure"))
    }
    assert not copies, f"retyped literal (import panel.INFRASTRUCTURE_KIND): {copies}"


_FOLLOW = """
import importlib, pkgutil, sys
from meshpipeline.cad.repair_v4.verdict import panel
fresh = "".join(["not-", "the-marker-", "any-more"])
panel.INFRASTRUCTURE_KIND = fresh            # BEFORE anything that imports it by name
pkg = importlib.import_module("meshpipeline.cad.repair_v4.verdict")
imported, copies = [], []
for info in pkgutil.iter_modules(pkg.__path__):
    if info.name == "panel":
        continue
    try:
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
    except ImportError:
        continue
    imported.append(info.name)
    copies += [f"{info.name}.{k}" for k, v in vars(mod).items() if isinstance(v, str) and v == "infrastructure"]
from meshpipeline.cad.repair_v4.verdict.gmsh_members import result_from_call
from meshpipeline.cad.repair_v4.worker_pool import CallResult
r = result_from_call("m", CallResult(ok=False, value=None, error="x", status="crashed", seconds=0.0))
print(repr((sorted(imported), copies, r.error_kind == fresh)))
"""


def test_every_verdict_module_reads_the_constant_it_does_not_copy_it() -> None:
    """Patch `panel.INFRASTRUCTURE_KIND` to a string no literal can equal, THEN import every verdict module
    (so each `from panel import ...` binds the patched one): no module may still hold the old string, and the
    gmsh members must produce the patched value. A retyped literal fails by value, interning or not."""
    done = subprocess.run([sys.executable, "-c", _FOLLOW], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    imported, copies, followed = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert {"gmsh_members", "mesh_kernel"} <= set(imported), imported
    assert copies == [] and followed is True, (copies, followed)


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
    assert bare.cause is None and bare.algorithms is None


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


def test_every_gmsh_member_meshes_single_threaded_with_a_fixed_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproducibility is pinned, not inherited. Lane D measured each panel form bit-identical across 5 fresh runs,
    but only because gmsh 4.15.2 defaults to one thread and seed 1; gmsh_prod's HXT 3D fill is multi-threaded and
    order-dependent once threads are on, so a later 'speed-up' that raised General.NumThreads would make a
    near-threshold verdict flip run to run. The child sets the values before it meshes and checks they took."""
    fake = _fake_gmsh(Exception("Could not open file"))
    seen: dict[str, float] = {}
    set_number = fake.option.setNumber
    fake.option.setNumber = lambda k, v: (seen.__setitem__(k, v), set_number(k, v))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gmsh", fake)
    mesh_child.run_in_process({"step": "/x.step", "profile": "prod"})
    assert {k: seen.get(k) for k in mesh_child.REPRODUCIBLE} == mesh_child.REPRODUCIBLE
    assert mesh_child.REPRODUCIBLE["General.NumThreads"] == 1 and mesh_child.REPRODUCIBLE["Mesh.MaxNumThreads3D"] == 1


# ---- what a retry hits after a first timeout is the clock's, not the file's ---------------------------------------

_FAIL = {"status": "fail", "error_kind": "other", "message": "gmsh stopped", "located": [], "min_sicn": None,
         "elements": 0}
_SEGV = {"status": "crashed", "error_kind": "gmsh_crash", "cause": "signal_11", "message": "killed by signal 11",
         "located": [], "min_sicn": None, "elements": 0}


def test_a_retry_after_a_timeout_is_one_outcome_whatever_it_hit() -> None:
    """Lane A, occt/bug32314_corrupted.stp, gmsh_alt alone, native x86, --cpus=1, 8 runs: the first attempt timed out
    every time, and the 2x retry ended timeout x2, fail/other x3, crashed/gmsh_crash (SIGSEGV) x3 - where gmsh was
    when the clock ran out. Recorded as is, one file's member status and kind flip run to run. Any non-pass that
    follows a timeout is now the timeout's outcome (the genuine kind, as if it had timed out twice); the original
    outcome stays in the message and the cause for triage."""
    seen = set()
    for second in (_call(None, status="timeout", error="slower"), _call(_FAIL), _call(_SEGV)):
        result = GmshAlt().run(_ScriptedPool(_TIMED_OUT, second), "/x.step", timeout_s=100)  # type: ignore[arg-type]
        seen.add((result.status, result.error_kind))
        assert not panel.is_inconclusive(result)
    assert seen == {("timeout", "timeout")}
    failed = GmshAlt().run(_ScriptedPool(_TIMED_OUT, _call(_FAIL)), "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert failed.cause == "after_timeout_fail" and "fail" in failed.message


def test_without_a_timeout_a_fail_and_a_crash_keep_their_own_kinds() -> None:
    assert GmshAlt().run(_ScriptedPool(_call(_FAIL)), "/x.step", timeout_s=100).error_kind == "other"  # type: ignore[arg-type]
    crash = GmshAlt().run(_ScriptedPool(_call(_SEGV)), "/x.step", timeout_s=100)  # type: ignore[arg-type]
    assert (crash.status, crash.error_kind) == ("crashed", "gmsh_crash")
