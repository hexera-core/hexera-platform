# Responsibility: Verify a timed-out guarded command leaves no descendant process running.
# Boundaries: the observable outcome only; how teardown is achieved is the runner's to change.
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    not Path("/proc").is_dir(), reason="needs Linux /proc for process-identity checks"
)

#: THE SUPERVISOR. It runs the real production call in its OWN session, so that if a future
#: implementation signals a process group, the signal can never reach pytest or the developer's
#: shell. It reports the exact identities it created, and pytest judges the outcome from those.
#:
#: Readiness is explicit: the child writes its own and its grandchild's identity, then the
#: grandchild announces itself, and only then does the supervisor let the timeout elapse. Nothing
#: here sleeps to hope a process has started - a run that cannot confirm readiness fails loudly
#: instead of passing because it observed a process that never existed.
_SUPERVISOR = r'''
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from meshpipeline.sandbox.safe_exec import run_guarded

work = Path(sys.argv[2])
ready = work / "ready"            # the grandchild writes here once it is genuinely running
pids  = work / "pids.json"

# A child that spawns a grandchild, records BOTH identities, announces readiness, then blocks.
# The grandchild deliberately stays in the child's process group, which is how a real mesher's
# mpirun/solver children behave.
script = f"""
sleep 600 &
gc=$!
printf '%s\\n%s\\n' "$$" "$gc" > {pids}
# readiness is the grandchild's own liveness, not merely that bash reached this line
while ! kill -0 $gc 2>/dev/null; do sleep 0.01; done
touch {ready}
wait
"""

started = time.time()
try:
    run_guarded(["bash", "-lc", script], timeout=6)
    timed_out = False
except subprocess.TimeoutExpired:
    timed_out = True

def state(pid):
    try:
        d = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    return d[d.rindex(")") + 2]

def starttime(pid):
    # FIELD 22 of /proc/<pid>/stat: the boot-relative start time. Paired with the pid it is a
    # stable identity, so a recycled pid cannot be mistaken for the process we launched.
    try:
        d = Path(f"/proc/{pid}/stat").read_text()
        return d[d.rindex(")") + 2:].split()[19]
    except Exception:
        return None

recorded = {}
if pids.exists():
    _lines = [ln for ln in pids.read_text().split() if ln.isdigit()]
    if len(_lines) == 2:
        recorded = {"child": int(_lines[0]), "grandchild": int(_lines[1])}
out = {
    "timed_out": timed_out,
    "seconds": round(time.time() - started, 2),
    "ready_observed": ready.exists(),
    "supervisor_pid": os.getpid(),
    "supervisor_sid": os.getsid(0),
    "recorded": recorded,
    "after": {},
}
for role, pid in recorded.items():
    out["after"][role] = {"pid": pid, "state": state(pid), "starttime": starttime(pid)}
print(json.dumps(out))
'''


def _identity(pid: int) -> tuple[str | None, str | None]:
    # (state, starttime). Together these distinguish running, exited, zombie and a recycled pid.
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None, None
    tail = data[data.rindex(")") + 2:]
    return tail[0], tail.split()[19]


def _alive(pid: int, born: str | None) -> bool:
    # Alive means: still present, not a zombie, and the SAME process we launched. A zombie has
    # already died, and a different start time means the pid was recycled by something unrelated.
    state, now = _identity(pid)
    if state is None or state in ("Z", "X"):
        return False
    return born is None or now == born


@pytest.fixture()
def supervisor(tmp_path):
    # Every process this test creates is recorded here, so teardown reaches exactly those and
    # nothing else. No name matching, no wildcard, no process-group signal from the test.
    created: list[tuple[int, str | None]] = []
    yield tmp_path, created
    for pid, born in created:
        if _alive(pid, born):
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


def _run(tmp_path) -> dict:
    script = tmp_path / "supervisor.py"
    script.write_text(_SUPERVISOR)
    work = tmp_path / "work"
    work.mkdir()
    done = subprocess.run(
        [sys.executable, str(script), str(REPO / "src"), str(work)],
        capture_output=True, text=True, timeout=180,
        # ITS OWN SESSION. os.setsid in the child means the scenario's process group can never be
        # this pytest process's group, whatever the runner does internally.
        preexec_fn=os.setsid,
    )
    assert done.returncode == 0, f"supervisor failed:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_timed_out_command_leaves_no_descendant_running(supervisor):
    tmp_path, created = supervisor
    result = _run(tmp_path)

    # the scenario really happened, and really timed out
    assert result["ready_observed"], "the grandchild never confirmed it was running"
    assert result["timed_out"], "the command did not time out, so teardown was never exercised"

    # PROMPTNESS IS PART OF THE CONTRACT, and it is what makes the survivor observable at all.
    # A runner that kills only the direct child then waits for its output blocks until the runaway
    # descendant exits by itself - here that would be ten minutes. The caller is held hostage by
    # exactly the process the timeout existed to stop, and any check made afterwards sees a tidy
    # tree because the leak has already resolved itself.
    assert result["seconds"] < 60, (
        f"run_guarded took {result['seconds']}s for a 6s timeout: it waited on the descendant "
        "instead of tearing it down")
    assert {"child", "grandchild"} <= set(result["recorded"]), result["recorded"]
    assert result["supervisor_sid"] != os.getsid(0), "the scenario shared pytest's session"

    for role in ("child", "grandchild"):
        seen = result["after"][role]
        created.append((seen["pid"], seen["starttime"]))

    survivors = [role for role in ("child", "grandchild")
                 if _alive(result["after"][role]["pid"], result["after"][role]["starttime"])]
    assert survivors == [], (
        f"{', '.join(survivors)} survived the timeout: {result['after']}. A timed-out command must "
        "leave nothing running, or a mesher's solver keeps consuming the machine after the job "
        "that owned it is gone.")


def test_the_test_runner_and_its_shell_are_untouched(supervisor):
    # The teardown must be scoped to what the command created. If it ever widened to a process
    # group the runner shares, this process would be among the casualties.
    tmp_path, created = supervisor
    before = os.getpid(), os.getppid()
    result = _run(tmp_path)
    for role in ("child", "grandchild"):
        seen = result["after"][role]
        created.append((seen["pid"], seen["starttime"]))

    assert (os.getpid(), os.getppid()) == before, "the run replaced pytest's own process identity"
    assert _identity(os.getpid())[0] not in (None, "Z"), "pytest itself was signalled"
    assert _identity(os.getppid())[0] not in (None, "Z"), "pytest's parent was signalled"
