# Responsibility: Verify the Netgen panel member's contracts with the panel, hermetically: a JSON-able
#                 `config` the verdict cache can key on, the shared per-file retry budget, and the
#                 infrastructure / vote-against-the-file split with a cause on every inconclusive result.
# Boundaries: hermetic - a scripted pool stands in for the worker, no Netgen, no gmsh. Real Netgen is
#             tests/native/test_native_repair_v4_netgen.py.
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v4.verdict import mesh_child, netgen_member, panel
from meshpipeline.cad.repair_v4.verdict.cache import panel_config_hash
from meshpipeline.cad.repair_v4.verdict.gmsh_members import GmshAlt, GmshDefault, GmshProd
from meshpipeline.cad.repair_v4.verdict.netgen_member import NETGEN_PIN, NetgenOCC
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND, inconclusive_tally, run_panel
from meshpipeline.cad.repair_v4.worker_pool import CallResult

PASS = {"ok": True, "tets": 10, "min_sicn": 0.3, "solids": 1}
STAGE_TIMEOUT = {"ok": False, "stage": "timeout", "error": "Netgen exceeded 1s"}
SIGKILLED = {"ok": False, "stage": "crash", "returncode": -9, "error": "killed"}
SEGV = {"ok": False, "stage": "crash", "returncode": -11, "error": "segfault"}
ABSENT = {"ok": False, "stage": "unavailable", "error": "no netgen interpreter"}
POOL_TIMEOUT = CallResult(ok=False, value=None, error="worker timed out", status="timeout", seconds=1.0)
POOL_CRASH = CallResult(ok=False, value=None, error="worker died", status="crashed", seconds=1.0)


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=1.0)


class ScriptedPool:
    """Answers each call from a script, in order, and records `(fn, payload, timeout_s)`."""

    def __init__(self, *answers: CallResult | dict) -> None:
        self.answers = [a if isinstance(a, CallResult) else _ok(a) for a in answers]
        self.calls: list[tuple[str, dict, float]] = []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.calls.append((fn, payload, timeout_s))
        return self.answers.pop(0)


def _run(*answers: CallResult | dict, timeout_s: float = 100.0):
    pool = ScriptedPool(*answers)
    return NetgenOCC().run(pool, "/x.step", timeout_s=timeout_s), pool  # type: ignore[arg-type]


# ---- the full tier holds a non-gmsh mesher ---------------------------------------------------------


def test_the_full_tier_contains_a_non_gmsh_member_and_the_cheap_tier_does_not() -> None:
    from meshpipeline.cad.repair_v4.verdict import assemble

    # By module, not by count: a fourth gmsh profile must not be able to stand in for Netgen.
    full = assemble.default_members("full")
    non_gmsh = [m for m in full if "gmsh" not in type(m).__module__.rsplit(".", 1)[-1]]
    assert [type(m) for m in non_gmsh] == [NetgenOCC]
    assert {type(m).__module__ for m in full} >= {
        "meshpipeline.cad.repair_v4.verdict.gmsh_members",
        "meshpipeline.cad.repair_v4.verdict.netgen_member",
    }
    assert [type(m) for m in full] == [GmshProd, GmshDefault, GmshAlt, NetgenOCC]
    assert [type(m) for m in assemble.default_members("cheap")] == [GmshDefault, GmshProd]
    # every member carries a config, so the cache can key the whole panel
    hash_a = panel_config_hash(full, 300.0, {"gmsh": "1"})
    assert len({json.dumps(m.config, sort_keys=True) for m in full}) == len(full) == 4
    assert hash_a == panel_config_hash(assemble.default_members("full"), 300.0, {"gmsh": "1"})


# ---- the argv reaches Netgen --------------------------------------------------------------------------


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    from meshpipeline.cad.repair_v4.verdict import netgen_member as nm

    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout='NETGEN_RESULT {"ok": true}\n', stderr="")

    monkeypatch.setattr(nm, "netgen_python", lambda: "/fake/python")
    monkeypatch.setattr(nm.subprocess, "run", fake_run)
    nm._run_netgen("/x.step", "/out.msh", 5.0)
    return seen


