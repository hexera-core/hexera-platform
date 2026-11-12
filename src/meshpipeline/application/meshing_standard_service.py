# Responsibility: An organisation's meshing standard as its members see and set it (the console's Meshing settings) and as an approved run is dispatched with it: the words, what the reader understood, who saved it and when.
# Owns: the membership check (a caller sees and sets ONLY the organisation they belong to), the reading at save time (asked once, kept with the words, reused while the words and the question stay the same), and the dispatch snapshot.
# Boundaries: the words are read by engines/meshing_standard (the notes' own reader); this module decides nothing about a mesh. A model that is down at save time never refuses the save: the words are kept unread and each run reads them.
# Collaborates with: api/v1/organization.py (GET and PUT /organization/meshing-standard), agents/intake/approval.py (for_dispatch), persistence/repositories/meshing_standard_repository.py, engines/meshing_standard.py.
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.engines import meshing_standard as MS
from meshpipeline.persistence.repositories import tenant_scope
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.meshing_standard_repository import MeshingStandardRepository

membership_repo = MembershipRepository()
standard_repo = MeshingStandardRepository()

#: the most characters a standard may hold (every one of them is read)
MAX_CHARS = MS.MAX_CHARS


class TooLong(ValueError):
    """The words are longer than the reader reads; the message is safe to show."""


async def _own(db: AsyncSession, *, owner_id: str, organization_id: str) -> uuid.UUID | None:
    """The organisation id when the caller belongs to it, else None. THE ISOLATION RULE: the id reaches here from
    the caller's own principal, and it is checked against the caller's membership all the same - a standard is one
    organisation's words, and another's must never be read or written through a foreign id (account_service's
    organisation view takes the same check for the same reason)."""
    parsed = tenant_scope.parsed_organization_id(organization_id)
    if parsed is None or not owner_id:
        return None
    own = await membership_repo.organization_id_for_email(db, owner_id)
    return parsed if own == parsed else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def view_of(text: str, reading: dict | None, *, updated_by: str = "", updated_at: Any = None,
            shell_elements: str = MS.QUADS) -> dict:
    """What the settings page shows: the words, one plain line per thing understood (as a run says it), and
    whether they were read ("read"), saved unread ("unread": each run reads them) or empty; and the page's one
    switch, a shell's elements (SHSTAGE-8: "quads" | "triangles")."""
    t = str(text or "")
    read = isinstance(reading, dict) and isinstance(reading.get("arguments"), str)
    return {"text": t, "lines": MS.understood(t, reading) if read else [],
            "status": "empty" if not t.strip() else ("read" if read else "unread"),
            "shell_elements": MS.pick_of(shell_elements) or MS.QUADS,
            "updated_by": updated_by or None, "updated_at": _iso(updated_at)}


EMPTY = view_of("", None)


async def view(db: AsyncSession, *, owner_id: str, organization_id: str) -> dict | None:
    """The caller's organisation's standard, or None when the caller belongs to no organisation it can name."""
    org = await _own(db, owner_id=owner_id, organization_id=organization_id)
    if org is None:
        return None
    row = await standard_repo.get_for_organization(db, org)
    if row is None:
        return dict(EMPTY)
    return view_of(row.text, row.reading, updated_by=row.updated_by, updated_at=row.updated_at,
                   shell_elements=row.shell_elements)


Reader = Callable[..., Awaitable[dict | None]]


async def save(*, owner_id: str, organization_id: str, text: str, sessions,
               read: Reader | None = None, shell_elements: str = MS.QUADS) -> dict | None:
    """Save the caller's organisation's standard and read it once (STD-1): the reading is kept with the words and
    every run reuses it while the words and the question stay the same. Empty words and Quads (the switch's
    default) clear the standard. `shell_elements`: the page's switch (SHSTAGE-8), "quads" | "triangles". None when
    the caller belongs to no organisation it can name. The model is asked OUTSIDE any transaction (it can take a
    minute), and its answer never decides whether the words are saved."""
    kind = MS.pick_of(shell_elements) or MS.QUADS
    t = str(text or "").replace("\r\n", "\n").strip()
    if len(t) > MAX_CHARS:
        raise TooLong(f"The standard can be at most {MAX_CHARS:,} characters.")
    async with sessions() as db:
        org = await _own(db, owner_id=owner_id, organization_id=organization_id)
        if org is None:
            return None
        if not t and kind == MS.QUADS:
            await standard_repo.clear(db, organization_id=org)
            await db.commit()
            return dict(EMPTY)
        row = await standard_repo.get_for_organization(db, org)
        same = row is not None and row.text == t and MS.kept_reading({"text": t, "reading": row.reading}) is not None
        kept = dict(row.reading) if same and row is not None and row.reading else None
    reading = kept if kept is not None else (await (read or MS.read_at_save)(t, user_id=owner_id) if t else None)
    async with sessions() as db:
        saved = await standard_repo.put(db, organization_id=org, text=t, reading=reading, updated_by=owner_id,
                                        shell_elements=kind)
        await db.commit()
        return view_of(saved.text, saved.reading, updated_by=saved.updated_by, updated_at=saved.updated_at,
                       shell_elements=saved.shell_elements)


async def for_dispatch(db: AsyncSession, *, organization_id: str) -> dict | None:
    """The standard an approved run is dispatched with (engines/meshing_standard.snapshot), or None when the
    organisation has none. The organisation id is the approving caller's own (api/security's principal)."""
    org = tenant_scope.parsed_organization_id(organization_id)
    if org is None:
        return None
    row = await standard_repo.get_for_organization(db, org)
    if row is None:
        return None
    return MS.snapshot(row.text, row.reading, updated_by=row.updated_by, updated_at=_iso(row.updated_at) or "",
                       shell_elements=row.shell_elements)


__all__ = ["EMPTY", "MAX_CHARS", "TooLong", "for_dispatch", "save", "view", "view_of"]
