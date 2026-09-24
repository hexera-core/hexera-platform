# Responsibility: Prove `devtools/quality/check_vendored_agent.py` fails on a stale vendored wheel, and that
#                 the three things it can conclude are told apart.
# Boundaries: it builds no wheel anybody installs and touches no real agent checkout except to READ it.
#
# THE DEFECT THIS EXISTS FOR is the one the gate exists for: the vendored wheel was fourteen agent commits
# behind, PROVENANCE.json recorded its commit faithfully, and nothing compared the two. The gate's own failure
# mode would be to look like it checks that while checking nothing, so the central test here builds a
# SYNTHETIC agent repository, vendors a wheel from it, moves the agent on, and demands a non-zero exit. The
# gate is run as a subprocess, the way the Makefile and CI run it, rather than imported and called.
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GATE = "devtools/quality/check_vendored_agent.py"

#: The gate's own three outcomes. Distinct on purpose: a caller that read "could not check" as "clean" would
#: ship exactly the staleness this is about.
EXIT_OK, EXIT_DRIFT, EXIT_CANNOT_CHECK = 0, 1, 2


def _run(cwd: Path, *args: str, env_agent: str | None = None,
         script: Path | None = None) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.pop("GEOMETRY_AGENT_REPO", None)
    if env_agent is not None:
        env["GEOMETRY_AGENT_REPO"] = env_agent
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, str(script or (cwd / GATE)), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=600, env=env)


# ---------------------------------------------------------------- the real checkout, in both worlds

def test_it_reaches_a_verdict_here_or_says_it_could_not_and_neither_is_a_skip():
    """This suite runs where the agent checkout is beside this one AND where it is not - a CI runner clones
    this repository alone. Both are real states and neither may be silent, so both are asserted: where the
    agent is reachable the tracked wheel must PASS its own gate, and where it is not the gate must say CANNOT
    CHECK and exit 2. What is refused is the third answer, the one that looks like a pass."""
    done = _run(REPO)
    out = done.stdout + done.stderr
    if done.returncode == EXIT_OK:
        assert out.startswith("OK:"), out
        assert "CANNOT CHECK" not in out
        # the verdict names the wheel and the commit it was reached about, or it is not a verdict
        assert re.search(r"hexera_geometry_agent-.*\.whl is agent [0-9a-f]{10}", out), out
    else:
        assert done.returncode == EXIT_CANNOT_CHECK, (
            f"the tracked wheel FAILS its own gate (exit {done.returncode}). Re-vendor it:\n{out}")
        assert "CANNOT CHECK" in out, out
        assert "GEOMETRY_AGENT_REPO" in out, f"it does not say what would make it work:\n{out}"


def test_it_does_not_fail_as_a_traceback(tmp_path):
    done = _run(REPO, env_agent=str(tmp_path / "nowhere"))
    combined = done.stdout + done.stderr
    assert done.returncode == EXIT_CANNOT_CHECK
    for noise in ("Traceback (most recent call last)", 'File "', "Error:"):
        assert noise not in combined, f"the operator is shown {noise!r}:\n{combined}"


def test_could_not_check_is_not_a_pass_and_the_flag_says_so_out_loud(tmp_path):
    """`--allow-unavailable` exists for CI, which has no agent checkout. It may not be a quiet downgrade: the
    run that checked nothing has to SAY it checked nothing, or a green lane reads as a verdict."""
    nowhere = str(tmp_path / "nowhere")
    strict = _run(REPO, env_agent=nowhere)
    lenient = _run(REPO, "--allow-unavailable", env_agent=nowhere)
    assert strict.returncode == EXIT_CANNOT_CHECK and lenient.returncode == EXIT_OK
    assert "CANNOT CHECK" in lenient.stdout, lenient.stdout
    assert "NOTHING ABOUT THE WHEEL'S CURRENCY WAS CHECKED" in lenient.stdout, (
        f"a run that checked nothing exits 0 without saying so:\n{lenient.stdout}")
    assert not lenient.stdout.startswith("OK:"), (
        "a run that could not check begins with the same word as a verdict")


