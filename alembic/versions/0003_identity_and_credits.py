# Responsibility: Create the identity and credit tables: organisations, users, memberships and the credit ledger.
# Boundaries: four new tables that reference nothing existing; adding organization_id to existing tables is 0004's work.

# Additive and reversible on its own terms, exactly as 0002 was: every table it creates is one
# nothing else references yet, so the downgrade drops precisely what the upgrade made. The
# organization_id columns on the EXISTING tables, and the backfill that fills them, are deliberately
# a separate revision - that is the half that touches live rows, and it must be reviewable,
# rehearsable and reversible without this half moving with it.
#
# Revision ID: 0003_identity_and_credits
# Revises: 0002_api_keys
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003_identity_and_credits'
down_revision = '0002_api_keys'
branch_labels = None
depends_on = None

#: Declared once each, because the downgrade must name exactly the types this revision created.
_MEMBERSHIP_ROLE = ("owner", "member")
_CREDIT_ENTRY_TYPE = ("grant", "debit", "refund")
_ENUM_TYPES = ("creditentrytype", "membershiprole")


def upgrade():
    op.create_table('organizations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_organizations_slug'), 'organizations', ['slug'], unique=True)

    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('firebase_uid', sa.String(length=128), nullable=True),
    sa.Column('email', sa.String(length=256), nullable=False),
    sa.Column('name', sa.String(length=256), server_default='', nullable=False),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('email = lower(email)', name='ck_users_email_lowercased'),
    sa.PrimaryKeyConstraint('id')
    )
    # UNIQUE on both: a duplicate uid would let one Identity Platform account resolve to two
    # users, and a duplicate email would make the backfill-linking lookup ambiguous.
    op.create_index(op.f('ix_users_firebase_uid'), 'users', ['firebase_uid'], unique=True)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    op.create_table('memberships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.Enum(*_MEMBERSHIP_ROLE, name='membershiprole'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'organization_id', name='uq_memberships_user_org')
    )
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_index(op.f('ix_memberships_organization_id'), 'memberships', ['organization_id'], unique=False)

    op.create_table('credit_ledger',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('entry_type', sa.Enum(*_CREDIT_ENTRY_TYPE, name='creditentrytype'), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('reason', sa.String(length=128), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount <> 0', name='ck_credit_ledger_amount_nonzero'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_credit_ledger_organization_id'), 'credit_ledger', ['organization_id'], unique=False)
    # The balance query and the ledger view read this one.
    op.create_index('ix_credit_ledger_org_created', 'credit_ledger', ['organization_id', 'created_at'], unique=False)


def downgrade():
    op.drop_index('ix_credit_ledger_org_created', table_name='credit_ledger')
    op.drop_index(op.f('ix_credit_ledger_organization_id'), table_name='credit_ledger')
    op.drop_table('credit_ledger')
    op.drop_index(op.f('ix_memberships_organization_id'), table_name='memberships')
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_index(op.f('ix_users_firebase_uid'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_organizations_slug'), table_name='organizations')
    op.drop_table('organizations')
    # The enum TYPES outlive their tables unless dropped by name - a re-run of the upgrade would
    # then fail on "type already exists". Same discipline as the baseline's _ENUM_TYPES.
    bind = op.get_bind()
    for name in _ENUM_TYPES:
        sa.Enum(name=name).drop(bind, checkfirst=True)
