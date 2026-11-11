# Responsibility: Keep each organisation's shell element choice - the Meshing settings page's one switch, "Shell
#                 elements: Quads | Triangles" (SHSTAGE-8, engines/meshing_standard STD-4) - with its meshing standard.
# Boundaries: one column on organization_meshing_standards (0020), nothing else touched. Order-safe for the image: the
#             previous image never reads it, and every existing row takes 'quads' (the shell mesh it always made).

# Revision ID: 0021_meshing_standard_shell_elements
# Revises: 0020_organization_meshing_standard
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_meshing_standard_shell_elements"
down_revision = "0020_organization_meshing_standard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # "quads" | "triangles": a shell's elements for every run of the organisation, under the run's notes and the
    # stage's switch
    op.add_column("organization_meshing_standards",
                  sa.Column("shell_elements", sa.String(16), nullable=False, server_default="quads"))


def downgrade() -> None:
    op.drop_column("organization_meshing_standards", "shell_elements")
