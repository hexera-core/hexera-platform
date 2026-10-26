# Responsibility: Keep the user's own notes on the chat session, word for word (contracts/user_notes).
# Boundaries: one nullable column. The new image REQUIRES it (the model selects it), so this is NOT
#             order-safe for the image: deploy runs migrations first, then rolls the image. The
#             previous image is unaffected by the extra column.

# Revision ID: 0017_chat_session_user_notes
# Revises: 0016_repair_review_history
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_chat_session_user_notes"
down_revision = "0016_repair_review_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("user_notes", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "user_notes")
