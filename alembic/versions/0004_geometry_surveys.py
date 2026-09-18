# Responsibility: Create the geometry_surveys table: the survey composed for what the customer said, and their answers.
# Boundaries: one table and its indexes; it touches nothing an earlier revision created.

# Additive and reversible on its own terms. Nothing references this table, so the downgrade drops
# exactly what it created. The whole path that writes it is behind GEOMETRY_SURVEY_ENABLED, which is
# off by default and dead unless the measurement and its readers are on too, so a deployment that
# upgrades and turns nothing on gains an empty table and no behaviour.
#
# Its own table rather than a key in geometry_measurements.document, because the look worker reads
# that document, renders for thirty seconds and writes it back: an answer recorded in between would
# be lost. RESTRICT on the source for the same reason the measurement has it: this is lineage.
#
# Revision ID: 0004_geometry_surveys
# Revises: 0003_geometry_measurements
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0004_geometry_surveys'
down_revision = '0003_geometry_measurements'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('geometry_surveys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('geometry_source_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('facts_sha256', sa.String(length=64), server_default='', nullable=False),
    sa.Column('stage', sa.String(length=32), server_default='surveyed', nullable=False),
    sa.Column('survey', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('composed_for', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('planner_block', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('asked', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('agent_git_sha', sa.String(length=64), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    # The digest is what an answer is bound to; a row whose text is not one describes nothing.
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_geometry_surveys_sha256_shape'),
    sa.ForeignKeyConstraint(['geometry_source_id'], ['geometry_sources.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    # ONE survey per upload, as there is one measurement per upload.
    sa.UniqueConstraint('geometry_source_id', name='uq_geometry_survey_source')
    )
    op.create_index('ix_geometry_surveys_owner_sha', 'geometry_surveys', ['owner_id', 'sha256'], unique=False)
    op.create_index(op.f('ix_geometry_surveys_owner_id'), 'geometry_surveys', ['owner_id'], unique=False)
    op.create_index(op.f('ix_geometry_surveys_sha256'), 'geometry_surveys', ['sha256'], unique=False)
    op.create_index(op.f('ix_geometry_surveys_session_id'), 'geometry_surveys', ['session_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_geometry_surveys_session_id'), table_name='geometry_surveys')
    op.drop_index(op.f('ix_geometry_surveys_sha256'), table_name='geometry_surveys')
    op.drop_index(op.f('ix_geometry_surveys_owner_id'), table_name='geometry_surveys')
    op.drop_index('ix_geometry_surveys_owner_sha', table_name='geometry_surveys')
    op.drop_table('geometry_surveys')
