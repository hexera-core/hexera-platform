# Responsibility: Hold what verified-email provisioning, the once-per-address signup grant and account erasure need,
#                 and record a claim for every mailbox the grant has ALREADY been paid to.
# Boundaries: one new table and three nullable columns. The new image REQUIRES them (the models select
#             them), so this is NOT order-safe for the image: deploy runs migrations first, then rolls
#             the image. The previous image is unaffected by the extra columns and table.

# Revision ID: 0019_signup_claims_and_erasure
# Revises: 0018_byte_counts_bigint
from __future__ import annotations

import hashlib

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
    _seed_existing_grants()

    op.add_column("organizations",
                  sa.Column("erasure_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations",
                  sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_organizations_erasure_pending", "organizations", ["erasure_requested_at"],
                    postgresql_where=sa.text("erasure_requested_at IS NOT NULL AND erased_at IS NULL"))


#: COPIES of application/signup_claims.py's normalisation and digest, frozen at this revision.
#: A migration must not import application code (the application moves on; this file may not),
#: and the context string below is fixed forever there for exactly this reason - the two must
#: produce identical digests or a seeded mailbox would be granted again.
_DIGEST_CONTEXT = b"hexera-signup-grant-claim:v1:"
_DOT_INSENSITIVE = {"gmail.com", "googlemail.com"}


def _digest(email: str) -> str:
    address = (email or "").strip().lower()
    local, sep, domain = address.rpartition("@")
    if sep and local:
        local = local.split("+", 1)[0]
        if domain in _DOT_INSENSITIVE:
            local, domain = local.replace(".", ""), "gmail.com"
        address = f"{local}@{domain}"
    return hashlib.sha256(_DIGEST_CONTEXT + address.encode("utf-8")).hexdigest()


def _seed_existing_grants() -> None:
    # EVERY MAILBOX THE GRANT WAS ALREADY PAID TO claims its row here, or "erase the account and
    # sign up again" pays each existing customer a second grant the day this ships. Members of an
    # organisation whose ledger carries a signup grant, by the reason string credit_service has
    # always written. Idempotent: ON CONFLICT DO NOTHING.
    bind = op.get_bind()
    emails = bind.execute(sa.text(
        "SELECT DISTINCT u.email FROM users u "
        "JOIN memberships m ON m.user_id = u.id "
        "JOIN credit_ledger cl ON cl.organization_id = m.organization_id "
        "WHERE cl.entry_type = 'grant' AND cl.reason = 'signup grant'")).scalars().all()
    for email in emails:
        bind.execute(sa.text(
            "INSERT INTO signup_grant_claims (email_digest) VALUES (:d) "
            "ON CONFLICT (email_digest) DO NOTHING"), {"d": _digest(email)})


def downgrade() -> None:
    op.drop_index("ix_organizations_erasure_pending", table_name="organizations")
    op.drop_column("organizations", "erased_at")
    op.drop_column("organizations", "erasure_requested_at")
    op.drop_table("signup_grant_claims")
    op.drop_column("users", "pending_organization_name")
