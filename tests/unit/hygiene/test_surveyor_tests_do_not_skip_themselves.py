# Responsibility: Prove the Surveyor's tests cannot go quiet when the geometry agent is missing.
# Boundaries: it scans the test tree's own text and exercises tests/_surveyor_package.require; it imports no
#             agent module, so its verdict does not depend on the package it is about.
#
# THE SHAPE OF THE DEFECT. Every surveyor test opened with pytest.importorskip("geometry_agent..."). Nothing
# installed the package into any image. So in the one environment where the Surveyor did not exist, the suite
# reported green and said nothing - and the worst defect in the project, that nothing installs the package,
# was invisible to every check for exactly that reason. A skip is a test declining to answer, and a suite
# that declines to answer about the product's central feature reads the same as one that says it works.
#
# The house already takes this position elsewhere: the `ui` marker in pyproject.toml says the browser tier
# "FAILS rather than skips without one - a release must not pass with its browser validation quietly
# absent". This is that rule, applied to the Surveyor.
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tests import _surveyor_package
from tests._surveyor_package import ABSENT_ENV, require

REPO = Path(__file__).resolve().parents[3]
TESTS = REPO / "tests"
#: A module name that cannot resolve however the agent is installed, so these tests measure the DECISION
#: require() makes about absence and never the presence of the real package.
ABSENT_MODULE = "geometry_agent.__no_such_module_for_this_test__"


def _test_files() -> list[Path]:
    return list(TESTS.rglob("test_*.py"))


