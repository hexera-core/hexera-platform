# Responsibility: Verify the harness reset drops every schema the migrations create.
# Boundaries: it reads both sides as text; whether a reset actually empties a database is the
#             integration tier's subject.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
VERSIONS = REPO / "alembic" / "versions"
HARNESS = REPO / "tests" / "harness_provisioning.py"

#: `CREATE SCHEMA foo` / `CREATE SCHEMA IF NOT EXISTS foo` inside a revision.
_CREATE_SCHEMA = re.compile(
    r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _schemas_migrations_create() -> set[str]:
    found: set[str] = set()
    for revision in VERSIONS.glob("*.py"):
        found.update(_CREATE_SCHEMA.findall(revision.read_text(encoding="utf-8")))
    # `public` already exists and the reset recreates it explicitly.
    return {name for name in found if name != "public"}


def test_the_reset_drops_every_schema_a_migration_creates() -> None:
    # THE FAILURE THIS PREVENTS, which the integration tier hit for real: `reset_schema` dropped
    # only `public`, so a schema created by a revision survived the reset. The upgrade that follows
    # re-runs every revision, and the first CREATE TABLE in the surviving schema failed with
    # "relation already exists" - in a suite whose subject was artifact concurrency, nowhere near
    # the migration that caused it.
    harness = HARNESS.read_text(encoding="utf-8")
    covered = set(
        re.findall(r'"([a-z_][a-z0-9_]*)"', harness.split("_MIGRATION_OWNED_SCHEMAS = (")[1].split(")")[0])
    )
    missing = _schemas_migrations_create() - covered
    assert not missing, (
        "these schemas are created by a migration but not dropped by reset_schema, so the "
        f"integration tier will fail on its second run: {sorted(missing)}"
    )


def test_the_marker_schema_is_never_dropped() -> None:
    # `_mesh_disposable` holds the marker proving a database may be destroyed at all, and it lives
    # outside `public` precisely because `public` is dropped. Dropping it would leave the database
    # unable to prove itself for the rest of the run.
    harness = HARNESS.read_text(encoding="utf-8")
    owned = harness.split("_MIGRATION_OWNED_SCHEMAS = (")[1].split(")")[0]
    assert "_mesh_disposable" not in owned
