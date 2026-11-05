# Responsibility: Register the callable a dispatch backend enters the pipeline through.
# Boundaries: a one-entry indirection that keeps the launcher from importing the graph.
from __future__ import annotations

from collections.abc import Callable

_RUN_ENTRY: Callable[..., object] | None = None


def register_run_entry(fn: Callable[..., object]) -> Callable[..., object]:
    global _RUN_ENTRY
    _RUN_ENTRY = fn
    return fn


def run_entry() -> Callable[..., object]:
    if _RUN_ENTRY is None:
        raise RuntimeError(
            "dispatch run entry not registered - import meshpipeline.application.pipeline_run "
            "before validating a dispatch payload (it registers run_pipeline at import).")
    return _RUN_ENTRY
