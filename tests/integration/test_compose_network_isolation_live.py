# Responsibility: Prove two live Compose projects never resolve one another's service names.
# Boundaries: real containers on real networks; how the tracked file RENDERS is the unit tier.

# THIS TIER STARTS CONTAINERS. It brings up two disposable Compose projects, creates their
# networks, and runs a throwaway resolver container, so it declares the `alpine:3` image it needs
# and removes everything it created. It lived in the unit tier once, where "hermetic, no services,
# no containers" is the stated contract: running it there started containers and could pull an
# image during what a developer had been told was the fast in-process suite.
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

#: The one image this suite needs, declared rather than assumed so a cold machine pulls it once,
#: visibly, instead of in the middle of a resolver call whose timeout would then be blamed.
RESOLVER_IMAGE = "alpine:3"

#: Every disposable project this suite creates is named from this prefix plus a fresh run id, so it
#: can never collide with - or be mistaken for - a developer's real stack.
PREFIX = "meshisotest"

#: Two services, one name. `db` is deliberately the kind of name every project has; that is the
#: whole point, because a name only means something inside the network it is resolved on.
_UNIT = f"services:\n  db:\n    image: {RESOLVER_IMAGE}\n    command: sleep 300\n"


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker is unavailable here")


def _run(*args: str, check: bool = True) -> str:
    done = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if check and done.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed ({done.returncode}):\n{done.stderr}")
    return done.stdout


@pytest.fixture(scope="module", autouse=True)
def resolver_image():
    # Declared, not incidental: the pull happens here, once, where a failure says "the image could
    # not be fetched" instead of surfacing as a mysteriously slow DNS lookup.
    _run("docker", "pull", "--quiet", RESOLVER_IMAGE)
    return RESOLVER_IMAGE


