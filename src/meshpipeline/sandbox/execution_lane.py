# Responsibility: Serialise every native render call onto one thread.
# Boundaries: VTK and gmsh are thread-affine, so construction, every call and teardown go through the same lane.
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

THREAD_NAME_PREFIX = "mesh-sb"


class LaneClosedError(RuntimeError):
    pass


class LaneReentrancyError(RuntimeError):
    pass


class RendererExecutionLane:

    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=THREAD_NAME_PREFIX)
        self._closing = False
        self._thread_id: int | None = None
        self._lock = threading.Lock()

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    def _record(self, fn, *args, **kwargs):
        self._thread_id = threading.get_ident()
        return fn(*args, **kwargs)

    async def run(self, fn, *args, **kwargs) -> Any:
        if self._executor is None or self._closing:
            raise LaneClosedError("the renderer execution lane is closed")
        if self._thread_id is not None and threading.get_ident() == self._thread_id:
            raise LaneReentrancyError(
                "a renderer call tried to re-enter its own single-worker lane")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self._record(fn, *args, **kwargs))

    async def run_final(self, fn, *args, **kwargs) -> Any:
        if self._executor is None:
            raise LaneClosedError("the renderer execution lane is already shut down")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self._record(fn, *args, **kwargs))

    def begin_closing(self) -> None:
        with self._lock:
            self._closing = True

    async def shutdown(self) -> None:
        self._closing = True
        executor, self._executor = self._executor, None
        if executor is None:
            return
        await asyncio.to_thread(executor.shutdown, wait=True)
