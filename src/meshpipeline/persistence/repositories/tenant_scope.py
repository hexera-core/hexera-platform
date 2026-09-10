# Responsibility: Express the one tenant predicate every scoped read uses, and the one stamp every scoped write applies.
# Owns: the rule that reads scope on the organisation and fall back to the owner when there is none.
# Boundaries: the predicate and the stamp; which table they are applied to is the repository's question.
from __future__ import annotations

import uuid


def scope(model, *, owner_id: str, organization_id: str = ""):
    """The WHERE clause a tenant-scoped read filters on.

    THE RULE: reads scope on organization_id. A principal that names no organisation - one whose
    lookup failed, or a deployment between 0004 and the image that fills the column - falls back
    to owner_id rather than matching nothing. Matching nothing would read as data loss to the
    person whose rows they are, and this fallback is exactly today's behaviour, so the degraded
    path is one we already ship.
    """
    if organization_id:
        return model.organization_id == uuid.UUID(organization_id)
    return model.owner_id == owner_id


def stamp(*, owner_id: str, organization_id: str = "") -> dict:
    """The columns a tenant-scoped write sets.

    BOTH, always. owner_id stays the actor - who did this - and organization_id becomes the
    tenant. Dropping owner_id would lose per-seat attribution the moment organisations hold more
    than one person, which memberships already allows.
    """
    values: dict = {"owner_id": owner_id}
    if organization_id:
        values["organization_id"] = uuid.UUID(organization_id)
    return values
