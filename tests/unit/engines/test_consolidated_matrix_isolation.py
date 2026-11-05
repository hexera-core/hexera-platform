# Responsibility: Verify the size matrix holds no shared accumulator and passes alone in a fresh interpreter.
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

MATRIX = Path(__file__).with_name("test_consolidated_matrix.py")
MATRIX_NODE = "tests/unit/engines/test_consolidated_matrix.py::test_the_measured_matrix_is_exactly_the_declared_one"
REPO = Path(__file__).resolve().parents[3]
RUN_TIMEOUT_S = 300


def _tree() -> ast.Module:
    return ast.parse(MATRIX.read_text(encoding="utf-8"))


def test_the_matrix_test_passes_alone_in_a_fresh_interpreter():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header",
         MATRIX_NODE],
        cwd=REPO, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    assert proc.returncode == 0, (
        "the consolidated matrix test does not pass on its own - it depends on another test "
        f"running first:\n{proc.stdout[-3000:]}")
    assert "1 passed" in proc.stdout, proc.stdout[-2000:]


def test_the_matrix_module_holds_no_mutable_accumulator():
    def _is_empty_container(value) -> bool:
        if isinstance(value, (ast.List, ast.Set)):
            return not value.elts
        if isinstance(value, ast.Dict):
            return not value.keys
        if isinstance(value, ast.Call):
            return ast.unparse(value.func) in ("list", "dict", "set", "defaultdict")
        return False

    offenders = []
    for node in _tree().body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if node.value is not None and _is_empty_container(node.value):
                offenders += names
    assert not offenders, (
        f"the matrix module declares mutable module-level state {offenders}; a test that fills it "
        "and a test that reads it are coupled by execution order")


def test_no_test_in_the_matrix_module_uses_naming_to_order_itself():
    names = [n.name for n in _tree().body
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert names, "no tests found - this check has stopped checking"
    offenders = [n for n in names if n.startswith(("test_zz", "test_aa", "test_00", "test_zzz"))]
    assert not offenders, (
        f"{offenders} use collection order as synchronisation; a shuffled run reorders them")


def test_the_matrix_test_builds_its_rows_inside_its_own_scope():
    fn = next(n for n in _tree().body
              if isinstance(n, ast.FunctionDef)
              and n.name == "test_the_measured_matrix_is_exactly_the_declared_one")
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "build_matrix" in calls, (
        "the matrix test no longer builds the matrix itself - it is reading state from somewhere")


def test_the_matrix_is_not_memoised_across_tests():
    import tests.unit.engines.test_consolidated_matrix as M

    assert not hasattr(M.build_matrix, "cache_info"), "build_matrix is memoised"
    for node in _tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_matrix":
            decorators = [ast.unparse(d) for d in node.decorator_list]
            assert not decorators, f"build_matrix carries decorators {decorators}"
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                src = ast.unparse(dec)
                if "fixture" in src:
                    assert "scope" not in src or "function" in src, (
                        f"{node.name} is a non-function-scoped fixture ({src}); state that "
                        "outlives one test reintroduces the coupling")


def test_the_matrix_test_asserts_an_exact_row_count():
    fn = next(n for n in _tree().body
              if isinstance(n, ast.FunctionDef)
              and n.name == "test_the_measured_matrix_is_exactly_the_declared_one")
    comparisons = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    exact = [c for c in comparisons
             if "len(rows)" in c and "TOTAL_ROWS" in c and "==" in c and ">=" not in c]
    assert exact, (
        "the matrix test no longer asserts an exact row count against TOTAL_ROWS: "
        f"{[c for c in comparisons if 'rows' in c]}")


def test_the_declared_matrix_is_not_vacuous():
    import tests.unit.engines.test_consolidated_matrix as M

    # Vacuity is the thing being refused: a declared total that no longer matches the rows behind
    # it, or a key that declares nothing. The former row and key COUNTS were pinned as literals,
    # which caught nothing these three do not and made every added row a hand-edited number.
    assert M.TOTAL_ROWS == sum(M.EXPECTED_ROWS.values())
    assert M.EXPECTED_ROWS, "the matrix declares no rows at all"
    assert all(n >= 1 for n in M.EXPECTED_ROWS.values())


@pytest.mark.parametrize("family", ["STL ascii", "STL binary", "VTP", "STEP", "IGES",
                                    "STEP assembly"])
def test_every_input_family_is_still_declared_in_the_matrix(family):
    import tests.unit.engines.test_consolidated_matrix as M

    assert any(k[0] == family for k in M.EXPECTED_ROWS), (
        f"the {family} family left the matrix - it can no longer regress unnoticed")
