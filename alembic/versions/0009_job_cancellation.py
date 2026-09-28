# Responsibility: Let a job end as `cancelled`, and keep the owner's reason beside it.
# Boundaries: one enum value and one column. WHEN a job may be cancelled, and by whom, is decided by
#             application/job_cancel.py; this revision decides nothing about that.

# WHY A STATUS AND NOT A FAILURE REASON.
#
# A cancel is the owner's decision, not something that broke. Recording it as `failed` with a new
# FailedReason would charge nothing (only a succeeded job is charged) but would also count it among
# the runs the product could not finish, and tell the owner their run "did not complete successfully"
# when they stopped it themselves. A terminal status of its own keeps every one of those readings
# truthful, and the transition table can then refuse a worker's late result the same way it refuses
# one on a finished job.
#
# WHY INSIDE THE TRANSACTION. `ALTER TYPE ... ADD VALUE` could not run inside a transaction block
# before PostgreSQL 12; from 12 on it can, as long as the new value is not USED in the same
# transaction - and nothing here uses it. Both the compose database and Cloud SQL run 16. Running it
# in the migration's own transaction also keeps env.py's `SET LOCAL search_path` in force, so the
# unqualified type name resolves in the application schema like every other statement here; an
# autocommit block would commit that SET LOCAL away and leave the statement to the role's own
# search_path. The version is checked rather than assumed.
#
# DOWNGRADE rebuilds the type without the value, and refuses while any row still carries it: a
# cancelled job rewritten as anything else would be a lie in the owner's history.

# Revision ID: 0009_job_cancellation
# Revises: 0008_overage_metering
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_job_cancellation"
down_revision = "0008_overage_metering"
branch_labels = None
depends_on = None

_JOB_STATUS_BEFORE = ("pending", "running", "succeeded", "failed", "queued", "pending_review")


def upgrade() -> None:
    bind = op.get_bind()
    version = tuple(getattr(bind.dialect, "server_version_info", None) or ())
    if version and version < (12,):
        raise RuntimeError(
            f"PostgreSQL {'.'.join(map(str, version))} cannot add an enum value inside a "
            "transaction; this revision needs 12 or later")
    op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'cancelled'")
    op.add_column("simulation_jobs",
                  sa.Column("cancel_reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(
        "SELECT count(*) FROM simulation_jobs WHERE status = 'cancelled'")).scalar() or 0
    if held:
        raise RuntimeError(
            f"{held} job(s) are cancelled; refusing to downgrade a status out of their history. "
            "Decide what those rows should say first.")
    op.drop_column("simulation_jobs", "cancel_reason")
    op.execute("ALTER TYPE jobstatus RENAME TO jobstatus_before_0009")
    op.execute("CREATE TYPE jobstatus AS ENUM ("
               + ", ".join(f"'{v}'" for v in _JOB_STATUS_BEFORE) + ")")
    op.execute("ALTER TABLE simulation_jobs ALTER COLUMN status TYPE jobstatus "
               "USING status::text::jobstatus")
    op.execute("DROP TYPE jobstatus_before_0009")
