# Responsibility: Create the `outreach` schema and the engine's 14 tables, its view and its indexes.
# Boundaries: it creates only what hexera-ops defined, inside a schema of its own; it touches
#             nothing in `public` and it changes no value semantics.

# THE OUTREACH SCHEMA, moved from a laptop's SQLite file onto this chain.
#
# WHY A SCHEMA AND NOT A NAME PREFIX. The original names are `events`, `settings`, `contacts`,
# `templates` and `messages` - every one a word this product will want for something else, so they
# cannot simply join the pipeline's tables in `public`.
#
# The first attempt at this renamed them to `outreach_*`. That works, and it makes every one of the
# ~100 SQL strings in the 15,000 lines being ported alongside this WRONG - each would need editing,
# and a mistyped table name in a query fails at runtime rather than at build. A schema is what
# Postgres provides for exactly this: the tables keep the names the application already writes, and
# `search_path = outreach, public` resolves them.
#
# It is also a real privilege boundary. The console's database role is granted on this schema, not
# on a naming convention, so "may it read the pipeline's tables" has an answer enforced by Postgres
# rather than by everyone remembering a prefix.
#
# WHY THE VALUE SEMANTICS ARE PRESERVED EXACTLY, AND WHAT THAT COSTS.
# Timestamps stay TEXT holding ISO-8601 UTC, and booleans stay INTEGER holding 0/1, because that is
# what the 15,000 lines being ported alongside this already read and write. The schema's own header
# explains the choice: ISO-8601 sorts lexically, so `<`, `>` and `ORDER BY` mean the same thing on
# TEXT in Postgres as they did in SQLite.
#
# Converting them here would be the better schema and the worse migration. The port's risk is
# already dominated by turning a synchronous database API asynchronous across 28 files; adding a
# representation change on top interleaves two classes of breakage, and the failure it produces -
# a Date where a string was expected, rendering as "Invalid Date" - is exactly the kind that
# survives review and reaches production. Types are modernised in their own revision once the
# engine change has settled, where the diff is about types and its tests can be about types.
#
# WHAT THE SOURCE schema.sql DOES NOT TELL YOU. The application's own `migrate()` carried an
# `ensureColumns()` step that ALTERed in columns added after the first release - `CREATE TABLE IF
# NOT EXISTS` cannot add a column to a table that already exists. Reading schema.sql alone
# therefore misses them. `contacts.is_yc` is one, and it is load-bearing.
#
# WHAT DID HAVE TO CHANGE: SQLite's `INTEGER PRIMARY KEY AUTOINCREMENT` has no Postgres spelling.
# It becomes a BigInteger identity column, which is the one place a row's type differs from the
# source. `AUTOINCREMENT` guarantees monotonic, never-reused ids; a Postgres identity sequence
# gives the same guarantee, so nothing downstream that assumes "higher id means later row" breaks.
#
# Revision ID: 0003_outreach_schema
# Revises: 0002_api_keys
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003_outreach_schema'
down_revision = '0002_api_keys'
branch_labels = None
depends_on = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    """An ISO-8601 UTC timestamp, stored as text. See the header for why."""
    return sa.Column(name, sa.Text(), nullable=nullable)


def _flag(name: str, default: str = '0') -> sa.Column:
    """A 0/1 boolean, stored as an integer. See the header for why."""
    return sa.Column(name, sa.Integer(), server_default=default, nullable=False)


