# Responsibility: Record how much of each debit fell beyond what the tenant had already paid for.
# Boundaries: one column, one constraint and the sweep's index; WHEN a debit is overage is decided by
#             application/metering_service.py at the moment it is written. This revision decides no price.

# WHY THE METER NEEDS ITS OWN NUMBER.
#
# 0007 reported every debit to the provider's metered price. But a subscriber's period allowance is
# granted into this same ledger, and the jobs it pays for are debits too - so every credit the flat
# fee already bought was ALSO billed a second time as overage. The metered price is for consumption
# above the allowance, and only the ledger knows where that line fell.
#
# It is decided when the debit is written, inside the job's terminal transaction, against the
# balance at that moment: the part the balance covered is not overage, the rest is. Deciding it
# later, in the sweep, would read a balance that has since moved (the next period's grant, the next
# job) and bill a different number for the same run depending on when the sweep happened to run.
#
# ZERO IS THE ORDINARY VALUE. A grant, a refund, a debit the balance covered, and every debit of a
# tenant with no subscription carry 0 and are never reported. That is also why the sweep's partial
# index now keys on `overage > 0` rather than on every unmetered debit: 0007's predicate would have
# kept every covered debit in the backlog forever, since nothing stamps a row with nothing to report.
#
# EXISTING ROWS become 0. No deployment has had billing configured, so no existing debit was owed to
# a meter - and backfilling a guess would put the one number a customer checks on an invoice.

# Revision ID: 0008_overage_metering
# Revises: 0007_usage_metering
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_overage_metering"
down_revision = "0007_usage_metering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credit_ledger",
                  sa.Column("overage", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_credit_ledger_overage_nonnegative", "credit_ledger",
                               "overage >= 0")
    op.drop_index("ix_credit_ledger_unmetered", table_name="credit_ledger")
    op.create_index("ix_credit_ledger_unmetered", "credit_ledger", ["created_at"],
                    postgresql_where=sa.text("metered_at IS NULL AND overage > 0"))


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_unmetered", table_name="credit_ledger")
    op.create_index("ix_credit_ledger_unmetered", "credit_ledger", ["created_at"],
                    postgresql_where=sa.text("metered_at IS NULL AND entry_type = 'debit'"))
    op.drop_constraint("ck_credit_ledger_overage_nonnegative", "credit_ledger", type_="check")
    op.drop_column("credit_ledger", "overage")
