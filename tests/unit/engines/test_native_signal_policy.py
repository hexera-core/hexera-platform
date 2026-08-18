# Responsibility: Verify a signalled child, an ordinary failure, a timeout and a cancellation stay distinct outcomes.
from __future__ import annotations

import ast
import subprocess
import sys

import pytest

from meshpipeline.sandbox.safe_exec import (
    NativeOutcome,
    command_identity,
    describe_native_result,
    run_guarded,
)

#: a child that raises the given signal against itself, and one that segfaults for real
_SELF_SIGNAL = "import os, signal, sys; os.kill(os.getpid(), getattr(signal, sys.argv[1]))"
_SEGFAULT = "import ctypes; ctypes.string_at(0)"


def _run(script: str, *args: str):
    return run_guarded([sys.executable, "-c", script, *args],
                       capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGKILL"])
def test_a_signalled_child_is_classified_as_signalled(signame):
    proc = _run(_SELF_SIGNAL, signame)
    result = describe_native_result(returncode=proc.returncode, args=proc.args,
                                    stage="mesh", output=proc.stderr or "")
    assert result["outcome"] == NativeOutcome.signalled.value
    assert result["signalled"] is True
    assert result["signal"] == signame
    assert result["rc"] < 0
    assert signame in result["log_tail"]
    assert "[NATIVE SIGNAL]" in result["log_tail"]


def test_a_real_segfault_is_classified_as_sigsegv():
    proc = _run(_SEGFAULT)
    if proc.returncode >= 0:                       # a hardened libc may trap it differently
        pytest.skip(f"this platform did not deliver SIGSEGV (rc={proc.returncode})")
    result = describe_native_result(returncode=proc.returncode, args=proc.args,
                                    stage="volume mesh", output="")
    assert result["signal"] == "SIGSEGV"
    assert result["outcome"] == NativeOutcome.signalled.value
    assert "volume mesh" in result["log_tail"]


def test_an_ordinary_failure_is_not_a_signal():
    proc = _run("import sys; sys.exit(3)")
    result = describe_native_result(returncode=proc.returncode, args=proc.args, stage="mesh")
    assert result["rc"] == 3
    assert result["outcome"] == NativeOutcome.failed.value
    assert result["signalled"] is False
    assert result["signal"] == ""
    assert "[NATIVE SIGNAL]" not in result["log_tail"]


def test_success_is_its_own_outcome():
    proc = _run("print('done')")
    result = describe_native_result(returncode=proc.returncode, args=proc.args, stage="mesh")
    assert result["outcome"] == NativeOutcome.ok.value
    assert (result["signalled"], result["timed_out"], result["cancelled"]) == (False, False, False)


def test_timeout_and_cancellation_stay_distinct_from_both():
    timed = describe_native_result(returncode=-1, args=["vmtk"], stage="mesh",
                                   outcome=NativeOutcome.timed_out)
    cancelled = describe_native_result(returncode=-1, args=["vmtk"], stage="mesh",
                                       outcome=NativeOutcome.cancelled)
    assert timed["timed_out"] is True and timed["signalled"] is False
    assert cancelled["cancelled"] is True and cancelled["signalled"] is False
    assert timed["outcome"] != cancelled["outcome"]
    for r in (timed, cancelled):
        assert "[NATIVE SIGNAL]" not in r["log_tail"]


def test_a_real_timeout_through_run_guarded_is_not_a_signal():
    with pytest.raises(subprocess.TimeoutExpired):
        run_guarded([sys.executable, "-c", "import time; time.sleep(30)"],
                    capture_output=True, text=True, timeout=1)


def test_the_operator_record_names_the_tool_and_never_where_it_lived():
    result = describe_native_result(
        returncode=-11,
        args=["bash", "-lc", "source /opt/openfoam/etc/bashrc >/dev/null && blockMesh -case /work/j1"],
        stage="blockMesh", output="Creating block mesh\nsegfault imminent")
    assert result["command"] == "bash:blockMesh"
    tail = result["log_tail"]
    assert "SIGSEGV" in tail and "blockMesh" in tail
    for secret in ("/opt/openfoam", "/work/j1", "bashrc", "-case"):
        assert secret not in tail, f"{secret!r} leaked into the operator record"


@pytest.mark.parametrize(("args", "expected"), [
    (["/opt/vmtk/bin/vmtk", "vmtkcenterlines", "-ifile", "/work/x/lumen.vtp"], "vmtk"),
    (["gmsh", "/work/a/geometry.step", "-3"], "gmsh"),
    (["bash", "-lc", "source /x/bashrc >/dev/null && cartesianMesh"], "bash:cartesianMesh"),
    ([], "unknown"),
])
def test_command_identity_is_the_tool_not_the_deployment(args, expected):
    assert command_identity(args) == expected


def test_every_engine_runner_returns_the_shared_contract():
    from meshpipeline.engines.dispatch import engine_runners

    runners = engine_runners()
    assert set(runners) == {"cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"}, runners
    shared = {"rc", "timed_out", "signalled", "outcome", "signal", "command", "stage", "log_tail"}
    for engine in sorted(runners):
        # The seam must be CALLED, not merely imported. A substring check passed a module that
        # imported `describe_native_result` at the top and then hand-built its own result dict -
        # which is exactly the divergence this rule exists to prevent, control M14).
        calls = [n for n in ast.walk(ast.parse(_runner_source(engine)))
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("describe_native_result")]
        assert calls, (
            f"{engine} builds its own process result instead of calling the shared seam")
    # the seam itself supplies every key the contract promises
    assert shared <= set(describe_native_result(returncode=0, args=["x"]))


# engine -> the module that actually launches native commands. moved multiregion's
# command sequencing into its own `native` module and did the same for cfMesh; the
#: shared-seam rule follows the code that runs processes, not a filename convention.
_NATIVE_MODULE = {
    "cfmesh": "meshpipeline.engines.cfmesh.native",
    "snappy": "meshpipeline.engines.snappy.native",
    "gmsh": "meshpipeline.engines.gmsh.gmsh_runner",
    "vmtk": "meshpipeline.engines.vmtk.vmtk_runner",
    "snappy_multiregion": "meshpipeline.engines.snappy_multiregion.native",
}


def _runner_source(engine: str) -> str:
    import importlib
    import inspect
    return inspect.getsource(importlib.import_module(_NATIVE_MODULE[engine]))
