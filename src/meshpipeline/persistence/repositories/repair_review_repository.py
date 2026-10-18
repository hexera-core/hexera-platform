# Responsibility: Read and write the parks and the unsure-defect items of the repair review.
# Owns: park lifecycle (open once, closed once), item minting, and the decision writes.
# Boundaries: WHO may decide is the API's; this refuses only what the data itself makes unsafe -
#             a decision on a closed park, and a group decision over a paired item.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import cast, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import RepairReviewItem, RepairReviewPark

_ITEM_FIELDS = (
    "group_key", "defect_code", "severity", "entity_type", "entity_index", "fingerprint",
    "measurements", "message", "origin", "pairing_key", "proposal", "camera", "decision_history",
)
_DECIDED = frozenset({"accepted", "left", "redirected", "unreviewed"})


class ParkClosedError(Exception):
    """A decision was offered on an item whose park has already closed."""


class RepairReviewRepository:

    async def create_park(self, db: AsyncSession, job_id: uuid.UUID, kept_operations: list,
                          source_sha256: str, geometry_session_id: str) -> RepairReviewPark:
        row = RepairReviewPark(job_id=job_id, status="open",
                               kept_operations=list(kept_operations),
                               source_sha256=source_sha256,
                               geometry_session_id=geometry_session_id)
        db.add(row)
        await db.flush()
        return row

    async def add_items(self, db: AsyncSession, job_id: uuid.UUID, park_id: uuid.UUID | None,
                        items: list[dict]) -> list[RepairReviewItem]:
        rows = []
        for spec in items:
            fields = {k: spec[k] for k in _ITEM_FIELDS if k in spec}
            fields.setdefault("pairing_key", "")
            fields.setdefault("measurements", {})
            fields.setdefault("camera", {})
            rows.append(RepairReviewItem(job_id=job_id, park_id=park_id, state="open", **fields))
        db.add_all(rows)
        await db.flush()
        return rows

    async def open_park(self, db: AsyncSession, job_id: uuid.UUID) -> RepairReviewPark | None:
        res = await db.execute(
            select(RepairReviewPark)
            .where(RepairReviewPark.job_id == job_id, RepairReviewPark.status == "open")
            .order_by(RepairReviewPark.created_at.desc()).limit(1))
        return res.scalars().first()

    async def latest_park(self, db: AsyncSession, job_id: uuid.UUID) -> RepairReviewPark | None:
        res = await db.execute(
            select(RepairReviewPark).where(RepairReviewPark.job_id == job_id)
            .order_by(RepairReviewPark.created_at.desc()).limit(1))
        return res.scalars().first()

    async def items_for(self, db: AsyncSession, job_id: uuid.UUID,
                        park_id: uuid.UUID | None) -> list[RepairReviewItem]:
        stmt = select(RepairReviewItem).where(RepairReviewItem.job_id == job_id)
        stmt = stmt.where(RepairReviewItem.park_id.is_(None) if park_id is None
                          else RepairReviewItem.park_id == park_id)
        res = await db.execute(stmt.order_by(RepairReviewItem.group_key,
                                             RepairReviewItem.entity_index,
                                             RepairReviewItem.id))
        return list(res.scalars().all())

    async def record_decision(self, db: AsyncSession, item_id: uuid.UUID, state: str,
                              decision: dict) -> RepairReviewItem:
        _check_state(state)
        # ONE conditional UPDATE: the park's open status is part of the write's own WHERE, so a
        # close_park committing concurrently cannot land a decision on a closed park.
        open_parks = select(RepairReviewPark.id).where(RepairReviewPark.status == "open")
        res = await db.execute(
            update(RepairReviewItem)
            .where(RepairReviewItem.id == item_id,
                   RepairReviewItem.park_id.is_(None) | RepairReviewItem.park_id.in_(open_parks))
            .values(state=state, decision=dict(decision), decided_at=datetime.now(UTC)))
        item = (await db.execute(
            select(RepairReviewItem).where(RepairReviewItem.id == item_id)
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if item is None:
            raise LookupError(f"no repair review item {item_id}")
        if not res.rowcount:
            raise ParkClosedError(f"park {item.park_id} is closed; item {item_id} is final")
        return item

    async def record_outcome(self, db: AsyncSession, item_id: uuid.UUID, outcome: dict) -> bool:
        """Note what became of a decided item's directive. Allowed under a CLOSED park.

        An outcome is a fact about what happened AFTER the review, not a decision, so it is the
        one write that may land on a closed park. It touches only the item's `decision` JSON
        (merging `outcome` and `outcome_reason` into it), only when a decision exists, and never
        its state.
        """
        patch = {"outcome": str(outcome["outcome"]),
                 "outcome_reason": str(outcome.get("reason", "") or "")}
        res = await db.execute(
            update(RepairReviewItem)
            .where(RepairReviewItem.id == item_id, RepairReviewItem.decision.is_not(None))
            .values(decision=RepairReviewItem.decision.op("||")(cast(patch, JSONB))))
        return bool(res.rowcount)

    async def decided_items(self, db: AsyncSession, job_id: uuid.UUID) -> list[RepairReviewItem]:
        """Every item of the job that was ever decided, oldest decision first."""
        res = await db.execute(
            select(RepairReviewItem)
            .where(RepairReviewItem.job_id == job_id, RepairReviewItem.decision.is_not(None))
            .order_by(RepairReviewItem.decided_at, RepairReviewItem.id)
            # the bulk `||` UPDATE above bypasses the identity map: re-read, never the stale decision
            .execution_options(populate_existing=True))
        return list(res.scalars().all())

    async def record_group_decision(self, db: AsyncSession, job_id: uuid.UUID, group_key: str,
                                    state: str, decision: dict) -> int:
        """Decide every still-open item of a group at once; returns how many changed.

        A PAIRED ITEM (non-empty pairing_key) IS NEVER TOUCHED. Its partner's decision changes its
        meaning, so it is decided singly or not at all. Items under a closed park, and items
        already decided, are left alone too.
        """
        _check_state(state)
        open_parks = select(RepairReviewPark.id).where(RepairReviewPark.status == "open")
        res = await db.execute(
            update(RepairReviewItem)
            .where(RepairReviewItem.job_id == job_id,
                   RepairReviewItem.group_key == group_key,
                   RepairReviewItem.pairing_key == "",
                   RepairReviewItem.state == "open",
                   (RepairReviewItem.park_id.is_(None))
                   | RepairReviewItem.park_id.in_(open_parks))
            .values(state=state, decision=dict(decision), decided_at=datetime.now(UTC)))
        return int(res.rowcount or 0)

    async def close_park(self, db: AsyncSession, park_id: uuid.UUID, closed_by: str) -> None:
        if closed_by not in ("run", "cancel"):
            raise ValueError("closed_by must be 'run' or 'cancel'")
        await db.execute(
            update(RepairReviewPark)
            .where(RepairReviewPark.id == park_id, RepairReviewPark.status.in_(("open",)))   # CAS shape the architecture fitness test requires
            .values(status="closed", closed_by=closed_by, closed_at=datetime.now(UTC)))

    async def reopen_park(self, db: AsyncSession, park_id: uuid.UUID) -> bool:
        """Undo a close: the compensation for a review run whose dispatch failed."""
        res = await db.execute(
            update(RepairReviewPark)
            .where(RepairReviewPark.id == park_id, RepairReviewPark.status.in_(("closed",)))   # CAS shape the architecture fitness test requires
            .values(status="open", closed_by=None, closed_at=None))
        return bool(res.rowcount)

    async def restore_open(self, db: AsyncSession, item_ids: list[uuid.UUID]) -> int:
        """Return `unreviewed` items to `open` (only those, only under an open park)."""
        if not item_ids:
            return 0
        open_parks = select(RepairReviewPark.id).where(RepairReviewPark.status == "open")
        res = await db.execute(
            update(RepairReviewItem)
            .where(RepairReviewItem.id.in_(item_ids), RepairReviewItem.state == "unreviewed",
                   RepairReviewItem.park_id.in_(open_parks))
            .values(state="open", decision=None, decided_at=None))
        return int(res.rowcount or 0)


def _check_state(state: str) -> None:
    if state not in _DECIDED:
        raise ValueError(f"a decision state must be one of {sorted(_DECIDED)}, got {state!r}")