def upgrade():
    op.execute('CREATE SCHEMA IF NOT EXISTS outreach')

    op.create_table(
        'contacts',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('source_sheet', sa.Text(), nullable=False),
        sa.Column('source_row_id', sa.BigInteger(), nullable=True),
        sa.Column('company', sa.Text(), nullable=False),
        sa.Column('full_name', sa.Text(), nullable=True),
        sa.Column('first_name', sa.Text(), nullable=True),
        # 'person' -> a real human name, safe to greet by first name.
        # 'placeholder' -> the sheet holds a role ("CEO") instead of a name, which blocks
        # personalized sends until a human fixes it.
        sa.Column('name_quality', sa.Text(), server_default='person', nullable=False),
        sa.Column('role', sa.Text(), nullable=True),
        sa.Column('role_group', sa.Text(), nullable=True),
        sa.Column('industry', sa.Text(), nullable=True),
        sa.Column('tier', sa.Text(), nullable=True),
        sa.Column('stage', sa.Text(), nullable=True),
        sa.Column('email', sa.Text(), nullable=True),
        sa.Column('email_normalized', sa.Text(), nullable=True),
        sa.Column('domain', sa.Text(), nullable=True),
        sa.Column('linkedin', sa.Text(), nullable=True),
        sa.Column('priority', sa.Text(), nullable=True),
        sa.Column('outreach_channel', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        # NOT in the source schema.sql - it was added later by an ALTER in the application's own
        # migrate(), which is why it is easy to miss when reading the schema alone. It is not
        # optional: lib/engine/enroll.ts gates auto-fill on `c.is_yc = 0`, so without the column the
        # enrollment query errors, and with a wrong default it would cold-email people who are warm
        # contacts reached through Bookface and the founder directory.
        _flag('is_yc'),
        _ts('created_at'),
        _ts('updated_at'),
        sa.PrimaryKeyConstraint('id'),
        schema='outreach',
    )
    # PARTIAL unique, and the partiality is the point: many rows legitimately have no email, and a
    # plain UNIQUE would let the first NULL through and reject every one after it.
    op.create_index(
        'ix_outreach_contacts_email', 'contacts', ['email_normalized'],
        unique=True, postgresql_where=sa.text('email_normalized IS NOT NULL'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_contacts_domain', 'contacts', ['domain'], schema='outreach')
    op.create_index(
        'ix_outreach_contacts_sheet', 'contacts', ['source_sheet'], schema='outreach')
    op.create_index(
        'ix_outreach_contacts_priority', 'contacts', ['priority'], schema='outreach')
    op.create_index(
        'ix_outreach_contacts_industry', 'contacts', ['industry'], schema='outreach')
    op.create_index(
        'ix_outreach_contacts_role_group', 'contacts', ['role_group'], schema='outreach')

    op.create_table(
        'verifications',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('contact_id', sa.BigInteger(), nullable=False),
        # valid | risky | invalid | unknown
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        _flag('syntax_ok'),
        _flag('mx_ok'),
        sa.Column('mx_hosts', sa.Text(), nullable=True),
        _flag('is_role'),
        _flag('is_disposable'),
        _flag('is_free_provider'),
        _flag('is_catch_all'),
        _flag('smtp_checked'),
        sa.Column('smtp_code', sa.Integer(), nullable=True),
        sa.Column('smtp_message', sa.Text(), nullable=True),
        sa.Column('provider', sa.Text(), server_default='builtin', nullable=False),
        sa.Column('reasons', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        _ts('checked_at'),
        sa.ForeignKeyConstraint(['contact_id'], ['outreach.contacts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_verifications_contact', 'verifications',
        ['contact_id', sa.text('checked_at DESC')], schema='outreach')

    op.create_table(
        'domain_intel',
        sa.Column('domain', sa.Text(), nullable=False),
        _flag('mx_ok'),
        sa.Column('mx_hosts', sa.Text(), nullable=True),
        # NULL means "not yet determined", which is distinct from "determined to be false".
        sa.Column('is_catch_all', sa.Integer(), nullable=True),
        _flag('is_disposable'),
        _flag('is_free'),
        _ts('checked_at'),
        sa.PrimaryKeyConstraint('domain'),
        schema='outreach',
    )

    op.create_table(
        'templates',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        # opener -> starts a thread; follow_up -> replies in-thread, inheriting Re:
        sa.Column('kind', sa.Text(), server_default='opener', nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        _ts('created_at'),
        _ts('updated_at'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_outreach_templates_name'),
        schema='outreach',
    )

    op.create_table(
        'campaigns',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), server_default='draft', nullable=False),
        sa.Column('timezone', sa.Text(), server_default='America/Los_Angeles', nullable=False),
        sa.Column('daily_cap', sa.Integer(), server_default='40', nullable=False),
        sa.Column('send_window_start', sa.Integer(), server_default='8', nullable=False),
        sa.Column('send_window_end', sa.Integer(), server_default='17', nullable=False),
        sa.Column('send_days', sa.Text(), server_default='1,2,3,4,5', nullable=False),
        # Two contacts at one company must not get mail the same day; it reads as a blast.
        sa.Column('per_domain_daily_cap', sa.Integer(), server_default='1', nullable=False),
        _ts('created_at'),
        _ts('updated_at'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_outreach_campaigns_name'),
        schema='outreach',
    )

    op.create_table(
        'sequence_steps',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('campaign_id', sa.BigInteger(), nullable=False),
        sa.Column('step_number', sa.Integer(), nullable=False),
        sa.Column('template_id', sa.BigInteger(), nullable=False),
        sa.Column('delay_days', sa.Integer(), server_default='3', nullable=False),
        # Every follow-up is conditional on silence, kept explicit so the rule lives in the data.
        _flag('only_if_no_reply', '1'),
        _ts('created_at'),
        sa.ForeignKeyConstraint(['campaign_id'], ['outreach.campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_id'], ['outreach.templates.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('campaign_id', 'step_number', name='uq_outreach_steps_campaign_step'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_steps_campaign', 'sequence_steps', ['campaign_id', 'step_number'],
        schema='outreach',
    )

    op.create_table(
        'enrollments',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('campaign_id', sa.BigInteger(), nullable=False),
        sa.Column('contact_id', sa.BigInteger(), nullable=False),
        # pending|active|completed|replied|bounced|stopped|review|suppressed
        sa.Column('status', sa.Text(), server_default='pending', nullable=False),
        sa.Column('current_step', sa.Integer(), server_default='0', nullable=False),
        _ts('next_send_at', nullable=True),
        sa.Column('thread_id', sa.Text(), nullable=True),
        sa.Column('last_message_id', sa.Text(), nullable=True),
        sa.Column('stopped_reason', sa.Text(), nullable=True),
        _ts('enrolled_at'),
        _ts('updated_at'),
        sa.ForeignKeyConstraint(['campaign_id'], ['outreach.campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['contact_id'], ['outreach.contacts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('campaign_id', 'contact_id', name='uq_outreach_enrollments_pair'),
        schema='outreach',
    )
    # The scheduler's hot path: "what is due right now". Partial, because most rows have nothing
    # scheduled and indexing their NULLs would be indexing the answer "no".
    op.create_index(
        'ix_outreach_enrollments_due', 'enrollments', ['status', 'next_send_at'],
        postgresql_where=sa.text('next_send_at IS NOT NULL'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_enrollments_contact', 'enrollments', ['contact_id'], schema='outreach')
    op.create_index(
        'ix_outreach_enrollments_campaign', 'enrollments', ['campaign_id', 'status'],
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_enrollments_thread', 'enrollments', ['thread_id'], schema='outreach')

    op.create_table(
        'messages',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('enrollment_id', sa.BigInteger(), nullable=True),
        sa.Column('contact_id', sa.BigInteger(), nullable=True),
        sa.Column('direction', sa.Text(), nullable=False),
        sa.Column('step_number', sa.Integer(), nullable=True),
        sa.Column('template_id', sa.BigInteger(), nullable=True),
        # queued|dry_run|sent|failed|skipped. `dry_run` is a first-class state: a message fully
        # rendered and recorded but deliberately not sent.
        sa.Column('status', sa.Text(), server_default='queued', nullable=False),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('snippet', sa.Text(), nullable=True),
        sa.Column('to_email', sa.Text(), nullable=True),
        sa.Column('from_email', sa.Text(), nullable=True),
        sa.Column('gmail_message_id', sa.Text(), nullable=True),
        sa.Column('gmail_thread_id', sa.Text(), nullable=True),
        sa.Column('rfc822_message_id', sa.Text(), nullable=True),
        sa.Column('in_reply_to', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        _ts('scheduled_for', nullable=True),
        _ts('sent_at', nullable=True),
        _ts('received_at', nullable=True),
        _ts('created_at'),
        sa.ForeignKeyConstraint(['enrollment_id'], ['outreach.enrollments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['contact_id'], ['outreach.contacts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['template_id'], ['outreach.templates.id']),
        sa.PrimaryKeyConstraint('id'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_messages_enrollment', 'messages', ['enrollment_id', 'created_at'],
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_messages_contact', 'messages', ['contact_id'], schema='outreach')
    op.create_index(
        'ix_outreach_messages_thread', 'messages', ['gmail_thread_id'], schema='outreach')
    op.create_index(
        'ix_outreach_messages_sent', 'messages', ['direction', 'status', 'sent_at'],
        schema='outreach',
    )
    # Partial unique: the idempotency key for inbound sync. Only sent/received messages have a
    # Gmail id, and the NULLs of everything queued must not collide.
    op.create_index(
        'ix_outreach_messages_gmail_id', 'messages', ['gmail_message_id'],
        unique=True, postgresql_where=sa.text('gmail_message_id IS NOT NULL'),
        schema='outreach',
    )

    op.create_table(
        'replies',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('enrollment_id', sa.BigInteger(), nullable=True),
        sa.Column('contact_id', sa.BigInteger(), nullable=True),
        # positive|negative|neutral|ooo|auto|bounce. `ooo` is deliberately not a reply: it
        # reschedules rather than halting the sequence.
        sa.Column('classification', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), server_default='0', nullable=False),
        sa.Column('classifier', sa.Text(), server_default='rules', nullable=False),
        sa.Column('matched_rules', sa.Text(), nullable=True),
        _flag('requires_review'),
        _ts('reviewed_at', nullable=True),
        sa.Column('reviewed_by', sa.Text(), nullable=True),
        sa.Column('human_override', sa.Text(), nullable=True),
        _ts('received_at'),
        _ts('created_at'),
        sa.ForeignKeyConstraint(['message_id'], ['outreach.messages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['enrollment_id'], ['outreach.enrollments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['contact_id'], ['outreach.contacts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_replies_classification', 'replies', ['classification', 'received_at'],
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_replies_review', 'replies', ['requires_review', 'received_at'],
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_replies_contact', 'replies', ['contact_id'], schema='outreach')

    # The hard "never contact" gate, checked immediately before every send. A domain-scope entry
    # blocks every address at that company, which is the correct reading of "take us off your list".
    op.create_table(
        'suppressions',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        # SET NULL, not CASCADE: deleting a contact must never delete the record that they asked
        # not to be contacted.
        sa.Column('contact_id', sa.BigInteger(), nullable=True),
        _ts('created_at'),
        sa.ForeignKeyConstraint(['contact_id'], ['outreach.contacts.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope', 'value', name='uq_outreach_suppressions_scope_value'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_suppressions_value', 'suppressions', ['value'], schema='outreach')

    # The append-only spine. Every state change writes one row, so a metric can always be traced
    # back to the transitions that produced it. No foreign keys on purpose: an event outlives the
    # entity it describes, and a cascade here would erase the audit trail with the record.
    op.create_table(
        'events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('type', sa.Text(), nullable=False),
        sa.Column('entity_type', sa.Text(), nullable=True),
        sa.Column('entity_id', sa.BigInteger(), nullable=True),
        sa.Column('campaign_id', sa.BigInteger(), nullable=True),
        sa.Column('contact_id', sa.BigInteger(), nullable=True),
        sa.Column('payload', sa.Text(), nullable=True),
        _ts('created_at'),
        sa.PrimaryKeyConstraint('id'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_events_type', 'events', ['type', 'created_at'], schema='outreach')
    op.create_index(
        'ix_outreach_events_entity', 'events', ['entity_type', 'entity_id'], schema='outreach')
    op.create_index(
        'ix_outreach_events_created', 'events', ['created_at'], schema='outreach')
    op.create_index(
        'ix_outreach_events_campaign', 'events', ['campaign_id', 'created_at'], schema='outreach')

    op.create_table(
        'settings',
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        _ts('updated_at'),
        sa.PrimaryKeyConstraint('key'),
        schema='outreach',
    )

    # The columns holding Gmail credentials are nullable and stay TEXT, but what goes in them
    # changes: the port envelope-encrypts access_token and refresh_token with Cloud KMS before
    # writing. A refresh token is long-lived mailbox access, and this table is the reason a
    # database dump would otherwise be a mailbox compromise.
    op.create_table(
        'oauth_tokens',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('account_email', sa.Text(), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=True),
        sa.Column('refresh_token', sa.Text(), nullable=True),
        sa.Column('scope', sa.Text(), nullable=True),
        sa.Column('token_type', sa.Text(), nullable=True),
        sa.Column('expiry_date', sa.BigInteger(), nullable=True),
        # Gmail's incremental sync cursor, so a restart resumes rather than re-reading the mailbox.
        sa.Column('history_id', sa.Text(), nullable=True),
        _ts('created_at'),
        _ts('updated_at'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_email', name='uq_outreach_oauth_tokens_account'),
        schema='outreach',
    )

    # Per-day send accounting in its own table rather than counted from messages: it keeps the cap
    # check O(1) and makes the cap auditable after the fact.
    op.create_table(
        'send_ledger',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        # 'YYYY-MM-DD' in the CAMPAIGN's timezone, not UTC - which is why it is not a date.
        sa.Column('day', sa.Text(), nullable=False),
        sa.Column('campaign_id', sa.BigInteger(), nullable=False),
        sa.Column('domain', sa.Text(), nullable=False),
        sa.Column('count', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['outreach.campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('day', 'campaign_id', 'domain', name='uq_outreach_ledger_day_campaign_domain'),
        schema='outreach',
    )
    op.create_index(
        'ix_outreach_ledger_day', 'send_ledger', ['day', 'campaign_id'], schema='outreach')

    # The newest verification per contact. Carried across because application code selects from it
    # by name; a missing view is a runtime error in a query, not a failure the migration reports.
    op.execute(
        """
        CREATE VIEW outreach.latest_verification AS
        SELECT v.*
        FROM outreach.verifications v
        WHERE v.id = (
          SELECT v2.id FROM outreach.verifications v2
          WHERE v2.contact_id = v.contact_id
          ORDER BY v2.checked_at DESC, v2.id DESC
          LIMIT 1
        )
        """
    )


def downgrade():
    # The schema owns every table, index and view this revision created, so dropping it is exact -
    # and cannot drift out of step with the upgrade the way a hand-maintained drop list does.
    # CASCADE is safe here precisely BECAUSE the schema is exclusive: nothing outside it may
    # reference these tables, which is the same boundary the console's database role is granted on.
    op.execute('DROP SCHEMA IF EXISTS outreach CASCADE')