def test_a_directory_that_is_not_the_agent_is_refused_rather_than_read(tmp_path):
    # A path that exists and is not the agent is the likeliest wrong value, and reading it would produce a
    # verdict about nothing.
    (tmp_path / "notagent").mkdir()
    done = _run(REPO, env_agent=str(tmp_path / "notagent"))
    assert done.returncode == EXIT_CANNOT_CHECK
    assert "hexera-geometry-agent" in done.stdout, done.stdout


# ---------------------------------------------------------------- the defect, on a synthetic agent

AGENT_PYPROJECT = """[project]
name = "hexera-geometry-agent"
version = "0.1.0"

[tool.setuptools.package-data]
geometry_agent = ["agent/thresholds.json"]
"""

#: The module the synthetic wheel carries, in two versions: the one the wheel is built from and the one the
#: agent moves on to. The second is the fourteen-commit defect in miniature - a name the platform would read
#: that the shipped wheel does not have.
BEFORE = 'LOOK_STATES = ("ok", "failed")\n'
AFTER = 'LOOK_STATES = ("ok", "failed", "not_attempted")\nLOOK_NOT_ATTEMPTED = "not_attempted"\n'


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo),
                           "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                           *args], capture_output=True, text=True, timeout=120)


@pytest.fixture
def synthetic(tmp_path):
    """A platform checkout carrying the real gate and a vendored wheel, beside an agent repository the wheel
    was really built from. Everything the gate reads is here; nothing real is touched."""
    plat = tmp_path / "platform"
    (plat / "devtools" / "quality").mkdir(parents=True)
    shutil.copy2(REPO / GATE, plat / GATE)
    agent = tmp_path / "geometry_agent"
    (agent / "src" / "geometry_agent" / "agent").mkdir(parents=True)
    (agent / "pyproject.toml").write_text(AGENT_PYPROJECT, encoding="utf-8")
    (agent / "src" / "geometry_agent" / "__init__.py").write_text(BEFORE, encoding="utf-8")
    (agent / "src" / "geometry_agent" / "agent" / "thresholds.json").write_text("{}\n", encoding="utf-8")
    assert _git(agent, "init", "-q", "-b", "master").returncode == 0
    assert _git(agent, "add", "-A").returncode == 0
    assert _git(agent, "commit", "-q", "-m", "the commit the wheel is built from").returncode == 0
    built_from = _git(agent, "rev-parse", "HEAD").stdout.strip()
    assert len(built_from) == 40

    def vendor(body: str, *, commit: str = built_from, crlf: bool = False) -> None:
        out = plat / "vendor" / "wheels"
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.whl"):
            old.unlink()
        wheel = out / f"hexera_geometry_agent-0.1.0+g{commit[:10]}-py3-none-any.whl"
        text = body.replace("\n", "\r\n") if crlf else body
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr("geometry_agent/__init__.py", text)
            z.writestr("geometry_agent/agent/thresholds.json", "{}\n")
        (out / "PROVENANCE.json").write_text(json.dumps(
            {"wheel": wheel.name, "version": f"0.1.0+g{commit[:10]}", "agent_commit": commit,
             "agent_commit_short": commit[:10], "agent_checkout_dirty": False}, indent=2), encoding="utf-8")

    def run(*args: str):
        return _run(plat, *args, env_agent=str(agent), script=plat / GATE)

    return type("Synthetic", (), {"plat": plat, "agent": agent, "built_from": built_from,
                                  "vendor": staticmethod(vendor), "run": staticmethod(run),
                                  "git": staticmethod(lambda *a: _git(agent, *a))})