def _imports_the_agent(text: str) -> bool:
    """True when this file really imports a geometry_agent module - decided by its SYNTAX.

    A line-by-line pattern cannot tell an import from an import QUOTED INSIDE A STRING, and it read one
    wrong: tests/unit/hygiene/test_publication_context_resolver.py holds synthetic module bodies as string
    constants, and one of them contains an import of the agent's contract package. That file imports no
    agent module - it never executes that text, it only parses it - and the guard named it anyway. A guard
    that reports a file which cannot fail the way the guard is about is a guard nobody can act on, and the
    scan below is worth its failure message only while this distinction holds.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:                       # pragma: no cover - a test file that will not parse
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "geometry_agent" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if (node.module or "").split(".")[0] == "geometry_agent":
                return True
    return False


#: What the detector must and must not call an import of the agent. The multi-line cases are written out
#: rather than escaped, so this file reads as the source it is about.
_A_MODULE_IMPORT = "import geometry_agent"
_A_FROM_IMPORT = "from geometry_agent.agent import hexera"
#: THE SHAPE THE GUARD IS REALLY FOR: a bare import inside a test body. Where the agent is absent it raises
#: ImportError mid-test, which reads as a broken test rather than as a missing Surveyor.
_AN_IMPORT_INSIDE_A_TEST = '''
def t():
    from geometry_agent.agent.hexera import MAX_CELLS_CAP
    return MAX_CELLS_CAP
'''
#: The false positive that cost this guard its credibility: source held as data.
_AN_IMPORT_QUOTED_AS_DATA = """
SOURCE = '''
from geometry_agent.contract import given
'''
"""
_A_COMMENTED_OUT_IMPORT = "# from geometry_agent.agent import hexera"
_A_PLATFORM_MODULE_NAMED_AFTER_THE_AGENT = (
    "from meshpipeline.contracts.geometry_agent_block import platform_cell_ceiling")
_THE_ONE_DOOR = 'hexera = require("geometry_agent.agent.hexera")'


# ---------------------------------------------------------------- the regression guard

def test_no_test_skips_itself_over_the_geometry_agent():
    offenders = []
    pattern = re.compile(r"importorskip\(\s*[\"']geometry_agent")
    for path in _test_files():
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            # A comment that QUOTES the old call is documentation, including the ones in this file and in
            # tests/unit/deploy/test_geometry_agent_distribution.py explaining why the call is gone.
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO).as_posix()}:{i}")
    assert not offenders, (
        "these tests skip themselves when the geometry agent is absent, which is how an image that shipped "
        f"without it passed every check: {offenders}. Use tests/_surveyor_package.require instead - it "
        f"fails loudly and cannot be silenced against an installed distribution.")


def test_every_surveyor_test_that_needs_the_agent_asks_for_it_loudly():
    # A test that imports the agent with a bare `import geometry_agent...` at module scope would end as a
    # collection ERROR rather than a readable failure, and one that wraps it in try/except would go quiet
    # again. require() is the one door.
    silent = []
    for path in _test_files():
        text = path.read_text(encoding="utf-8")
        if not _imports_the_agent(text):
            continue
        if "_surveyor_package import require" not in text and "require(" not in text:
            silent.append(path.relative_to(REPO).as_posix())
    assert not silent, (
        f"these tests import the geometry agent directly without going through "
        f"tests/_surveyor_package.require, so its absence is a collection error or a silence rather than a "
        f"stated failure: {silent}")


@pytest.mark.parametrize("source,detected", [
    (_A_MODULE_IMPORT, True),
    (_A_FROM_IMPORT, True),
    (_AN_IMPORT_INSIDE_A_TEST, True),
    (_AN_IMPORT_QUOTED_AS_DATA, False),
    (_A_COMMENTED_OUT_IMPORT, False),
    (_A_PLATFORM_MODULE_NAMED_AFTER_THE_AGENT, False),
    (_THE_ONE_DOOR, False),
])
def test_the_import_detector_answers_on_syntax_and_not_on_text(source, detected):
    # Measured, not inferred from the scan above coming back empty: an empty scan is also what a detector
    # that answers False to everything produces.
    assert _imports_the_agent(source) is detected


# ---------------------------------------------------------------- where the line is drawn

def test_absence_is_a_failure_when_nothing_has_said_otherwise(monkeypatch):
    # THE DEFAULT IS THE LOUD ONE. A CI job, a container or a script that forgot to say anything gets the
    # noise. This is the direction that matters: the old default was silence.
    monkeypatch.delenv(ABSENT_ENV, raising=False)
    monkeypatch.setattr(_surveyor_package, "_meshpipeline_is_installed", lambda: False)
    with pytest.raises(pytest.fail.Exception) as caught:
        require(ABSENT_MODULE, needs="a module that does not exist")
    assert ABSENT_ENV in str(caught.value), "the failure does not name the one variable that silences it"
    assert "a module that does not exist" in str(caught.value), (
        "the failure does not say what the test wanted the package FOR")


def test_a_developer_without_the_agent_can_still_run_the_rest_of_the_suite(monkeypatch):
    # The cost of the loud default, paid once, by the only person entitled to pay it: someone working from
    # a checkout who does not have the agent repository.
    monkeypatch.setenv(ABSENT_ENV, "1")
    monkeypatch.setattr(_surveyor_package, "_meshpipeline_is_installed", lambda: False)
    with pytest.raises(pytest.skip.Exception):
        require(ABSENT_MODULE)


@pytest.mark.parametrize("opt_out", ["1", "true", "yes", "on"])
def test_the_opt_out_cannot_travel_into_a_built_environment(monkeypatch, opt_out):
    # THE OTHER HALF OF THE LINE, and the half the defect needed. Against an INSTALLED meshpipeline - a
    # built image, or any environment built from the wheel - absence is a failure and nothing silences it.
    # An image either carries the package or it is broken, so no environment variable may be able to turn
    # that answer into a skip. `make test-container-smoke` runs the suite inside the image, which is where
    # this rule bites.
    monkeypatch.setenv(ABSENT_ENV, opt_out)
    monkeypatch.setattr(_surveyor_package, "_meshpipeline_is_installed", lambda: True)
    with pytest.raises(pytest.fail.Exception) as caught:
        require(ABSENT_MODULE)
    message = str(caught.value)
    assert "INSTALLED" in message and "deliberately NOT honoured" in message, (
        "the failure does not say why the opt-out was ignored here")


def test_the_helper_refuses_to_be_used_for_anything_but_the_agent():
    # It decides what ABSENCE OF THE AGENT means. Pointed at pyvista or gmsh it would turn an ordinary
    # optional dependency into a release-blocking failure, which is not a judgement it is entitled to make.
    with pytest.raises(ValueError):
        require("pyvista")


def test_the_installed_check_asks_the_same_question_the_image_build_asks():
    # require()'s hard half turns on whether meshpipeline resolves from an install root. deploy/
    # verify_install.py asks that of the geometry agent inside the build. If the two ever disagree about
    # what "installed" means, one of them is guarding nothing.
    verify = (REPO / "deploy" / "verify_install.py").read_text(encoding="utf-8")
    helper = (TESTS / "_surveyor_package.py").read_text(encoding="utf-8")
    for source, name in ((verify, "deploy/verify_install.py"), (helper, "tests/_surveyor_package.py")):
        assert "site.getsitepackages()" in source and "getusersitepackages()" in source, (
            f"{name} no longer decides 'installed' by the site roots, so the two checks have drifted")
        assert ".parents" in source, f"{name} no longer tests the package path against those roots"
