# Responsibility: Give a customer repair job somewhere durable to live, with its attempt history.
# Boundaries: two new tables and one new enum. WHICH transitions are legal is declared in
#             persistence/repair_job_state.py; this revision stores the state, not the rules.

# WHY NOT ON simulation_jobs.
#
# A simulation job is one attempt to mesh an approved request, and it ends. A repair job is the
# engagement around a customer's unusable geometry: it may inspect, repair, mesh, go to an
# operator, go back to the customer for a file we never received, and be reopened after delivery.
# It outlives any single mesh run and sometimes produces none at all. Folding the two together
# would mean either a simulation job that never meshes - breaking what that row means to every
# reader of it - or a repair history with nowhere to live.
#
# WHY THE ATTEMPTS ARE THEIR OWN TABLE, APPEND-ONLY.
#
# The two hash columns are the audit: input_sha256 is the bytes an attempt read, output_sha256 the
# bytes it produced (NULL for an inspection, which produces no geometry). Together they are the
# proof that a customer's original file was added to and never replaced - the single claim this
# service most needs to be able to make. Updating an attempt in place would erase it, so a second
# look is a second row.
#
# ADDITIVE AND SAFE IN EITHER ROLLOUT ORDER. Two new tables and one new enum type: nothing
# existing is reshaped, no row is rewritten, and the previous image neither reads nor writes any
# of it. The foreign keys to geometry_sources and geometry_interpretations are RESTRICT because
# the repair job's whole purpose is to reference the immutable customer upload - a job must not
# outlive the identity of the bytes it is about. simulation_jobs is SET NULL: the mesh run is a
# thing that HAPPENED to this job, and losing it must not take the service record with it.

# Revision ID: 0012_cad_repair_jobs
# Revises: 0011_repair_report_artifact
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_cad_repair_jobs"
down_revision = "0011_repair_report_artifact"
branch_labels = None
depends_on = None

#: The enum's labels, spelled once. Must equal persistence.models.RepairJobStatus's values,
#: which the unit tier asserts member for member.
_REPAIR_JOB_STATUS = (
    "received", "inspecting", "awaiting_strategy", "repairing", "repair_review",
    "meshing", "mesh_review", "manual_cleanup", "retrying", "escalated",
    "waiting_customer", "customer_blocked", "service_failed", "delivered",
    "repair_delivered", "delivery_disputed", "dead_lettered", "expired", "cancelled",
)


def upgrade() -> None:
    op.create_table(
        "cad_repair_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.String(length=256), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("geometry_source_id", sa.UUID(), nullable=False),
        sa.Column("geometry_interpretation_id", sa.UUID(), nullable=True),
        sa.Column("simulation_job_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Enum(*_REPAIR_JOB_STATUS, name="repairjobstatus"), nullable=False),
        sa.Column("target_engine", sa.String(length=64), server_default="", nullable=False),
        sa.Column("service_priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("customer_intent", sa.Text(), nullable=True),
        sa.Column("current_strategy", sa.String(length=32), server_default="", nullable=False),
        sa.Column("repair_status", sa.String(length=32), server_default="", nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["geometry_source_id"], ["geometry_sources.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["geometry_interpretation_id"], ["geometry_interpretations.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["simulation_job_id"], ["simulation_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cad_repair_jobs_owner_id"), "cad_repair_jobs", ["owner_id"],
                    unique=False)
    op.create_index(op.f("ix_cad_repair_jobs_organization_id"), "cad_repair_jobs",
                    ["organization_id"], unique=False)
    op.create_index(op.f("ix_cad_repair_jobs_geometry_source_id"), "cad_repair_jobs",
                    ["geometry_source_id"], unique=False)
    op.create_index(op.f("ix_cad_repair_jobs_simulation_job_id"), "cad_repair_jobs",
                    ["simulation_job_id"], unique=False)
    op.create_index(op.f("ix_cad_repair_jobs_status"), "cad_repair_jobs", ["status"], unique=False)
    # THE QUEUE'S OWN INDEX, in the order the queue reads: narrow by state, then most urgent
    # first, then oldest. Without it every operator page scans every repair job the tenant owns.
    op.create_index("ix_cad_repair_jobs_queue", "cad_repair_jobs",
                    ["status", "service_priority", "created_at"], unique=False)

    op.create_table(
        "cad_repair_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("repair_job_id", sa.UUID(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("profile", sa.String(length=32), server_default="", nullable=False),
        sa.Column("tool_version", sa.String(length=128), server_default="", nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="", nullable=False),
        sa.Column("report", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("caps", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("measurements", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["repair_job_id"], ["cad_repair_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # THE NUMBERING AUTHORITY. The attempt budget is counted on these, so two writers must not
        # both be able to call themselves attempt 3; the repository derives MAX+1 inside the
        # insert's own transaction and lets this constraint settle a race.
        sa.UniqueConstraint("repair_job_id", "attempt_no", name="uq_cad_repair_attempts_job_no"),
    )
    op.create_index(op.f("ix_cad_repair_attempts_repair_job_id"), "cad_repair_attempts",
                    ["repair_job_id"], unique=False)


def downgrade() -> None:
    op.drop_table("cad_repair_attempts")
    op.drop_table("cad_repair_jobs")
    # The enum type outlives its table unless it is dropped explicitly, and leaving it would make
    # a re-upgrade fail on "type already exists".
    sa.Enum(name="repairjobstatus").drop(op.get_bind(), checkfirst=True)
