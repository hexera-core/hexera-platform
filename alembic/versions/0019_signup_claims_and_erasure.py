# Responsibility: Hold what verified-email provisioning, the once-per-address signup grant and account erasure need.
# Boundaries: one new table and three nullable columns. The new image REQUIRES them (the models select
#             them), so this is NOT order-safe for the image: deploy runs migrations first, then rolls
#             the image. The previous image is unaffected by the extra columns and table.

# Revision ID: 0019_signup_claims_and_erasure
# Revises: 0018_byte_counts_bigint
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_signup_claims_and_erasure"
down_revision = "0018_byte_counts_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The organisation name typed at sign-up, held until the address is verified and the
    # organisation is actually created. Cleared once it has been used.
    op.add_column("users", sa.Column("pending_organization_name", sa.String(256), nullable=True))

    # ONE ROW PER NORMALISED ADDRESS THAT HAS EVER RECEIVED THE SIGNUP GRANT. A digest, never the
    # address, so the row survives account erasure without keeping the person's email.
    op.create_table(
        "signup_grant_claims",
        sa.Column("email_digest", sa.String(64), primary_key=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )

    op.add_column("organizations",
                  sa.Column("erasure_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations",
                  sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_organizations_erasure_pending", "organizations", ["erasure_requested_at"],
                    postgresql_where=sa.text("erasure_requested_at IS NOT NULL AND erased_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_organizations_erasure_pending", table_name="organizations")
    op.drop_column("organizations", "erased_at")
    op.drop_column("organizations", "erasure_requested_at")
    op.drop_table("signup_grant_claims")
    op.drop_column("users", "pending_organization_name")
