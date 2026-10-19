# Responsibility: Verify a worker VM heals a stuck container by itself - both containers carry the liveness healthcheck,
#                 the watchdog restarts an unhealthy one (never during shutdown) and publishes what the VM runs - and
#                 that every template ships the watchdog and lets it publish.
# Boundaries: static reads of the VM scripts, the watchdog run once against a fake docker and metadata server, and the
#             template the fleet stage creates against a fake gcloud; nothing reaches a cloud.
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
WORKER = REPO / "deploy" / "gcp" / "worker"
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
STARTUP = (WORKER / "startup.sh").read_text(encoding="utf-8")
SHUTDOWN = (WORKER / "shutdown.sh").read_text(encoding="utf-8")
WATCHDOG = WORKER / "watchdog.sh"

# THE DEFECT THIS PINS. Shared dev, 2026-10-16 06:58 UTC to 10-19: hexera-geometry-worker's broker
# connection hung after a reset and the container stayed "Up" - `--restart always` acts only on a
# process that exits. No geometry check ran for three days, and nothing said so.


def _run_block(name: str) -> str:
    start = STARTUP.index(f"docker run -d --name {name} ")
    return STARTUP[start:STARTUP.find("\n\n", start)]


@pytest.mark.parametrize("name", ["hexera-worker", "hexera-geometry-worker"])
def test_both_containers_carry_the_liveness_healthcheck(name):
    assert '"${HEALTH_FLAGS[@]}"' in _run_block(name)
    flags = STARTUP[STARTUP.index("HEALTH_FLAGS=("):]
    flags = flags[: flags.index(")\n")]
    assert "python -m meshpipeline.runtime.worker_liveness check" in flags
    for flag in ("--health-interval", "--health-retries", "--health-start-period", "--health-timeout"):
        assert flag in flags


def test_the_healthcheck_names_a_command_the_module_has():
    from meshpipeline.runtime import worker_liveness
    assert worker_liveness.main(["check", "--pid", "999999"]) == 1      # no beat: unhealthy, no crash


def test_the_watchdog_is_installed_as_a_unit_that_comes_back():
    assert "md watchdog-script > /usr/local/bin/hexera-worker-watchdog" in STARTUP
    assert "Restart=always" in STARTUP
    assert "systemctl enable hexera-worker-watchdog" in STARTUP
    # after both containers exist, so its first pass has something to report
    assert STARTUP.index("systemctl restart hexera-worker-watchdog") > STARTUP.index(
        "docker run -d --name hexera-geometry-worker")
    # a reboot must not leave the watchdog idle behind the previous shutdown's mark
    assert STARTUP.index("rm -f /run/hexera-shutting-down") < STARTUP.index(
        "systemctl restart hexera-worker-watchdog")


def test_shutdown_stops_the_watchdog_before_it_stops_the_containers():
    mark = SHUTDOWN.index("touch /run/hexera-shutting-down")
    stop = SHUTDOWN.index("systemctl stop hexera-worker-watchdog")
    first_docker = SHUTDOWN.index("\ndocker stop ")
    assert mark < first_docker and stop < first_docker


# ---------------------------------------------------------------------------------------------
# the watchdog, run once
# ---------------------------------------------------------------------------------------------

_FAKE_DOCKER = r"""#!/usr/bin/env bash
STATE="${FAKE_STATE:?}"
echo "$*" >> "${STATE}/docker.log"
case "$1" in
  inspect)
    fmt="$3"; name="$4"
    case "${fmt}" in
      *State.Running*)
        if [ "${name}" = hexera-worker ]; then echo "${FAKE_WORKER_STATE:-true healthy}"; else echo "${FAKE_GEOMETRY_STATE:-true healthy}"; fi ;;
      *Config.Image*) echo "${FAKE_IMAGE:-app@sha256:c0ffee}" ;;
      *Health.Log*) echo "the consumer last beat 300s ago" ;;
    esac ;;
  restart) exit 0 ;;
esac
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
STATE="${FAKE_STATE:?}"
url="${@: -1}"; data=""
while [ $# -gt 0 ]; do [ "$1" = --data ] && data="$2"; shift; done
echo "${url##*/guest-attributes/hexera/}=${data}" >> "${STATE}/ga.log"
"""


