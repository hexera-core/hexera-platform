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
    if sink is None or not isinstance(stage, dict):
        return
    # A pointer to a stored frame of the mesh-so-far (`partial_key`) is reportable on its own: the
    # beacon can carry one before the mesher has written a stage line this side can label, and a
    # frame nobody is told about is a frame nobody draws.
    if not (stage.get("label") or stage.get("partial_key")):
        return
    try:
        sink(stage)
    except Exception as exc:  # noqa: BLE001 - the live line is a courtesy
        logger.debug("mesh progress: a stage report was dropped (%s)", type(exc).__name__)


def beat() -> None:
    """The mesh is still running and its stage has not moved: say so (a heartbeat). The sink words
    it from the stage it last said and how long that stage has run. Never raises."""
    sink = _SINK.get()
    if sink is None:
        return
    try:
        sink({"beat": True})
    except Exception as exc:  # noqa: BLE001 - the live line is a courtesy
        logger.debug("mesh progress: a heartbeat was dropped (%s)", type(exc).__name__)


def publishing_sink(publish: ExecutionEventPublisher, loop, *, op_prefix: str,
                    of: int | None = None, unit: str = "") -> StageSink:
    """A sink that publishes each NEW stage on the run's stream, from whatever thread reports it, on
    the event loop `loop` the publisher belongs to: as a `mesh_stage` event (the mesher's own
    label) AND as a narration step in the user's words (contracts/narration - never the label,
    which can name a tool). Repeats of the stage already said are dropped; a heartbeat (`beat`)
    re-says the current stage as "Still ... · N min". Each line gets its own identity, so a replay
    dedupes. A count (`n`) that moves while the stage stays is said again, so the number on the
    row moves; `of` and `unit` are the plan's own estimate ("of about 1.9M elements") when the
    mesher does not say one.

    AN ESTIMATE THE MESH HAS ALREADY PASSED IS DROPPED, not stretched: a mesh can and does come out
    bigger than its plan (a thin plate's layer stage can add several times the castellated cells),
    and "2.9M of about 1.9M cells" with a bar pinned at the end is a line an engineer would laugh
    at. Past the estimate the row says only the count it has measured - "2.9M cells so far" - and
    the bar goes with it. The count itself is never clamped: it is a measurement."""
    import asyncio
    import time

    from meshpipeline.contracts import narration as N

    said: dict = {"key": None, "since": 0.0, "n": None, "partial": None}
    count = [0]

    async def _publish_stage(key: str, label: str, op_id: str) -> None:
        # awaited on the run's own loop, through the ownership-checked publisher like every
        # other execution event: a lost claim drops the line, never the mesh
        await publish.amesh_stage(key, label, op_id=op_id)

    async def _narrate(kind: str, words: str, op_id: str, n=None, of=None, unit: str = "") -> None:
        await N.astep(publish, kind, words, n=n, of=of, unit=unit, op_id=op_id)

    async def _publish_partial(phase: str, object_key: str, op_id: str) -> None:
        await publish.amesh_partial(phase, object_key, op_id=op_id)

    def _quiet(fut) -> None:
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _sink(stage: dict) -> None:
        if stage.get("beat"):
            if said["key"] is None:
                return
            count[0] += 1
            kind, words = N.stage_words(said["key"])
            _quiet(asyncio.run_coroutine_threadsafe(
                _narrate(kind, N.still_words(words, time.monotonic() - said["since"]),
                         f"{op_prefix}:beat:{count[0]}"), loop))
            return
        # A FRAME OF THE MESH-SO-FAR, said once per stored object and on its own identity. Read
        # BEFORE the stage dedupe below: the beacon carries the pointer alongside whichever stage
        # the mesher is on, and that stage is usually one already said - which would drop the
        # pointer with it. No geometry travels here; see events.mesh_partial.
        pkey = str(stage.get("partial_key") or "")
        if pkey and pkey != said["partial"]:
            said["partial"] = pkey
            count[0] += 1
            _quiet(asyncio.run_coroutine_threadsafe(
                _publish_partial(str(stage.get("partial_stage") or ""), pkey,
                                 f"{op_prefix}:partial:{count[0]}"), loop))
        if not (stage.get("key") or stage.get("label")):
            return          # a pointer-only beacon says nothing about the stage; do not word one
        key = str(stage.get("key") or "")
        n = _int(stage.get("n"))
        moved = key == said["key"] and n is not None and n != said["n"]
        if key == said["key"] and not moved:
            return
        count[0] += 1
        op = f"{op_prefix}:{count[0]}"
        if not moved:
            said["key"], said["since"] = key, time.monotonic()
            _quiet(asyncio.run_coroutine_threadsafe(_publish_stage(key, str(stage.get("label") or ""), op), loop))
        said["n"] = n
        kind, words = N.stage_words(key)
        _of = _int(stage.get("of")) or (of if n is not None else None)
        if _of is not None and n is not None and n > _of:
            _of = None      # past its estimate: the count stands alone (see the docstring)
        _quiet(asyncio.run_coroutine_threadsafe(
            _narrate(kind, words, f"{op}:say", n=n, of=_of,
                     unit=str(stage.get("unit") or (unit if n is not None else ""))), loop))

    return _sink


def stage_op_prefix(workspace, n: int) -> str:
    """The identity a mesh run's stage reports share: the attempt's workspace (generation_N/
    attempt_N) and which mesh run of it this is - so a later attempt's stages are never taken
    for repeats of an earlier one's."""
    from pathlib import Path

    parts = Path(str(workspace)).parts[-2:]
    return "mesh_stage:" + "/".join(parts) + f":{int(n)}"


__all__ = ["StageSink", "beat", "publishing_sink", "relaying", "report", "stage_op_prefix"]
