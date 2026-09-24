# Responsibility: Prove what one worker instance's startup script actually does, by running it.
# Boundaries: it executes the script against fake docker/gcloud/curl/install and reads back the
#             commands it issued; it starts no container, boots no instance and calls no cloud.
#
# THE DEFECT THIS FILE EXISTS FOR. deploy/gcp/worker/startup.sh bind-mounts two HOST directories over
# /srv/workspaces and /srv/data. Docker creates a missing mount source as root:root, the containers
# run as uid 1000, and a bind mount replaces the image's own uid-1000-owned directory with the host's
# - so the application could not write to its workspace root or its data root on any fresh instance.
# Every job failed at the first mkdir with EACCES, after the instance had installed docker, pulled a
# multi-gigabyte image and taken the job, and the fleet reported healthy throughout.
#
# WHY IT IS ASSERTED ON THE EXECUTED SCRIPT AND NOT ON THE TEXT. A test that grepped the file for
# `chown` or for `-o 1000` would pass on a line inside a branch that never runs, and on a line placed
# AFTER the `docker run` that needs it. This project's signature failure is a check with the same
# blind spot as the thing it checks, so the script is run and the ordering and arguments are read off
# the tools it called.
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
STARTUP = REPO / "deploy" / "gcp" / "worker" / "startup.sh"

_FAKE_CURL = """#!/bin/bash
# The metadata server, answering the instance attributes startup.sh reads and nothing else. An
# unknown attribute exits non-zero, which is how the script learns a credential is not used here.
for a in "$@"; do case "$a" in
  *"attributes/worker-image") echo "reg/app@sha256:abc"; exit 0;;
  *"attributes/redis-url") echo "redis://10.0.0.2:6379/0"; exit 0;;
  *"attributes/database-url") echo ""; exit 0;;
  *"attributes/env-uri") echo "gs://bucket/worker.env"; exit 0;;
  *"project/project-id") echo "fake-project"; exit 0;;
  *"attributes/"*) exit 1;;
esac; done
exit 1
"""

_FAKE_GCLOUD = """#!/bin/bash
# `gcloud storage cp <src> <dst> --quiet` has to leave a file behind, because the script then chmods
# it and appends the endpoints to it.
if [ "$1" = "storage" ] && [ "$2" = "cp" ]; then printf 'POSTGRES_HOST=db\\n' > "$4"; fi
exit 0
"""

_FAKE_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "${DOCKER_LOG}"
exit 0
"""

# `install` IS FAKED, and it has to be. The real one would try to create /var/lib/hexera on the
# machine running the tests and then chown it to uid 1000, which needs root - so an honest test that
# let it run would fail everywhere except as root, and a test that dropped the `-o 1000` to make it
# pass would no longer be measuring the thing that was broken. Faked, the arguments the script asked
# for are exactly what is asserted.
_FAKE_INSTALL = """#!/bin/bash
printf '%s\\n' "$*" >> "${INSTALL_LOG}"
exit 0
"""


class Run:
    """What one execution of the startup script asked its tools to do, in order."""

    def __init__(self, docker: list[str], install: list[str]) -> None:
        self.docker_runs = [line for line in docker if line.startswith("run ")]
        self.docker_all = docker
        self.installs = install


def run_startup(tmp_path: Path, script_text: str | None = None) -> Run:
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine, and this script is bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("curl", _FAKE_CURL), ("gcloud", _FAKE_GCLOUD), ("docker", _FAKE_DOCKER),
                       ("install", _FAKE_INSTALL), ("systemctl", "#!/bin/bash\nexit 0\n")):
        path = bin_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    docker_log = tmp_path / "docker.log"
    install_log = tmp_path / "install.log"
    for log in (docker_log, install_log):
        log.write_text("", encoding="utf-8")
    # /etc/hexera is the only path rewritten, and only because it is not writable here. The two
    # `docker run` lines and the `install -d` line - which are what this file is about - run exactly
    # as they are written.
    etc = tmp_path / "etc"
    text = STARTUP.read_text(encoding="utf-8") if script_text is None else script_text
    script = tmp_path / "startup.sh"
    script.write_text(text.replace("/etc/hexera", str(etc)), encoding="utf-8", newline="\n")
    done = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=300, check=False,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
             "DOCKER_LOG": str(docker_log), "INSTALL_LOG": str(install_log)})
    assert done.returncode == 0, (
        f"the startup script failed:\n{done.stdout[-3000:]}{done.stderr[-3000:]}")
    return Run(docker_log.read_text(encoding="utf-8").splitlines(),
               install_log.read_text(encoding="utf-8").splitlines())


# ------------------------------------------------------- the mount roots the containers must own

def test_the_bind_mount_roots_are_created_owned_by_the_container_uid(tmp_path):
    run = run_startup(tmp_path)
    creations = [line for line in run.installs if "/var/lib/hexera" in line]
    assert creations, (
        "the startup script never created the host directories it bind-mounts. Docker creates a "
        f"missing mount source as root:root and the containers run as uid 1000. It ran: {run.installs}")
    both = " ".join(creations)
    for path in ("/var/lib/hexera/workspaces", "/var/lib/hexera/data"):
        assert path in both, f"{path} is bind-mounted but never created with an owner: {creations}"
    # The uid is the assertion. A directory that is created and left root-owned is exactly the
    # defect, so it is not enough that the paths appear.
    assert "-o 1000" in both and "-g 1000" in both, (
        "the mount roots are created but not given to uid 1000, which is the uid the image's USER_A "
        f"is pinned to and the uid both containers run as: {creations}")


def test_the_mount_roots_are_owned_before_the_first_container_is_started(tmp_path):
    # ORDER IS THE WHOLE POINT. `install -d` after the `docker run` fixes the directory a moment
    # after docker has already created it root-owned and a container is already running against it,
    # which is a passing grep and a failing fleet. The fake tools log to two files, so the ordering
    # is checked by running the script twice over: once with everything after the first `docker run`
    # removed, where the creation must already have happened.
    text = STARTUP.read_text(encoding="utf-8")
    marker = "docker rm -f hexera-worker"
    assert marker in text, "the container-start block moved; this test no longer cuts where it thinks"
    run = run_startup(tmp_path, text.split(marker)[0])
    assert run.docker_runs == [], "nothing should have been `docker run` before the cut"
    assert [line for line in run.installs if "/var/lib/hexera" in line], (
        "the mount roots are created AFTER the first container is started, so docker has already "
        "created them root-owned by the time the ownership is set")