@pytest.fixture
def watchdog(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", _FAKE_DOCKER), ("curl", _FAKE_CURL),
                       ("logger", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)

    def _run(**fake):
        state = tmp_path / f"state{len(list(tmp_path.glob('state*')))}"
        state.mkdir()
        done = subprocess.run(
            ["bash", str(WATCHDOG)], capture_output=True, text=True, timeout=60,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_STATE": str(state),
                 "WATCHDOG_ONCE": "1", "WATCHDOG_SHUTDOWN_MARK": str(state / "shutting-down"),
                 **fake})
        docker = (state / "docker.log").read_text() if (state / "docker.log").exists() else ""
        ga = (state / "ga.log").read_text() if (state / "ga.log").exists() else ""
        return done, docker, dict(line.split("=", 1) for line in ga.splitlines()), state
    return _run


def test_an_unhealthy_running_container_is_restarted_with_its_stop_budget(watchdog):
    done, docker, ga, _ = watchdog(FAKE_GEOMETRY_STATE="true unhealthy")
    assert done.returncode == 0, done.stderr
    restarts = [ln for ln in docker.splitlines() if ln.startswith("restart")]
    # the geometry worker's own 30 s - the same budget startup.sh and shutdown.sh give it
    assert restarts == ["restart -t 30 hexera-geometry-worker"]
    assert ga["restarts"] == "1" and "hexera-geometry-worker" in ga["last-restart"]


def test_a_healthy_fleet_is_left_alone_and_reported(watchdog):
    done, docker, ga, _ = watchdog()
    assert done.returncode == 0, done.stderr
    assert "restart" not in docker
    assert ga["worker-image"] == ga["geometry-image"] == "app@sha256:c0ffee"
    assert ga["worker-health"] == ga["geometry-health"] == "healthy"
    assert ga["heartbeat"].isdigit()


def test_a_container_being_shut_down_is_never_restarted(watchdog, tmp_path):
    done, docker, _, state = watchdog(FAKE_WORKER_STATE="true unhealthy",
                                      WATCHDOG_SHUTDOWN_MARK="/")   # an existing path: shutting down
    assert done.returncode == 0, done.stderr
    assert "restart" not in docker


def test_a_stopped_container_is_not_restarted_by_the_watchdog(watchdog):
    # an exited container is docker's restart policy's to handle, or was stopped on purpose
    done, docker, ga, _ = watchdog(FAKE_WORKER_STATE="false unhealthy")
    assert "restart" not in docker
    assert ga["worker-health"] == "unhealthy"


def test_the_simulation_worker_gets_its_75_seconds_to_hand_its_job_back(watchdog):
    _, docker, _, _ = watchdog(FAKE_WORKER_STATE="true unhealthy")
    assert "restart -t 75 hexera-worker" in docker
    budget = re.search(r"docker stop --time (\d+) hexera-worker\b", SHUTDOWN)
    assert budget and budget.group(1) == "75"


# ---------------------------------------------------------------------------------------------
# the template ships it
# ---------------------------------------------------------------------------------------------

def test_the_template_ships_the_watchdog_and_lets_it_publish():
    fleet = (SCRIPTS / "create-worker-fleet.sh").read_text(encoding="utf-8")
    assert "watchdog-script=${WATCHDOG}" in fleet
    assert '"enable-guest-attributes=TRUE"' in fleet
    # a change to the watchdog is a new template, so it reaches the VMs
    assert 'cat - "${STARTUP}" "${SHUTDOWN}" "${WATCHDOG}"' in fleet
