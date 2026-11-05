# Responsibility: Verify every owner-scoped table carries the tenant column, nullable and indexed.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.session import Base

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic" / "versions" / "0004_tenant_columns.py"

#: The owner-scoped tables that PREDATE the tenant boundary, and which revision 0004 therefore had
#: to add the column to. `artifacts` is absent deliberately: it carries no owner_id, reaching its
#: tenant through the job it belongs to.
_TENANT_TABLES = (
    "geometry_sources", "simulation_jobs", "chat_sessions", "geometry_interpretations",
    "capture_operations", "artifact_reconciliations", "source_object_cleanups",
)

#: Owner-scoped tables CREATED WITH the tenant column, by the revision that created the table.
#: They are held apart from _TENANT_TABLES because 0004 cannot be expected to mention a table
#: that did not exist when it was written - but the same three structural rules below still apply
#: to them, and the coverage guard still refuses a new owner-scoped table absent from both lists.
_TENANT_NATIVE_TABLES = (
    "cad_repair_jobs",      # 0012
)

#: Every owner-scoped table, however it got its tenant column.
_ALL_TENANT_TABLES = _TENANT_TABLES + _TENANT_NATIVE_TABLES


def test_every_owner_scoped_table_carries_the_tenant_column():
    for table in _ALL_TENANT_TABLES:
        columns = {c.name for c in Base.metadata.tables[table].columns}
        assert "organization_id" in columns, table


def test_the_tenant_column_is_nullable_everywhere():
    # 0004 runs BEFORE the new image (deploy.sh stage 220 precedes 245), so the old revision
    # briefly inserts rows that name no organisation. NOT NULL here would fail those inserts.
    for table in _ALL_TENANT_TABLES:
        column = Base.metadata.tables[table].columns["organization_id"]
        assert column.nullable, table


def test_the_tenant_column_is_indexed_everywhere():
    for table in _ALL_TENANT_TABLES:
        indexes = Base.metadata.tables[table].indexes
        assert any("organization_id" in [c.name for c in i.columns] for i in indexes), table


def test_every_owner_scoped_table_is_covered_by_this_test():
    # The guard that stops a NEW owner-scoped table quietly escaping the tenant boundary.
    owner_scoped = {name for name, table in Base.metadata.tables.items()
                    if "owner_id" in {c.name for c in table.columns}}
    assert owner_scoped - {"api_keys"} == set(_ALL_TENANT_TABLES), owner_scoped


def test_the_api_keys_tenant_column_finally_has_its_foreign_key():
    # 0002 shipped it unconstrained with a comment promising the FK "arrives with the
    # organisations migration". This is that migration.
    column = Base.metadata.tables["api_keys"].columns["organization_id"]
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert targets == {"organizations.id"}


def test_the_migration_descends_from_the_identity_revision_and_reverses_itself():
    src = MIGRATION.read_text()
    tree = ast.parse(src)
    assigned = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert assigned["revision"] == "0004_tenant_columns"
    assert assigned["down_revision"] == "0003_identity_and_credits"
    for table in _TENANT_TABLES:
        assert f"op.drop_column('{table}', 'organization_id')" in src, table
    assert "meshpipeline" not in src, "the migration imports the application it is meant to outlive"
