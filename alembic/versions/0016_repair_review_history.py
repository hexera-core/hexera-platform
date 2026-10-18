# Responsibility: Let a re-minted review item carry the decisions made on the same defect before.
# Boundaries: one nullable column. The new image REQUIRES it (the model selects it), so this is NOT
#             order-safe for the image: deploy runs migrations first, then rolls the image. The
#             previous image is unaffected by the extra column.

# Revision ID: 0016_repair_review_history
# Revises: 0015_repair_review
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_repair_review_history"
down_revision = "0015_repair_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repair_review_items",
                  sa.Column("decision_history", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("repair_review_items", "decision_history")
