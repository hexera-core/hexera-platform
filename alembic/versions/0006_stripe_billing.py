# Responsibility: Give an organisation a Stripe identity and a plan, and give webhook delivery a memory.
# Boundaries: columns and one table; what a plan ALLOWS is settings/plans.py, and what a webhook
#             MEANS is application/billing_service.py. This revision decides no policy.

# THE BILLING COLUMNS. The 2026-09-07 design left the charging model out on purpose and named the
# seams it was leaving behind: `PLANS` empty in settings/plans.py, `CreditEntryType.debit` declared
# and unwritten, `organizations` established as the tenant key. This revision fills the one seam
# that could not be left to configuration - a Stripe customer is a durable remote identity, and
# durable remote identities live in columns.
#
# WHY THE CUSTOMER ID IS ON `organizations` AND NOT ON `users`. The organisation is what this schema
# scopes on - every tenant-scoped read filters on `organization_id`, and the credit ledger is keyed
# by it. Billing the user would mean a second tenant boundary that disagrees with the first the
# moment an organisation has two members, which memberships already permit.
#
# WHY `plan` IS A PLAIN STRING AND NOT AN ENUM. settings/plans.py resolves an unknown plan name to
# the deployment's defaults rather than refusing, and its comment says why: a row naming a plan the
# running revision does not declare is a rollback or a half-finished product change, not a reason to
# stop serving a paying caller. A Postgres enum would turn that survivable disagreement into a write
# that fails, and it would make adding a tier a migration instead of a dict entry.
#
# WHY `stripe_events` EXISTS. Stripe delivers at least once and retries on any non-2xx, so the same
# event arrives more than once as a matter of routine - not as a fault. Every handler this unlocks
# is a write: granting credits, moving a plan. Replaying a grant mints credits that were never
# bought. Recording the event id inside the SAME transaction as the effect makes the handler
# idempotent by construction rather than by each handler remembering to check.

# Revision ID: 0006_stripe_billing
# Revises: 0005_outreach_schema
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_stripe_billing"
down_revision = "0005_outreach_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULLABLE, like organization_id before it and for the same reason the design gave: deploy.sh
    # migrates (stage 220) before the new image serves (stage 245), so the OLD revision runs briefly
    # against this schema. A NOT NULL with no default would fail that revision's inserts.
    op.add_column("organizations",
                  sa.Column("stripe_customer_id", sa.String(255), nullable=True))
    # UNIQUE because the customer is the remote identity: two organisations pointing at one Stripe
    # customer would have their invoices and their credits cross. The index the constraint builds is
    # also the lookup the webhook path uses - an event names a customer, never an organisation.
    op.create_index("ix_organizations_stripe_customer", "organizations",
                    ["stripe_customer_id"], unique=True)

    # THE SUBSCRIPTION, recorded so the plan can be reconciled against Stripe without a round trip
    # on every request. It is nullable for the whole life of a free organisation, which is most of
    # them: no subscription is the ordinary state, not a missing value.
    op.add_column("organizations",
                  sa.Column("stripe_subscription_id", sa.String(255), nullable=True))
    # THE PLAN NAME, lowercase, matching a key in settings.plans.PLANS. Empty means no plan, which
    # resolves to the deployment's configured limits - the behaviour that exists today.
    op.add_column("organizations",
                  sa.Column("plan", sa.String(64), nullable=False, server_default=""))
    # STRIPE'S OWN WORD for the subscription's state (active, past_due, canceled...), stored rather
    # than collapsed to a boolean. `past_due` is not `canceled`: one keeps serving while the card is
    # retried, the other stops, and a boolean cannot tell the support question from the billing one.
    op.add_column("organizations",
                  sa.Column("subscription_status", sa.String(32), nullable=False,
                            server_default=""))
    # WHEN THE PAID PERIOD ENDS. The overage reconciler reads it to know which window it is closing,
    # and it is what the console shows as "renews on".
    op.add_column("organizations",
                  sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True))

    # THE DELIVERY MEMORY. One row per event this deployment has finished handling.
    op.create_table(
        "stripe_events",
        # STRIPE'S EVENT ID IS THE PRIMARY KEY, not a surrogate. The uniqueness that matters is the
        # remote one, and making it the key means a duplicate delivery collides on insert inside the
        # handler's own transaction - the check cannot be forgotten or raced.
        sa.Column("id", sa.String(255), primary_key=True),
        # WHAT IT WAS, kept for operators reading this table during an incident. Nothing branches on
        # it: the row's existence is the whole contract.
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stripe_events")
    op.drop_column("organizations", "current_period_end")
    op.drop_column("organizations", "subscription_status")
    op.drop_column("organizations", "plan")
    op.drop_column("organizations", "stripe_subscription_id")
    op.drop_index("ix_organizations_stripe_customer", table_name="organizations")
    op.drop_column("organizations", "stripe_customer_id")
