# Responsibility: Verify Gate C removes only its own labelled containers with their volumes, and never prunes.
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VALIDATE = REPO / "devtools" / "release" / "validate.sh"

#: The image Gate C's Postgres service uses. Chosen here because it declares a VOLUME, which is
#: the whole point - a container with no VOLUME could not demonstrate the leak.
VOLUME_DECLARING_IMAGE = "postgres:16-alpine"


# source contract (hermetic)

@pytest.fixture(scope="module")
def script() -> str:
    return VALIDATE.read_text()


def _docker_rm_lines(script: str) -> list[str]:
    return [ln.strip() for ln in script.splitlines()
            if "docker rm" in ln and not ln.lstrip().startswith("#")]


def test_every_container_removal_takes_its_anonymous_volumes(script):
    lines = _docker_rm_lines(script)
    assert lines, "no docker rm found - this contract is pointed at the wrong file"
    for ln in lines:
        assert " -v " in f" {ln} ", (
            f"a container is removed without -v, so its anonymous volumes outlive it: {ln}")


def test_the_mid_run_removals_are_covered_too(script):
    assert len(_docker_rm_lines(script)) >= 6, (
        "expected the trap sweep plus the UI and native-tier removals")


def test_cleanup_removes_only_this_run_s_labelled_containers(script):
    body = script.split("cleanup() {", 1)[1].split("\ntrap cleanup EXIT", 1)[0]
    assert 'docker ps -aq --filter "label=amp-release=${STAMP}"' in body, (
        "the id list must stay scoped to THIS run's unique label")
    assert '[ -n "${ids}" ]' in body, "an empty id list must not become a broad removal"


@pytest.mark.parametrize("forbidden", [
    "docker system prune", "docker volume prune", "docker image prune",
    "docker container prune", "docker network prune", "dangling=true",
])
def test_gate_c_never_prunes_or_sweeps_global_docker_state(script, forbidden):
    assert forbidden not in script


def test_no_volume_is_removed_by_name_or_by_scanning(script):
    executable = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("docker volume rm" in ln or "docker volume ls" in ln for ln in executable)


def test_cleanup_preserves_the_original_exit_status(script):
    body = script.split("cleanup() {", 1)[1].split("\ntrap cleanup EXIT", 1)[0]
    assert "local rc=$?" in body and 'return "${rc}"' in body, (
        "cleanup must return the status it was entered with, not its own")


@pytest.mark.parametrize("sig,code", [("INT", "130"), ("TERM", "143")])
def test_signals_reach_the_same_cleanup_authority(script, sig, code):
    assert f"trap 'exit {code}' {sig}" in script, (
        f"{sig} must turn into an exit so the EXIT trap runs the same cleanup")


def test_keep_work_retains_only_the_scratch_directory_never_docker(script):
    body = script.split("cleanup() {", 1)[1].split("\ntrap cleanup EXIT", 1)[0]
    docker_part = body.split("KEEP_WORK_REQUESTED", 1)[0]
    assert "docker rm -f -v" in docker_part, (
        "container/volume removal must happen BEFORE and independently of the KEEP_WORK branch")


def test_the_script_is_syntactically_valid():
    assert subprocess.run(["bash", "-n", str(VALIDATE)], capture_output=True).returncode == 0


# behavioural contract (real Docker)

def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_ok(), reason="a real Docker daemon is required to prove -v semantics")


def _d(*args: str, check: bool = True) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _anon_volumes_of(container: str) -> set[str]:
    out = _d("inspect", container, "--format",
             '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}')
    return {n for n in out.split() if len(n) == 64 and all(c in "0123456789abcdef" for c in n)}


@pytest.fixture()
def label():
    return f"amp-gatec-cleanup-test={uuid.uuid4().hex[:12]}"


@pytest.fixture()
def reaper(label):
    created: dict[str, list[str]] = {"containers": [], "volumes": []}
    yield created
    for cid in created["containers"]:
        subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True)
    for vol in created["volumes"]:
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True)


