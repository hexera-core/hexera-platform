# Responsibility: Verify the native terminal tier proves disposable-database authority before it clears tables.
# Boundaries: the gate's shape - whether it refuses at runtime is the native runner's own proof.
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NATIVE = REPO / "tests" / "native"
CONFTEST = NATIVE / "conftest.py"
RUNNER = NATIVE / "run_terminal_matrix.sh"
TIER_RUNNER = NATIVE / "run_tier.sh"
DESTRUCTIVE = ("truncate_tables", "reset_schema", "drop_tables")


def _native_sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in sorted(NATIVE.rglob("*.py")) if "__pycache__" not in str(p)]


def test_every_native_destructive_caller_sits_under_the_session_gate():
    callers = [p.name for p, src in _native_sources()
               if any(f"hp.{d}" in src or f" {d}(" in src for d in DESTRUCTIVE)]
    assert callers, "no native module calls a destructive helper - has the tier moved?"
    src = CONFTEST.read_text()
    assert "def pytest_collection(" in src, (
        f"{callers} clear application tables, but tests/native/conftest.py establishes no "
        "disposable-database authority - the helpers would fail closed with no way to be granted it")


def test_the_native_gate_uses_the_shared_authority_implementation():
    src = CONFTEST.read_text()
    assert "from tests import disposable_database as dd" in src
    assert "dd.authorize(" in src, "the gate no longer proves the database from the live server"
    assert "hp.set_session_authority(" in src, \
        "the granted authority is not published where the destructive helpers look for it"
    # dd.authorize is the ONLY way to mint one. Constructing the dataclass directly would let the
    # gate assert disposability instead of proving it from the server.
    assert "DisposableAuthority(" not in src, \
        "the gate constructs an authority object directly instead of proving one"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "pytest_collection")
    grants = [ast.unparse(n) for n in ast.walk(fn)
              if isinstance(n, ast.Call) and "set_session_authority" in ast.unparse(n.func)]
    assert grants and all("dd.authorize(" in g for g in grants), \
        f"an authority is published without being proven: {grants}"


def test_the_native_gate_duplicates_no_authority_rule():
    src = CONFTEST.read_text()
    for rule in ("current_database()", "system_identifier", "_mesh_disposable", "MARKER"):
        assert rule not in src, (
            f"tests/native/conftest.py re-implements {rule!r}; the rules live in "
            "tests/disposable_database.py and must have exactly one implementation")


def test_the_native_gate_refuses_rather_than_skipping():
    tree = ast.parse(CONFTEST.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "pytest_collection")
    body = ast.unparse(fn)
    assert "UsageError" in body, "the gate no longer fails the session"
    for soft in ("pytest.skip", "return True", "pass  # noqa"):
        assert soft not in body, f"the gate softens refusal with {soft!r}"


def test_the_runner_mints_a_fresh_identity_and_provisions_the_marker():
    src = RUNNER.read_text()
    assert "MESH_TEST_RUN_ID" in src, "the runner exports no run identity"
    assert "/dev/urandom" in src, "the run identity is not freshly generated per invocation"
    assert not re.search(r'^RUN_ID="(?!\$\()', src, re.MULTILINE), \
        "the run identity is a literal; every invocation would reuse the same one"
    assert "dd.provision(" in src, "the runner does not provision the marker"
    assert "meshtest_" in src, "the suite is not pointed at the provisioned task database"


def _code(path: Path) -> str:
    # Executable lines only: a comment naming a removed escape hatch is documentation, not one.
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


def test_the_runner_accepts_no_caller_supplied_disposability_claim():
    code = _code(RUNNER)
    for escape in ("SKIP_BUILD", "ALLOW_RESET", "FORCE_RESET", "ASSUME_DISPOSABLE"):
        assert escape not in code, f"{escape} lets a caller assert disposability by hand"


def test_the_runner_cannot_select_the_development_database():
    code = _code(RUNNER)
    assert 'POSTGRES_HOST="$PG"' in code, "the runner no longer pins its own PostgreSQL service"
    # Only endpoints matter here; the mesh image tag legitimately contains the product name.
    for leak in ("localhost:5432", "127.0.0.1:5432", "@postgres:5432", "/meshpipeline?",
                 "POSTGRES_DB=meshpipeline"):
        assert leak not in code, f"the native runner can reach {leak!r}"


def test_neither_native_runner_builds_and_both_require_a_current_image():
    for runner in (RUNNER, TIER_RUNNER):
        src = runner.read_text()
        assert "docker build" not in src, f"{runner.name} builds its own image"
        assert "assert_mesh_image_current.sh" in src, f"{runner.name} skips the freshness check"


def test_authorization_precedes_the_first_native_container_that_touches_data():
    src = RUNNER.read_text()
    provision = src.index("dd.provision(")
    matrix = src.index("-m native_terminal")
    assert provision < matrix, "the matrix starts before the marker exists"
    assert re.search(r"trap cleanup EXIT", src), "task resources are not removed on every exit"
