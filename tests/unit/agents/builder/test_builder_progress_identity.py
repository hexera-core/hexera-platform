# Responsibility: Verify every builder progress event the user sees carries a replay identity.
# Boundaries: the executor's publication seam; how the event log deduplicates is the event authority's.
from __future__ import annotations

import asyncio
from pathlib import Path

from meshpipeline.agents.builder.executor import BuilderToolExecutor
from meshpipeline.agents.builder.tool_context import BuilderToolContext


class _Recorder:
    # Records what the executor asked the publisher to say, including the identity it named.
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def ameshed(self, cells=None, op_id: str = "") -> None:
        self.calls.append(("meshed", op_id))

    async def afile(self, display_path: str, byte_count: int, operation: str = "created",
                    op_id: str = "") -> None:
        self.calls.append(("file", op_id))

    async def awarn(self, text: str, op_id: str = "") -> None:
        self.calls.append(("warn", op_id))

    async def asearch(self, *a, **k) -> None:
        self.calls.append(("search", k.get("op_id", "")))


def _executor(rec: _Recorder, tmp_path: Path) -> BuilderToolExecutor:
    ctx = BuilderToolContext(workspace=str(tmp_path), job_id="job-1", geometry=None)
    return BuilderToolExecutor(context=ctx, publish=rec)


def _run(rec: _Recorder, tmp_path: Path) -> list[tuple[str, str]]:
    ex = _executor(rec, tmp_path)
    asyncio.run(ex._publish_for("run_mesh", {}, {"cells": 1000, "success": True}))
    asyncio.run(ex._publish_for("write_file", {}, {"written": "system/blockMeshDict",
                                                   "bytes": 812, "operation": "created"}))
    return rec.calls


def test_every_published_progress_event_names_its_occurrence(tmp_path):
    calls = _run(_Recorder(), tmp_path)
    assert calls, "the executor published nothing - this proves nothing"
    unnamed = [name for name, op_id in calls if not op_id]
    assert unnamed == [], (
        f"these builder progress events carry no identity: {unnamed}. A replayed node republishes "
        "them verbatim, so the user sees the same progress line twice in one run.")


def test_a_rebuilt_executor_replays_onto_the_same_identities(tmp_path):
    # THE REPLAY RULE the executor documents: it is rebuilt when the node re-runs, so the second
    # run must land on exactly the identities the first one used, or the event log cannot collapse
    # them. Two runs, identical identities.
    first = _run(_Recorder(), tmp_path)
    second = _run(_Recorder(), tmp_path)
    assert first == second, f"a replay produced different identities:\n{first}\n{second}"
