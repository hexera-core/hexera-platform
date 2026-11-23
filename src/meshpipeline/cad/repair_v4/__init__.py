# Responsibility: The one entry point of the CRS v4 search engine - `repair_v4` - and the plain
#                 result it hands back to `repair_step_file`.
# Owns: `V4Result`, the placeholder `Budget`, `SearchUnavailable`, and the `_run_search` seam.
# Boundaries: a THIN seam. It searches nothing and checks nothing itself: the search (Task 18) and
#             the verdict (Task 5) are wired in by replacing `_run_search`. Imports here are
#             stdlib-only so that importing the package never pulls a kernel or an op module
#             (`kernel_calls` imports the op modules at its foot, and the registry auto-discovers).
"""CRS v4 search-based CAD repair, selected by an env flag that defaults to v3."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Budget:
    """What one v4 run may spend. PLACEHOLDER with Task 18's field names; when Task 18 lands its
    own `Budget(wall_s, ops, model_calls, model_usd)`, this definition is deleted and re-exported
    from there so the entry point's signature is unchanged."""

    wall_s: float
    ops: int
    model_calls: int = 0
    model_usd: float = 0.0


@dataclass(frozen=True)
class V4Result:
    """The outcome of one v4 run.

    `delivered` True: the repaired STEP is at `dest`. False: the run found nothing it would stand
    behind and `dest` carries the upload's bytes unchanged (a PASSTHROUGH). `trace_path` is the
    search trace on disk (None when none was written); `verdict_summary` is the checker's verdict
    reduced to plain data (Task 5's shape: layer results and the overall status)."""

    trace_path: Path | None
    verdict_summary: dict = field(default_factory=dict)
    delivered: bool = False

    @property
    def passthrough(self) -> bool:
        return not self.delivered


class SearchUnavailable(RuntimeError):
    """The v4 search cannot run here (it is not wired in yet). OUR limitation, never a statement
    about the customer's file."""


def _run_search(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result:
    # SEAM FOR TASK 18 (search) + TASK 5 (verdict): replace this body with the search driver; it
    # must write `dest` (repaired STEP, or the upload's bytes on a passthrough) and return the
    # result. WIRING TASK 18 ALSO MEANS deleting the placeholder `Budget` above and importing its
    # own. NOT PLUMBED YET (a later task): CAD_REPAIR_ENGINE is not carried through deploy.yml,
    # create-api-service.sh, create-worker-fleet.sh or worker/startup.sh the way
    # CAD_REPAIR_MESHABILITY_PASS is, so a personal slug cannot select `search` yet. A delivered
    # report's `defects`/`operations` are empty (conservative._repair_via_v4): Task 18 must fill them.
    # Until then the engine reports itself unavailable rather than pretend to have run.
    raise SearchUnavailable("CRS v4 search is not wired in this build (Tasks 5 and 18 pending)")


def repair_v4(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result:
    """Repair the STEP at `source` into `dest` with the v4 search engine.

    `deadline_s` is an absolute monotonic-clock deadline for the whole run, the same clock
    `repair_step_file` takes its own deadline from; `budget` bounds what the search may spend
    inside it."""
    return _run_search(str(source), str(dest), budget=budget, deadline_s=deadline_s)
