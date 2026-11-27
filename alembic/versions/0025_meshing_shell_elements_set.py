# Responsibility: Keep whether the user SET the Meshing settings page's "Shell elements" switch (engines/meshing_standard STD-4): a switch left at its default (Quads) no longer sets aside the element kind the standard's typed words name ("tria mesh only").
# Boundaries: one column on organization_meshing_standards (0021's switch beside it), nothing else touched. Existing rows on Triangles were set by a user (Triangles is never the default) and are marked set; rows on Quads are taken as left at the default. The new image REQUIRES the column (the model selects it): deploy runs migrations first, then rolls the image; the previous image never reads it.

# Revision ID: 0025_meshing_shell_elements_set
# Revises: 0024_chat_session_attachments
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_meshing_shell_elements_set"
down_revision = "0024_chat_session_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organization_meshing_standards",
                  sa.Column("shell_elements_set", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE organization_meshing_standards SET shell_elements_set = true WHERE shell_elements = 'triangles'")


def downgrade() -> None:
    op.drop_column("organization_meshing_standards", "shell_elements_set")
