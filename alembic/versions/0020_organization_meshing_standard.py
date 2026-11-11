# Responsibility: Hold each organisation's meshing standard - the mesh requirements its members set once in the
#                 console's Meshing settings and every run applies under that run's notes (engines/meshing_standard).
# Boundaries: one new table, nothing else touched. Order-safe for the image: the previous image never reads it,
#             and the new image reads it only on the settings routes and at approval (a missing table there is
#             caught and the run is dispatched without a standard) - deploy still runs migrations first.

# Revision ID: 0020_organization_meshing_standard
# Revises: 0019_signup_claims_and_erasure
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_organization_meshing_standard"
down_revision = "0019_signup_claims_and_erasure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ONE ROW PER ORGANISATION, keyed by it: no row is no standard. The row goes with its organisation.
    op.create_table(
        "organization_meshing_standards",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        # the reading kept at save time: the model's answer and the key of the question it answered
        sa.Column("reading", postgresql.JSONB(), nullable=True),
        sa.Column("updated_by", sa.String(320), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("organization_meshing_standards")
