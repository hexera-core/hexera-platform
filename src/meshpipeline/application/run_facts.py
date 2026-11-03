# Responsibility: Read what a finished run delivered - its mesh's size, the engine that built it, what it was for - for the chat.
# Boundaries: reads the job's durable result and its viewer payload; best effort, never raises, decides nothing.
# Collaborates with: agents/intake/message.py (the run a conversation already made), api/v1/chat.py (carries it
#                    to the intake turn), application/viewer_payload.py (where the delivered size is stated).
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def of_final_result(stored: Mapping | None) -> dict[str, str]:
    """The engine that built the delivered mesh and what it was for, from the job's final result."""
    fr = dict(stored or {})
    ladder = fr.get("engine_ladder") if isinstance(fr.get("engine_ladder"), Mapping) else {}
    engine = str((ladder or {}).get("delivered_by") or fr.get("engine") or "")
    if engine:
        from meshpipeline.engines.registry import engine_label
        engine = engine_label(engine)
    return {"engine": engine, "purpose": str(fr.get("purpose") or "")}


def delivered_size(viewer: Mapping | None) -> int | None:
    """The delivered mesh's cell (or element) count as the viewer payload states it."""
    q = (viewer or {}).get("quality") if isinstance(viewer, Mapping) else None
    try:
        n = int((q or {}).get("cell_count") or 0)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


async def read_delivered_size(job_id: Any) -> int | None:
    """The size of the mesh a run delivered, read from its viewer payload; None when the run left
    none or it cannot be read. Never raises: a chat answer without it says the size is not known."""
    try:
        from meshpipeline.application.viewer_payload import VIEWER_LOGICAL_KEY
        from meshpipeline.contracts.object_storage import get_object_store
        from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository
        from meshpipeline.persistence.session import get_db

        async with get_db() as db:
            row = await ArtifactRepository().get_by_logical_key(db, uuid.UUID(str(job_id)),
                                                                VIEWER_LOGICAL_KEY)
        if row is None:
            return None
        raw = await asyncio.to_thread(lambda: get_object_store().get_bytes(object_key=row.storage_key))
        return delivered_size(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 - context for the chat, never a gate
        logger.info("run facts: the delivered size of %s is unavailable (%s)", job_id, type(exc).__name__)
        return None


__all__ = ["delivered_size", "of_final_result", "read_delivered_size"]
