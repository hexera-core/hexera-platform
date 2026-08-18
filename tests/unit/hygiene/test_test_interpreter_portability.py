# Responsibility: Keep the suite runnable by whatever interpreter collected it, not by one path.
# Boundaries: it inspects how tests NAME an interpreter; it runs none of them and judges no assertion.
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from tests._scan import scanned

REPO = Path(__file__).parents[3]

#: A repository-managed virtualenv used as an EXECUTABLE. `make setup` builds one, so a developer
#: never notices the coupling - but CI installs into the runner's own Python and builds no .venv,
#: and the release images have no checkout at all. A test that spawns this path is therefore
#: silently unenforceable in the two environments that actually gate a merge and a release.
#: This is not hypothetical: it is how 21 Gate D promotion tests came to raise FileNotFoundError
#: everywhere except a developer's own machine. `sys.executable` is the portable answer.
_VENV_PATH_FRAGMENT = ".venv/bin/"

#: Comments are prose and may DISCUSS the path (the one above does). Only a string literal can be
#: handed to subprocess, so the scan reads literals from the parsed tree rather than raw text -
#: that is the difference between naming the defect and committing it.
def _interpreter_literals(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:                      # not importable anyway; another test owns that
        return []
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and _VENV_PATH_FRAGMENT in node.value]


#: This file is the ONE exemption, and it is unavoidable: a rule against naming a string has to
#: name that string to define itself. Nothing else is exempt, and the test below pins the exemption
#: to exactly this path so it cannot quietly widen into "hygiene tests may do as they please".
_SELF = Path(__file__).resolve().relative_to(REPO.resolve()).as_posix()


def _tracked_test_sources() -> list[str]:
    out = subprocess.run(["git", "ls-files", "tests"], cwd=str(REPO),
                         capture_output=True, text=True, check=True).stdout.split()
    return [rel for rel in out if rel.endswith(".py") and rel != _SELF]


def test_the_scan_exempts_only_its_own_definition():
    assert _SELF == "tests/unit/hygiene/test_test_interpreter_portability.py"
    assert _SELF not in _tracked_test_sources()
    # and the exemption is load-bearing: this file really does carry the fragment it forbids
    assert _interpreter_literals(Path(__file__).read_text(encoding="utf-8"))


def test_no_test_spawns_a_repository_venv_interpreter():
    offenders = []
    for rel in scanned(_tracked_test_sources(), "tracked test sources", at_least=300):
        for literal in _interpreter_literals((REPO / rel).read_text(encoding="utf-8")):
            offenders.append(f"{rel}: {literal!r}")
    assert offenders == [], (
        "a test names a repository-venv interpreter as a string. Use `sys.executable` so the "
        "suite runs under whatever interpreter collected it:\n  " + "\n  ".join(offenders))


def test_the_promotion_gate_spawns_the_collecting_interpreter():
    # The concrete control. The scan above forbids the wrong path; this proves the ONE test that
    # shells out to the release-record tool names the right one - so the guard cannot pass merely
    # because that call was deleted.
    src = (REPO / "tests/unit/deploy/test_release_promotion.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    spawns = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"]
    record_tool_spawns = [
        call for call in spawns
        if call.args and isinstance(call.args[0], ast.List)
        and any(isinstance(el, ast.Call) and isinstance(el.func, ast.Name) and el.func.id == "str"
                and isinstance(el.args[0], ast.Name) and el.args[0].id == "RECORD_TOOL"
                for el in call.args[0].elts)
    ]
    assert record_tool_spawns, (
        "no subprocess.run([... RECORD_TOOL ...]) found in test_release_promotion.py - the "
        "promotion gate no longer shells out to the record tool, so this control is vacuous")
    for call in record_tool_spawns:
        head = call.args[0].elts[0]
        assert (isinstance(head, ast.Attribute) and head.attr == "executable"
                and isinstance(head.value, ast.Name) and head.value.id == "sys"), (
            "the release-record tool must be invoked with sys.executable, not a fixed path")
