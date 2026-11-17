# Responsibility: Widen every column that holds a byte count of a file this system produces, from
# 32-bit to 64-bit.
# Boundaries: three column types. No data moves, no row is read, nothing else changes.

# WHAT THIS COST, so nobody narrows them again. Job 0ff6f9e5 meshed a 283,410,955-cell conjugate
# cold plate over 5h07m on a 64-core VM, passed every gate, and was then marked FAILED because its
# OpenFOAM case bundle is 10,616,027,998 bytes and `artifacts.size_bytes` stopped at 2,147,483,647:
#
#     invalid input for query argument $8: 10616027998 (value out of int32 range)
#
# The refusal is asyncpg inferring the bind parameter's type from the int4 COLUMN, so it lands on
# the parameter and no Python-side coercion can dodge it. Delivery is one transaction, so that one
# row write took mesh.msh and viewer_data.json down with it, and the user was told the review had
# not finished.
#
# THE OTHER TWO ARE NOT SPECULATIVE. `artifact_reconciliations.object_size` is the same number on
# the orphan path: had the bundle's cleanup delete failed, persist_orphans would have thrown here
# in exactly the same way, on the error path, where there is nothing left to catch it.
# `geometry_sources.size_bytes` holds an uploaded CAD file and is the one a customer controls.
#
# 2 GiB is not a bound anything in this product respects. A conjugate case grows with the cell
# count and the cell count is bounded by the 2^31 FACE label at 600-650M cells, so the bundle has
# another factor of two in it before the mesher itself runs out.
#
# ALTER ... TYPE BIGINT on an int4 column rewrites the table in PostgreSQL. These three tables are
# small (artifacts is one row per delivered file), so the rewrite is cheap here; on a large
# deployment it takes an ACCESS EXCLUSIVE lock for the duration and belongs in a maintenance
# window.
#
# Revision ID: 0018_byte_counts_are_64_bit
# Revises: 0017_chat_session_user_notes
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0018_byte_counts_are_64_bit'
down_revision = '0017_chat_session_user_notes'
branch_labels = None
depends_on = None

#: (table, column, nullable) for every byte count that can exceed 2 GiB. The nullability is
#: restated so the downgrade puts back exactly what was there, not alembic's default.
_BYTE_COUNTS = (
    ('artifacts', 'size_bytes', True),
    ('artifact_reconciliations', 'object_size', True),
    ('geometry_sources', 'size_bytes', False),
)


def upgrade() -> None:
    for table, column, nullable in _BYTE_COUNTS:
        op.alter_column(table, column,
                        existing_type=sa.Integer(),
                        type_=sa.BigInteger(),
                        existing_nullable=nullable)


def downgrade() -> None:
    # NOT LOSSLESS, and that is the point of saying so here: any row already holding a count above
    # 2,147,483,647 makes this fail. That is the correct outcome - silently truncating the recorded
    # size of a delivered mesh would make the integrity record lie.
    for table, column, nullable in _BYTE_COUNTS:
        op.alter_column(table, column,
                        existing_type=sa.BigInteger(),
                        type_=sa.Integer(),
                        existing_nullable=nullable)
