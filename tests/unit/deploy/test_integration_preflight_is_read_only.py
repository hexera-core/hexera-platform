# Responsibility: Prove the integration tier's inspection phase decides by reading and can never mutate Docker.
# Boundaries: the real preflight and the real runner script, driven against recorded command doubles.

# This exists because the guard it protects once did the opposite of its job. The refusal that
# announced a stale worker image was an unquoted shell heredoc whose text contained `make rebuild`
# in backticks; bash substitutes commands in an unquoted heredoc, so PRINTING the complaint
# rebuilt and replaced four running application services. The contract below is therefore about
# which commands are issued, not about what any file says.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "tests/integration/preflight.py"
RUNNER = ROOT / "tests/integration/run_disposable.sh"

STALE = "b" * 64

#: Anything here changes Docker. If the inspection phase issues one, the phase is not read-only.
MUTATING = ("up", "build", "create", "start", "stop", "rm", "run", "restart", "pull", "push",
            "tag", "commit", "compose", "kill", "exec", "cp", "volume", "network", "image",
            "container", "system", "prune", "load", "import", "rename", "update")


class Doubles:
    def __init__(self, bin_dir: Path, record: Path):
        self.bin = bin_dir
        self.record = record

    def calls(self) -> list[str]:
        return [ln for ln in self.record.read_text().splitlines() if ln.strip()]

    def docker(self) -> list[list[str]]:
        return [ln.split()[1:] for ln in self.calls() if ln.startswith("docker ")]

    def named(self, program: str) -> list[str]:
        return [ln for ln in self.calls() if ln.startswith(program + " ")]

    def assert_read_only(self) -> None:
        for call in self.docker():
            verb = next((a for a in call if not a.startswith("-")), "")
            assert verb not in MUTATING, (
                f"the inspection phase issued a mutating command: docker {' '.join(call)}")
        for program in ("make", "compose"):
            assert not self.named(program), (
                f"the inspection phase executed {program}:\n" + "\n".join(self.calls()))

    def env(self) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env.pop("COMPOSE_PROJECT_NAME", None)
        return env


def _doubles(tmp_path: Path, *, worker_ids=("workerone",), stamp: str | None = "MATCH",
             dirty: str = "") -> Doubles:
    # A `docker`, `git` and `make` on PATH that answer plausibly and write down every call. None of
    # them decides the outcome: the REAL preflight does, and these only record what it asked for.
    # `git` is doubled rather than real so the digest below is a fixed, hermetic value and the test
    # does not change meaning depending on whether this checkout happens to be committed yet.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "record.txt"
    record.write_text("")

    stamp_line = "" if stamp is None else f"MESH_SOURCE_TREE={stamp}"
    newline = "\\n"
    quoted_ids = " ".join(f"'{w}'" for w in worker_ids)
    ps_branch = f"printf '%s{newline}' {quoted_ids}" if worker_ids else "true"
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
printf '%s{newline}' "docker $*" >> {record}
case "$1" in
  ps)      {ps_branch} ;;
  inspect) printf 'OTHER=1{newline}{stamp_line}{newline}' ;;
  *)       exit 0 ;;
esac
""")
    (bin_dir / "git").write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "git $*" >> {record}
case "$1 $2" in
  "status --porcelain") printf '%s' {dirty!r} ;;
  *) true ;;
esac
""")
    (bin_dir / "make").write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"make $*\" >> {record}\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return Doubles(bin_dir, record)


def _digest_under(d: Doubles) -> str:
    # THE production digest authority, run exactly as the preflight runs it, under the same
    # doubled git. Whatever it says is what a matching stamp has to be - the test never invents a
    # digest of its own, so it cannot drift away from the thing it is checking.
    done = subprocess.run(["bash", str(ROOT / "tests/integration/source_digest.sh")],
                          capture_output=True, text=True, env=d.env(), cwd=ROOT)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _run_preflight(d: Doubles, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = d.env()
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(PREFLIGHT)], capture_output=True, text=True,
                          env=env, cwd=ROOT, stdin=subprocess.DEVNULL)


