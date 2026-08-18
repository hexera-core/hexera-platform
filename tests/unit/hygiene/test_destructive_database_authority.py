# Responsibility: Verify no destructive database statement exists outside the disposable-database authority.
# Boundaries: the shape of the test tree - whether a guard REFUSES at runtime is the behavioural suite.
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TESTS = REPO / "tests"

#: The one module allowed to create, mark and drop a database, and the one helper allowed to drop
#: a schema. Everything else must take a DisposableAuthority and let these do the work.
AUTHORITY = "disposable_database.py"
RESET_HELPER = "harness_provisioning.py"

#: Statements that destroy data. `DELETE ... WHERE` is not here: a suite deleting rows it created
#: is ordinary cleanup. An unrestricted `DELETE FROM x` with no WHERE is not, and is caught below.
DESTRUCTIVE = re.compile(
    r"\b(DROP\s+SCHEMA|DROP\s+DATABASE|DROP\s+TABLE|TRUNCATE)\b", re.IGNORECASE)
UNRESTRICTED_DELETE = re.compile(r"\bDELETE\s+FROM\s+[\"\w.]+\s*(?:$|;|\"|')", re.IGNORECASE)
#: A literal that IS a destructive statement: the keyword first, then something to destroy.
STATEMENT = re.compile(
    r"^(DROP\s+(SCHEMA|DATABASE|TABLE)|TRUNCATE|DELETE\s+FROM)\s+\S", re.IGNORECASE)

#: Suites whose whole subject is the guard itself, or the provisioning contract the guard rests
#: on. They reach destructive SQL deliberately, always through an authority, to prove it works.
PROVING_THE_GUARD = {
    "integration/test_disposable_database_authority.py",
    "integration/test_provisioning_contract.py",
    "integration/test_harness_provisioning.py",
    "integration/test_schema_authority.py",
    "integration/test_production_schema_guard.py",
    # The migration wrapper's whole subject is what happens to a schema across upgrade and
    # downgrade; it manipulates schemas by definition, always inside this run's database.
    "integration/test_migration_wrapper_postgres.py",
    "unit/hygiene/test_destructive_database_authority.py",
}


def _test_sources() -> list[tuple[str, str]]:
    out = []
    for p in sorted(TESTS.rglob("*.py")):
        rel = p.relative_to(TESTS).as_posix()
        if "__pycache__" in rel:
            continue
        out.append((rel, p.read_text()))
    return out


# Only SQL actually handed to the database: the literal inside `text(...)` or `.execute(...)`.
# A literal that merely MENTIONS a destructive statement - prose in a comment-string, or an
# assertion about generated migration source - destroys nothing, and flagging it would train
# people to add exemptions rather than fix real ones.
def _statement_strings(src: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:                      # pragma: no cover - a broken test fails elsewhere
        return []
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name not in ("text", "execute", "exec_driver_sql"):
            continue
        for a in n.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.append((a.lineno, a.value))
    # `from sqlalchemy import text as _t` renames the call out of the list above, so also take any
    # literal that IS a destructive statement - keyword first, with an object after it. Prose that
    # merely mentions one does not start with the keyword, and a bare "DROP TABLE" assertion about
    # migration source names no object.
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and STATEMENT.match(n.value.strip()):
            out.append((n.lineno, n.value))
    return out


def test_no_test_module_issues_destructive_sql_outside_the_authority():
    offenders = []
    for rel, src in _test_sources():
        if rel in PROVING_THE_GUARD or rel.endswith(AUTHORITY) or rel.endswith(RESET_HELPER):
            continue
        for line, literal in _statement_strings(src):
            if DESTRUCTIVE.search(literal) or UNRESTRICTED_DELETE.search(literal):
                offenders.append(f"{rel}:{line}  {literal.strip()[:60]}")
    assert offenders == [], (
        "these modules destroy data with their own SQL instead of going through the "
        "disposable-database authority:\n  " + "\n  ".join(offenders))


def test_the_reset_helper_requires_an_authority():
    src = (TESTS / RESET_HELPER).read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "reset_schema")
    assert "authority" in [a.arg for a in fn.args.kwonlyargs], \
        "reset_schema no longer takes an authority - a caller can forget the check again"
    # The docstring names the statements it guards, so compare positions in the CODE only.
    stripped = ast.unparse(ast.parse(ast.unparse(fn)))
    fn2 = ast.parse(stripped).body[0]
    if (fn2.body and isinstance(fn2.body[0], ast.Expr)
            and isinstance(fn2.body[0].value, ast.Constant)):
        fn2.body = fn2.body[1:]
    body = ast.unparse(fn2)
    assert "dd.require" in body, "reset_schema no longer re-proves the authority"
    require_at = body.index("dd.require")
    for statement in ("DROP SCHEMA", "CREATE SCHEMA"):
        assert body.index(statement) > require_at, \
            f"{statement} runs before the authority check - the guard is decorative"


def test_the_integration_conftest_refuses_before_collection():
    src = (TESTS / "integration" / "conftest.py").read_text()
    assert "def pytest_collection(" in src, \
        "the session gate is gone; a destructive fixture could reach a shared database first"
    assert "dd.authorize(" in src, "the gate no longer proves the database from the live server"


@pytest.mark.parametrize("forbidden", [
    "ALLOW_RESET", "ALLOW_DB_RESET", "FORCE_RESET", "PYTEST_CURRENT_TEST", "CI"])
def test_no_environment_flag_can_authorize_destruction(forbidden):
    src = (TESTS / AUTHORITY).read_text()
    assert forbidden not in src, (
        f"{forbidden} appears in the disposable-database authority. A database is disposable "
        "because the server says it was provisioned for this run, never because a process "
        "environment says so - the development database satisfies every such flag equally.")


def test_the_authority_decides_from_the_server_not_the_url_text():
    src = (TESTS / AUTHORITY).read_text()
    for query in ("current_database()", "system_identifier", "MARKER_SCHEMA"):
        assert query in src, f"the authority no longer consults {query}"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "authorize")
    body = ast.unparse(fn)
    for text_test in ('"test" in', "'test' in", ".startswith(", "localhost"):
        assert text_test not in body, (
            f"authorize() inspects {text_test!r}. A name, a prefix or a host is not proof; two "
            "URLs can name the same storage and a shared database can be called anything.")


# mutation controls - each proves the guard above would catch a real regression

def test_a_rogue_truncate_would_be_caught(tmp_path):
    rogue = tmp_path / "rogue.py"
    rogue.write_text('from sqlalchemy import text\nx = text("TRUNCATE simulation_jobs CASCADE")\n')
    found = [lit for _, lit in _statement_strings(rogue.read_text()) if DESTRUCTIVE.search(lit)]
    assert found, "the destructive-SQL detector sees nothing - this guard proves nothing"


def test_an_unrestricted_delete_would_be_caught(tmp_path):
    rogue = tmp_path / "rogue.py"
    rogue.write_text('from sqlalchemy import text\n'
                     'q = text("DELETE FROM simulation_jobs")\n'
                     'keep = text("delete from x where id = :i")\n')
    lits = [lit for _, lit in _statement_strings(rogue.read_text())]
    assert any(UNRESTRICTED_DELETE.search(x) for x in lits), "unrestricted DELETE is not detected"
    assert not UNRESTRICTED_DELETE.search(lits[1]), "a scoped DELETE must stay allowed"
