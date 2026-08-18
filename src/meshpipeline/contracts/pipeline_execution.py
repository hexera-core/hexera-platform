# Responsibility: Declare how a pipeline run is launched, wherever it runs.
# Boundaries: a Protocol and its binding; Celery and the deferred launcher live in adapters/pipeline_execution/.
from __future__ import annotations

from typing import Protocol, runtime_checkable


class PipelineLaunchError(RuntimeError):
    pass


@runtime_checkable
class PipelineLauncher(Protocol):

    async def launch(self, db, job_id: str, payload: dict) -> None: ...


_launcher: PipelineLauncher | None = None


def set_pipeline_launcher(launcher: PipelineLauncher) -> None:
    global _launcher
    _launcher = launcher


def get_pipeline_launcher() -> PipelineLauncher:
    if _launcher is None:
        raise PipelineLaunchError(
            "no pipeline launcher configured - runtime composition must call "
            "set_pipeline_launcher() at process startup before a run is dispatched")
    return _launcher