@pytest.fixture()
def matching(tmp_path):
    probe = _doubles(tmp_path / "probe")
    return _digest_under(probe)


# 1. the inspection phase issues only read-only Docker commands


def test_the_inspection_phase_only_reads(tmp_path, matching):
    d = _doubles(tmp_path, stamp=matching)
    done = _run_preflight(d)
    assert done.returncode == 0, done.stderr
    calls = d.docker()
    assert calls, "the preflight asked Docker nothing at all - it cannot have inspected anything"
    for call in calls:
        verb = next((a for a in call if not a.startswith("-")), "")
        assert verb in ("ps", "inspect"), f"docker {' '.join(call)} is not a read"
    d.assert_read_only()


# 2. missing, empty, malformed and stale stamps refuse before any mutation


@pytest.mark.parametrize("stamp,because", [
    (None, "absent"),
    ("", "empty"),
    ("   ", "blank"),
    ("not-a-digest", "malformed"),
    ("abc123", "too short"),
    (STALE, "stale"),
])
def test_an_unusable_stamp_refuses_without_touching_anything(tmp_path, stamp, because):
    d = _doubles(tmp_path, stamp=stamp)
    done = _run_preflight(d)
    assert done.returncode != 0, f"a {because} stamp was accepted"
    assert "REFUSED" in done.stderr
    d.assert_read_only()


# 3. a matching stamp advances to the integration phase


def test_a_matching_stamp_resolves_the_worker(tmp_path, matching):
    d = _doubles(tmp_path, worker_ids=("thechosenworker",), stamp=matching)
    done = _run_preflight(d)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "thechosenworker", (
        "the preflight must hand the runner the container it validated, and nothing else")


def test_the_runner_reaches_the_test_phase_once_the_stamp_matches(tmp_path, matching):
    # Vacuity guard for the whole contract: a preflight that refused everything would satisfy every
    # read-only assertion above. Prove the accepted path actually goes on to run the tier.
    d = _doubles(tmp_path, stamp=matching)
    done = subprocess.run(["bash", str(RUNNER)], capture_output=True, text=True, env=d.env(),
                          cwd=ROOT, stdin=subprocess.DEVNULL)
    assert "workerone matches the committed tree" in done.stdout, (
        f"the runner did not accept a matching stamp:\n{done.stdout}\n{done.stderr}")
    provisioning = [c for c in d.docker() if c and c[0] == "exec"]
    assert provisioning, (
        "the runner stopped after the preflight and never began provisioning the tier - the "
        "read-only contract would then be protecting a phase that never runs")


# 4. multiple candidates refuse rather than choosing silently


def test_two_candidate_workers_refuse(tmp_path, matching):
    d = _doubles(tmp_path, worker_ids=("workerone", "workertwo"), stamp=matching)
    done = _run_preflight(d)
    assert done.returncode != 0, "the preflight picked one of two candidates"
    assert "workerone" in done.stderr and "workertwo" in done.stderr, (
        "a refusal that does not name the candidates is not actionable")
    d.assert_read_only()


def test_no_candidate_worker_refuses(tmp_path, matching):
    d = _doubles(tmp_path, worker_ids=(), stamp=matching)
    done = _run_preflight(d)
    assert done.returncode != 0, "the preflight resolved a worker that does not exist"
    d.assert_read_only()


# 5. a dirty tracked tree refuses


def test_a_dirty_tree_refuses_before_docker_is_asked_anything(tmp_path, matching):
    d = _doubles(tmp_path, stamp=matching, dirty=" M src/meshpipeline/api/app.py\n")
    done = _run_preflight(d)
    assert done.returncode != 0, "the tier accepted a dirty tree"
    assert "app.py" in done.stderr, "the refusal must name what is dirty"
    assert not d.docker(), (
        "the tree was judged after Docker was consulted; the cheapest refusal must come first")


# 6. the preflight leaves every Docker ID set unchanged


@pytest.mark.parametrize("stamp", [None, "", STALE, "MATCH"])
def test_the_preflight_changes_no_docker_state(tmp_path, stamp, matching):
    d = _doubles(tmp_path, stamp=matching if stamp == "MATCH" else stamp)
    _run_preflight(d)
    d.assert_read_only()


