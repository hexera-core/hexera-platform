# Responsibility: Give a parked repair run, and the unsure defects put to an engineer, somewhere durable to live.
# Boundaries: two new tables. WHAT may be decided, and by whom, is the repository's and the API's;
#             this revision stores the state, not the rules.

# WHY TWO TABLES, AND WHY park_id IS NULLABLE.
#
# A park is the run: one row per run that stopped to wait for a person, closed exactly once. An
# item is the defect: one row per unsure finding. A delivered run mints items too - its unsure
# defects are shown to the engineer after the fact - but it never parked, so an item cannot require
# a park. Both tables hang off simulation_jobs with CASCADE: they are working state of a run, not
# service record, and have no meaning once the run is gone.
#
# ADDITIVE AND SAFE IN EITHER ROLLOUT ORDER. Two new tables, nothing existing reshaped, and the
# previous image neither reads nor writes either. The vocabularies are CHECK constraints over
# plain text, not enum types, so a label added later is a constraint swap and not an ADD VALUE
# that cannot be undone.

# Revision ID: 0015_repair_review
# Revises: 0014_repaired_cad_artifact
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_repair_review"
down_revision = "0014_repaired_cad_artifact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repair_review_parks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(length=16), nullable=True),
        sa.Column("kept_operations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("geometry_session_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["simulation_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_repair_review_parks_status"),
        sa.CheckConstraint("closed_by IS NULL OR closed_by IN ('run', 'cancel')",
                           name="ck_repair_review_parks_closed_by"),
    )
    op.create_index(op.f("ix_repair_review_parks_job_id"), "repair_review_parks", ["job_id"],
                    unique=False)

    op.create_table(
        "repair_review_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("park_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("group_key", sa.String(length=128), nullable=False),
        sa.Column("defect_code", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_index", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("measurements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("pairing_key", sa.String(length=128), server_default="", nullable=False),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("camera", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["park_id"], ["repair_review_parks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["simulation_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("origin IN ('triage', 'stall', 'refusal', 'survivor')",
                           name="ck_repair_review_items_origin"),
        sa.CheckConstraint(
            "state IN ('open', 'accepted', 'left', 'redirected', 'unreviewed')",
            name="ck_repair_review_items_state"),
    )
    op.create_index(op.f("ix_repair_review_items_park_id"), "repair_review_items", ["park_id"],
                    unique=False)
    op.create_index(op.f("ix_repair_review_items_job_id"), "repair_review_items", ["job_id"],
                    unique=False)
    op.create_index("ix_repair_review_items_group", "repair_review_items",
                    ["job_id", "group_key"], unique=False)


def downgrade() -> None:
    op.drop_table("repair_review_items")
    op.drop_table("repair_review_parks")
