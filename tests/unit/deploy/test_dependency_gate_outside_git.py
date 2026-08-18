# Responsibility: Prove `make dependencies` reports its own limits instead of failing as a traceback.
# Boundaries: how the gate behaves when it cannot run; what it decides when it can is asserted elsewhere.
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GATE = "devtools/quality/check_dependency_drift.py"

#: `make dependencies` returns 1 for real drift. A run that reached no verdict must NOT look like
#: that: a build reading exit codes has to be able to tell "clean" from "never checked".
EXIT_DRIFT = 1
EXIT_CANNOT_CHECK = 2


@pytest.fixture(scope="module")
def outside_git(tmp_path_factory) -> Path:
    # A directory copy, which is what an unpacked archive or a COPY'd image layer looks like: every
    # file is there and none of the tracking is. Nothing here is a repository, real or invented.
    root = tmp_path_factory.mktemp("nogit")
    dst = root / GATE
    dst.parent.mkdir(parents=True)
    shutil.copy2(REPO / GATE, dst)

    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(root),
                         capture_output=True, text=True)
    assert top.returncode != 0, (
        f"this fixture sits inside a Git checkout ({top.stdout.strip()}), so it cannot prove "
        "anything about running outside one")
    return root


def _run(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(cwd / GATE)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


# 1-4. the reported failure

def test_it_fails_rather_than_reporting_a_clean_result(outside_git):
    done = _run(outside_git)
    assert done.returncode != 0, (
        "the gate reported success without reading a single file. An empty scan passes every rule "
        "it has, which is worse than failing.")


def test_it_does_not_fail_as_a_traceback(outside_git):
    done = _run(outside_git)
    combined = done.stdout + done.stderr
    for noise in ("Traceback (most recent call last)", "CalledProcessError", 'File "'):
        assert noise not in combined, (
            f"the operator is shown {noise!r} from a subprocess they never invoked:\n{combined}")


def test_it_names_the_reason_it_could_not_run(outside_git):
    out = _run(outside_git).stdout
    assert "CANNOT CHECK" in out, f"the outcome is not distinguishable from a verdict:\n{out}"
    assert "git" in out.lower(), f"the message does not say what was unavailable:\n{out}"


def test_it_says_what_would_make_it_work(outside_git):
    out = _run(outside_git).stdout.lower()
    assert "git clone" in out or "checkout" in out, (
        f"the message diagnoses without telling the operator what to do:\n{out}")


# 5. and none of that weakened the gate where it does apply

def test_the_gate_still_reaches_a_real_verdict_in_the_checkout():
    done = subprocess.run([sys.executable, str(REPO / GATE)], cwd=str(REPO),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"the tracked tree no longer passes its own gate:\n{done.stdout}"
    assert done.stdout.startswith("OK:"), done.stdout
    assert "CANNOT CHECK" not in done.stdout


def test_the_two_failures_are_told_apart_by_exit_code(outside_git):
    # Distinct codes, because `make dependencies` is a gate: a caller that treats "could not check"
    # as "no drift" would ship exactly the drift this exists to catch.
    assert _run(outside_git).returncode == EXIT_CANNOT_CHECK
    assert EXIT_CANNOT_CHECK != EXIT_DRIFT
