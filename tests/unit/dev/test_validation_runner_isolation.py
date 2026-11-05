# Responsibility: Prove containerized validation is built from the working tree and never from a host virtualenv.
# Boundaries: the runner's copy and environment construction; which tests a tier selects is elsewhere.
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "devtools" / "validation" / "in_container.sh"
RECORD = REPO / "devtools" / "validation" / "assert_record.py"

#: Printed by the interpreter the runner hands the tier to, from inside the copied checkout. Every
#: question this suite asks is answered from THERE, not from the fixture on disk.
PROBE = (
    "import json, os, sys;"
    "import importlib.util as u;"
    "sys.path.insert(0, 'src');"
    "import meshprobe;"
    "print(json.dumps({"
    "'prefix': sys.prefix,"
    "'executable': sys.executable,"
    "'executable_real': os.path.realpath(sys.executable),"
    "'version': '%d.%d' % sys.version_info[:2],"
    "'marker': meshprobe.MARKER,"
    "'host_pkg': u.find_spec('hostsentinel') is not None,"
    "'host_env': os.environ.get('HOST_SENTINEL', ''),"
    "'cwd': os.getcwd(),"
    "'untracked': os.path.exists('untracked_helper.txt'),"
    "'venv_in_copy': os.path.exists('.venv'),"
    "}))"
)

_PYPROJECT = (
    '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
    '[project]\nname = "meshprobe"\nversion = "0.0.0"\n\n'
    '[tool.setuptools.packages.find]\nwhere = ["src"]\n'
)


def _fixture_checkout(root: Path, *, marker: str = "working-tree") -> Path:
    pkg = root / "src" / "meshprobe"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f'MARKER = "{marker}"\n')
    (root / "pyproject.toml").write_text(_PYPROJECT)
    return root


def _fingerprint(path: Path) -> str:
    # Content AND symlink targets: a run that repaired `.venv/bin/python` would leave every file
    # byte-identical and still have changed the thing that matters.
    h = hashlib.sha256()
    if not path.exists():
        return "absent"
    for p in sorted(path.rglob("*")):
        rel = p.relative_to(path).as_posix()
        if p.is_symlink():
            h.update(f"{rel}->{os.readlink(p)}\n".encode())
        elif p.is_file():
            h.update(f"{rel}:".encode())
            h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
            h.update(b"\n")
        else:
            h.update(f"{rel}/\n".encode())
    return h.hexdigest()


def _ambient_python(tmp: Path) -> Path:
    # The runner builds its environment from whatever `python` its PATH offers - inside the image
    # that is the image's own interpreter. Here that interpreter is pinned to the one running this
    # test, so the suite asserts the same invariant on a developer host, where a bare `python` may
    # be some unrelated installation, as it does in the image.
    d = tmp / "ambient-bin"
    if not (d / "python").exists():
        d.mkdir(parents=True, exist_ok=True)
        (d / "python").symlink_to(sys.executable)
    return d


def _run(src: Path, task: Path, *args: str, env_extra: dict | None = None):
    env = {**os.environ, "VALIDATION_SRC": str(src), "VALIDATION_TASK": str(task)}
    env.pop("HOST_SENTINEL", None)
    env["PATH"] = f"{_ambient_python(src.parent)}{os.pathsep}{env.get('PATH', '')}"
    env.update(env_extra or {})
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          timeout=600, env=env)


def _probe(src: Path, task: Path, **kw) -> tuple[dict, subprocess.CompletedProcess]:
    done = _run(src, task, "-c", PROBE, **kw)
    assert done.returncode == 0, f"the runner failed:\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1]), done



def _bare_venv(root: Path, target: str) -> Path:
    # A virtualenv-shaped directory whose interpreter points where we say. Built by hand rather
    # than by `python -m venv` because the interesting shapes - an interpreter this image does not
    # have, an absolute link into nowhere - are exactly the ones venv will not produce.
    v = root / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "bin" / "python").symlink_to(target)
    (v / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.3\n")
    sp = v / "lib" / "python3.12" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "hostsentinel.py").write_text("ORIGIN = 'host'\n")
    return v


def _real_venv(root: Path) -> Path:
    v = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(v)], check=True, capture_output=True,
                   timeout=600)
    return v


CASES = ["absent", "valid_same_python", "different_installed_python",
         "unavailable_interpreter", "dangling_absolute_link", "sentinel_package",
         "activation_variable"]