class Project:
    # One disposable Compose project: brought up, interrogated over real DNS, then removed.

    def __init__(self, tmp: Path, suffix: str, body: str):
        self.name = f"{PREFIX}{uuid.uuid4().hex[:10]}{suffix}"
        assert self.name != "autonomousmeshingplatform", "refusing to touch the protected project"
        self.dir = tmp / self.name
        self.dir.mkdir(parents=True)
        (self.dir / "docker-compose.yml").write_text(body)

    def up(self) -> Project:
        _run("docker", "compose", "--project-directory", str(self.dir), "-p", self.name,
             "up", "-d", "--quiet-pull")
        return self

    def down(self) -> None:
        subprocess.run(["docker", "compose", "--project-directory", str(self.dir), "-p", self.name,
                        "down", "-v", "--remove-orphans", "--timeout", "1"],
                       capture_output=True, text=True, timeout=300)

    @property
    def network(self) -> str:
        out = _run("docker", "network", "ls",
                   "--filter", f"label=com.docker.compose.project={self.name}",
                   "--format", "{{.Name}}").split()
        assert len(out) == 1, f"project {self.name} owns {len(out)} networks: {out}"
        return out[0]

    def _db_container(self) -> str:
        cid = _run("docker", "ps", "-q",
                   "--filter", f"label=com.docker.compose.project={self.name}",
                   "--filter", "label=com.docker.compose.service=db").strip()
        assert cid, f"project {self.name} has no running db"
        return cid

    def db_network(self) -> str:
        # What the container is ATTACHED to, which is not the same question as what the project
        # OWNS: a project that adopts another's network is attached to a network it never created.
        nets = _run("docker", "inspect", "-f",
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                    self._db_container()).split()
        assert len(nets) == 1, f"db of {self.name} is on {len(nets)} networks: {nets}"
        return nets[0]

    def db_ip(self) -> str:
        cid = self._db_container()
        ips = _run("docker", "inspect", "-f",
                   "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid).split()
        assert len(ips) == 1, f"db of {self.name} is on {len(ips)} networks: {ips}"
        return ips[0]


def _resolve(network: str, name: str) -> set[str]:
    # A REAL lookup, made by a real container on that network through Docker's own resolver.
    out = subprocess.run(["docker", "run", "--rm", "--network", network, RESOLVER_IMAGE,
                          "getent", "ahostsv4", name], capture_output=True, text=True, timeout=300)
    return {ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()}


@pytest.fixture(scope="module")
def two_projects(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("iso")
    a, b = Project(tmp, "a", _UNIT), Project(tmp, "b", _UNIT)
    try:
        yield a.up(), b.up()
    finally:
        a.down()
        b.down()


@pytest.fixture(scope="module")
def shared_name_projects(tmp_path_factory):
    # THE CONTROL. Two projects that both pin one network name - the arrangement this repository
    # used to ship. It reproduces the defect so the proofs above cannot pass for some other reason.
    tmp = tmp_path_factory.mktemp("iso-fixed")
    fixed = f"{PREFIX}fixed{uuid.uuid4().hex[:8]}"
    body = _UNIT + f"networks:\n  default:\n    name: {fixed}\n"
    a, b = Project(tmp, "a", body), Project(tmp, "b", body)
    try:
        yield a.up(), b.up(), fixed
    finally:
        a.down()
        b.down()
        subprocess.run(["docker", "network", "rm", fixed], capture_output=True, timeout=60)


# two live projects, and Docker's own resolver

def test_two_live_projects_are_on_different_networks(two_projects):
    a, b = two_projects
    assert a.network != b.network, f"both projects landed on {a.network}"


def test_a_service_name_resolves_to_its_own_projects_container(two_projects):
    a, b = two_projects
    assert _resolve(a.network, "db") == {a.db_ip()}, "project A's `db` is not project A's db"
    assert _resolve(b.network, "db") == {b.db_ip()}, "project B's `db` is not project B's db"


def test_a_service_name_never_resolves_into_the_other_project(two_projects):
    a, b = two_projects
    assert a.db_ip() != b.db_ip(), "the two databases share an address; the test proves nothing"
    assert b.db_ip() not in _resolve(a.network, "db"), (
        "a container in project A resolved `db` to project B's container. This is the whole "
        "failure: the connection succeeds, the query runs, and the log line still says `db`.")
    assert a.db_ip() not in _resolve(b.network, "db")


def test_neither_project_can_see_the_others_containers_at_all(two_projects):
    a, b = two_projects
    on_a = set(_run("docker", "network", "inspect", a.network,
                    "-f", "{{range .Containers}}{{.Name}} {{end}}").split())
    assert not any(b.name in n for n in on_a), (
        f"project B's containers are attached to project A's network: {on_a}")


# the control: the arrangement that was shipped before, reproduced

def test_a_fixed_network_name_really_does_merge_two_projects(shared_name_projects):
    a, b, fixed = shared_name_projects
    assert a.db_network() == b.db_network() == fixed, (
        "the control did not reproduce the old arrangement, so it proves nothing about the fix")
    seen = _resolve(fixed, "db")
    assert {a.db_ip(), b.db_ip()} <= seen, (
        f"`db` on the shared network resolved to {seen}, expected both projects' databases "
        f"({a.db_ip()}, {b.db_ip()}) - the defect this fix removes")


def test_the_merged_projects_do_not_even_own_the_network_they_are_on(shared_name_projects):
    # HOW THE COLLISION HIDES. Whichever project starts first creates the network and is labelled
    # its owner; the second simply joins. `docker compose ls` shows two healthy projects, and
    # nothing in either one's output says it is sharing service names with the other.
    a, b, fixed = shared_name_projects
    owner = _run("docker", "network", "inspect", fixed,
                 "-f", '{{index .Labels "com.docker.compose.project"}}').strip()
    assert owner in (a.name, b.name), f"unexpected owner {owner!r} for the control network"
    adopted = b if owner == a.name else a
    assert _run("docker", "network", "ls", "--filter",
                f"label=com.docker.compose.project={adopted.name}",
                "--format", "{{.Name}}").split() == [], (
        f"{adopted.name} was expected to own no network at all - it joined one it did not create")
