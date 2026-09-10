# Responsibility: Verify the identity and credit tables the design specifies are what the ORM and the migration both declare.
from __future__ import annotations

import ast
from pathlib import Path

from meshpipeline.persistence.session import Base

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic" / "versions" / "0003_identity_and_credits.py"

_COLUMNS = {
    "organizations": {"id", "name", "slug", "created_at"},
    "users": {"id", "firebase_uid", "email", "name", "email_verified_at", "last_login_at",
              "created_at"},
    "memberships": {"id", "user_id", "organization_id", "role", "created_at"},
    "credit_ledger": {"id", "organization_id", "entry_type", "amount", "reason", "created_at"},
}


def test_the_orm_declares_the_four_tables_the_design_specifies():
    for table, columns in _COLUMNS.items():
        assert table in Base.metadata.tables, table
        assert {c.name for c in Base.metadata.tables[table].columns} == columns, table


def test_no_column_could_hold_a_password():
    for table in _COLUMNS:
        names = {c.name for c in Base.metadata.tables[table].columns}
        assert not {n for n in names if "password" in n or "secret" in n}, table


def test_the_lookup_columns_are_unique_and_indexed():
    users = Base.metadata.tables["users"]
    for column in ("firebase_uid", "email"):
        indexes = [i for i in users.indexes if [c.name for c in i.columns] == [column]]
        assert indexes, f"{column} carries no index - every sign-in would scan the table"
        assert all(i.unique for i in indexes), f"two users could share {column}"


def test_a_backfilled_user_may_have_no_firebase_uid_yet():
    cols = {c.name: c for c in Base.metadata.tables["users"].columns}
    assert cols["firebase_uid"].nullable, "a backfilled user has no uid until they first sign up"
    assert not cols["email"].nullable, "a user with no email cannot be linked to their owner_id"


def test_one_user_joins_an_organisation_at_most_once():
    memberships = Base.metadata.tables["memberships"]
    uniques = [c for c in memberships.constraints
               if getattr(c, "columns", None) is not None
               and {col.name for col in c.columns} == {"user_id", "organization_id"}]
    assert uniques, "a user could hold two memberships in one organisation"


def test_the_ledger_is_shaped_for_a_derived_balance():
    ledger = Base.metadata.tables["credit_ledger"]
    cols = {c.name: c for c in ledger.columns}
    assert not cols["amount"].nullable, "a null amount cannot be summed"
    assert not cols["organization_id"].nullable, "an entry belonging to nobody has no balance"
    assert any([c.name for c in i.columns] == ["organization_id", "created_at"]
               for i in ledger.indexes), "the balance query has no index to read"


def test_the_migration_creates_exactly_the_columns_the_orm_declares():
    tree = ast.parse(MIGRATION.read_text())
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and ast.unparse(call.func).endswith("create_table")):
            continue
        table = call.args[0].value
        if table not in _COLUMNS:
            continue
        declared = {a.args[0].value for a in call.args[1:]
                    if isinstance(a, ast.Call) and ast.unparse(a.func).endswith("Column")}
        assert declared == _COLUMNS[table], table


def test_the_migration_descends_from_the_api_keys_revision_and_reverses_itself():
    src = MIGRATION.read_text()
    tree = ast.parse(src)
    assigned = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert assigned["revision"] == "0003_identity_and_credits"
    assert assigned["down_revision"] == "0002_api_keys"
    for table in _COLUMNS:
        assert f"op.drop_table('{table}')" in src, table
    assert "meshpipeline" not in src, "the migration imports the application it is meant to outlive"
