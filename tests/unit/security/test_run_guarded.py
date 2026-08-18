# Responsibility: Verify the guarded subprocess helper captures output and returns a completed process.
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from meshpipeline.sandbox.safe_exec import run_guarded

pytestmark = pytest.mark.skipif(
    not Path("/proc").is_dir(), reason="needs Linux /proc + process groups"
)


def _proc_state(pid: int) -> str | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return data[data.rindex(")") + 2]


def test_run_guarded_captures_output_and_returns_completedprocess():
    out = run_guarded(["bash", "-lc", "echo out; echo err >&2; exit 3"],
                      capture_output=True, text=True, timeout=10)
    assert isinstance(out, subprocess.CompletedProcess)
    assert out.returncode == 3
    assert "out" in out.stdout and "err" in out.stderr


def test_run_guarded_timeout_reraises_with_output_attributes():
    # the gmsh runner reads exc.stdout / exc.stderr on timeout - they must be
    # attached (as subprocess.run does), not missing.
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        run_guarded(["bash", "-lc", "echo hi; sleep 30"],
                    capture_output=True, text=True, timeout=1)
    assert hasattr(ei.value, "stdout") and hasattr(ei.value, "stderr")


def test_run_guarded_kills_whole_process_group_on_timeout(tmp_path):
    pidfile = tmp_path / "gc.pid"
    # Background grandchild stays in bash's process group (no setsid - exactly how
    # a real OpenFOAM/mpirun child behaves); record its pid; bash blocks on it.
    cmd = f"sleep 60 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_guarded(["bash", "-lc", cmd], timeout=1)

    gc_pid = int(pidfile.read_text().strip())
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _proc_state(gc_pid) in (None, "Z", "X"):   # gone or zombie == killed
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(gc_pid, 9)                         # clean up the leak before failing
        except ProcessLookupError:
            pass
        pytest.fail(f"grandchild pid={gc_pid} survived run_guarded timeout (orphan leak)")
