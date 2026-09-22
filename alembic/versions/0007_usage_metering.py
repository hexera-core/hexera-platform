# Responsibility: Record which ledger debits have been reported to the billing provider's meter.
# Boundaries: one column and one index; what a job COSTS is settings/policy.py, and when a debit is
#             reported is application/metering_service.py. This revision decides no price.

# WHY A DEBIT NEEDS A SECOND TIMESTAMP.
#
# A job's charge is written to `credit_ledger` inside the SAME fenced transaction that marks the job
# terminal, which is what makes it exactly-once: the transition CAS has already decided there is one
# winner, so the debit rides that decision rather than needing its own.
#
# Reporting that consumption to the billing provider's meter cannot ride it. That is an HTTP call,
# and making it inside the fenced transaction would hold a row lock on a running job across a remote
# round trip - so a provider having a slow afternoon would stall finalisation for every mesh in the
# fleet. It has to happen after the commit, from something that can be retried.
#
# `metered_at` is what makes that retry safe. A sweep claims unreported debits, reports them, and
# stamps them; a sweep that dies half way leaves the unstamped rows to the next run. Without the
# column the only alternatives are reporting inside the lock, or re-reporting the whole period and
# double-charging - the meter aggregates, so a second report of the same job ADDS to the bill.
#
# NULL IS THE ORDINARY STATE for a row that was just written, and the permanent state for every
# grant and refund: only a debit is ever metered. That is why this is nullable rather than defaulted.

# Revision ID: 0007_usage_metering
# Revises: 0006_stripe_billing
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_usage_metering"
down_revision = "0006_stripe_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("credit_ledger",
                  sa.Column("metered_at", sa.DateTime(timezone=True), nullable=True))
    # A PARTIAL INDEX, not a plain one. The sweep's only question is "which debits are not reported
    # yet", and that set is a vanishing fraction of a ledger that grows forever - every historical
    # row is stamped. Indexing the whole column would index mostly answers nobody asks for, and the
    # index would grow with the table instead of with the backlog.
    op.create_index("ix_credit_ledger_unmetered", "credit_ledger", ["created_at"],
                    postgresql_where=sa.text("metered_at IS NULL AND entry_type = 'debit'"))


def downgrade() -> None:
    op.drop_index("ix_credit_ledger_unmetered", table_name="credit_ledger")
    op.drop_column("credit_ledger", "metered_at")
