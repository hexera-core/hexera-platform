# Responsibility: Prove an absent geometry agent fails the tests that need it and no others, and that a lane which ran nothing says so.
# Boundaries: it exercises tests/_surveyor_package.require and drives pytest over throwaway files; it imports no agent module.
#
# THE DEFECT, which is the previous fix's own side effect. Every surveyor test used to open with
# `pytest.importorskip("geometry_agent...")`, so the suite went green in the one environment where the
# package was missing. That was replaced with tests/_surveyor_package.require, which FAILS instead of
# skipping - correctly. But three modules call it at MODULE SCOPE, as a gate over the whole file, and a
# failure raised during import is a COLLECTION ERROR. pytest answers three of those with
# "Interrupted: 3 errors during collection" and stops, so 7252 collected tests ran ZERO times on every
# commit. The noise was aimed at the right thing and arrived at the wrong granularity: a test that says
# nothing because it skipped and a suite that says nothing because it never ran are the same lie.
#
# WHAT THIS FILE PINS. That a module-scope gate fails ITS OWN module's tests and leaves every other
# test in the session alone; that the developer's opt-out still skips; that the returned stand-in
# cannot be used by accident; and that the collection gate refuses a run which collected nothing.
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from tests import _surveyor_package
from tests._surveyor_package import ABSENT_ENV, GATE_FIXTURE, require

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import check_unit_collection as collection_gate  # noqa: E402, I001 - the gate lives outside the package

#: A module name that cannot resolve however the agent is installed, so these tests measure the DECISION
#: require() makes about absence and never the presence of the real package.
ABSENT_MODULE = "geometry_agent.__no_such_module_for_this_test__"


# ---------------------------------------------------------------- the granularity, in a real session

_GATED_MODULE = '''
from tests._surveyor_package import require

agent = require("geometry_agent.__no_such_module_for_this_test__", needs="a module that does not exist")


def test_one_that_needs_the_agent():
    assert agent is not None


def test_another_that_needs_the_agent():
    assert True
'''

_BYSTANDER_MODULE = '''
def test_a_test_that_has_nothing_to_do_with_the_agent():
    assert 2 + 2 == 4
'''


def _session(tmp_path: Path, *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """A real pytest session over two throwaway modules: one gated on an absent agent, one not.

    Driven as a subprocess because what is being measured is the SESSION's behaviour - whether it
    interrupts - and a session cannot measure that about itself.
    """
    (tmp_path / "test_gated.py").write_text(textwrap.dedent(_GATED_MODULE), encoding="utf-8")
    (tmp_path / "test_bystander.py").write_text(textwrap.dedent(_BYSTANDER_MODULE), encoding="utf-8")
    import os

    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + str(REPO / "src")}
    env.pop(ABSENT_ENV, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", "-rsE"],
        capture_output=True, text=True, cwd=str(REPO), timeout=600, check=False, env=env)


def test_a_module_scope_gate_does_not_interrupt_the_session(tmp_path):
    done = _session(tmp_path)
    assert "errors during collection" not in done.stdout, (
        "a module-scope require() still ends collection, which stops the whole session and runs "
        f"nothing:\n{done.stdout}")
    assert "Interrupted" not in done.stdout, done.stdout


def test_the_bystander_still_runs(tmp_path):
    # The whole point. The tests that need the package fail; the ones that do not still answer.
    done = _session(tmp_path)
    assert "1 passed" in done.stdout, (
        f"a test with nothing to do with the geometry agent did not run:\n{done.stdout}")


def test_every_test_in_the_gated_module_fails_and_says_why(tmp_path):
    done = _session(tmp_path)
    assert "2 errors" in done.stdout or "2 failed" in done.stdout, done.stdout
    assert "a module that does not exist" in done.stdout, (
        f"the failure does not say what the module wanted the package FOR:\n{done.stdout}")
    assert ABSENT_ENV in done.stdout, (
        f"the failure does not name the one variable that silences it:\n{done.stdout}")


def test_the_opt_out_turns_the_gate_into_a_skip(tmp_path):
    # The cost of the loud default, paid by the only person entitled to pay it: somebody working from a
    # checkout who does not have the agent repository.
    done = _session(tmp_path, env_extra={ABSENT_ENV: "1"})
    assert "2 skipped" in done.stdout, done.stdout
    assert "1 passed" in done.stdout, done.stdout


