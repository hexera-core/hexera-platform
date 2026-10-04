# Responsibility: Record who a repair job is assigned to, and every decision a person made on it.
# Boundaries: one column pair and one append-only table. WHICH decisions are legal is
#             persistence/repair_job_state.py's; this revision stores that they were made.

# WHY A DECISION NEEDS ITS OWN ROW.
#
# A repair job's status says where it IS. It cannot say who sent it there, when, or why - and for
# this service those are the questions that matter most, because the decisions being recorded are
# human judgements about somebody else's geometry. "Who approved inflating the tolerance on this
# customer's part" has to be answerable months later, and a status column answers none of it.
#
# APPEND-ONLY. Nothing updates a decision: a changed mind is a new decision, with its own actor and
# reason. Overwriting one would erase the audit this table exists to be.
#
# THE ACTOR IS SELF-ASSERTED, and the column says so by being plain text. The operator surface is
# reached with a shared admin credential today, which proves staff access but names nobody - so the
# operator states who they are and this records the claim. That is weaker than an identity and is
# recorded as a claim rather than dressed up as one; when real operator identity exists, this
# column is where it lands.
#
# ADDITIVE AND SAFE IN EITHER ROLLOUT ORDER: two nullable columns and one new table, nothing
# reshaped and no row rewritten. The previous image neither reads nor writes any of it.

# Revision ID: 0013_repair_operator_queue
# Revises: 0012_cad_repair_jobs
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_repair_operator_queue"
down_revision = "0012_cad_repair_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cad_repair_jobs",
                  sa.Column("assigned_operator", sa.String(length=256), nullable=True))
    op.add_column("cad_repair_jobs",
                  sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
    # THE QUEUE READS "WHO HAS WHAT", so unassigned work is findable and one operator's load is a
    # single index scan rather than a table scan over every tenant's jobs.
    op.create_index("ix_cad_repair_jobs_assignee", "cad_repair_jobs",
                    ["assigned_operator", "status"], unique=False)

    op.create_table(
        "cad_repair_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repair_job_id", sa.UUID(), nullable=False),
        # WHAT WAS DECIDED: the status the job was moved to, as text rather than the enum. A
        # decision is a historical fact and must stay readable after a label is retired from the
        # enum - a foreign key onto a type would make the audit depend on today's vocabulary.
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=256), nullable=False),
        # WHY. Required by the API for anything that blocks or escalates a customer's job: a
        # refusal with no stated reason is one nobody can answer a complaint about.
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["cad_repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cad_repair_decisions_repair_job_id"), "cad_repair_decisions",
                    ["repair_job_id"], unique=False)
    # The job's own history, newest last, read whole on every operator screen.
    op.create_index("ix_cad_repair_decisions_history", "cad_repair_decisions",
                    ["repair_job_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_table("cad_repair_decisions")
    op.drop_index("ix_cad_repair_jobs_assignee", table_name="cad_repair_jobs")
    op.drop_column("cad_repair_jobs", "assigned_at")
    op.drop_column("cad_repair_jobs", "assigned_operator")
