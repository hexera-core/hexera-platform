# Responsibility: Publish terminal events that were written durably but not yet announced.
# Boundaries: the transactional-outbox drain.
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from meshpipeline.contracts.event_stream import publisher as _publisher
from meshpipeline.persistence.repositories.terminal_outbox_repository import TerminalOutboxRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishStats:
    published: int = 0
    failed: int = 0
    scanned: int = 0


async def publish_pending(session_factory, *, job_id: uuid.UUID | None = None, limit: int = 50,
                          repo: TerminalOutboxRepository | None = None) -> PublishStats:
    repo = repo or TerminalOutboxRepository()
    published = failed = 0
    async with session_factory() as db:
        rows = await repo.claim_pending(db, limit=limit, job_id=job_id)
        for row in rows:
            closing = str((row.event_payload or {}).get("closing_message") or "")
            try:
                # STRICT emit: raises on transport failure so we only mark_published on real delivery.
                _publisher(str(row.job_id)).publish_terminal(closing, row.dedup_key)
                await repo.mark_published(db, row.id)
                published += 1
            except Exception as exc:  # noqa: BLE001 - a transient transport failure must not lose the row
                await repo.record_failure(db, row.id, type(exc).__name__)
                failed += 1
                logger.warning("outbox: terminal publish failed job_id=%s attempt=%d - %s (row stays "
                               "pending for the next sweep)", row.job_id, row.publish_attempts,
                               type(exc).__name__)
        await db.commit()
    return PublishStats(published=published, failed=failed, scanned=len(rows))


async def deliver_own_terminal_event(session_factory, job_id: uuid.UUID | str,
                                     *, repo: TerminalOutboxRepository | None = None) -> PublishStats:
    try:
        return await publish_pending(session_factory, job_id=uuid.UUID(str(job_id)), limit=4, repo=repo)
    except Exception as exc:  # noqa: BLE001 - the terminal truth is already durable; delivery is retried
        logger.warning("outbox: fast-path delivery failed job_id=%s - %s; recovery sweep will retry",
                       job_id, type(exc).__name__)
        return PublishStats()


__all__ = ["publish_pending", "deliver_own_terminal_event", "PublishStats"]