# ---------------------------------------------------------------- the stand-in cannot be used quietly

def test_the_stand_in_fails_when_anything_reads_off_it(monkeypatch):
    monkeypatch.delenv(ABSENT_ENV, raising=False)
    monkeypatch.setattr(_surveyor_package, "_meshpipeline_is_installed", lambda: False)
    verdict = _surveyor_package.Verdict(outcome=pytest.fail, message="the package is not here")
    absent = _surveyor_package.Absent("geometry_agent.facts.measure", verdict)
    with pytest.raises(pytest.fail.Exception, match="the package is not here"):
        absent.measure_isolated
    with pytest.raises(pytest.fail.Exception, match="the package is not here"):
        absent()


def test_the_stand_ins_repr_never_raises():
    # pytest prints the repr of a module attribute while building a failure report. A repr that failed
    # would replace the real reason with a second, confusing one.
    verdict = _surveyor_package.Verdict(outcome=pytest.fail, message="not here")
    assert "geometry_agent.facts.measure" in repr(_surveyor_package.Absent(
        "geometry_agent.facts.measure", verdict))


def test_a_call_from_inside_a_test_still_ends_that_test_immediately(monkeypatch):
    # The module-scope branch must not have changed the ordinary case: inside a test or a fixture,
    # raising is exactly right and the outcome arrives now.
    monkeypatch.delenv(ABSENT_ENV, raising=False)
    monkeypatch.setattr(_surveyor_package, "_meshpipeline_is_installed", lambda: False)
    with pytest.raises(pytest.fail.Exception):
        require(ABSENT_MODULE, needs="a module that does not exist")


def test_the_gate_fixture_is_scoped_to_the_module_that_asked_for_it():
    # A fixture defined in a test module is visible to that module and to nothing else, which is why the
    # gate is installed there rather than in a conftest. If it leaked, an unrelated module that happened
    # to request it would fail for a reason that is not about it.
    assert GATE_FIXTURE not in globals(), (
        "this module never called require() at module scope, so it must carry no gate fixture")


# ---------------------------------------------------------------- a lane that ran nothing says so

def test_the_collection_gate_refuses_a_run_that_collected_nothing(monkeypatch, capsys):
    monkeypatch.setattr(collection_gate, "collect", lambda: (2, [], "Interrupted: 3 errors during collection"))
    assert collection_gate.check(quiet=True) == 1
    out = capsys.readouterr().out
    assert "ran zero times" in out, out


def test_the_collection_gate_refuses_a_plausible_total_with_a_dark_area(monkeypatch, capsys):
    # The version of this defect a count alone cannot see: the total stays believable and one tree
    # stopped being collected.
    ids = [f"tests/unit/hygiene/test_x.py::test_{i}" for i in range(collection_gate.FLOOR + 10)]
    monkeypatch.setattr(collection_gate, "collect", lambda: (0, ids, ""))
    assert collection_gate.check(quiet=True) == 1
    assert "contributed no collected test" in capsys.readouterr().out


def test_the_collection_gate_reports_the_floor_it_is_measuring_against(monkeypatch, capsys):
    ids = [f"tests/unit/hygiene/test_x.py::test_{i}" for i in range(3)]
    monkeypatch.setattr(collection_gate, "collect", lambda: (0, ids, ""))
    assert collection_gate.check(quiet=True) == 1
    assert str(collection_gate.FLOOR) in capsys.readouterr().out


def test_the_ci_unit_lane_checks_collection_before_it_runs_the_suite():
    # The order is the property. A collection check after the suite tells you why a lane you already
    # read as three broken tests actually ran nothing.
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "devtools/quality/check_unit_collection.py" in workflow
    assert workflow.index("check_unit_collection.py") < workflow.index("run: make test-fast")


def test_ci_installs_the_geometry_agent_so_the_surveyor_tests_actually_run():
    # The other half of item 4. With no wheel installed, every surveyor test in CI fails loudly, which
    # is correct and useless: the lane is red for a reason nobody can fix by editing a test. CI carries
    # the same distribution the image does.
    action = (REPO / ".github" / "actions" / "setup-hexera" / "action.yml").read_text(encoding="utf-8")
    assert "hexera_geometry_agent-*.whl" in action
    assert "--no-deps" in action
    assert "import geometry_agent" in action, (
        "installing the wheel is not the point; importing it is. A wheel that installs and cannot "
        "import is the defect this whole arrangement exists to catch.")
