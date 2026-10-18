# Responsibility: Verify the upload-hold revision extends the chain, adds exactly the one nullable column the model declares, and removes it on the way down.
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "alembic" / "versions" / "0010_source_upload_hold.py").read_text()


def _constants() -> dict:
    tree = ast.parse(SOURCE)
    return {n.targets[0].id: n.value.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Constant)}


def test_it_extends_the_chain_rather_than_branching_it():
    values = _constants()
    assert values["revision"] == "0010_source_upload_hold"
    assert values["down_revision"] == "0009_job_cancellation"
    assert values["branch_labels"] is None


def test_upgrade_adds_one_nullable_column_and_nothing_else():
    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    assert up.count("op.add_column(") == 1
    assert '"source_object_cleanups"' in up and '"next_attempt_at"' in up
    assert "DateTime(timezone=True)" in up and "nullable=True" in up
    # NULL is "due now" - every intent recorded before this revision keeps its meaning
    assert "server_default" not in up
    for unwanted in ("create_table", "CREATE SCHEMA", "alter_column", "drop_"):
        assert unwanted not in up


def test_the_column_matches_the_model():
    from meshpipeline.persistence.models import SourceObjectCleanup

    column = SourceObjectCleanup.__table__.c.next_attempt_at
    assert column.nullable is True
    assert column.type.timezone is True
    assert column.server_default is None


def test_downgrade_drops_exactly_that_column():
    down = SOURCE[SOURCE.index("def downgrade"):]
    assert 'op.drop_column("source_object_cleanups", "next_attempt_at")' in down