def test_the_size_divisor_and_glue_are_passed_to_netgen_not_merely_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_argv(monkeypatch)
    assert seen[0][-2:] == ["10.0", "glue"]
    assert seen[0][2:4] == ["/x.step", "/out.msh"]


def test_turning_glue_off_and_resizing_reach_netgen_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4.verdict import netgen_member as nm

    monkeypatch.setattr(nm, "GLUE_TOUCHING_SOLIDS", False)
    monkeypatch.setattr(nm, "MAXH_DIVISOR", 25.0)
    assert _capture_argv(monkeypatch)[0][-2:] == ["25.0", "noglue"]


# ---- config ------------------------------------------------------------------------------------------


def test_netgen_config_is_json_able_and_covers_what_decides_its_answer() -> None:
    config = NetgenOCC().config
    assert json.loads(json.dumps(config, sort_keys=True)) == config
    assert config["min_sicn"] == mesh_child.MIN_SICN
    assert config["versions"] == NETGEN_PIN and config["glue"] is True
    assert config["sizing"]["maxh_divisor"] == 10.0


def test_the_four_members_have_distinct_configs_and_hash_distinctly() -> None:
    members = [GmshProd(), GmshDefault(), GmshAlt(), NetgenOCC()]
    dumped = [json.dumps(m.config, sort_keys=True) for m in members]
    assert len(set(dumped)) == 4
    versions = {"gmsh": "1", "netgen": "absent", "ocp": "1"}
    full = panel_config_hash(members, 300.0, versions)
    assert panel_config_hash(members[:3], 300.0, versions) != full  # Netgen is in the key
    assert panel_config_hash(list(reversed(members)), 300.0, versions) == full


