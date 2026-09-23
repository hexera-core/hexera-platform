# Responsibility: Create the geometry_measurements table: what an uploaded file was measured to be.
# Boundaries: one table and its indexes; it touches nothing an earlier revision created.

# Additive and reversible on its own terms. Nothing references this table, so the downgrade drops
# exactly what it created and the schema underneath is untouched. When this revision shipped the path
# that writes it was behind a flag that was off by default, so a deployment that upgraded and turned
# nothing on gained an empty table and no behaviour. The flag is retired and every upload is measured;
# the table and this migration are unchanged by that.
#
# The unique constraint is on geometry_source_id alone: one upload, one measurement, because the
# measurement is a pure function of the bytes. RESTRICT on the foreign key matches every other
# reference to geometry_sources - a measurement is lineage, and deleting a source must not quietly
# destroy the record of what it was.
#
# Revision ID: 0003_geometry_measurements
# Revises: 0002_api_keys
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0003_geometry_measurements'
down_revision = '0002_api_keys'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('geometry_measurements',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('geometry_source_id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('purpose', sa.String(length=32), server_default='', nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.String(length=512), server_default='', nullable=False),
    sa.Column('document', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('facts_schema_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('agent_git_sha', sa.String(length=64), server_default='', nullable=False),
    sa.Column('measure_seconds', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    # The digest is the identity of the bytes; a row whose text is not one is not a measurement of
    # anything. Checked in the database because the column is what a reader compares a session's
    # source against before it trusts a stored document.
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_geometry_measurements_sha256_shape'),
    sa.ForeignKeyConstraint(['geometry_source_id'], ['geometry_sources.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    # ONE measurement per upload. Two rows for one source would be two answers to a question with
    # one answer, and nothing downstream could choose between them.
    sa.UniqueConstraint('geometry_source_id', name='uq_geometry_measurement_source')
    )
    op.create_index('ix_geometry_measurements_owner_sha', 'geometry_measurements', ['owner_id', 'sha256'], unique=False)
    op.create_index(op.f('ix_geometry_measurements_owner_id'), 'geometry_measurements', ['owner_id'], unique=False)
    op.create_index(op.f('ix_geometry_measurements_sha256'), 'geometry_measurements', ['sha256'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_geometry_measurements_sha256'), table_name='geometry_measurements')
    op.drop_index(op.f('ix_geometry_measurements_owner_id'), table_name='geometry_measurements')
    op.drop_index('ix_geometry_measurements_owner_sha', table_name='geometry_measurements')
    op.drop_table('geometry_measurements')
