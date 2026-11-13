# Responsibility: Verify the dev-fast release profile by running the real validate.sh against fake build tools.
# Boundaries: nothing is built or pulled; a copy of the gate runs in a throwaway repository, so its REPO_ROOT is that one.
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
FAST_PROFILE_CHECK = "development deploy fast profile"

#: The gate declares associative arrays and reads sizes with GNU stat, so stock macOS cannot run it.
requires_gate_shell = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "declare -A x; stat -c%s /"], capture_output=True).returncode != 0,
    reason="validate.sh needs bash 4+ and GNU stat",
)

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >>"${FAKE_LOG}"
case "$1" in
  build)
    [ "$3" != "${FAKE_FAIL_TARGET:-}" ] || exit 1 ;;
  image)
    echo "sha256:$(printf '%064d' 1)" ;;
  run)
    echo '{"product_version": "1.0.0", "final_result": 1, "pipeline_state": 1}' ;;
esac
exit 0
"""

#: Stands in for the release interpreter: only `build` and `venv` are faked, so the gate's inline
#: wheel inspection and record.py run for real.
FAKE_PYTHON = """#!/bin/sh
case "$1 $2" in
  "-c import build") exit 0 ;;
  "-m build")
    while [ "$1" != "--outdir" ]; do shift; done
    mkdir -p "$2"
    top=meshpipeline
    [ -z "${FAKE_FOREIGN_WHEEL:-}" ] || top=foreign
    exec "${REAL_PYTHON}" -c '
import sys, zipfile
with zipfile.ZipFile(sys.argv[1] + "/meshpipeline-1.0.0-py3-none-any.whl", "w") as z:
    z.writestr(sys.argv[2] + "/prompts/a.txt", "x")
' "$2" "${top}" ;;
  "-m venv")
    mkdir -p "$3/bin"
    printf '#!/bin/sh\\nexit 0\\n' >"$3/bin/pip"
    printf '#!/bin/sh\\ncat >/dev/null\\necho 1.0.0\\n' >"$3/bin/python"
    chmod +x "$3/bin/pip" "$3/bin/python"
    exit 0 ;;
esac
exec "${REAL_PYTHON}" "$@"
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def gate(tmp_path) -> dict:
    root = tmp_path / "repo"
    (root / "devtools" / "release").mkdir(parents=True)
    for name in ("validate.sh", "record.py"):
        shutil.copy(REPO / "devtools" / "release" / name, root / "devtools" / "release" / name)
    (root / "src" / "meshpipeline").mkdir(parents=True)
    (root / "src" / "meshpipeline" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (root / "requirements").mkdir()
    (root / "requirements" / "constraints.txt").write_text("")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "docker", FAKE_DOCKER)
    _executable(bin_dir / "gate-python", FAKE_PYTHON)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    log = tmp_path / "docker.log"
    log.touch()
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log),
           "REAL_PYTHON": sys.executable, "RELEASE_PYTHON": str(bin_dir / "gate-python"),
           "TMPDIR": str(scratch)}
    for name in ("RELEASE_VALIDATION_PROFILE", "RELEASE_ALLOW_INCOMPLETE", "RELEASE_BUILD_CACHE"):
        env.pop(name, None)
    return {"root": root, "env": env, "log": log}


def _validate(gate: dict, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "devtools/release/validate.sh"], cwd=gate["root"],
                          env={**gate["env"], **env}, capture_output=True, text=True, timeout=120)


def _record(gate: dict) -> dict:
    return json.loads((gate["root"] / "deploy" / "output" / "release.json").read_text())


def _statuses(record: dict) -> dict[str, str]:
    return {c["check"]: c["status"] for c in record["checks"]}


@requires_gate_shell
def test_a_clean_fast_run_passes_and_runs_only_the_version_read(gate):
    done = _validate(gate, RELEASE_VALIDATION_PROFILE="dev-fast")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "DEV RELEASE BUILD PASSED" in done.stdout
    record = _record(gate)
    assert record["verdict"] == "passed"
    assert _statuses(record)[FAST_PROFILE_CHECK] == "passed"
    calls = gate["log"].read_text().splitlines()
    assert sum(c.startswith("docker build") for c in calls) == 4
    assert sum(c.startswith("docker run") for c in calls) == 1, (
        "a fast run starts one container, to read the version; any other is a check it should skip")


@requires_gate_shell
@pytest.mark.parametrize(("fault", "check"), [
    ({"FAKE_FAIL_TARGET": "mesh"}, "image build (mesh: Dockerfile target mesh)"),
    ({"FAKE_FOREIGN_WHEEL": "1"}, "wheel inspect"),
])
def test_a_failed_required_check_still_fails_the_fast_profile(gate, fault, check):
    done = _validate(gate, RELEASE_VALIDATION_PROFILE="dev-fast", **fault)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "RELEASE VALIDATION FAILED" in done.stdout
    assert "DEV RELEASE BUILD PASSED" not in done.stdout
    record = _record(gate)
    assert record["verdict"] == "failed"
    assert _statuses(record)[check] == "failed"


@requires_gate_shell
def test_the_default_profile_carries_on_into_the_exhaustive_checks(gate):
    # Stopped at the first exhaustive stage: past it the fakes prove nothing about the profile.
    run = subprocess.Popen(["bash", "devtools/release/validate.sh"], cwd=gate["root"], env=gate["env"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           start_new_session=True)
    seen = []
    try:
        for line in run.stdout:
            seen.append(line)
            if "images: entrypoints and in-image behaviour" in line:
                break
    finally:
        if run.poll() is None:
            os.killpg(run.pid, signal.SIGTERM)
        run.wait(timeout=30)
        run.stdout.close()
    output = "".join(seen)
    assert "images: entrypoints and in-image behaviour" in output, output
    assert "development deploy profile" not in output


def test_an_unknown_profile_is_refused_before_anything_runs(gate):
    done = _validate(gate, RELEASE_VALIDATION_PROFILE="fast")
    assert done.returncode == 2
    assert "unknown RELEASE_VALIDATION_PROFILE=fast" in done.stderr
    assert gate["log"].read_text() == ""