@pytest.mark.parametrize("field", ["versions", "glue", "min_sicn", "sizing"])
def test_changing_any_netgen_setting_changes_the_panel_hash(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    from meshpipeline.cad.repair_v4.verdict import netgen_member as nm

    versions = {"gmsh": "1", "netgen": "absent", "ocp": "1"}
    before = panel_config_hash([NetgenOCC()], 300.0, versions)
    patch = {
        "versions": ("NETGEN_PIN", {"netgen-mesher": "9"}),
        "glue": ("GLUE_TOUCHING_SOLIDS", False),
        "min_sicn": ("MIN_SICN", 0.05),
        "sizing": ("MAXH_DIVISOR", 20.0),
    }[field]
    monkeypatch.setattr(nm, *patch)
    assert panel_config_hash([NetgenOCC()], 300.0, versions) != before


def test_netgen_config_is_stable_across_processes() -> None:
    code = (
        "import json;from meshpipeline.cad.repair_v4.verdict.netgen_member import NetgenOCC;"
        "print(json.dumps(NetgenOCC().config, sort_keys=True))"
    )
    outs = [
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
        for _ in range(2)
    ]
    assert outs[0] == outs[1] == json.dumps(NetgenOCC().config, sort_keys=True)


def test_the_pin_is_the_one_requirements_netgen_txt_installs() -> None:
    text = (Path(__file__).resolve().parents[4] / "requirements" / "netgen.txt").read_text()
    pinned = dict(re.findall(r"^(netgen-[a-z]+)==(\S+)", text, re.M))
    assert pinned == NETGEN_PIN


# ---- the shared retry budget -------------------------------------------------------------------------


def test_run_goes_through_the_shared_retry_with_the_shared_inner_clock() -> None:
    result, pool = _run(POOL_TIMEOUT, PASS, timeout_s=100.0)
    assert result.status == "pass"
    assert [c[2] for c in pool.calls] == [100.0, 200.0]  # one retry at twice the budget
    assert [c[1]["timeout_s"] for c in pool.calls] == [panel.inner_timeout_s(100.0), panel.inner_timeout_s(200.0)]


def test_a_single_pool_timeout_is_infrastructure_and_two_are_the_files() -> None:
    once, _ = _run(POOL_TIMEOUT, PASS)
    assert once.status == "pass"
    twice, pool = _run(POOL_TIMEOUT, POOL_TIMEOUT)
    assert (twice.status, twice.error_kind, twice.cause) == ("timeout", "netgen_timeout", "timeout_twice")
    assert len(pool.calls) == 2


def test_netgens_own_subprocess_timeout_follows_the_same_ruling() -> None:
    result, pool = _run(STAGE_TIMEOUT, PASS)
    assert result.status == "pass" and len(pool.calls) == 2
    twice, _ = _run(STAGE_TIMEOUT, STAGE_TIMEOUT)
    assert (twice.status, twice.error_kind, twice.cause) == ("timeout", "netgen_timeout", "timeout_twice")
    # a single timeout that the retry could not run (budget spent elsewhere) is infrastructure, not the file
    with panel.retry_budget(0):
        spent, pool = _run(STAGE_TIMEOUT)
    assert (spent.status, spent.error_kind, spent.cause) == ("timeout", INFRASTRUCTURE_KIND, "timeout_retry_spent")
    assert len(pool.calls) == 1


def test_a_single_pool_timeout_with_the_retry_spent_is_infrastructure_on_the_run_branch() -> None:
    # `run`'s own timeout branch (a pool-level timeout), not `member_result`: with no retry left, ONE
    # timeout is not the file's. A constant "netgen_timeout" there would be the fail-open.
    with panel.retry_budget(0):
        spent, pool = _run(POOL_TIMEOUT)
    assert (spent.status, spent.error_kind, spent.cause) == ("timeout", INFRASTRUCTURE_KIND, "timeout_retry_spent")
    assert len(pool.calls) == 1
    assert INFRASTRUCTURE_KIND == "infrastructure"


def test_a_sigterm_is_infrastructure() -> None:
    result, _ = _run({"ok": False, "stage": "crash", "returncode": -15, "error": "terminated"})
    assert (result.status, result.error_kind, result.cause) == ("crashed", "infrastructure", "signal_15")


def test_a_sigkill_once_is_infrastructure_twice_is_the_files() -> None:
    assert _run(SIGKILLED, PASS)[0].status == "pass"
    twice, pool = _run(SIGKILLED, SIGKILLED)
    # the literal, not the constant the implementation reads: this is Netgen, so it is NOT `gmsh_crash`
    assert (twice.status, twice.error_kind, twice.cause) == ("crashed", "netgen_crash", "sigkill_twice")
    assert [c[2] for c in pool.calls] == [100.0, 100.0]  # a kill is re-run at the same budget
    with panel.retry_budget(0):
        spent, _ = _run(SIGKILLED)
    assert (spent.error_kind, spent.cause) == (INFRASTRUCTURE_KIND, "sigkill_retry_spent")


def test_the_file_gets_one_retry_across_the_whole_panel_netgen_adds_none() -> None:
    class First:
        name, config = "first", {"m": 1}

        def run(self, pool, step_path, *, timeout_s):  # noqa: ANN001
            return panel.MemberResult("first", "pass", min_sicn=0.5, elements=1) if pool.call(
                "x", {}, timeout_s=timeout_s).ok else panel.MemberResult("first", "fail")

    pool = ScriptedPool(PASS, POOL_TIMEOUT, POOL_TIMEOUT)
    # Netgen's first attempt times out, spending the file's one retry; its second times out again.
    results = run_panel(pool, "/x.step", [First(), NetgenOCC()], timeout_s=100.0)  # type: ignore[arg-type]
    assert len(pool.calls) == 3
    assert (results[1].error_kind, results[1].cause) == ("netgen_timeout", "timeout_twice")
    # and a later member finds the budget spent and the file doomed: ONE timeout is already the file's
    pool = ScriptedPool(POOL_TIMEOUT, POOL_TIMEOUT, POOL_TIMEOUT)
    results = run_panel(pool, "/x.step", [NetgenOCC(), NetgenOCC()], timeout_s=100.0)  # type: ignore[arg-type]
    assert len(pool.calls) == 3 and results[1].error_kind == "netgen_timeout"
    assert results[1].cause == "timeout_after_hang"


# ---- infrastructure vs the file, and the cause that makes the tally cover Netgen -----------------------


@pytest.mark.parametrize(
    ("answer", "status", "kind", "cause"),
    [
        (POOL_CRASH, "crashed", INFRASTRUCTURE_KIND, "pool_worker"),
        (ABSENT, "crashed", INFRASTRUCTURE_KIND, "toolchain_absent"),
        ({"ok": False, "stage": "import", "error": "no netgen"}, "crashed", INFRASTRUCTURE_KIND, "toolchain_absent"),
        (SEGV, "crashed", "netgen_crash", "signal_11"),
        ({"ok": False, "stage": "crash", "returncode": 1, "error": "x"}, "crashed", "netgen_crash", "exit_1"),
    ],
)
def test_causes_and_kinds(answer, status, kind, cause) -> None:  # noqa: ANN001
    result, _ = _run(answer)
    assert (result.status, result.error_kind, result.cause) == (status, kind, cause)


def test_the_tally_counts_netgens_infrastructure_results_and_not_its_findings() -> None:
    infra, _ = _run(ABSENT)
    crash, _ = _run(SEGV)
    assert inconclusive_tally([infra, crash]) == {"netgen_occ:toolchain_absent": 1}


def test_a_pass_and_a_low_quality_fail_use_the_one_floor() -> None:
    ok, _ = _run(PASS)
    assert (ok.status, ok.min_sicn, ok.elements) == ("pass", 0.3, 10)
    low, _ = _run({**PASS, "min_sicn": mesh_child.MIN_SICN / 2})
    assert (low.status, low.error_kind) == ("fail", "low_quality")


def test_a_netgen_retry_after_a_timeout_is_one_outcome_whatever_it_hit() -> None:
    """The same rule as the gmsh members (Lane A's bug32314 measurement): after a first timeout, a retry that
    crashed or timed out is one outcome, the genuine netgen timeout."""
    outcomes = {(r.status, r.error_kind) for r, _ in (_run(POOL_TIMEOUT, POOL_TIMEOUT), _run(POOL_TIMEOUT, SEGV),
                                                     _run(STAGE_TIMEOUT, SEGV))}
    assert outcomes == {("timeout", "netgen_timeout")}
    crash, _ = _run(SEGV)  # no timeout before it: the crash is the file's own answer, kind kept
    assert crash.status == "crashed" and crash.error_kind != "netgen_timeout"


def test_a_netgen_failure_naming_a_face_is_located_at_that_faces_box() -> None:
    """Netgen names the face it could not triangulate ("OCC-Triangulation could not be built for face 1"); the script
    sends that face's box, and the member names it by the same box fingerprint gmsh's member uses, which `find_sites`
    translates to the live face (lane C5)."""
    from meshpipeline.cad.repair.identity import fingerprint_of, quantum_for

    rec = {"ok": False, "stage": "mesh", "error": "NgException: OCC-Triangulation could not be built for face 1",
           "face_box": [0.0, 0.0, 0.0, 10.0, 10.0, 0.0], "face": 1, "diag": 100.0}
    r = netgen_member.member_result("netgen_occ", rec, 1.0)
    assert r.status == "fail"
    (loc,) = r.located
    assert loc.entity == fingerprint_of("face", {"bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 0.0]},
                                        quantum=quantum_for(100.0))
    assert loc.mesher_tag == "netgen face 1" and loc.xyz is None


def test_a_netgen_failure_naming_no_face_stays_unlocated() -> None:
    r = netgen_member.member_result("netgen_occ", {"ok": False, "stage": "mesh", "error": "NgException: Meshing failed!"}, 1.0)
    assert r.located == []
