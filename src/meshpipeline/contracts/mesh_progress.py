# Responsibility: Carry what a running mesher reports about itself ("adding wall layers") from the
# thread that waits on it to the run's event stream, without the executor knowing the stream.
# Boundaries: a context-local relay. The executor adapter calls `report` while it waits; the code
# that starts the mesh installs where reports go (`relaying`). Nothing is published when nothing
# is installed, and a report that cannot be published is dropped - it is a courtesy, never a
# reason to fail or slow a mesh.
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

StageSink = Callable[[dict], None]
_SINK: ContextVar[StageSink | None] = ContextVar("mesh_progress_sink", default=None)


@contextmanager
def relaying(sink: StageSink | None) -> Iterator[None]:
    """Send the mesher's stage reports made in this context (and the threads started from it with
    its context, as asyncio.to_thread does) to `sink`."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def report(stage: dict | None) -> None:
    """The mesher says it is at `stage` ({"key", "label"}). Never raises."""
    sink = _SINK.get()
    if sink is None or not isinstance(stage, dict) or not stage.get("label"):
        return
    try:
        sink(stage)
    except Exception as exc:  # noqa: BLE001 - the live line is a courtesy
        logger.debug("mesh progress: a stage report was dropped (%s)", type(exc).__name__)


def publishing_sink(publish: ExecutionEventPublisher, loop, *, op_prefix: str) -> StageSink:
    """A sink that publishes each NEW stage on the run's stream as a `mesh_stage` event, from
    whatever thread reports it, on the event loop `loop` the publisher belongs to. Repeats of the
    stage already said are dropped; each new one gets its own identity, so a replay dedupes."""
    import asyncio

    said: dict[str, str | None] = {"key": None}
    count = [0]

    async def _publish_stage(key: str, label: str, op_id: str) -> None:
        # awaited on the run's own loop, through the ownership-checked publisher like every
        # other execution event: a lost claim drops the line, never the mesh
        await publish.amesh_stage(key, label, op_id=op_id)

    def _sink(stage: dict) -> None:
        key = str(stage.get("key") or "")
        if key == said["key"]:
            return
        said["key"] = key
        count[0] += 1
        fut = asyncio.run_coroutine_threadsafe(
            _publish_stage(key, str(stage.get("label") or ""), f"{op_prefix}:{count[0]}"), loop)
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    return _sink


def stage_op_prefix(workspace, n: int) -> str:
    """The identity a mesh run's stage reports share: the attempt's workspace (generation_N/
    attempt_N) and which mesh run of it this is - so a later attempt's stages are never taken
    for repeats of an earlier one's."""
    from pathlib import Path

    parts = Path(str(workspace)).parts[-2:]
    return "mesh_stage:" + "/".join(parts) + f":{int(n)}"


__all__ = ["StageSink", "publishing_sink", "relaying", "report", "stage_op_prefix"]
