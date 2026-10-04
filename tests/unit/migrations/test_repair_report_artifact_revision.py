# Responsibility: Verify the repair-report revision extends the chain and adds exactly the one enum label the model declares.
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "alembic" / "versions" / "0011_repair_report_artifact.py").read_text()


def _constants() -> dict:
    tree = ast.parse(SOURCE)
    return {n.targets[0].id: n.value.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and isinstance(n.value, ast.Constant)}


def test_it_extends_the_chain_rather_than_branching_it():
    values = _constants()
    assert values["revision"] == "0011_repair_report_artifact"
    assert values["down_revision"] == "0010_source_upload_hold"
    assert values["branch_labels"] is None


def test_upgrade_adds_one_enum_label_and_touches_no_table():
    up = SOURCE[SOURCE.index("def upgrade"):SOURCE.index("def downgrade")]
    assert up.count("op.execute(") == 1
    assert "ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS" in up
    # ADD VALUE cannot run inside a transaction block on older PostgreSQL
    assert "autocommit_block()" in up
    # no row is rewritten and no table is reshaped by adding a label
    for unwanted in ("create_table", "add_column", "alter_column", "drop_", "UPDATE "):
        assert unwanted not in up


def test_the_label_matches_the_model():
    from meshpipeline.persistence.models import ArtifactType

    assert _constants()["_LABEL"] == ArtifactType.repair_report.value


def test_downgrade_keeps_the_label_because_postgres_cannot_drop_one():
    down = SOURCE[SOURCE.index("def downgrade"):]
    assert "DROP" not in down.upper().replace("DROP A LABEL", "")
    assert "op.execute(" not in down


def test_the_new_class_is_evidence_and_never_a_deliverable():
    from meshpipeline.application import artifact_policy as ap
    from meshpipeline.persistence.models import ArtifactType

    label = ArtifactType.repair_report.value
    # not required for ANY engine, and not an optional export whose absence warrants a warning:
    # a job that produced no report is not short a deliverable.
    assert label not in ap.OPTIONAL_CLASSES
    assert not ap.is_required_class("snappy", label)
    assert label not in ap.required_output_classes("snappy")
    # and its presence cannot make an undelivered job look ready
    assert ap.required_ready("snappy", [label]) is False
    assert ap.optional_warnings([ap.REQUIRED_BUNDLE_CLASS, ap.REQUIRED_VIEWER_CLASS,
                                 "mesh", label]) == []
