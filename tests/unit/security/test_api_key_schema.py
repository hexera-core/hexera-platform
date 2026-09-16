# Responsibility: Verify the api_keys table stores a hash rather than a secret, and that the migration declares what the ORM does.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.session import Base

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic" / "versions" / "0002_api_keys.py"

_TABLE = "api_keys"
_COLUMNS = {"id", "owner_id", "organization_id", "name", "key_prefix", "key_hash", "plan",
            "created_at", "last_used_at", "revoked_at", "expires_at"}


def _table():
    return Base.metadata.tables[_TABLE]


def test_the_orm_declares_the_table_the_design_specifies():
    assert _TABLE in Base.metadata.tables
    assert {c.name for c in _table().columns} == _COLUMNS


def test_no_column_could_hold_the_secret():
    names = {c.name for c in _table().columns}
    assert not {n for n in names if "secret" in n or n == "key"}, names


def test_the_lookup_column_is_unique_and_indexed():
    prefix_indexes = [i for i in _table().indexes if [c.name for c in i.columns] == ["key_prefix"]]
    assert prefix_indexes, "key_prefix carries no index - every request would scan the table"
    assert all(i.unique for i in prefix_indexes), "two rows could share a prefix"


def test_the_tenant_columns_are_scoped_the_way_the_rest_of_the_schema_is():
    cols = {c.name: c for c in _table().columns}
    assert not cols["owner_id"].nullable, "a key with no owner authenticates as nobody"
    assert cols["owner_id"].type.length == 256, "owner_id diverges from the rest of the schema"
    # Nullable still: a key issued before the backfill names no organisation.
    assert cols["organization_id"].nullable
    assert any([c.name for c in i.columns] == ["organization_id"] for i in _table().indexes), \
        "organization_id is not indexed - it becomes the tenant filter"


def test_the_lifecycle_columns_are_nullable_because_absence_is_the_normal_state():
    cols = {c.name: c for c in _table().columns}
    for name in ("last_used_at", "revoked_at", "expires_at"):
        assert cols[name].nullable, name
    assert not cols["created_at"].nullable


def test_the_migration_creates_exactly_the_columns_the_orm_declares():
    tree = ast.parse(MIGRATION.read_text())
    declared = {n.args[0].value for n in ast.walk(tree)
                if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("Column") and n.args
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}
    assert declared == _COLUMNS


def test_the_migration_descends_from_the_baseline_and_reverses_itself():
    src = MIGRATION.read_text()
    tree = ast.parse(src)
    assigned = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert assigned["revision"] == "0002_api_keys"
    assert assigned["down_revision"] == "0001_schema_baseline"
    assert "op.drop_table('api_keys')" in src
    assert "meshpipeline" not in src, "the migration imports the application it is meant to outlive"