def test_the_preflight_changes_no_real_docker_state(tmp_path, matching):
    # The doubles prove intent; this proves consequence, against the real daemon. The preflight is
    # run for real (it will refuse - this checkout's worker is whatever it is) and the four ID sets
    # are compared across it.
    def ids():
        out = {}
        for what, cmd in (("containers", ["docker", "ps", "-aq"]), ("images", ["docker", "images", "-aq"]),
                          ("volumes", ["docker", "volume", "ls", "-q"]),
                          ("networks", ["docker", "network", "ls", "-q"])):
            done = subprocess.run(cmd, capture_output=True, text=True)
            if done.returncode != 0:
                pytest.skip("no reachable Docker daemon")
            out[what] = sorted(done.stdout.split())
        return out

    before = ids()
    subprocess.run([sys.executable, str(PREFLIGHT)], capture_output=True, text=True, cwd=ROOT,
                   stdin=subprocess.DEVNULL)
    assert ids() == before, "the preflight changed Docker state on this machine"


# 7. the exact production harness is exercised, not a copy


def test_the_runner_delegates_to_this_preflight(tmp_path):
    text = RUNNER.read_text()
    assert "preflight.py" in text, "the runner no longer consults the preflight"
    before_provisioning = text.split("# provision one disposable database")[0]
    assert "docker compose" not in before_provisioning, (
        "the runner resolves the worker through Compose again, which loads the project and can "
        "reconcile it")


def test_the_runner_refusal_cannot_execute_what_it_recommends(tmp_path):
    # The precise defect, end to end: a `make` that tells on itself, and a stamp that must be
    # refused. If the refusal text is ever written as an unquoted heredoc again, this fails.
    d = _doubles(tmp_path, stamp="")
    done = subprocess.run(["bash", str(RUNNER)], capture_output=True, text=True, env=d.env(),
                          cwd=ROOT, stdin=subprocess.DEVNULL)
    assert done.returncode != 0, "an empty stamp was accepted by the runner"
    assert not d.named("make"), (
        "the refusal path executed make - this is exactly the incident:\n" + "\n".join(d.calls()))
    d.assert_read_only()


# 8. an early consumer cannot leave a partially reconciled stack


def test_a_closed_consumer_cannot_turn_inspection_into_mutation(tmp_path):
    # The incident's second half: output was piped into `head`, which exited and turned the next
    # write into SIGPIPE mid-reconciliation. If the phase only reads, an early consumer can
    # truncate the message and nothing else.
    d = _doubles(tmp_path, stamp=STALE)
    subprocess.run(f"{sys.executable} {PREFLIGHT} 2>&1 | head -1", shell=True,
                   capture_output=True, text=True, env=d.env(), cwd=ROOT)
    d.assert_read_only()
    assert d.docker(), "nothing was inspected, so the SIGPIPE case was never reached"


# 9. the canonical command depends on no ambient alias, wrapper or repository virtualenv


def test_the_preflight_runs_without_the_repository_venv(tmp_path, matching):
    d = _doubles(tmp_path, stamp=matching)
    env = {"PATH": f"{d.bin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, str(PREFLIGHT)], capture_output=True, text=True,
                          env=env, cwd=ROOT, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, f"the preflight needed something from the ambient shell:\n{done.stderr}"
    d.assert_read_only()


def test_the_runner_invokes_a_plain_interpreter(tmp_path):
    text = RUNNER.read_text()
    assert ".venv" not in text, "the canonical runner reaches for a repository virtualenv"
    assert "python3 tests/integration/preflight.py" in text


# 10. a refused preflight reports the rebuild command without executing it


def test_a_refusal_names_the_rebuild_without_running_it(tmp_path, matching):
    d = _doubles(tmp_path, stamp=STALE)
    done = _run_preflight(d)
    assert done.returncode != 0
    assert "make rebuild" in done.stderr, "the refusal does not say how to fix it"
    assert STALE in done.stderr and matching in done.stderr, (
        "a stale-stamp refusal must show both digests, or it cannot be acted on")
    d.assert_read_only()
