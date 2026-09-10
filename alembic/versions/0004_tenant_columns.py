# Responsibility: Give every owner-scoped table an organisation, and fill it in for the rows that already exist.
# Boundaries: the tenant column and its backfill; the tables it points at were created by 0003.

# THIS IS THE HALF THAT TOUCHES LIVE ROWS, which is why it is not 0003. It must be idempotent -
# re-running it changes nothing - and it must be rehearsed against a restored copy of the dev
# database before it is allowed near prod.
#
# The column is NULLABLE on purpose. deploy.sh migrates at stage 220 and deploys the API at stage
# 245, so between them the OLD revision serves against the NEW schema and inserts rows naming no
# organisation. NOT NULL would fail those inserts. 0005 closes the column once no writer can
# produce one.
#
# Revision ID: 0004_tenant_columns
# Revises: 0003_identity_and_credits
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0004_tenant_columns'
down_revision = '0003_identity_and_credits'
branch_labels = None
depends_on = None

#: Every table that scopes on owner_id. `artifacts` is deliberately absent: it has no owner_id and
#: reaches its tenant through the job it belongs to.
_TENANT_TABLES = (
    'geometry_sources', 'simulation_jobs', 'chat_sessions', 'geometry_interpretations',
    'capture_operations', 'artifact_reconciliations', 'source_object_cleanups',
)


def upgrade():
    for table in _TENANT_TABLES:
        op.add_column(table, sa.Column('organization_id', sa.UUID(), nullable=True))
        op.create_index(op.f(f'ix_{table}_organization_id'), table, ['organization_id'],
                        unique=False)
        op.create_foreign_key(f'fk_{table}_organization_id', table, 'organizations',
                              ['organization_id'], ['id'], ondelete='RESTRICT')

    # The key 0002 declared without one, now that the table it points at exists.
    op.create_foreign_key('fk_api_keys_organization_id', 'api_keys', 'organizations',
                          ['organization_id'], ['id'], ondelete='RESTRICT')

    _backfill()


def _backfill():
    bind = op.get_bind()

    # 1. ONE ORGANISATION AND ONE USER PER DISTINCT owner_id, across every tenant table.
    #    owner_id is an email everywhere it is a real identity. A value that is not one still
    #    gets a row: it owns data, and leaving it unstamped would make that data unreachable
    #    once reads scope on the organisation.
    #
    #    `WHERE NOT EXISTS` is what makes the whole backfill idempotent: a second run inserts
    #    nothing, which matters because this migration will be rehearsed more than once.
    owners_union = " UNION ".join(
        f"SELECT DISTINCT owner_id FROM {table} WHERE owner_id IS NOT NULL AND owner_id <> ''"
        for table in _TENANT_TABLES)

    bind.execute(sa.text(f"""
        INSERT INTO users (id, firebase_uid, email, name, created_at)
        SELECT gen_random_uuid(), NULL, lower(o.owner_id), '', now()
        FROM ({owners_union}) AS o
        WHERE o.owner_id LIKE '%@%'
          AND NOT EXISTS (SELECT 1 FROM users u WHERE u.email = lower(o.owner_id))
    """))

    bind.execute(sa.text(f"""
        INSERT INTO organizations (id, name, slug, created_at)
        SELECT gen_random_uuid(), o.owner_id, 'backfill-' || md5(lower(o.owner_id)), now()
        FROM ({owners_union}) AS o
        WHERE NOT EXISTS (
            SELECT 1 FROM organizations g WHERE g.slug = 'backfill-' || md5(lower(o.owner_id)))
    """))

    #    A membership only where the owner_id was an address and so produced a user. An owner_id
    #    that is not an address still has an organisation - its data has a tenant - but nobody
    #    can sign in as it, which is correct.
    bind.execute(sa.text(f"""
        INSERT INTO memberships (id, user_id, organization_id, role, created_at)
        SELECT gen_random_uuid(), u.id, g.id, 'owner', now()
        FROM ({owners_union}) AS o
        JOIN users u ON u.email = lower(o.owner_id)
        JOIN organizations g ON g.slug = 'backfill-' || md5(lower(o.owner_id))
        WHERE NOT EXISTS (
            SELECT 1 FROM memberships m
            WHERE m.user_id = u.id AND m.organization_id = g.id)
    """))

    # 2. STAMP EVERY EXISTING ROW from its own owner_id. `IS NULL` keeps it idempotent and means
    #    a row written between the stamp and the new image is left for the application to fill.
    for table in _TENANT_TABLES:
        bind.execute(sa.text(f"""
            UPDATE {table} SET organization_id = g.id
            FROM organizations g
            WHERE g.slug = 'backfill-' || md5(lower({table}.owner_id))
              AND {table}.organization_id IS NULL
              AND {table}.owner_id IS NOT NULL AND {table}.owner_id <> ''
        """))

    bind.execute(sa.text("""
        UPDATE api_keys SET organization_id = g.id
        FROM organizations g
        WHERE g.slug = 'backfill-' || md5(lower(api_keys.owner_id))
          AND api_keys.organization_id IS NULL
          AND api_keys.owner_id IS NOT NULL AND api_keys.owner_id <> ''
    """))


def downgrade():
    # The FK first: a column cannot be dropped while a constraint names it. The backfilled
    # organisations, users and memberships are 0003's tables and are NOT removed here - dropping
    # them is that revision's downgrade, and doing it from this one would delete accounts.
    op.drop_constraint('fk_api_keys_organization_id', 'api_keys', type_='foreignkey')
    op.execute("UPDATE api_keys SET organization_id = NULL")
    for table in _TENANT_TABLES:
        op.drop_constraint(f'fk_{table}_organization_id', table, type_='foreignkey')
        op.drop_index(op.f(f'ix_{table}_organization_id'), table_name=table)

    # Named per table, not looped: this migration must drop exactly the columns it created.
    op.drop_column('geometry_sources', 'organization_id')
    op.drop_column('simulation_jobs', 'organization_id')
    op.drop_column('chat_sessions', 'organization_id')
    op.drop_column('geometry_interpretations', 'organization_id')
    op.drop_column('capture_operations', 'organization_id')
    op.drop_column('artifact_reconciliations', 'organization_id')
    op.drop_column('source_object_cleanups', 'organization_id')
