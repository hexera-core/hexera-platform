# Responsibility: The organisation's repair settings: whether its runs MAY auto-accept repairs and
#                 skip the review screen (CRS v5 beta, "Bypass permission"), and who may change it.
# Owns: the read for producers (geometry-check confirm, the approval that builds the dispatch
#       payload) and the owner-only write for the console's settings.
# Boundaries: one column on `organizations` (0023). It decides nothing about any run: the per-run
#             choice is the customer's, and this only says whether that choice is allowed.
# Collaborates with: api/v1/organization.py (the settings route), api/v1/geometry.py (the confirm
#                    that refuses a bypass the organisation does not allow), agents/intake/
#                    approval.py (which carries the bit onto the dispatch payload only when allowed),
#                    pipeline/repair_goal.py (the bit's shape on the payload).
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.repositories import tenant_scope

logger = logging.getLogger(__name__)

#: The only membership role that may change the setting: "admins" in the spec's words.
ADMIN_ROLE = "owner"


class NotAllowed(Exception):
    """The caller may not change the organisation's repair settings (not its owner)."""


async def bypass_allowed(db: AsyncSession, *, organization_id: str) -> bool:
    """Whether runs of this organisation may auto-accept repairs. FAILS CLOSED: no organisation,
    an unreadable one, or an old schema all answer False (review required)."""
    parsed = tenant_scope.parsed_organization_id(organization_id)
    if parsed is None:
        return False
    try:
        from meshpipeline.persistence.models import Organization
        row = (await db.execute(select(Organization.repair_auto_accept_allowed)
                                .where(Organization.id == parsed))).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - a setting that cannot be read is "not allowed"
        logger.warning("repair settings: unreadable (%s) - review required", type(exc).__name__)
        return False
    return bool(row)


async def view(db: AsyncSession, *, owner_id: str, organization_id: str) -> dict | None:
    """The settings as the console shows them, and whether this caller may change them."""
    from meshpipeline.application import account_service
    got = await account_service.organization_view(db, owner_id=owner_id,
                                                  organization_id=organization_id)
    if got.organization is None:
        return None
    role = next((m.role for m in got.members if m.email.lower() == owner_id.lower()), "")
    return {"auto_accept_repairs": bool(getattr(got.organization,
                                                "repair_auto_accept_allowed", False)),
            "can_change": role == ADMIN_ROLE}


async def set_bypass_allowed(db: AsyncSession, *, owner_id: str, organization_id: str,
                             allowed: bool) -> dict | None:
    """Turn the permission on or off. Owner only (`NotAllowed`); None for no organisation."""
    current = await view(db, owner_id=owner_id, organization_id=organization_id)
    if current is None:
        return None
    if not current["can_change"]:
        raise NotAllowed("only the organisation's owner can change its repair settings")
    from meshpipeline.persistence.models import Organization
    parsed = tenant_scope.parsed_organization_id(organization_id)
    await db.execute(update(Organization).where(Organization.id == parsed)
                     .values(repair_auto_accept_allowed=bool(allowed)))
    await db.commit()
    return {**current, "auto_accept_repairs": bool(allowed)}


__all__ = ["ADMIN_ROLE", "NotAllowed", "bypass_allowed", "set_bypass_allowed", "view"]
