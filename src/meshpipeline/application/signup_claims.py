# Responsibility: Decide whether an address may still receive the one-time signup grant, and claim it.
# Owns: address normalisation for abuse purposes, the claim digest, and the withheld-domain rule.
# Boundaries: it never grants credits itself (credit_service does) and never refuses an account -
#             a withheld grant still leaves a working account that can buy a plan.
from __future__ import annotations

import hashlib

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.persistence.models import SignupGrantClaim

#: Providers that ignore dots in the local part, so `j.doe` and `jdoe` are one mailbox.
_DOT_INSENSITIVE = frozenset({"gmail.com", "googlemail.com"})

#: A fixed context string, so this digest can never be confused with one computed elsewhere.
_DIGEST_CONTEXT = b"hexera-signup-grant-claim:v1:"


def normalized_mailbox(email: str) -> str:
    """The mailbox an address delivers to, as far as a grant is concerned.

    A `+tag` suffix is dropped for every domain, and dots are dropped where the provider ignores
    them, because both mint unlimited addresses for one inbox - which is exactly the shape a
    script farming signup credits takes. This is used ONLY to decide the grant; the account keeps
    the address the person typed.
    """
    address = (email or "").strip().lower()
    local, sep, domain = address.rpartition("@")
    if not sep or not local:
        return address
    local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def claim_digest(email: str) -> str:
    # A DIGEST AND NOT THE ADDRESS because the claim must outlive account erasure: forgetting it
    # would make "delete my account, sign up again" an unlimited credit tap. A digest is
    # pseudonymous rather than anonymous - anyone holding a candidate address can test it - so it is
    # recorded in docs/compliance/records-of-processing.md as fraud-prevention data kept under
    # legitimate interest, and nothing else in the schema is keyed by it.
    return hashlib.sha256(_DIGEST_CONTEXT + normalized_mailbox(email).encode("utf-8")).hexdigest()


def grant_withheld_domain(email: str) -> bool:
    """Whether the address belongs to a domain that never receives the free grant.

    Disposable-inbox providers are the obvious members. The account is still provisioned; only the
    free credits are withheld, so a real person on such a domain can still buy a plan.
    """
    _, sep, domain = (email or "").strip().lower().rpartition("@")
    if not sep or not domain:
        return True
    blocked = {d.strip().lower() for d in polcfg.SIGNUP_GRANT_WITHHELD_DOMAINS.split(",")
               if d.strip()}
    return any(domain == d or domain.endswith("." + d) for d in blocked)


async def claim(db: AsyncSession, email: str) -> bool:
    """Record that this mailbox has received its grant. False when it already had one.

    INSERT ... ON CONFLICT DO NOTHING in the grant's own transaction: two concurrent signups for
    the same mailbox cannot both see "unclaimed", because the second insert waits on the first's
    row and then finds the conflict.
    """
    stmt = (insert(SignupGrantClaim)
            .values(email_digest=claim_digest(email))
            .on_conflict_do_nothing(index_elements=["email_digest"])
            .returning(SignupGrantClaim.email_digest))
    return (await db.execute(stmt)).scalar_one_or_none() is not None
