# Responsibility: Let a run resume from its durable checkpoint without letting a superseded worker resume it.
# Owns: the fenced checkpointer wrapper, the continuation decision, and graph entry.
# Boundaries: durability and ownership around the graph; it runs no node.
from __future__ import annotations

import logging
from typing import Any, NamedTuple

from langgraph.checkpoint.base import BaseCheckpointSaver

from meshpipeline.application import execution_fence as _fence

logger = logging.getLogger(__name__)


class FencedCheckpointer(BaseCheckpointSaver):

    def __init__(self, inner: BaseCheckpointSaver, *, job_id: str = "",
                 execution_generation: int = 0, approved_intent_fingerprint: str = "",
                 state_schema_version: Any = None) -> None:
        super().__init__(serde=getattr(inner, "serde", None))
        self._inner = inner
        self._identity = {
            "job_id": str(job_id),
            "execution_generation": int(execution_generation),
            "approved_intent_fingerprint": str(approved_intent_fingerprint or ""),
            "state_schema_version": state_schema_version,
        }

 # identity
    @property
    def inner(self) -> BaseCheckpointSaver:
        return self._inner

    @property
    def config_specs(self):
        return self._inner.config_specs

    def checkpoint_identity(self, *, attempt: int = 0) -> dict:
        return {**self._identity, "attempt": int(attempt)}

    def __getattr__(self, item):
        # Only reached for attributes defined on NEITHER this class nor BaseCheckpointSaver -
        # concrete savers add their own surface, and it must keep working.
        return getattr(self._inner, item)

 # FENCED writes
    async def aput(self, config, checkpoint, metadata, new_versions):
        await _fence.assert_current_owner("checkpoint write (aput)")
        return await self._inner.aput(config, checkpoint, self._stamp(metadata), new_versions)

    async def aput_writes(self, config, writes, task_id, task_path: str = ""):
        await _fence.assert_current_owner("checkpoint write (aput_writes)")
        return await self._inner.aput_writes(config, writes, task_id, task_path)

 # UNFENCED reads (resume must keep working)
    async def aget(self, config):
        return await self._inner.aget(config)

    async def aget_tuple(self, config):
        return await self._inner.aget_tuple(config)

    def alist(self, config, *, filter=None, before=None, limit=None):  # noqa: A002 - LangGraph's name
        return self._inner.alist(config, filter=filter, before=before, limit=limit)

    async def aget_delta_channel_history(self, *, config, channels):
        return await self._inner.aget_delta_channel_history(config=config, channels=channels)

 # pass-through lifecycle/versioning
    def get_next_version(self, current, channel=None):
        return self._inner.get_next_version(current, channel)

    async def adelete_thread(self, thread_id):
        return await self._inner.adelete_thread(thread_id)

    async def acopy_thread(self, source_thread_id, target_thread_id):
        return await self._inner.acopy_thread(source_thread_id, target_thread_id)

 # metadata stamping
    def _stamp(self, metadata):
        try:
            if isinstance(metadata, dict):
                attempt = 0
                _w = metadata.get("writes") or {}
                if isinstance(_w, dict):
                    for _v in _w.values():
                        if isinstance(_v, dict) and "retry_count" in _v:
                            attempt = int(_v.get("retry_count") or 0)
                return {**metadata, "meshpipeline": self.checkpoint_identity(attempt=attempt)}
        except Exception:  # noqa: BLE001 - enrichment is never worth losing a checkpoint over
            logger.warning("checkpoint metadata stamping failed - persisting unstamped",
                           exc_info=True)
        return metadata


__all__ = ["FencedCheckpointer"]


# #
# CONTINUATION - what to hand `ainvoke`, given what the durable thread already holds.
# Extracted from application/pipeline_run._run_async. Interpreting a checkpoint disposition is
# checkpoint semantics, not sequencing, and it belongs beside the fenced writer that performs the
# one durable write it can make.
# The thread was classified BEFORE any source byte was fetched, so this acts on the decision that
# was already made rather than reading the thread again.
# #


class Continuation(NamedTuple):

    #: The graph input. `None` means CONTINUE from the durable position - LangGraph resumes the
    #: pending tasks. A dict means start fresh from START.
    graph_input: dict | None
    #: True only for a pending thread carrying a process-local geometry handle that must be
    #: refreshed before resuming.
    refresh_geometry: bool
    disposition: str

    @property
    def is_fresh(self) -> bool:
        return self.graph_input is not None


def plan_continuation(disposition: str, initial_state, materialized) -> Continuation:
    if disposition == "absent":
        return Continuation(initial_state, False, disposition)
    if disposition == "pending":
        return Continuation(None, materialized is not None, disposition)
    return Continuation(None, False, disposition)


async def enter_graph(graph, graph_config, continuation: Continuation, materialized, *,
                      job_id: str, generation: int, jlog):
    if continuation.is_fresh:
        return continuation.graph_input
    if continuation.disposition == "pending":
        jlog.info("Resuming job %s from its durable position (generation=%d)", job_id, generation)
        if continuation.refresh_geometry:
            await graph.aupdate_state(graph_config, {"geometry": materialized.to_state()})
    else:
        jlog.info("Job %s has a COMPLETE saved graph and no terminal status - reconciling the "
                  "terminal result without re-running any node", job_id)
    return None
