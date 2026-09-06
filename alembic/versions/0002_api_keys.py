# Responsibility: Create the api_keys table: the durable record behind a programmatic credential.
# Boundaries: one table, its indexes and its shape constraints; it touches nothing the baseline created.

# The baseline is a pre-release squash and stays that way. This revision is additive and reversible
# on its own terms: it creates a table nothing else references, so its downgrade drops exactly what
# it created and leaves the baseline's schema untouched.
#
# `organization_id` carries NO foreign key. Organisations are a later item and their table does not
# exist yet; the column is here so the tenant boundary is present before data accumulates, and the
# constraint arrives with the organisations migration.
#
# Revision ID: 0002_api_keys
# Revises: 0001_schema_baseline
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002_api_keys'
down_revision = '0001_schema_baseline'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('api_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('owner_id', sa.String(length=256), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=128), server_default='', nullable=False),
    sa.Column('key_prefix', sa.String(length=64), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('plan', sa.String(length=32), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name='ck_api_keys_key_hash_shape'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_api_keys_owner_created', 'api_keys', ['owner_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_api_keys_organization_id'), 'api_keys', ['organization_id'], unique=False)
    op.create_index(op.f('ix_api_keys_owner_id'), 'api_keys', ['owner_id'], unique=False)
    # UNIQUE: two rows sharing a prefix would make one presented key resolve to two owners.
    op.create_index(op.f('ix_api_keys_key_prefix'), 'api_keys', ['key_prefix'], unique=True)


def downgrade():
    op.drop_index(op.f('ix_api_keys_key_prefix'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_owner_id'), table_name='api_keys')
    op.drop_index(op.f('ix_api_keys_organization_id'), table_name='api_keys')
    op.drop_index('ix_api_keys_owner_created', table_name='api_keys')
    op.drop_table('api_keys')
