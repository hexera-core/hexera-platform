# Responsibility: Let a byte count hold any real file's size - every size column becomes BIGINT.
# Boundaries: three column type changes, nothing else.

# WHY. The baseline made artifacts.size_bytes, geometry_sources.size_bytes and
# artifact_reconciliations.object_size 32-bit INTEGER, whose ceiling is 2,147,483,647 bytes
# (2 GiB). A 16.5M-tet FEA assembly (job 986ea907, 2026-11-02) writes a 3.6 GB mesh.msh, 3.5-6.2 GB
# solver decks and a 4.5 GB bundle. Each one uploaded fine, then its artifact row was refused
# (asyncpg: value out of int32 range), the uploader deleted the object as an orphan, and the
# required bundle's refusal failed the whole delivery three times over: "the mesh was built but
# could not be stored for download". Only the 72 MB viewer payload survived. BIGINT's ceiling is
# 9.2 EB, so no file this service will ever hold is too big to be recorded.
#
# ORDER-SAFE FOR THE IMAGE. The running image writes Python ints; an int that fits INTEGER fits
# BIGINT, so the previous image keeps working against the widened columns and this can migrate
# before the new image rolls. Widening INTEGER to BIGINT rewrites each table under an ACCESS
# EXCLUSIVE lock; these tables hold one row per file, so the rewrite is brief.

# Revision ID: 0018_byte_counts_bigint
# Revises: 0017_chat_session_user_notes
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_byte_counts_bigint"
down_revision = "0017_chat_session_user_notes"
branch_labels = None
depends_on = None

#: (table, column) of every byte count. persistence.models declares each as BigInteger, and the
#: unit tier asserts no byte-count column anywhere in the models is narrower.
BYTE_COLUMNS = (
    ("artifacts", "size_bytes"),
    ("geometry_sources", "size_bytes"),
    ("artifact_reconciliations", "object_size"),
)


def upgrade() -> None:
    for table, column in BYTE_COLUMNS:
        op.alter_column(table, column, type_=sa.BigInteger(), existing_type=sa.Integer())


def downgrade() -> None:
    # A row over 2 GiB cannot narrow back; the cast fails loudly rather than truncating a size.
    for table, column in BYTE_COLUMNS:
        op.alter_column(table, column, type_=sa.Integer(), existing_type=sa.BigInteger())