def test_a_wheel_built_from_the_agents_current_source_passes(synthetic):
    # The premise of every failing case below. Without it a failure proves nothing: a gate that always
    # fails catches the defect and is useless.
    synthetic.vendor(BEFORE)
    done = synthetic.run()
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    assert done.stdout.startswith("OK:")


def test_a_wheel_the_agent_has_moved_past_fails_and_names_the_file_that_moved(synthetic):
    """THE DEFECT, reproduced. The wheel is a faithful build of a commit that is no longer the agent source,
    which is exactly the state the real vendor/wheels/ was in for fourteen commits."""
    synthetic.vendor(BEFORE)
    (synthetic.agent / "src" / "geometry_agent" / "__init__.py").write_text(AFTER, encoding="utf-8")
    assert synthetic.git("add", "-A").returncode == 0
    assert synthetic.git("commit", "-q", "-m", "the agent gains what the platform reads").returncode == 0
    done = synthetic.run()
    assert done.returncode == EXIT_DRIFT, (
        f"the agent moved on under the wheel and the gate called it clean:\n{done.stdout}{done.stderr}")
    assert "src/geometry_agent/__init__.py" in done.stdout, (
        f"the failure does not name the file that moved:\n{done.stdout}")
    assert "1 commit(s) on" in done.stdout, done.stdout
    assert "vendor_geometry_agent.sh" in done.stdout, (
        f"the failure diagnoses without saying how to fix it:\n{done.stdout}")


