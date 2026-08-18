# Responsibility: Verify no code creates a schema outside the migration authority, and the rule really scanned for it.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests._scan import scanned

REPO = Path(__file__).resolve().parents[3]
SEARCHED = ("src", "tests", "devtools", "alembic")
#: this file - it must be able to write example calls without reporting itself
_ALLOWED_TEXT_ONLY = {Path(__file__).resolve()}



def _python_files() -> list[Path]:
    out: list[Path] = []
    for top in SEARCHED:
        root = REPO / top
        if root.is_dir():
            out += [p for p in root.rglob("*.py")
                    if "__pycache__" not in p.parts and ".venv" not in p.parts]
    return sorted(out)


def test_the_rule_actually_scanned_the_repository():
    files = _python_files()
    assert len(files) > 300, f"only {len(files)} python files scanned - the rule is not looking"
    assert any(p.name == "models.py" for p in files)
    assert any("integration" in p.parts for p in files)


def _create_all_calls(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:                                  # pragma: no cover - surfaced elsewhere
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "create_all":
                found.append(ast.dump(node)[:80])
        # `run_sync(Base.metadata.create_all)` passes it as a VALUE rather than calling it
        if isinstance(node, ast.Attribute) and node.attr == "create_all":
            found.append(f"reference at line {node.lineno}")
    return found


def test_no_executable_create_all_anywhere():
    offenders = {}
    for path in _python_files():
        if path.resolve() in _ALLOWED_TEXT_ONLY:
            continue
        hits = _create_all_calls(path)
        if hits:
            offenders[str(path.relative_to(REPO))] = hits
    assert offenders == {}, (
        "create_all builds a schema no migration produced; provision through Alembic "
        f"(tests/harness_provisioning.py): {offenders}")


def test_the_rule_detects_a_representative_unauthorized_call(tmp_path):
    direct = tmp_path / "direct.py"
    direct.write_text("import x\nx.metadata.create_all(bind=e)\n")
    assert _create_all_calls(direct), "a direct call was not detected"

    passed_as_value = tmp_path / "as_value.py"
    passed_as_value.write_text("conn.run_sync(Base.metadata.create_all)\n")
    assert _create_all_calls(passed_as_value), "create_all passed as a value was not detected"

    aliased = tmp_path / "aliased.py"
    aliased.write_text("build = Base.metadata.create_all\nbuild(engine)\n")
    assert _create_all_calls(aliased), "an aliased reference was not detected"

    clean = tmp_path / "clean.py"
    clean.write_text('"""mentions create_all in prose only."""\n# and in a comment\n')
    assert _create_all_calls(clean) == [], "prose was wrongly reported as a call"


def test_production_code_never_creates_a_schema():
    offenders = {}
    for path in scanned((REPO / "src").rglob("*.py"), "the shipped source tree"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("create_all", "CREATE SCHEMA", "CREATE TABLE IF NOT EXISTS"):
            if marker in text:
                offenders.setdefault(str(path.relative_to(REPO)), []).append(marker)
    assert offenders == {}, f"production code creates schema implicitly: {offenders}"


def test_integration_provisioning_calls_the_canonical_migration_entry_point():
    path = REPO / "tests" / "harness_provisioning.py"
    seam = path.read_text()
    # Stronger than "it runs Alembic": it runs PRODUCTION's migration entry point, so the guard
    # and the schema anchoring apply to test provisioning exactly as they do to a deployment.
    assert "migrate.run_migrations" in seam, \
        "the provisioning seam bypasses the production migration entry point"
    # executable calls, not prose: the seam's comment explains why it does NOT call
    # `command.upgrade` itself
    calls = {ast.dump(n.func) for n in ast.walk(ast.parse(seam)) if isinstance(n, ast.Call)}
    assert not any("upgrade" in c and "command" in c for c in calls), \
        "the seam drives Alembic directly, skipping production's guard"
    # executable use, not prose: the seam's docstrings explain WHY create_all is gone
    assert _create_all_calls(path) == [], "the seam still builds tables from the ORM"


def test_destructive_migration_tests_use_isolated_database_identities():
    for name in ("test_schema_authority.py", "test_migration_wrapper_postgres.py"):
        src = (REPO / "tests" / "integration" / name).read_text()
        assert "CREATE DATABASE" in src or "empty_db" in src, (
            f"{name} is destructive but does not take an isolated database")


@pytest.mark.parametrize("marker", ["sqlite", "sqlite+aiosqlite"])
def test_no_hidden_sqlite_schema_claims_to_be_postgres(marker):
    offenders = [str(p.relative_to(REPO)) for p in _python_files()
                 if p.resolve() not in _ALLOWED_TEXT_ONLY
                 and f'"{marker}:' in p.read_text(encoding="utf-8", errors="replace")]
    assert offenders == [], f"a SQLite schema stands in for PostgreSQL: {offenders}"