@requires_docker
def test_removing_a_container_with_v_takes_its_anonymous_volume_but_spares_named_and_foreign(
        label, reaper, tmp_path):
    key, val = label.split("=")
    named = f"gatec-named-{val}"
    _d("volume", "create", "--label", label, named)
    reaper["volumes"].append(named)

    # A DIFFERENT container, with its own anonymous volume. Nothing this test does to the subject
    # container may touch it.
    foreign = _d("run", "-d", "--label", label, "--name", f"gatec-foreign-{val}",
                 "-e", "POSTGRES_PASSWORD=x", VOLUME_DECLARING_IMAGE)
    reaper["containers"].append(foreign)
    foreign_anon = _anon_volumes_of(foreign)
    assert len(foreign_anon) == 1, f"expected one anonymous volume on the foreign container: {foreign_anon}"

    # The SUBJECT: an anonymous volume (the image's own VOLUME) plus a NAMED mount.
    subject = _d("run", "-d", "--label", label, "--name", f"gatec-subject-{val}",
                 "-e", "POSTGRES_PASSWORD=x", "-v", f"{named}:/mnt/named", VOLUME_DECLARING_IMAGE)
    reaper["containers"].append(subject)
    subject_anon = _anon_volumes_of(subject)
    assert len(subject_anon) == 1, f"expected one anonymous volume on the subject: {subject_anon}"

    before = set(_d("volume", "ls", "-q").splitlines())
    assert subject_anon <= before and foreign_anon <= before and named in before

    # THE OPERATION UNDER TEST - exactly what Gate C's cleanup now does.
    _d("rm", "-f", "-v", subject)
    reaper["containers"].remove(subject)

    after = set(_d("volume", "ls", "-q").splitlines())
    assert subject_anon.isdisjoint(after), "the subject's anonymous volume outlived its container"
    assert named in after, "-v removed a NAMED volume; it must not"
    assert foreign_anon <= after, "-v removed another container's anonymous volume; it must not"
    assert before - after == subject_anon, (
        f"exactly one volume should have gone; delta was {before - after}")


@requires_docker
def test_removing_the_same_container_twice_is_idempotent_and_removes_nothing_further(
        label, reaper):
    key, val = label.split("=")
    c = _d("run", "-d", "--label", label, "--name", f"gatec-idem-{val}",
           "-e", "POSTGRES_PASSWORD=x", VOLUME_DECLARING_IMAGE)
    reaper["containers"].append(c)
    _d("rm", "-f", "-v", c)
    reaper["containers"].remove(c)
    after_first = set(_d("volume", "ls", "-q").splitlines())

    # Second call: the container is gone, so this fails - and must change nothing.
    subprocess.run(["docker", "rm", "-f", "-v", c], capture_output=True)
    assert set(_d("volume", "ls", "-q").splitlines()) == after_first


@requires_docker
def test_removal_of_an_absent_container_deletes_nothing(reaper):
    before = set(_d("volume", "ls", "-q").splitlines())
    containers_before = set(_d("ps", "-aq").splitlines())
    subprocess.run(["docker", "rm", "-f", "-v", f"gatec-never-existed-{uuid.uuid4().hex}"],
                   capture_output=True)
    assert set(_d("volume", "ls", "-q").splitlines()) == before
    assert set(_d("ps", "-aq").splitlines()) == containers_before


@requires_docker
def test_an_empty_ownership_inventory_removes_nothing(reaper):
    before = set(_d("volume", "ls", "-q").splitlines())
    ids = _d("ps", "-aq", "--filter", "label=amp-release=definitely-no-such-stamp")
    assert ids == "", "precondition: the label must match nothing"
    r = subprocess.run("ids=''; [ -n \"$ids\" ] && docker rm -f -v $ids; true",
                       shell=True, capture_output=True)
    assert r.returncode == 0
    assert set(_d("volume", "ls", "-q").splitlines()) == before