def test_a_commit_that_touched_nothing_the_wheel_is_built_from_is_not_drift(synthetic):
    """The precision that keeps this from crying wolf. This repository does not own the agent's branch, and an
    agent commit that only touched its own tests, docs or eval corpus produces a byte-identical wheel. A gate
    that measured DISTANCE would fail on it, be overridden, and then be off on the day it mattered."""
    synthetic.vendor(BEFORE)
    (synthetic.agent / "tests").mkdir()
    (synthetic.agent / "tests" / "test_something.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert synthetic.git("add", "-A").returncode == 0
    assert synthetic.git("commit", "-q", "-m", "the agent's own suite moves").returncode == 0
    done = synthetic.run()
    assert done.returncode == EXIT_OK, (
        f"an agent commit outside src/ and pyproject.toml was called a stale wheel:\n{done.stdout}")


def test_a_wheel_relabelled_with_a_commit_it_was_not_built_from_fails(synthetic):
    """The half PROVENANCE.json's sha256 cannot answer. That digest is taken FROM the wheel, so it proves only
    that nobody swapped the file afterwards - a wheel built by hand, or from a dirty tree, or from one commit
    and labelled with another, matches its own digest perfectly."""
    synthetic.vendor(AFTER, commit=synthetic.built_from)   # content of a later state, labelled as the first
    done = synthetic.run()
    assert done.returncode == EXIT_DRIFT, (
        f"the wheel's contents are not the commit it names and the gate called it clean:\n{done.stdout}")
    assert "differ from commit" in done.stdout, done.stdout


def test_an_agent_checkout_with_uncommitted_source_changes_is_drift(synthetic):
    # No wheel matches a working tree that is ahead of its own HEAD, so the state cannot be called clean.
    synthetic.vendor(BEFORE)
    (synthetic.agent / "src" / "geometry_agent" / "__init__.py").write_text(AFTER, encoding="utf-8")
    done = synthetic.run()
    assert done.returncode == EXIT_DRIFT, done.stdout
    assert "uncommitted change" in done.stdout, done.stdout


def test_a_recorded_commit_the_agent_does_not_have_is_named_rather_than_ignored(synthetic):
    synthetic.vendor(BEFORE, commit="0" * 40)
    done = synthetic.run()
    assert done.returncode == EXIT_DRIFT, done.stdout
    assert "does not have commit" in done.stdout, done.stdout


def test_line_endings_alone_are_not_drift_which_is_why_the_comparison_normalises(synthetic):
    """MEASURED, and the reason the comparison is not exact bytes. The agent checkout on Windows has CRLF
    working-tree files while `git show` returns the LF blob, so an exact comparison called 97 of 111 files in a
    CORRECT wheel different. A gate that fails on every Windows build gets switched off, and a gate that is off
    is the silence this replaces."""
    synthetic.vendor(BEFORE, crlf=True)
    done = synthetic.run()
    assert done.returncode == EXIT_OK, (
        f"a wheel whose only difference from its commit is line endings was called stale:\n{done.stdout}")
    # and the normalisation did not blind it: the same wheel, CRLF and all, still fails on real drift
    (synthetic.agent / "src" / "geometry_agent" / "__init__.py").write_text(AFTER, encoding="utf-8")
    assert synthetic.git("add", "-A").returncode == 0
    assert synthetic.git("commit", "-q", "-m", "real drift, CRLF wheel").returncode == 0
    assert synthetic.run().returncode == EXIT_DRIFT, (
        "normalising line endings swallowed a real difference as well")


# ---------------------------------------------------------------- it is wired in, not merely present

def test_the_gate_is_in_gate_b_and_not_downgraded_there():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    check = next((ln for ln in makefile.splitlines() if ln.startswith("check:")), "")
    assert "vendored-agent" in check, (
        f"`make check` does not depend on the vendored-agent gate, so nothing runs it:\n{check}")
    target = re.search(r"^vendored-agent:.*\n(?:\t.*\n)+", makefile, re.M)
    assert target, "the Makefile declares no vendored-agent recipe"
    assert GATE in target.group(0), target.group(0)
    assert "--allow-unavailable" not in target.group(0), (
        "`make vendored-agent` passes --allow-unavailable, so on the one kind of machine that CAN compare "
        "the wheel to the agent it reports instead of failing")
    assert "vendored-agent" in re.search(r"^\.PHONY:(?:.*\\\n)*.*$", makefile, re.M).group(0), (
        "vendored-agent is not in .PHONY, so a directory of that name would silence the gate")


def test_ci_runs_it_and_says_which_of_the_two_things_happened():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert GATE in ci, "no CI lane runs the vendored-agent gate at all"
    # The runner clones this repository alone, so it cannot compare and must not pretend to. The flag is
    # therefore expected HERE and refused in the Makefile target above.
    assert f"python {GATE} --allow-unavailable" in ci, (
        "CI runs the gate without --allow-unavailable, so the contracts lane is red on every commit for "
        "want of an agent checkout the runner has no way to get")


def test_the_gate_reads_what_the_vendor_script_builds_from():
    """The gate's subject has to be the vendoring script's inputs, or it watches the wrong files.

    Derived from the script rather than agreed with it in prose: the day it starts copying a third thing into
    the build, this fails instead of the gate quietly ignoring it.
    """
    script = (REPO / "deploy" / "vendor_geometry_agent.sh").read_text(encoding="utf-8")
    copied = set(re.findall(r'^\s*cp (?:-R )?"\$\{AGENT\}/([A-Za-z0-9_.\-/]+)"', script, re.M))
    assert copied, "no `cp \"${AGENT}/...\"` found in deploy/vendor_geometry_agent.sh"
    gate = (REPO / GATE).read_text(encoding="utf-8")
    watched = set(re.findall(r'"([A-Za-z0-9_.\-/]+)"',
                             re.search(r"WHEEL_IS_BUILT_FROM = \(([^)]*)\)", gate).group(1)))
    # the script copies `src`; the gate watches the one package under it that becomes the wheel
    unwatched = {c for c in copied if c not in watched and not any(w.startswith(c + "/") for w in watched)}
    assert not unwatched, (
        f"deploy/vendor_geometry_agent.sh copies {sorted(unwatched)} into the wheel build and "
        f"{GATE}'s WHEEL_IS_BUILT_FROM does not watch {'them' if len(unwatched) > 1 else 'it'}, so a change "
        f"there is a wheel that changed and a gate that says nothing")
