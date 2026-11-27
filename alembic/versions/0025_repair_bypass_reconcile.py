# Responsibility: Bring a database that took our 0023/0024 before main's 0023 up to what main's 0023_repair_bypass_and_viewer builds.
# Boundaries: every statement is conditional on the live schema, so a database that came through main's
#             0023 sees no change. Nothing is rewritten and nothing is dropped; the downgrade is empty
#             because main's 0023 owns these objects.
#
# WHY THIS EXISTS. 0023_chat_session_attachments and 0024_meshing_shell_elements_set were written
# behind 0022_returned_cad_hints, and a database (the dev site) ran them there. Main then landed its
# own 0023_repair_bypass_and_viewer behind 0022. The merged chain puts main's 0023 first and ours
# after it, keeping our ids as they were, so an already-migrated database is still at a known
# revision: 0024_meshing_shell_elements_set. Alembic counts main's 0023 as an ancestor of that
# revision and will never run it there. This revision does its work instead, and only the part that
# is missing:
#   - organizations.repair_auto_accept_allowed BOOLEAN NOT NULL DEFAULT false, added only if absent
#     (same name, type, nullability and server default as main's 0023);
#   - the 'repair_viewer' label on the artifacttype enum, through the same
#     ALTER TYPE ... ADD VALUE IF NOT EXISTS that main's 0023 runs, which PostgreSQL itself skips
#     when the label is already there.

# Revision ID: 0025_repair_bypass_reconcile
# Revises: 0024_meshing_shell_elements_set
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_repair_bypass_reconcile"
down_revision = "0024_meshing_shell_elements_set"
branch_labels = None
depends_on = None

#: Must equal main's 0023_repair_bypass_and_viewer._VIEWER_LABEL (and
#: persistence.models.ArtifactType.repair_viewer.value).
_VIEWER_LABEL = "repair_viewer"


def _has_column(table: str, column: str) -> bool:
    # current_schema(), not the inspector's cached default: alembic/env.py anchors search_path to the
    # application schema for this transaction, and that is where the table lives
    return op.get_bind().execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"),
        {"t": table, "c": column}).first() is not None


def upgrade() -> None:
    if not _has_column("organizations", "repair_auto_accept_allowed"):
        op.add_column("organizations",
                      sa.Column("repair_auto_accept_allowed", sa.Boolean(), nullable=False,
                                server_default=sa.text("false")))
    op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{_VIEWER_LABEL}'")


def downgrade() -> None:
    # Nothing: the column and the label belong to 0023_repair_bypass_and_viewer, whose downgrade
    # removes the column. Dropping them here would take them from a database that came through
    # main's path, where this revision added nothing.
    pass
