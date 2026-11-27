# Responsibility: Keep the index of the files a user attached in the chat or notes box on the chat session (application/chat_attachments, NOTE-16).
# Boundaries: one nullable column. The new image REQUIRES it (the model selects it), so this is NOT
#             order-safe for the image: deploy runs migrations first, then rolls the image. The
#             previous image is unaffected by the extra column.

# Revision ID: 0024_chat_session_attachments
# Revises: 0023_repair_bypass_and_viewer
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_chat_session_attachments"
down_revision = "0023_repair_bypass_and_viewer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("attachments", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "attachments")
