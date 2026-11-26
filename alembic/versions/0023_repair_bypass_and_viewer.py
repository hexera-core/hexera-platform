# Responsibility: Let an organisation allow its runs to auto-accept repairs (skip the review screen),
#                 and let an artifact row hold the repair viewer's payload (CRS v5 beta).
# Boundaries: one boolean column on organizations and one new label on the artifacttype enum,
#             nothing else touched. Order-safe for the image: the previous image never reads the
#             column, every existing organisation takes `false` (review required, today's rule),
#             and an unused enum label constrains nothing.

# Revision ID: 0023_repair_bypass_and_viewer
# Revises: 0022_returned_cad_hints
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_repair_bypass_and_viewer"
down_revision = "0022_returned_cad_hints"
branch_labels = None
depends_on = None

#: Must equal persistence.models.ArtifactType.repair_viewer.value, which the unit tier asserts.
_VIEWER_LABEL = "repair_viewer"


def upgrade() -> None:
    op.add_column("organizations",
                  sa.Column("repair_auto_accept_allowed", sa.Boolean(), nullable=False,
                            server_default=sa.text("false")))
    op.execute(f"ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS '{_VIEWER_LABEL}'")


def downgrade() -> None:
    # The column goes; the enum label stays (PostgreSQL cannot drop one - see revision 0011).
    op.drop_column("organizations", "repair_auto_accept_allowed")
