# Responsibility: Carry CRS v5's plain `repair_progress` events from the repair's thread onto the job's live stream.
# Owns: `ProgressRelay`.
# Boundaries: it says nothing of its own: every line is one `repair_v5.result.ProgressMapper` already made plain, and
#             it passes the narration leak guard again on the way out (`contracts/narration.astep`). The stream is a
#             courtesy: a relay that cannot publish drops the line, never the run.
# Collaborates with: pipeline/repair_attempt.py (starts and stops it), pipeline/repair_words.py (the lane's agent).
"""The repair lane's live progress lines (CRS v5 beta)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: the narration kind each progress stage shows as (contracts/narration.KINDS)
STAGE_KIND = {"checking": "check", "repairing": "refine", "broader_repair": "refine", "verifying": "check",
              "done": "check"}
#: the op id the lines carry, so a reader of the stream can pick out the repair's progress
OP_ID = "repair:progress"


class ProgressRelay:
    """`push` is called from the repair's worker thread; `run` (a task on the node's loop) publishes each event in
    order as a narration step row: text, `n` = problem index, `of` = problem total. Each event is also kept
    (`events`) so the run's record can carry them."""

    def __init__(self, job_id: str, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.job_id = job_id
        self.loop = loop or asyncio.get_running_loop()
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.events: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None

    def push(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(RuntimeError):     # the loop has closed: the line is dropped
            self.loop.call_soon_threadsafe(self.queue.put_nowait, dict(event))

    def start(self) -> ProgressRelay:
        self._task = self.loop.create_task(self._run())
        return self

    async def stop(self) -> None:
        if self._task is None:
            return
        self.queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, timeout=10)

    async def _run(self) -> None:
        from meshpipeline.contracts.event_stream import StaleExecutionPublish
        from meshpipeline.contracts.narration import astep
        from meshpipeline.pipeline.repair_words import AGENT

        publisher: Any = None
        while True:
            event = await self.queue.get()
            if event is None:
                return
            self.events.append(event)
            try:
                if publisher is None:
                    from meshpipeline.application.execution_publisher import execution_publisher
                    publisher = execution_publisher(self.job_id, agent=AGENT)
                await astep(publisher, STAGE_KIND.get(str(event.get("stage")), "check"), str(event.get("text") or ""),
                            n=event.get("problem_index"), of=event.get("problem_total"), op_id=OP_ID)
            except StaleExecutionPublish:
                return          # a superseded generation stops publishing; the repair itself is not this task's
            except Exception:  # noqa: BLE001 - the user stream is never load-bearing
                logger.debug("repair progress line not published", exc_info=True)


__all__ = ["OP_ID", "STAGE_KIND", "ProgressRelay"]