def _build_case(root: Path, case: str) -> Path | None:
    if case == "absent":
        return None
    if case == "valid_same_python":
        return _real_venv(root)
    if case == "different_installed_python":
        return _bare_venv(root, "python3.10")
    if case == "unavailable_interpreter":
        return _bare_venv(root, "python3.12")
    if case == "dangling_absolute_link":
        return _bare_venv(root, "/nonexistent/bin/python3.99")
    if case == "sentinel_package":
        v = _real_venv(root)
        sites = list(v.glob("lib/python*/site-packages"))
        assert sites, "the fixture virtualenv has no site-packages to plant a sentinel in"
        (sites[0] / "hostsentinel.py").write_text("ORIGIN = 'host'\n")
        return v
    if case == "activation_variable":
        v = _real_venv(root)
        (v / "bin" / "activate").write_text('export HOST_SENTINEL=leaked-from-host\n')
        return v
    raise AssertionError(case)


@pytest.mark.parametrize("case", CASES)
def test_validation_ignores_every_shape_of_host_virtualenv(case, tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    _build_case(src, case)
    before = _fingerprint(src / ".venv")

    facts, _ = _probe(src, tmp_path / "task")

    # it ran on the environment the RUNNER built, not the one it found
    assert facts["prefix"] == str(tmp_path / "task" / "venv"), (
        f"[{case}] the tier ran under {facts['prefix']}, not the validation-owned environment")
    assert facts["executable"].startswith(str(tmp_path / "task" / "venv")), facts["executable"]
    # and that environment's interpreter is the ambient one - the image's, in the tier that ships -
    # rather than anything the checkout carried
    assert facts["executable_real"] == os.path.realpath(sys.executable), (
        f"[{case}] the tier ran under {facts['executable_real']}, not the ambient interpreter")
    assert not facts["executable_real"].startswith(str(src)), (
        f"[{case}] the interpreter came from the checkout: {facts['executable_real']}")
    assert facts["version"] == f"{sys.version_info[0]}.{sys.version_info[1]}", (
        f"[{case}] the tier ran under Python {facts['version']}, not the ambient interpreter's")
    # and nothing of the host environment came with it
    assert facts["host_pkg"] is False, f"[{case}] a package from the host virtualenv was importable"
    assert facts["host_env"] == "", f"[{case}] the host activation script leaked {facts['host_env']}"
    assert facts["venv_in_copy"] is False, f"[{case}] a host virtualenv reached the copied checkout"
    # the tests really ran
    assert facts["marker"] == "working-tree"
    # and the host's own virtualenv is exactly as it was
    assert _fingerprint(src / ".venv") == before, f"[{case}] the runner modified the host virtualenv"


def test_the_host_virtualenv_that_used_to_kill_the_tier_is_now_irrelevant(tmp_path):
    # The shape that used to kill the tier: setup prefers python3.12, this image ships 3.11, and
    # the copied `bin/python -> python3.12` was the interpreter the runner then tried to exec.
    src = _fixture_checkout(tmp_path / "repo")
    _bare_venv(src, "python3.12")
    facts, done = _probe(src, tmp_path / "task")
    assert facts["version"] == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert "python3.12" not in done.stderr



def test_a_tracked_source_modification_reaches_validation(tmp_path):
    src = _fixture_checkout(tmp_path / "repo", marker="committed")
    subprocess.run(["git", "init", "-q"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
                   cwd=src, check=True, capture_output=True)
    # the working tree now disagrees with HEAD - which is the normal state while fixing something
    (src / "src" / "meshprobe" / "__init__.py").write_text('MARKER = "uncommitted-edit"\n')

    facts, _ = _probe(src, tmp_path / "task")
    assert facts["marker"] == "uncommitted-edit", (
        "validation ran against the committed tree, so a developer cannot test what they just "
        "changed. `git archive HEAD` is not a substitute for the working tree.")


def test_an_untracked_helper_reaches_validation(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    (src / "untracked_helper.txt").write_text("a new test's fixture, not yet added\n")
    facts, _ = _probe(src, tmp_path / "task")
    assert facts["untracked"] is True, "an untracked file in the working tree never reached the copy"


def test_the_runner_does_not_substitute_the_committed_tree():
    # Matched loosely on purpose: `git archive`, `git -C "$SRC" archive` and `git --work-tree=...
    # archive` are the same substitution, and a guard that only knows the shortest spelling is a
    # guard that passes while the behaviour it forbids is present.
    import re
    text = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"^[^#\n]*\bgit\b[^\n]*\barchive\b", text, re.M)
    assert not found, (
        f"the runner exports the committed tree ({found.group(0).strip()!r}); the two tests above "
        "would then describe a different program than the one that ships")


def test_the_copy_keeps_git_so_the_hygiene_tier_can_ask_what_is_tracked(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    subprocess.run(["git", "init", "-q"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=src, check=True, capture_output=True)
    done = _run(src, tmp_path / "task", "-c",
                "import subprocess,sys;"
                "print(subprocess.run(['git','ls-files'],capture_output=True,text=True).stdout)")
    assert done.returncode == 0, done.stderr[-2000:]
    assert "pyproject.toml" in done.stdout, "the copy has no git history to interrogate"



def test_the_workspace_is_removed_after_a_successful_run(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    task = tmp_path / "task"
    _probe(src, task)
    assert not task.exists(), f"the validation workspace survived the run at {task}"


def test_the_workspace_is_removed_after_a_failing_test_run(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    task = tmp_path / "task"
    done = _run(src, task, "-c", "raise SystemExit(3)")
    assert done.returncode == 3, "the tier's own exit status was not preserved"
    assert not task.exists(), "a failing run left its workspace behind"


def test_a_failing_test_run_is_not_reported_as_a_bootstrap_failure(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    done = _run(src, tmp_path / "task", "-c", "raise SystemExit(1)")
    assert done.returncode == 1
    assert "BOOTSTRAP FAILED" not in done.stderr, (
        "a test failure was reported as a broken environment, which sends the developer to the "
        "wrong problem entirely")


def test_a_virtualenv_that_cannot_be_created_stops_before_any_test(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    shim = tmp_path / "bin"
    shim.mkdir()
    # `python -m venv` fails; everything else still resolves normally
    (shim / "python").write_text('#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
                                 '  echo "venv: refused" >&2; exit 1\nfi\n'
                                 f'exec "{sys.executable}" "$@"\n')
    (shim / "python").chmod(0o755)
    done = _run(src, tmp_path / "task", "-c", "open('/tmp/should-never-exist-probe','w')",
                env_extra={"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"})
    assert done.returncode != 0
    assert "BOOTSTRAP FAILED" in done.stderr and "virtualenv" in done.stderr, done.stderr[-2000:]
    # the message wraps, so it is read as prose rather than as one literal line
    assert "No test has run" in " ".join(done.stderr.split()), (
        "the message does not say whether anything ran")
    assert not Path("/tmp/should-never-exist-probe").exists(), "a test ran after bootstrap failed"


def test_a_project_that_cannot_be_installed_fails_nonzero(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    (src / "pyproject.toml").write_text("[project]\nthis is not toml\n")
    done = _run(src, tmp_path / "task", "-c", "print('unreachable')")
    assert done.returncode != 0, "an uninstallable checkout reported success"
    assert "BOOTSTRAP FAILED" in done.stderr
    assert "unreachable" not in done.stdout


def test_the_task_workspace_path_is_checked_before_anything_is_removed(tmp_path):
    src = _fixture_checkout(tmp_path / "repo")
    for bad in ("", "relative/path", "/tmp", "/", str(tmp_path / "repo")):
        done = _run(src, Path(bad) if bad else Path(""), "-c", "print(1)")
        assert done.returncode != 0, f"the runner accepted a task workspace of {bad!r}"
        assert "BOOTSTRAP FAILED" in done.stderr, done.stderr[-500:]
    assert (src / "pyproject.toml").exists(), "the checkout was damaged by a refused path"



def _junit(path: Path, *, tests: int, failures: int = 0, skips: list[str] | None = None) -> Path:
    cases = "".join(
        f'<testcase classname="c" name="t{i}"><skipped message="{m}"/></testcase>'
        for i, m in enumerate(skips or []))
    path.write_text(f'<testsuite tests="{tests}" failures="{failures}" errors="0">{cases}'
                    "</testsuite>")
    return path


def _assert_record(xml: Path, tier: str, allow: str = ""):
    return subprocess.run([sys.executable, str(RECORD), str(xml), tier, allow],
                          capture_output=True, text=True, timeout=120)


def test_a_run_that_collected_nothing_is_refused(tmp_path):
    done = _assert_record(_junit(tmp_path / "z.xml", tests=0), "unit")
    assert done.returncode != 0, "an empty run was accepted as a passing run"
    assert "ZERO tests" in done.stderr


def test_a_run_that_collected_tests_is_accepted(tmp_path):
    done = _assert_record(_junit(tmp_path / "ok.xml", tests=7), "unit")
    assert done.returncode == 0, done.stderr
    assert "7 collected" in done.stdout


def test_a_skip_outside_the_policy_is_refused(tmp_path):
    xml = _junit(tmp_path / "s.xml", tests=3, skips=["needs a licensed fixture"])
    assert _assert_record(xml, "unit").returncode != 0
    assert _assert_record(xml, "unit", "licensed").returncode == 0
