# Responsibility: Express the one tenant predicate every scoped read uses, and the one stamp every scoped write applies.
# Owns: the rule that reads scope on the organisation and fall back to the owner when there is none.
# Boundaries: the predicate and the stamp; which table they are applied to is the repository's question.
from __future__ import annotations

import uuid


def parsed_organization_id(organization_id: str) -> uuid.UUID | None:
    """The organisation id as a UUID, or None for absent-or-malformed.

    PUBLIC because it is the one place this narrowing lives, and three callers had written it out
    for themselves - here, `account_service`, and inline in `api_keys.create_key` - which is three
    chances for one of them to decide a malformed id is an exception after all.

    FAIL OPEN TO THE OWNER FALLBACK, never to an exception inside a request. Every caller today
    hands this a value a Principal produced, which is always either "" or a real UUID string -
    but a future caller (a new credential path, an admin tool, a test helper) that passes
    something unsanitised must not get an unhandled 500. A malformed id narrows to owner-scoping
    rather than widening access, which is the same direction _organization_for already fails in
    (api/security.py).
    """
    if not organization_id:
        return None
    try:
        return uuid.UUID(organization_id)
    except (ValueError, AttributeError, TypeError):
        return None


def scope(model, *, owner_id: str, organization_id: str = ""):
    """The WHERE clause a tenant-scoped read filters on.

    THE RULE: reads scope on organization_id. A principal that names no organisation - one whose
    lookup failed, or a deployment between 0004 and the image that fills the column - falls back
    to owner_id rather than matching nothing. Matching nothing would read as data loss to the
    person whose rows they are, and this fallback is exactly today's behaviour, so the degraded
    path is one we already ship. A MALFORMED organisation id takes the same fallback: it narrows
    to owner-scoping rather than raising inside a request.
    """
    parsed = parsed_organization_id(organization_id)
    if parsed is not None:
        return model.organization_id == parsed
    return model.owner_id == owner_id


def stamp(*, owner_id: str, organization_id: str = "") -> dict:
    """The columns a tenant-scoped write sets.

    BOTH, always, when the organisation is real. owner_id stays the actor - who did this - and
    organization_id becomes the tenant. Dropping owner_id would lose per-seat attribution the
    moment organisations hold more than one person, which memberships already allows. A malformed
    organisation id is treated exactly like an absent one: the write stamps owner_id alone rather
    than raising.
    """
    values: dict = {"owner_id": owner_id}
    parsed = parsed_organization_id(organization_id)
    if parsed is not None:
        values["organization_id"] = parsed
    return values
